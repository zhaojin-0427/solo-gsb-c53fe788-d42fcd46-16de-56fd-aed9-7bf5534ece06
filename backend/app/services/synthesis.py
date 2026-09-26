"""最佳片段合成轨：从多次演奏对比的只读快照规划并渲染一条合成 WAV。

规则总览
========
1. **只在未过期的对比上工作**：对比的任一成员快照过期（对齐版本 / WAV 哈希
   变化）时，规划与导出被拒绝，必须先在对比上"显式刷新"。合成任务在**构建
   时**再次固化每个来源的对齐版本与音频哈希；之后来源变化只会把任务标记为
   *过期*，已固化 WAV 不变，必须由用户显式重建。
2. **按连续谱音区间选源**：每个谱音位置只允许使用该录音中 *已确认且置信度
   达标* 的片段（op=match、无待确认阴影、非排除、confidence ≥ 阈值）。
   错音 / 漏音 / 起音偏移 / 时值偏差仍可选（用于凑齐无候选位置），但计入
   异常代价。
3. **全局代价（权重全部公开）**::

       cost = N_异常 * W_ANOMALY
            + N_切换 * W_SWITCH
            + Σ_cut |L_left - L_right|(dB) * W_LOUDNESS_PER_DB
            + Σ_cut |r_left - r_right| * W_TEMPO_RATIO

   相同代价按 *录音顺序（快照 ord）* 与 *谱位* 稳定排序（动态规划同分时
   取录音序号序列字典序最小）。
4. **用户约束**：可钉住（pin）某谱位的来源录音，或禁用（disable）某录音；
   钉住的来源在该位置不合格时，该位置进入阻塞清单。
5. **切点只落在已对齐的起音边界**；每段按基线局部节拍时间伸缩，切点处
   短交叉淡化（默认单侧 20ms，线性等功率）。
6. 任一谱位无合格来源 -> 阻止导出（status=blocked），清单与界面逐位给出
   原因定位。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
import wave

from .. import config as C
from .. import db
from . import comparison as cmp_service

# 与 comparison 模块保持一致的"异常"定义
ABNORMAL_TAGS = cmp_service.ABNORMAL_TAGS
ABNORMAL_STATUSES = cmp_service.ABNORMAL_STATUSES


# ============================================================ 参数快照

def public_params() -> dict:
    """代价权重与渲染参数 —— 对外公开，清单 params 段原样携带。"""
    return {
        "synth_algorithm_version": C.SYNTH_ALGORITHM_VERSION,
        "min_confidence": C.SYNTH_MIN_CONFIDENCE,
        "weights": {
            "anomaly": C.SYNTH_W_ANOMALY,
            "switch": C.SYNTH_W_SWITCH,
            "loudness_per_db": C.SYNTH_W_LOUDNESS_PER_DB,
            "tempo_ratio": C.SYNTH_W_TEMPO_RATIO,
        },
        "crossfade_s": C.SYNTH_CROSSFADE_S,
        "loudness_window_s": C.SYNTH_LOUDNESS_WINDOW_S,
        "db_floor": C.SYNTH_DB_FLOOR,
        "output_headroom": C.SYNTH_OUTPUT_HEADROOM,
        "tie_break": "piece_ord_then_score_index_ascending",
    }


# ============================================================ 数据装载

def _snapshots(cmp_id: str):
    rows = db.get_snapshots(cmp_id)
    out = []
    for r in rows:
        snap = json.loads(r["snapshot_json"])
        snap["_ord"] = r["ord"]
        snap["_frozen_version"] = r["alignment_version"]
        snap["_wav_sha256"] = r["wav_sha256"]
        out.append(snap)
    out.sort(key=lambda s: s["_ord"])
    return out


def _assert_not_stale(cmp_id: str):
    """对比任一成员过期即拒绝：先显式刷新对比，再规划/重建合成。"""
    stale = []
    for sr in db.get_snapshots(cmp_id):
        reasons = cmp_service._stale_reasons(sr)  # noqa: SLF001
        if reasons:
            stale.append({"piece_id": sr["piece_id"], "reasons": reasons})
    if stale:
        names = "、".join(s["piece_id"] for s in stale)
        raise StaleError(
            f"对比已过期（{names} 的对齐版本或音频已变化），"
            "请先在对比页显式刷新后再规划/重建合成轨", stale)


class StaleError(Exception):
    def __init__(self, msg, stale_members):
        super().__init__(msg)
        self.stale_members = stale_members


# ============================================================ 合格性

def _note_state(note):
    return cmp_service.classify_note(note)


def eligibility(note) -> tuple[bool, str, bool]:
    """返回 (合格可用, 不合格原因, 是否异常)。

    合格 = 已确认配对（op=match/locked_match，含锁定/强制锁定）、无待确认
    阴影、非用户排除、置信度达标。错音/起音偏移/时值偏差不影响合格性，
    但 abnormal=True，计入异常代价。
    """
    if note is None:
        return False, "no_snapshot_note", False
    if note.get("shadow_pending"):
        return False, "pending_unresolved", False
    if note.get("op") not in ("match", "locked_match"):
        return False, f"not_confirmed:{note.get('op')}", False
    if note.get("status") == "excluded":
        return False, "excluded", False
    if note.get("obs_index") is None or note.get("onset_s") is None:
        return False, "no_aligned_onset", False
    conf = note.get("confidence")
    if conf is None or float(conf) < C.SYNTH_MIN_CONFIDENCE:
        return False, "low_confidence", False
    abnormal = note.get("status") in ABNORMAL_STATUSES or \
        bool(ABNORMAL_TAGS.intersection(note.get("tags") or []))
    return True, "ok", abnormal


# ============================================================ 音频 I/O（numpy 可选）

try:
    import numpy as _np
except ImportError:  # 纯标准库环境（测试）
    _np = None


def _wav_path(piece_id: str) -> str:
    return os.path.join(os.environ.get("DATA_DIR", "/data"), "files",
                        f"{piece_id}.wav")


def load_audio(piece_id: str):
    """读取 WAV -> (samples[float mono], sr)。

    生产环境（有 numpy/librosa）走 librosa，兼容各种 WAV 格式并统一到
    config.SAMPLE_RATE；无 numpy 的测试环境用标准库 wave 读取
    PCM16/PCM24/PCM32/PCM8 单声道测试文件（不重采样）。
    """
    path = _wav_path(piece_id)
    if _np is not None:
        import librosa
        y, sr = librosa.load(path, sr=C.SAMPLE_RATE, mono=True)
        return y, sr
    return _load_wav_stdlib(path)


def _load_wav_stdlib(path: str):
    with wave.open(path, "rb") as w:
        nch, sw, sr, n = w.getnchannels(), w.getsampwidth(), \
            w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 1:
        vals = [(b - 128) / 128.0 for b in raw]
    elif sw == 2:
        import struct
        vals = [x / 32768.0 for x in
                struct.unpack("<%dh" % (len(raw) // 2), raw)]
    elif sw == 3:
        vals = []
        for k in range(0, len(raw), 3):
            v = raw[k] | (raw[k + 1] << 8) | (raw[k + 2] << 16)
            if v & 0x800000:
                v -= 1 << 24
            vals.append(v / 8388608.0)
    elif sw == 4:
        import struct
        vals = [x / 2147483648.0 for x in
                struct.unpack("<%dl" % (len(raw) // 4), raw)]
    else:
        raise ValueError(f"不支持的采样位宽 {sw*8}bit")
    if nch > 1:
        vals = [sum(vals[k:k + nch]) / nch
                for k in range(0, len(vals), nch)]
    return vals, sr


# ============================================================ 规划

def _r(v, nd=C.ROUND_NDIGITS):
    return None if v is None else round(float(v), nd)


def plan(cmp_row: dict, overrides: dict | None = None) -> dict:
    """纯规划：选源 + 连续区间 + 切点 + 代价明细 + 阻塞定位（不渲染音频）。"""
    cmp_id = cmp_row["id"]
    snaps = _snapshots(cmp_id)
    baseline_id = cmp_row["baseline_piece"]
    base = next(s for s in snaps if s["piece_id"] == baseline_id)
    pieces = [{"piece_id": s["piece_id"], "title": s["title"],
               "ord": s["_ord"], "is_baseline": s["piece_id"] == baseline_id,
               "frozen_version": s["_frozen_version"],
               "wav_sha256": s["_wav_sha256"]} for s in snaps]
    rank = {p["piece_id"]: k for k, p in enumerate(pieces)}
    notes_by_piece = {s["piece_id"]:
                      {n["score_index"]: n for n in s["notes"]}
                      for s in snaps}

    positions = sorted(n["score_index"] for n in base["notes"])
    ovr = _normalize_overrides(overrides or {})

    # 读取各来源音频（规划阶段就要算响度；对比未过期 => 哈希就是固化值）
    audio = {}
    for p in pieces:
        try:
            y, sr = load_audio(p["piece_id"])
        except Exception as exc:  # noqa: BLE001
            raise StaleError(
                f"来源《{p['title']}》的 WAV 不可读：{exc}",
                [{"piece_id": p["piece_id"], "reasons": ["audio_missing"]}]) \
                from exc
        audio[p["piece_id"]] = (y, sr)

    # 每位置的候选信息（合格性 / 异常 / 响度）
    pos_info = []
    for idx in positions:
        bn = next(n for n in base["notes"] if n["score_index"] == idx)
        cands = {}
        for p in pieces:
            pid = p["piece_id"]
            n = notes_by_piece[pid].get(idx)
            ok, reason, abnormal = eligibility(n)
            c = {"piece_id": pid, "eligible": ok, "reason": reason,
                 "abnormal": abnormal, "score_index": idx,
                 "measure": n.get("measure") if n else bn.get("measure"),
                 "beat_in_measure": (n.get("beat_in_measure") if n
                                     else bn.get("beat_in_measure")),
                 "occurrence": n.get("occurrence", 0) if n
                               else bn.get("occurrence", 0),
                 "midi": n.get("midi") if n else bn.get("midi"),
                 "status": n.get("status") if n else None,
                 "tags": (n.get("tags") or []) if n else [],
                 "confidence": n.get("confidence") if n else None,
                 "onset_s": n.get("onset_s") if n else None,
                 "loudness_db": None}
            if ok:
                c["loudness_db"] = _onset_loudness(
                    audio[pid], float(n["onset_s"]))
            cands[pid] = c
        pos_info.append({"score_index": idx,
                         "measure": bn.get("measure"),
                         "beat_in_measure": bn.get("beat_in_measure"),
                         "occurrence": bn.get("occurrence", 0),
                         "beat": bn.get("beat"),
                         "midi": bn.get("midi"),
                         "candidates": cands})

    # 输出时间轴：基线起音（已对齐用实际值，漏音等用局部速度预测值）
    for info in pos_info:
        bn = info["candidates"][baseline_id]
        info["output_onset_s"] = (bn.get("onset_s")
                                  if bn.get("onset_s") is not None
                                  else bn.get("predicted_onset_s"))
        info["_baseline_note"] = bn

    # 每个 (位置, 来源) 的区间伸缩比（来源 Δt / 基线 Δt）
    for i, info in enumerate(pos_info):
        info["ratio_in"] = {}
        info["ratio_out"] = {}
        if i > 0:
            prev = pos_info[i - 1]
            db_dt = _interval_base_dt(prev, info)
            for pid in rank:
                r = _ratio_between(prev, info, pid, db_dt)
                if r is not None:
                    info["ratio_in"][pid] = r
        if i + 1 < len(pos_info):
            nxt = pos_info[i + 1]
            db_dt = _interval_base_dt(info, nxt)
            for pid in rank:
                r = _ratio_between(info, nxt, pid, db_dt)
                if r is not None:
                    info["ratio_out"][pid] = r

    # 用户约束：钉住 / 禁用
    blocked, allowed = [], []
    for info in pos_info:
        idx = info["score_index"]
        pin = ovr["pins"].get(str(idx))
        disabled = ovr["disabled"]
        cand = info["candidates"]
        if pin is not None:
            if pin not in cand:
                blocked.append(_blocked(info, pin, "pinned_unknown_piece"))
                allowed.append(None)
                continue
            if pin in disabled:
                blocked.append(_blocked(info, pin,
                                        "pinned_but_disabled"))
                allowed.append(None)
                continue
            if not cand[pin]["eligible"]:
                blocked.append(_blocked(info, pin, "pinned_unavailable"))
                allowed.append(None)
                continue
            allowed.append([pin])
            continue
        ok_pids = [pid for pid in rank
                   if cand[pid]["eligible"] and pid not in disabled]
        if not ok_pids:
            any_eligible = any(cand[pid]["eligible"] for pid in rank)
            reason = "all_disabled" if any_eligible \
                else "no_eligible_source"
            blocked.append(_blocked(info, None, reason))
            allowed.append(None)
        else:
            allowed.append(ok_pids)

    chosen = _dp_select(pos_info, allowed, rank)

    # 连续区间、切点、代价
    segments, cuts = _runs(pos_info, chosen, rank, pieces)
    total = _cost_totals(pos_info, chosen, rank, cuts)

    return {
        "status": "blocked" if blocked else "draft",
        "weights": public_params()["weights"],
        "baseline_piece": baseline_id,
        "pieces": pieces,
        "positions": pos_info,
        "selection": [None if c is None else
                      {"score_index": pos_info[k]["score_index"],
                       "piece_id": c} for k, c in enumerate(chosen)],
        "segments": segments,
        "cuts": cuts,
        "blocked": blocked,
        "cost": total,
    }


def _normalize_overrides(ovr: dict) -> dict:
    if not isinstance(ovr, dict):
        raise ValueError("overrides 必须是对象")
    raw_pins = ovr.get("pins") or {}
    raw_dis = ovr.get("disabled") or []
    pins = {}
    if isinstance(raw_pins, dict):
        for k, v in raw_pins.items():
            if v is None or v == "":
                continue
            try:
                int(k)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"钉住键必须是谱位下标：{k}") from exc
            pins[str(int(k))] = str(v)
    disabled = [str(x) for x in raw_dis]
    return {"pins": pins, "disabled": set(disabled)}


def _blocked(info, pinned_pid, reason):
    cand = info["candidates"]
    per_piece = {}
    for pid, c in cand.items():
        per_piece[pid] = {"eligible": c["eligible"], "reason": c["reason"],
                          "abnormal": c["abnormal"],
                          "confidence": c["confidence"]}
    return {"score_index": info["score_index"],
            "measure": info["measure"],
            "beat_in_measure": info["beat_in_measure"],
            "occurrence": info["occurrence"],
            "midi": info["midi"],
            "reason": reason,
            "pinned_piece": pinned_pid,
            "candidates": per_piece}


def _onset_loudness(audio_pair, onset_s: str | float):
    y, sr = audio_pair
    t0 = max(0.0, float(onset_s))
    i0 = int(t0 * sr)
    i1 = min(len(y), i0 + max(1, int(C.SYNTH_LOUDNESS_WINDOW_S * sr)))
    if i1 <= i0:
        return _r(C.SYNTH_DB_FLOOR, 3)
    if _np is not None:
        seg = _np.asarray(y[i0:i1], dtype=float)
        rms = float(_np.sqrt(_np.mean(seg * seg))) if seg.size else 0.0
    else:
        seg = y[i0:i1]
        rms = math.sqrt(sum(v * v for v in seg) / len(seg))
    if rms <= 1e-7:
        return _r(C.SYNTH_DB_FLOOR, 3)
    return _r(20.0 * math.log10(rms), 3)


# ---------------- 动态规划选源（同分稳定排序）

def _interval_base_dt(info_a, info_b):
    ta, tb = info_a["output_onset_s"], info_b["output_onset_s"]
    if ta is None or tb is None:
        return None
    dt = float(tb) - float(ta)
    return dt if dt > 0 else None


def _ratio_between(info_a, info_b, pid, base_dt=None):
    """某来源在相邻谱位区间相对基线的局部节拍时间伸缩比 r。"""
    na, nb = info_a["candidates"][pid], info_b["candidates"][pid]
    if not na["eligible"] or not nb["eligible"]:
        return None
    if base_dt is None:
        base_dt = _interval_base_dt(info_a, info_b)
    if not base_dt:
        return None
    dt = float(nb["onset_s"]) - float(na["onset_s"])
    return round(dt / base_dt, 6)


def _edge_ratio_left(info_prev, pid_out):
    """切点左侧（前一来源）局部速度比：优先尾区间，缺则头区间，再缺 1.0。"""
    r = info_prev["ratio_out"].get(pid_out)
    if r is None:
        r = info_prev["ratio_in"].get(pid_out)
    return 1.0 if r is None else r


def _edge_ratio_right(info, pid_in):
    """切点右侧（新来源）局部速度比：优先头区间，缺则尾区间，再缺 1.0。"""
    r = info["ratio_in"].get(pid_in)
    if r is None:
        r = info["ratio_out"].get(pid_in)
    return 1.0 if r is None else r


def _cut_delta_loud(prev_info, info, pid_out, pid_in):
    lin = info["candidates"][pid_in].get("loudness_db")
    lout = prev_info["candidates"][pid_out].get("loudness_db")
    if lin is None or lout is None:
        return 0.0
    return round(abs(lin - lout), 3)


def _cut_delta_tempo(prev_info, info, pid_out, pid_in):
    return round(abs(_edge_ratio_right(info, pid_in)
                     - _edge_ratio_left(prev_info, pid_out)), 6)


def _cut_cost(prev_info, info, pid_out, pid_in):
    """单个切点代价（切换 + 响度差 + 局部速度差）。DP 与清单共用此函数，
    保证规划最优值与代价汇总逐项一致。"""
    dl = _cut_delta_loud(prev_info, info, pid_out, pid_in)
    dr = _cut_delta_tempo(prev_info, info, pid_out, pid_in)
    return round(C.SYNTH_W_SWITCH
                 + dl * C.SYNTH_W_LOUDNESS_PER_DB
                 + dr * C.SYNTH_W_TEMPO_RATIO, 9)


def _edge_cost_at(prev_info, info, pid_in, pid_out):
    """切点代价：切换 + 响度差 + 局部速度差（pid_out=None 表示曲首无切）。"""
    if pid_out is None:
        return 0.0
    return _cut_cost(prev_info, info, pid_out, pid_in)


def _dp_select(pos_info, allowed, rank):
    """最小化全局代价；同代价按录音 ord 序列字典序最小（稳定）。

    dp[i] 只保留到达每个来源的最优 (float_cost, tie_tuple)；
    阻塞位置 (allowed[i] is None) 不参与，chosen 记 None。
    """
    dp = [None] * len(pos_info)
    back = [None] * len(pos_info)
    for i, info in enumerate(pos_info):
        cands = allowed[i]
        if cands is None:
            continue
        prev = dp[i - 1] if i > 0 else None
        prev_info = pos_info[i - 1] if i > 0 else None
        j = i - 1
        while j >= 0 and allowed[j] is None:
            j -= 1
        prev = dp[j] if j >= 0 else None
        prev_info = pos_info[j] if j >= 0 else None
        cur, back_i = {}, {}
        for pid in cands:
            anomaly = C.SYNTH_W_ANOMALY \
                if info["candidates"][pid]["abnormal"] else 0.0
            best, best_prev_pid = None, None
            if prev is None:
                best = (round(anomaly, 9), (rank[pid],))
            else:
                for ppid in prev:
                    pcost, ptie = prev[ppid]
                    add = anomaly + _edge_cost_at(prev_info, info, pid, ppid)
                    key = (round(pcost + add, 9), ptie + (rank[pid],))
                    if best is None or key < best:
                        best, best_prev_pid = key, ppid
            cur[pid] = best
            back_i[pid] = best_prev_pid
        dp[i], back[i] = cur, back_i

    # 回溯
    chosen = [None] * len(pos_info)
    last = len(pos_info) - 1
    while last >= 0 and dp[last] is None:
        last -= 1
    if last < 0:
        return chosen
    pid = min(dp[last], key=lambda p: dp[last][p])
    for i in range(last, -1, -1):
        if dp[i] is None:
            continue
        chosen[i] = pid
        pid = back[i][pid]
    return chosen


# ---------------- 连续区间 / 切点

def _runs(pos_info, chosen, rank, pieces):
    """把选源结果合并为连续同来源区间，并计算切点与代价明细。"""
    segments, cuts = [], []
    n = len(pos_info)
    i = 0
    while i < n:
        if chosen[i] is None:
            i += 1
            continue
        pid = chosen[i]
        j = i
        while j + 1 < n and chosen[j + 1] == pid:
            j += 1
        segments.append(_segment(pos_info, i, j, pid, pieces))
        i = j + 1

    # 切点：相邻区间交界处（切点 = 后段首个谱音的已对齐起音边界）
    for k in range(len(segments) - 1):
        a, b = segments[k], segments[k + 1]
        cut_info = next(info for info in pos_info
                        if info["score_index"] == b["score_index_start"])
        ia = next(info for info in pos_info
                  if info["score_index"] == a["score_index_end"])
        lin = cut_info["candidates"][b["piece_id"]]["loudness_db"]
        lout = ia["candidates"][a["piece_id"]]["loudness_db"]
        dl = _cut_delta_loud(ia, cut_info, a["piece_id"], b["piece_id"])
        if lin is None or lout is None:
            dl = None
        rout = _edge_ratio_left(ia, a["piece_id"])
        rin = _edge_ratio_right(cut_info, b["piece_id"])
        dr = round(abs(rin - rout), 6)
        cuts.append({
            "score_index": b["score_index_start"],
            "measure": cut_info["measure"],
            "beat_in_measure": cut_info["beat_in_measure"],
            "occurrence": cut_info["occurrence"],
            "out_piece": a["piece_id"],
            "in_piece": b["piece_id"],
            "out_time_s": _r(ia["candidates"][a["piece_id"]]["onset_s"]),
            "in_time_s": _r(cut_info["candidates"][b["piece_id"]]["onset_s"]),
            "output_time_s": _r(cut_info["output_onset_s"]),
            "loudness_left_db": lout,
            "loudness_right_db": lin,
            "loudness_delta_db": dl,
            "tempo_ratio_left": _r(rout),
            "tempo_ratio_right": _r(rin),
            "tempo_ratio_delta": _r(dr),
            "cost": _r(_cut_cost(ia, cut_info, a["piece_id"],
                                 b["piece_id"])),
            "cost_terms": {
                "switch": _r(C.SYNTH_W_SWITCH),
                "loudness": _r((dl or 0.0) * C.SYNTH_W_LOUDNESS_PER_DB, 3),
                "tempo": _r(dr * C.SYNTH_W_TEMPO_RATIO),
            },
            "crossfade_s": C.SYNTH_CROSSFADE_S,
        })

    return segments, cuts


def _segment(pos_info, i, j, pid, pieces):
    p = next(x for x in pieces if x["piece_id"] == pid)
    notes = []
    for k in range(i, j + 1):
        info = pos_info[k]
        c = info["candidates"][pid]
        notes.append({
            "score_index": info["score_index"],
            "measure": info["measure"],
            "beat_in_measure": info["beat_in_measure"],
            "occurrence": info["occurrence"],
            "midi": info["midi"],
            "source_onset_s": _r(c["onset_s"]),
            "output_onset_s": _r(info["output_onset_s"]),
            "abnormal": c["abnormal"],
            "confidence": c["confidence"],
            "status": c["status"],
            "tags": c["tags"],
        })
    # 区间内逐区间伸缩比（来源 Δt / 基线 Δt）
    ratios = []
    for k in range(i, j):
        r = pos_info[k + 1]["ratio_in"].get(pid)
        if r is None:
            r = _ratio_between(pos_info[k], pos_info[k + 1], pid)
        ratios.append({"score_index_a": pos_info[k]["score_index"],
                       "score_index_b": pos_info[k + 1]["score_index"],
                       "ratio": _r(r)})
    # 尾区间伸缩比：最后内部区间外推；单音区间取头侧比值，再缺退化为 1
    tail_ratio = ratios[-1]["ratio"] if ratios \
        else _r(_edge_ratio_right(pos_info[i], pid))
    return {
        "piece_id": pid,
        "piece_title": p["title"],
        "ord": p["ord"],
        "score_index_start": pos_info[i]["score_index"],
        "score_index_end": pos_info[j]["score_index"],
        "measure_start": pos_info[i]["measure"],
        "measure_end": pos_info[j]["measure"],
        "occurrence": pos_info[i]["occurrence"],
        "source_time_start_s": _r(pos_info[i]["candidates"][pid]["onset_s"]),
        "source_time_end_s": _r(pos_info[j]["candidates"][pid]["onset_s"]),
        "output_time_start_s": _r(pos_info[i]["output_onset_s"]),
        "output_time_end_s": _r(pos_info[j]["output_onset_s"]),
        "tail_ratio": tail_ratio,
        "interval_ratios": ratios,
        "notes": notes,
    }


def _cost_totals(pos_info, chosen, rank, cuts):
    n_anom = 0
    for i, info in enumerate(pos_info):
        if chosen[i] is None:
            continue
        if info["candidates"][chosen[i]]["abnormal"]:
            n_anom += 1
    n_sw = sum(1 for k in range(1, len(chosen))
               if chosen[k] is not None and chosen[k - 1] is not None
               and chosen[k] != chosen[k - 1])
    loud = sum(c["loudness_delta_db"] or 0.0 for c in cuts)
    tempo = sum(c["tempo_ratio_delta"] or 0.0 for c in cuts)
    return {
        "anomaly_count": n_anom,
        "switch_count": n_sw,
        "loudness_delta_db_sum": _r(loud, 3),
        "tempo_ratio_delta_sum": _r(tempo),
        "total": _r(n_anom * C.SYNTH_W_ANOMALY
                   + n_sw * C.SYNTH_W_SWITCH
                   + loud * C.SYNTH_W_LOUDNESS_PER_DB
                   + tempo * C.SYNTH_W_TEMPO_RATIO),
        "terms": {
            "anomaly": _r(n_anom * C.SYNTH_W_ANOMALY),
            "switch": _r(n_sw * C.SYNTH_W_SWITCH),
            "loudness": _r(loud * C.SYNTH_W_LOUDNESS_PER_DB, 3),
            "tempo": _r(tempo * C.SYNTH_W_TEMPO_RATIO),
        },
    }


# ============================================================ 任务 CRUD

def create_task(cmp_id: str, title: str | None = None,
                overrides: dict | None = None) -> str:
    cmp_row = db.get_comparison(cmp_id)
    if not cmp_row:
        raise ValueError("对比不存在")
    if db.get_synthesis_task_by_comparison(cmp_id):
        raise ValueError("该对比已有合成轨任务，请直接调整或重建")
    _assert_not_stale(cmp_id)
    plan_data = plan(cmp_row, overrides)
    task_id = uuid.uuid4().hex[:12]
    db.create_synthesis_task(
        task_id, cmp_id,
        title or f"{cmp_row['title']} · 最佳片段合成轨",
        json.dumps(public_params(), ensure_ascii=False, sort_keys=True),
        json.dumps(_persist_overrides(overrides), ensure_ascii=False,
                   sort_keys=True),
        json.dumps(plan_data, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")),
        plan_data["status"])
    return task_id


def update_overrides(task_id: str, overrides: dict) -> dict:
    """施加钉住 / 禁用约束并重新规划（对比须未过期）。

    **绝不删除已固化产物**：
    * 从未固化的任务（draft/blocked）：按新规划更新 status
      （draft / blocked），本来就没有 WAV / 清单。
    * 已 built 任务：status 保持 ``built``，旧 WAV、清单、固化版本与
      试听/下载能力全部保留——即使新规划因"无合格来源"而 blocked。
      此时阻塞信息写入 plan_json，显式重建会被 422 拒绝直到解除阻塞。
    """
    task = db.get_synthesis_task(task_id)
    if not task:
        raise ValueError("合成任务不存在")
    _assert_not_stale(task["comparison_id"])
    cmp_row = db.get_comparison(task["comparison_id"])
    plan_data = plan(cmp_row, overrides)
    params_js = json.dumps(public_params(), ensure_ascii=False,
                           sort_keys=True)
    overrides_js = json.dumps(_persist_overrides(overrides),
                              ensure_ascii=False, sort_keys=True)
    plan_js = json.dumps(plan_data, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    if _has_build(task):
        # 钉住固化产物：只改规划，不动 status/manifest/wav_*，不删文件。
        db.set_synthesis_plan_keep_built(
            task_id, params_js, overrides_js, plan_js)
    else:
        db.update_synthesis_plan(
            task_id, params_js, overrides_js, plan_js, plan_data["status"])
    return get_task(task_id)


def _has_build(task) -> bool:
    """该任务是否持有固化产物（built 状态且清单/文件字段齐全）。"""
    return bool(task and task["status"] == "built"
                and task["manifest_json"] and task["wav_path"])


def rebuild(task_id: str) -> dict:
    """显式重建：重新规划（对比须未过期）并**原子**固化 WAV + 清单。

    阻塞（任一谱位无合格来源）时**只读校验、拒绝导出**：返回 422 语义
    （抛 :class:`BlockedError`，携带逐谱位原因），不改动规划行、不删除
    旧 WAV / 清单——已 built 任务的试听/下载继续可用。解除阻塞后须再次
    显式调用本函数，新 WAV 先写临时文件再 ``os.replace`` 原子替换，
    渲染失败不会留下半截产物。
    """
    task = db.get_synthesis_task(task_id)
    if not task:
        raise ValueError("合成任务不存在")
    cmp_id = task["comparison_id"]
    _assert_not_stale(cmp_id)
    cmp_row = db.get_comparison(cmp_id)
    overrides = json.loads(task["overrides_json"] or "{}")
    plan_data = plan(cmp_row, overrides)
    if plan_data["blocked"]:
        # 只报告逐位原因；不更新规划、不清空字段、不删文件。
        raise BlockedError(plan_data["blocked"])

    wav_bytes, manifest = render(cmp_row, plan_data, overrides)
    files_dir = os.path.join(os.environ.get("DATA_DIR", "/data"), "files")
    os.makedirs(files_dir, exist_ok=True)
    final_path = os.path.join(files_dir, f"synth_{task_id}.wav")
    # 原子替换：同目录临时文件 + fsync + os.replace，
    # 旧 WAV 在替换成功前保持可读，渲染/写盘失败不影响固化产物。
    tmp_path = os.path.join(
        files_dir, f".synth_{task_id}.{uuid.uuid4().hex[:8]}.tmp.wav")
    try:
        with open(tmp_path, "wb") as f:
            f.write(wav_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, final_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    wav_hash = hashlib.sha256(wav_bytes).hexdigest()
    db.mark_synthesis_built(
        task_id,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")),
        final_path, wav_hash)
    return get_task(task_id)


class BlockedError(Exception):
    def __init__(self, blocked):
        super().__init__("存在无合格来源的谱位，已阻止重建（已固化产物保留）")
        self.blocked = blocked


def _persist_overrides(overrides: dict | None) -> dict:
    ovr = _normalize_overrides(overrides or {})
    return {"pins": ovr["pins"],
            "disabled": sorted(ovr["disabled"])}


def get_task(task_id: str) -> dict:
    task = db.get_synthesis_task(task_id)
    if not task:
        raise ValueError("合成任务不存在")
    plan_data = json.loads(task["plan_json"])
    # 过期判定：对比快照变化（对齐版本 / 音频哈希）-> 只标记，不改动固化结果
    stale_members = _task_stale_members(task)
    has_build = _has_build(task)
    overrides = json.loads(task["overrides_json"] or "{}")
    # 当前规划相对已固化结果是否需要显式重建：
    # 约束（钉住/禁用）改变，或来源刷新导致固化版本/哈希与现行快照不一致。
    frozen = _frozen_from_manifest(task)
    rebuild_required = False
    if has_build and not stale_members:
        rebuild_required = (
            _persist_overrides(overrides) != frozen.get("overrides")
            or _current_frozen_sources(task["comparison_id"])
            != frozen.get("sources"))
    wav_exists = bool(task["wav_path"]
                      and os.path.exists(task["wav_path"]))
    out = {
        "id": task["id"],
        "comparison_id": task["comparison_id"],
        "title": task["title"],
        # 行生命周期：draft（从未固化）/ built（持有固化产物，
        # 即使当前规划 blocked 或需要重建也保持 built）
        "status": task["status"],
        # 当前规划状态：draft（可导出）/ blocked（有谱位无合格来源）
        "plan_status": plan_data["status"],
        "blocked": plan_data["blocked"],
        "created_at": task["created_at"],
        "built_at": task["built_at"],
        "stale": bool(stale_members),
        "stale_members": stale_members,
        # 已固化产物在阻塞/改约束后是否仍被保留（试听/下载继续可用）
        "build_preserved": bool(has_build and plan_data["blocked"]),
        "rebuild_required": bool(rebuild_required),
        "wav_available": bool(has_build and wav_exists),
        "wav_url": (f"/files/synth_{task['id']}.wav"
                    if has_build and wav_exists else None),
        "wav_sha256": task["wav_sha256"],
        "manifest_url": (f"/api/synthesis/{task['id']}/manifest"
                         if has_build else None),
        "overrides": overrides,
        "params": json.loads(task["params_json"]),
        "plan": plan_data,
    }
    if task["manifest_json"]:
        out["manifest"] = json.loads(task["manifest_json"])
    return out


def _frozen_from_manifest(task) -> dict:
    """从已固化清单提取 (规范化约束, 冻结来源版本/哈希)；未固化返回空。"""
    if not task["manifest_json"]:
        return {}
    man = json.loads(task["manifest_json"])
    sources = [
        {"piece_id": f["piece_id"],
         "alignment_version": f["alignment_version"],
         "wav_sha256": f["wav_sha256"]}
        for f in man.get("frozen_sources", [])]
    return {"overrides": man.get("overrides"), "sources": sources}


def _current_frozen_sources(cmp_id: str) -> list[dict]:
    """当前对比快照中各来源的对齐版本 / WAV 哈希（按 ord 排序）。"""
    rows = sorted(db.get_snapshots(cmp_id), key=lambda r: r["ord"])
    return [{"piece_id": r["piece_id"],
             "alignment_version": r["alignment_version"],
             "wav_sha256": r["wav_sha256"]} for r in rows]


def get_task_by_comparison(cmp_id: str):
    task = db.get_synthesis_task_by_comparison(cmp_id)
    return get_task(task["id"]) if task else None


def _task_stale_members(task) -> list[dict]:
    stale = []
    for sr in db.get_snapshots(task["comparison_id"]):
        reasons = cmp_service._stale_reasons(sr)  # noqa: SLF001
        if reasons:
            stale.append({"piece_id": sr["piece_id"], "reasons": reasons})
    return stale


def delete_task(task_id: str):
    task = db.get_synthesis_task(task_id)
    if not task:
        return False
    if task["wav_path"] and os.path.exists(task["wav_path"]):
        os.remove(task["wav_path"])
    return bool(db.delete_synthesis_task(task_id))


# ============================================================ 渲染

def render(cmp_row: dict, plan_data: dict, overrides: dict):
    """按规划把各段伸缩、交叉淡化后混入统一输出时间轴，写 PCM16 WAV。"""
    pieces = {p["piece_id"] for p in plan_data["pieces"]}
    audio = {pid: load_audio(pid) for pid in pieces}

    # 生产环境 librosa 已统一到 config.SAMPLE_RATE；纯标准库回退路径
    # （测试）要求各测试 WAV 同速率，并以该速率输出。
    srs = {s for _y, s in audio.values()}
    if _np is None and len(srs) != 1:
        raise ValueError("纯标准库渲染要求各来源 WAV 采样率一致（请安装 librosa）")
    sr = C.SAMPLE_RATE if _np is not None else next(iter(srs))

    out_end = max((seg["output_time_end_s"] for seg in plan_data["segments"]),
                  default=0.0)
    # 尾长：最后一段按自身最后区间伸缩比外推一个基线期望时值
    last_seg = plan_data["segments"][-1] if plan_data["segments"] else None
    tail_out = 0.0
    if last_seg:
        tail_src = _source_tail_seconds(plan_data, last_seg, audio)
        tail_out = tail_src / max(last_seg["tail_ratio"], 1e-6)
    total_s = out_end + tail_out + 0.05
    n_out = int(total_s * sr) + 1

    if _np is not None:
        mix = _np.zeros(n_out, dtype=float)
    else:
        mix = [0.0] * n_out

    xfade = C.SYNTH_CROSSFADE_S

    for k, seg in enumerate(plan_data["segments"]):
        y, _sr = audio[seg["piece_id"]]
        # 源区间映射锚点：本段各谱位 (output_t, source_t)
        anchors = [(nt["output_onset_s"], nt["source_onset_s"])
                   for nt in seg["notes"]]
        is_first = k == 0
        is_last = k == len(plan_data["segments"]) - 1
        t_start = seg["output_time_start_s"] - (0.0 if is_first else xfade)
        if is_last:
            src_end = anchors[-1][1] + tail_src
        else:
            # 多渲染 xfade 供交叉淡化；源端按最后区间伸缩比外推
            src_end = anchors[-1][1] + xfade * max(seg["tail_ratio"], 1e-6)
        t_end = (seg["output_time_end_s"] + (0.0 if is_last else xfade))
        i0 = max(0, int(t_start * sr))
        i1 = min(n_out - 1, int(t_end * sr) + 1)
        _render_segment(y, mix, i0, i1, sr, anchors, seg["tail_ratio"],
                        src_end, cut_left=not is_first,
                        cut_right=not is_last, xfade=xfade)

    # 确定性归一化：只降不升，峰值到 headroom
    if _np is not None:
        peak = float(_np.max(_np.abs(mix))) if mix.size else 0.0
    else:
        peak = max((abs(v) for v in mix), default=0.0)
    gain = (C.SYNTH_OUTPUT_HEADROOM / peak) if peak > C.SYNTH_OUTPUT_HEADROOM \
        else 1.0
    if gain != 1.0:
        if _np is not None:
            mix = mix * gain
        else:
            mix = [v * gain for v in mix]

    wav_bytes = _write_pcm16(mix, sr)
    manifest = _manifest(cmp_row, plan_data, overrides,
                         hashlib.sha256(wav_bytes).hexdigest(),
                         round(gain, 6), round(total_s, C.ROUND_NDIGITS), sr)
    return wav_bytes, manifest


def _source_tail_seconds(plan_data, seg, audio):
    """尾段源音频的实际可用尾部长度（截到文件末尾）。"""
    y, sr = audio[seg["piece_id"]]
    last_onset = seg["source_time_end_s"]
    return max(0.0, len(y) / sr - last_onset)


def _sample_at(y, pos):
    """线性插值取采样（位置可为越界小数；越界用边界值）。"""
    if pos <= 0:
        return float(y[0])
    if pos >= len(y) - 1:
        return float(y[-1])
    i = int(math.floor(pos))
    f = pos - i
    return float(y[i]) * (1.0 - f) + float(y[i + 1]) * f


def _warp_source_time(anchors, tail_ratio, t_out):
    """输出时间 -> 来源时间：锚点间逐段线性，尾区间按 tail_ratio 外推。"""
    if t_out <= anchors[0][0]:
        # 头外推：用首区间比（切点淡化只有 xfade 长度）
        if len(anchors) >= 2:
            r = ((anchors[1][1] - anchors[0][1])
                 / max(anchors[1][0] - anchors[0][0], 1e-9))
        else:
            r = tail_ratio
        return anchors[0][1] - (anchors[0][0] - t_out) * r
    for k in range(len(anchors) - 1):
        t0, s0 = anchors[k]
        t1, s1 = anchors[k + 1]
        if t0 <= t_out <= t1 and t1 > t0:
            return s0 + (t_out - t0) * (s1 - s0) / (t1 - t0)
    t0, s0 = anchors[-1]
    return s0 + (t_out - t0) * tail_ratio


def _render_segment(y, mix, i0, i1, sr, anchors, tail_ratio, src_end_cap,
                    cut_left, cut_right, xfade):
    first_out = anchors[0][0]
    last_out = anchors[-1][0]
    nsrc = len(y)
    if _np is not None:
        idx = _np.arange(i0, i1)
        if idx.size == 0:
            return
        t = idx / sr
        spos = _np.empty_like(t)
        # 头外推
        head_r = (((anchors[1][1] - anchors[0][1])
                   / max(anchors[1][0] - anchors[0][0], 1e-9))
                  if len(anchors) >= 2 else tail_ratio)
        head = t <= anchors[0][0]
        spos[head] = anchors[0][1] - (anchors[0][0] - t[head]) * head_r
        rest = ~head
        # 尾外推默认值
        spos[rest] = anchors[-1][1] + (t[rest] - anchors[-1][0]) * tail_ratio
        for k in range(len(anchors) - 1):
            t0, s0 = anchors[k]
            t1, s1 = anchors[k + 1]
            if t1 <= t0:
                continue
            m = rest & (t >= t0) & (t <= t1)
            spos[m] = s0 + (t[m] - t0) * (s1 - s0) / (t1 - t0)
        spos = _np.clip(spos, 0.0, nsrc - 1.0001)
        valid = spos <= src_end_cap
        ipos = _np.floor(spos).astype(int)
        frac = spos - ipos
        ya = _np.asarray(y, dtype=float)
        ipos_c = _np.clip(ipos, 0, nsrc - 2)
        v = ya[ipos_c] * (1.0 - frac) + ya[ipos_c + 1] * frac
        gain = _np.ones_like(v)
        if cut_left:
            m = (t < first_out) & (first_out - t < xfade)
            gain[m] = (first_out - t[m]) / xfade
        if cut_right:
            m = (t > last_out) & (t - last_out < xfade)
            gain[m] = 1.0 - (t[m] - last_out) / xfade
        v = v * gain
        v[~valid] = 0.0
        mix[i0:i1] += v
        return
    for i in range(i0, i1):
        t = i / sr
        s_pos = _warp_source_time(anchors, tail_ratio, t)
        # 越界钳制到来源文件范围
        s_pos = min(max(s_pos, 0.0), nsrc - 1.0001)
        if s_pos > src_end_cap:
            continue
        v = _sample_at(y, s_pos)
        # 切点两侧短线性淡化
        if cut_left and t < first_out:
            d = first_out - t
            if d < xfade:
                v *= d / xfade
        if cut_right and t > last_out:
            d = t - last_out
            if d < xfade:
                v *= 1.0 - d / xfade
        mix[i] += v


def _write_pcm16(samples, sr) -> bytes:
    import struct
    if _np is not None:
        clipped = _np.clip(_np.asarray(samples, dtype=float), -1.0, 1.0)
        pcm = _np.round(clipped * 32767.0).astype("<i2").tobytes()
    else:
        pcm = b"".join(struct.pack(
            "<h",
            max(-32768, min(32767,
                int(round(max(-1.0, min(1.0, v)) * 32767)))))
            for v in samples)
    buf = _io_bytesio()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def _io_bytesio():
    import io
    return io.BytesIO()


# ============================================================ 确定性清单

def _manifest(cmp_row, plan_data, overrides, wav_sha256, gain,
              duration_s, sr) -> dict:
    frozen = []
    for p in plan_data["pieces"]:
        frozen.append({
            "piece_id": p["piece_id"],
            "title": p["title"],
            "ord": p["ord"],
            "is_baseline": p["is_baseline"],
            "alignment_version": p["frozen_version"],
            "wav_sha256": p["wav_sha256"],
        })
    return {
        "format": "best-take-synthesis",
        "algorithm_version": C.SYNTH_ALGORITHM_VERSION,
        "comparison_id": cmp_row["id"],
        "comparison_title": cmp_row["title"],
        "baseline_piece": cmp_row["baseline_piece"],
        "params": public_params(),
        "overrides": _persist_overrides(overrides),
        "frozen_sources": frozen,
        "output": {
            "sample_rate": sr,
            "channels": 1,
            "duration_s": duration_s,
            "normalization_gain": gain,
            "wav_sha256": wav_sha256,
        },
        "cost": plan_data["cost"],
        "segments": [
            {k: seg[k] for k in (
                "piece_id", "piece_title", "ord",
                "score_index_start", "score_index_end",
                "measure_start", "measure_end", "occurrence",
                "source_time_start_s", "source_time_end_s",
                "output_time_start_s", "output_time_end_s",
                "tail_ratio", "interval_ratios")}
            for seg in plan_data["segments"]
        ],
        "cuts": plan_data["cuts"],
        "note_selection": [
            {"score_index": s["score_index"], "piece_id": s["piece_id"]}
            for s in plan_data["selection"] if s is not None
        ],
        "blocked": plan_data["blocked"],
    }

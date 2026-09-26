"""多次演奏对比：乐谱指纹、只读对齐快照、版本矩阵与过期判定。

核心规则
--------
1. **乐谱指纹**：由展开音符序列（midi / beat / dur_q）与原谱坐标
   （measure / beat_in_measure / occurrence）生成。指纹不同的录音
   （非同曲 / 反复展开不同）拒绝混入同一对比。
2. **只读快照**：创建或显式刷新时，把每个成员*当前已确认*对齐版本的
   按谱音结果固化到 comparison_snapshots。此后源锚点变化、重新对齐
   只会把对比标记为 stale，**绝不静默改写**已有结论。
3. **版本矩阵（按谱音逐音）**：
   * 基线异常 + 目标正常            -> resolved（已解决）
   * 基线正常 + 目标异常            -> new（新出现 = 相对基线回退）
   * 两侧均异常                     -> persistent（持续）
   * 任一侧待确认 / 用户排除 / 低置信 -> incomparable（不可比）
   pending 与低置信结果永远不算改善，也不算回退。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid

from .. import config as C
from .. import db
from . import review

# 视为"仍异常"的判定（错音 / 漏音 / 起音偏移 / 时值偏差）
ABNORMAL_TAGS = {"wrong_pitch", "onset_offset", "duration_deviation"}
ABNORMAL_STATUSES = {"wrong", "missing"}
# 参与矩阵的判定状态
_ST_NORMAL = "normal"
_ST_ABNORMAL = "abnormal"
_ST_PENDING = "pending_confirm"
_ST_EXCLUDED = "excluded"
_ST_LOWCONF = "low_confidence"

VERDICT_RESOLVED = "resolved"
VERDICT_NEW = "new"
VERDICT_PERSISTENT = "persistent"
VERDICT_INCOMPARABLE = "incomparable"


# ------------------------------------------------------------ 乐谱指纹

def score_fingerprint(score_notes) -> str:
    """展开音符序列 + 原谱坐标 -> sha256。

    指纹只与谱面有关：同一 MusicXML 解析出的各次录音必然相同；
    不同曲目（或展开结果不同）的录音指纹不同，拒绝混入。
    """
    payload = [
        [round(float(n["midi"]), 4),
         round(float(n["beat"]), 5),
         round(float(n["dur_q"]), 5),
         n.get("measure"),
         (round(float(n["beat_in_measure"]), 4)
          if n.get("beat_in_measure") is not None else None),
         n.get("occurrence", 0)]
        for n in score_notes]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------ 快照构建

def _file_hash(piece_id: str, suffix: str):
    path = os.path.join(os.environ.get("DATA_DIR", "/data"), "files",
                        f"{piece_id}{suffix}")
    return review._file_hash(path)  # noqa: SLF001


def _nearest_score_index(onset_s, chain, score_notes, alpha, beta):
    """为未匹配观测（多音 / 待确认）找最近的谱音下标，供同步定位。

    chain: [(score_index, obs_index_onset_time)...] 已按 beat 排序，
    用分段线性插值反推 beat，再取最近谱音；配对不足时退回全局速度模型。
    """
    if not score_notes:
        return None
    if len(chain) >= 2:
        for k in range(len(chain) - 1):
            s0, t0 = chain[k]
            s1, t1 = chain[k + 1]
            b0, b1 = score_notes[s0]["beat"], score_notes[s1]["beat"]
            if t0 <= onset_s <= t1 and b1 > b0:
                beat = b0 + (onset_s - t0) / (t1 - t0) * (b1 - b0)
                return _nearest_by_beat(beat, score_notes)
        # 落在链外：用首/尾段斜率外推
        s0, t0 = chain[0]
        s1, t1 = chain[-1]
        b0, b1 = score_notes[s0]["beat"], score_notes[s1]["beat"]
        if b1 > b0:
            slope = (t1 - t0) / (b1 - b0)
            beat = b0 + (onset_s - t0) / slope
            return _nearest_by_beat(beat, score_notes)
    if alpha and alpha > 0:
        return _nearest_by_beat((onset_s - beta) / alpha, score_notes)
    return score_notes[0]["index"]


def _nearest_by_beat(beat, score_notes):
    best, best_d = None, None
    for n in score_notes:
        d = abs(n["beat"] - beat)
        if best_d is None or d < best_d:
            best, best_d = n["index"], d
    return best


def build_snapshot(piece_id: str) -> dict:
    """把某录音当前已确认的对齐版本固化为只读快照内容。"""
    bundle = review.get_bundle(piece_id)
    if not bundle:
        raise ValueError(f"录音 {piece_id} 不存在")
    align_row = db.get_latest_alignment(piece_id)
    if not align_row:
        raise ValueError(f"录音 {piece_id} 尚无对齐结果，请先完成分析")

    score_rows = {n["index"]: n for n in bundle["score_notes"]}
    obs_rows = {o["index"]: o for o in bundle["obs_notes"]}

    # 已匹配链（含锁定 / 强制锁定），用于未匹配观测的邻近谱音推算
    chain = []
    note_map = {}
    for e in bundle["alignment"]["entries"]:
        si, oj = e["score_index"], e["obs_index"]
        if si is not None and oj is not None:
            on = obs_rows.get(oj)
            if on is not None:
                chain.append((si, float(on["onset"])))
        if si is not None and si in score_rows:
            on = obs_rows.get(oj) if oj is not None else None
            sn = score_rows[si]
            note_map[si] = {
                "score_index": si,
                "measure": sn["measure"],
                "beat_in_measure": sn["beat_in_measure"],
                "occurrence": sn.get("occurrence", 0),
                "beat": round(float(sn["beat"]), C.ROUND_NDIGITS),
                "midi": round(float(sn["midi"]), 4),
                "dur_q": round(float(sn["dur_q"]), C.ROUND_NDIGITS),
                "status": e["status"],
                "op": e["op"],
                "tags": list(e.get("tags") or []),
                "confidence": e["confidence"],
                "obs_index": oj,
                "onset_s": (round(float(on["onset"]), C.ROUND_NDIGITS)
                            if on is not None else None),
                "predicted_onset_s": e.get("predicted_onset"),
                "expected_duration_s": e.get("expected_duration_s"),
                "onset_offset_s": e.get("onset_offset"),
                "duration_ratio": e.get("duration_ratio"),
                "duration_deviation_s": e.get("duration_deviation_s"),
                "pitch_delta": e.get("pitch_delta"),
                "basis": list(e.get("basis") or []),
            }
    chain.sort(key=lambda x: score_rows[x[0]]["beat"])

    alpha = bundle["alignment"]["alpha"] or 0.5
    beta = bundle["alignment"]["beta"] or 0.0

    # 未匹配观测：多音（额外音）与待确认（歧义/无声）。它们不直接占据
    # 矩阵行，但：
    #  * 待确认观测就近投到一个谱音位置；若该谱音未与稳定观测配对
    #    （missing / 低置信），则把该谱音标记 shadow_pending ——
    #    位置上歧义未处置，无法判对错，不可与基线比成改善/回退；
    #  * 多音单独固化，供"新出现的多音"回退汇总与同步定位。
    extras, pending = [], []
    for e in bundle["alignment"]["entries"]:
        if e["score_index"] is not None:
            continue
        oj = e["obs_index"]
        on = obs_rows.get(oj)
        if on is None:
            continue
        item = {
            "obs_index": oj,
            "onset_s": round(float(on["onset"]), C.ROUND_NDIGITS),
            "midi": (round(float(on["midi"]), 4)
                     if on["midi"] is not None else None),
            "state": on["state"],
            "status": e["status"],
            "confidence": e["confidence"],
            "near_score_index": _nearest_score_index(
                float(on["onset"]), chain, bundle["score_notes"],
                alpha, beta),
        }
        if e["status"] == "pending":
            item["reason"] = next(
                (b for b in e.get("basis", [])
                 if b in ("polyphony_ambiguous", "silent_segment")),
                "ambiguous")
            pending.append(item)
        elif e["op"] == "insert" and e["status"] == "extra":
            extras.append(item)

    # 待确认阴影：只覆盖"没有稳定结论"的谱音
    for p in pending:
        ni = p.get("near_score_index")
        if ni is None or ni not in note_map:
            continue
        n = note_map[ni]
        if n.get("obs_index") is None and not n.get("shadow_pending"):
            n["shadow_pending"] = True
            n["shadow_pending_obs"] = p["obs_index"]
            n["shadow_pending_reason"] = p["reason"]

    fingerprint = score_fingerprint(bundle["score_notes"])
    return {
        "piece_id": piece_id,
        "title": bundle["piece"]["title"],
        "audio_url": bundle["piece"]["audio_url"],
        "alignment_version": align_row["version"],
        "musicxml_sha256": _file_hash(piece_id, ".xml"),
        "wav_sha256": _file_hash(piece_id, ".wav"),
        "fingerprint": fingerprint,
        "note_count": len(note_map),
        "notes": [note_map[i] for i in sorted(note_map)],
        "extras": extras,
        "pending": pending,
    }


# ------------------------------------------------------------ 单音判定

def classify_note(note: dict | None) -> str:
    """把快照中的单个谱音结果归一为矩阵状态。"""
    if note is None:
        return _ST_PENDING
    # 该谱音位置存在未处置的待确认观测（且谱音未与稳定观测配对）：
    # 无法判断对错，按待确认处理，绝不与基线比成改善/回退。
    if note.get("shadow_pending"):
        return _ST_PENDING
    if note["op"] == "excluded_score" or note["status"] == "excluded":
        return _ST_EXCLUDED
    if note["confidence"] is None or \
            float(note["confidence"]) < C.COMPARE_MIN_CONFIDENCE:
        return _ST_LOWCONF
    if note["status"] in ABNORMAL_STATUSES or \
            ABNORMAL_TAGS.intersection(note.get("tags") or []):
        return _ST_ABNORMAL
    return _ST_NORMAL


def cell_verdict(base_state: str, target_state: str) -> str:
    """按谱音的基线/目标状态 -> 矩阵判定。"""
    if base_state == _ST_ABNORMAL and target_state == _ST_NORMAL:
        return VERDICT_RESOLVED
    if base_state == _ST_NORMAL and target_state == _ST_ABNORMAL:
        return VERDICT_NEW
    if base_state == _ST_ABNORMAL and target_state == _ST_ABNORMAL:
        return VERDICT_PERSISTENT
    return VERDICT_INCOMPARABLE


# ------------------------------------------------------------ 创建 / 刷新

def create_comparison(member_ids: list[str], baseline_id: str,
                      title: str | None = None) -> str:
    members = _validate_members(member_ids, baseline_id)
    snapshots = [build_snapshot(pid) for pid in members]
    _assert_same_fingerprint(members, snapshots)

    cmp_id = uuid.uuid4().hex[:12]
    base_title = next(s["title"] for s in snapshots
                      if s["piece_id"] == baseline_id)
    db.create_comparison(cmp_id,
                         title or f"{base_title} · {len(members)}次演奏对比",
                         baseline_id)
    _persist_snapshots(cmp_id, members, baseline_id, snapshots)
    return cmp_id


def refresh_comparison(cmp_id: str) -> dict:
    """用户显式刷新：用当前已确认对齐重建全部成员快照。

    指纹已不再一致（理论上不应发生：谱面不可变）时拒绝刷新，保留旧结论。
    """
    cmp_row = db.get_comparison(cmp_id)
    if not cmp_row:
        raise ValueError("对比不存在")
    rows = db.get_snapshots(cmp_id)
    members = [r["piece_id"] for r in rows]
    baseline_id = cmp_row["baseline_piece"]
    _validate_members(members, baseline_id)
    snapshots = [build_snapshot(pid) for pid in members]
    _assert_same_fingerprint(members, snapshots)
    _persist_snapshots(cmp_id, members, baseline_id, snapshots)
    db.touch_comparison(cmp_id)
    return get_comparison(cmp_id)


def _validate_members(member_ids, baseline_id):
    ids = list(dict.fromkeys(member_ids))   # 去重保序
    if not (C.COMPARE_MIN_MEMBERS <= len(ids) <= C.COMPARE_MAX_MEMBERS):
        raise ValueError(
            f"对比需选择 {C.COMPARE_MIN_MEMBERS}–{C.COMPARE_MAX_MEMBERS} "
            f"次录音，当前 {len(ids)} 次")
    if baseline_id not in ids:
        raise ValueError("基线必须是所选录音之一")
    for pid in ids:
        piece = db.get_piece(pid)
        if not piece:
            raise ValueError(f"录音 {pid} 不存在")
        if piece["status"] != "analyzed":
            raise ValueError(f"《{piece['title']}》尚未完成分析，不能加入对比")
        if not db.get_latest_alignment(pid):
            raise ValueError(f"《{piece['title']}》尚无已确认对齐版本")
    return ids


def _assert_same_fingerprint(members, snapshots):
    fps = {s["piece_id"]: s["fingerprint"] for s in snapshots}
    baseline_fp = next(iter(fps.values()))
    mismatched = [s for s in snapshots if s["fingerprint"] != baseline_fp]
    if mismatched:
        names = "、".join(f"《{s['title']}》" for s in mismatched)
        raise ValueError(
            f"乐谱指纹不一致，拒绝混入不可比录音：{names} "
            "（展开音符序列或原谱坐标不同，请确认是同一首曲子的同一份谱）")


def _persist_snapshots(cmp_id, members, baseline_id, snapshots):
    for ord_, snap in enumerate(snapshots):
        db.upsert_snapshot(
            cmp_id, snap["piece_id"],
            is_baseline=snap["piece_id"] == baseline_id,
            ord_=ord_,
            alignment_version=snap["alignment_version"],
            musicxml_sha256=snap["musicxml_sha256"],
            wav_sha256=snap["wav_sha256"],
            fingerprint=snap["fingerprint"],
            snapshot_json=json.dumps(snap, ensure_ascii=False,
                                     sort_keys=True,
                                     separators=(",", ":")))


# ------------------------------------------------------------ 过期判定

def _stale_reasons(row) -> list[str]:
    """快照固化后来源是否变化。任何变化都只标记过期，不改写结论。"""
    reasons = []
    latest = db.get_latest_alignment(row["piece_id"])
    if latest is None:
        reasons.append("alignment_missing")
    elif latest["version"] != row["alignment_version"]:
        # 源锚点增删 / 待确认处置 / 手动重算都会产生新版本号
        reasons.append("alignment_version_changed")
    cur_wav = _file_hash(row["piece_id"], ".wav")
    if cur_wav is not None and cur_wav != row["wav_sha256"]:
        reasons.append("audio_changed")
    cur_xml = _file_hash(row["piece_id"], ".xml")
    if cur_xml is not None and cur_xml != row["musicxml_sha256"]:
        reasons.append("score_changed")
    return reasons


# ------------------------------------------------------------ 矩阵与汇总

def build_matrix(snapshots: list[dict], baseline_id: str) -> dict:
    base_snap = next(s for s in snapshots if s["piece_id"] == baseline_id)
    ordered = [base_snap] + [s for s in snapshots
                             if s["piece_id"] != baseline_id]
    base_by_idx = {n["score_index"]: n for n in base_snap["notes"]}

    cells, target_ids = [], [s["piece_id"] for s in ordered[1:]]
    for idx in sorted(base_by_idx):
        bn = base_by_idx[idx]
        bs = classify_note(bn)
        row = {
            "score_index": idx,
            "measure": bn["measure"],
            "beat_in_measure": bn["beat_in_measure"],
            "occurrence": bn.get("occurrence", 0),
            "beat": bn["beat"],
            "midi": bn["midi"],
            "baseline": _cell_side(bn, bs),
            "targets": {},
        }
        for s in ordered[1:]:
            tn = next((n for n in s["notes"]
                       if n["score_index"] == idx), None)
            ts = classify_note(tn)
            row["targets"][s["piece_id"]] = {
                "verdict": cell_verdict(bs, ts),
                **_cell_side(tn, ts),
            }
        cells.append(row)

    measures = sorted({c["measure"] for c in cells
                       if c["measure"] is not None})
    return {"cells": cells, "target_order": target_ids, "measures": measures}


def _cell_side(note, state):
    if note is None:
        return {"state": state, "status": None, "tags": [],
                "confidence": None, "obs_index": None, "onset_s": None}
    return {
        "state": state,
        "status": note["status"],
        "tags": note.get("tags") or [],
        "confidence": note["confidence"],
        "obs_index": note.get("obs_index"),
        "onset_s": note.get("onset_s"),
        "predicted_onset_s": note.get("predicted_onset_s"),
        "pitch_delta": note.get("pitch_delta"),
        "onset_offset_s": note.get("onset_offset_s"),
    }


def build_member_stats(snap: dict, base_snap: dict,
                       baseline_fp: str) -> dict:
    """单个目标成员相对基线的汇总（不可比不计入改善/回退）。"""
    b_by = {n["score_index"]: n for n in base_snap["notes"]}
    counts = {VERDICT_RESOLVED: 0, VERDICT_NEW: 0,
              VERDICT_PERSISTENT: 0, VERDICT_INCOMPARABLE: 0,
              "normal_normal": 0}
    base_extra_keys = {_extra_key(e) for e in base_snap["extras"]}
    new_extras = [e for e in snap["extras"]
                  if _extra_key(e) not in base_extra_keys]

    for n in snap["notes"]:
        bs = classify_note(b_by.get(n["score_index"]))
        ts = classify_note(n)
        v = cell_verdict(bs, ts)
        if bs == _ST_NORMAL and ts == _ST_NORMAL:
            counts["normal_normal"] += 1
        else:
            counts[v] += 1

    return {
        "piece_id": snap["piece_id"],
        "title": snap["title"],
        "audio_url": snap["audio_url"],
        "alignment_version": snap["alignment_version"],
        "resolved": counts[VERDICT_RESOLVED],
        "new": counts[VERDICT_NEW],
        "persistent": counts[VERDICT_PERSISTENT],
        "incomparable": counts[VERDICT_INCOMPARABLE],
        "normal": counts["normal_normal"],
        "new_extra_count": len(new_extras),
        "pending_count": len(snap["pending"]),
        "fingerprint_match": snap["fingerprint"] == baseline_fp,
    }


def _extra_key(e):
    """多音按"邻近谱音 + 音高"匹配基线中的同一处（时间轴各次不同）。"""
    return (e.get("near_score_index"),
            round(e["midi"], 1) if e.get("midi") is not None else None)


def build_problem_segments(snap: dict, base_snap: dict) -> list[dict]:
    """该次仍异常（new / persistent）的连续谱音区段，按连续下标合并。"""
    b_by = {n["score_index"]: n for n in base_snap["notes"]}
    n_by = {n["score_index"]: n for n in snap["notes"]}
    flagged = []
    for idx, n in n_by.items():
        v = cell_verdict(classify_note(b_by.get(idx)), classify_note(n))
        if v in (VERDICT_NEW, VERDICT_PERSISTENT):
            flagged.append((idx, n, v))
    flagged.sort(key=lambda x: x[0])

    segments, cur = [], []
    for item in flagged:
        if cur and item[0] != cur[-1][0] + 1:
            segments.append(cur)
            cur = []
        cur.append(item)
    if cur:
        segments.append(cur)

    out = []
    for seg in segments:
        idxs = [i for i, _n, _v in seg]
        notes = [n for _i, n, _v in seg]
        verdicts = sorted({v for _i, _n, v in seg})
        tag_counts = {}
        for n in notes:
            for t in ABNORMAL_TAGS.intersection(n.get("tags") or []):
                tag_counts[t] = tag_counts.get(t, 0) + 1
            if n["status"] in ABNORMAL_STATUSES:
                tag_counts[n["status"]] = tag_counts.get(n["status"], 0) + 1
        out.append({
            "piece_id": snap["piece_id"],
            "score_index_start": idxs[0],
            "score_index_end": idxs[-1],
            "measure_start": notes[0]["measure"],
            "measure_end": notes[-1]["measure"],
            "occurrence": notes[0].get("occurrence", 0),
            "verdicts": verdicts,
            "note_count": len(seg),
            "tag_counts": tag_counts,
        })
    return out


def build_regressions(snap: dict, base_snap: dict) -> dict:
    """相对基线的回退清单：新出现异常谱音 + 新出现的多音。"""
    b_by = {n["score_index"]: n for n in base_snap["notes"]}
    new_notes = []
    for n in snap["notes"]:
        bn = b_by.get(n["score_index"])
        if cell_verdict(classify_note(bn), classify_note(n)) == VERDICT_NEW:
            new_notes.append({
                "score_index": n["score_index"],
                "measure": n["measure"],
                "beat_in_measure": n["beat_in_measure"],
                "occurrence": n.get("occurrence", 0),
                "midi": n["midi"],
                "status": n["status"],
                "tags": n.get("tags") or [],
                "onset_s": n.get("onset_s"),
                "confidence": n["confidence"],
            })
    base_extra_keys = {_extra_key(e) for e in base_snap["extras"]}
    new_extras = [e for e in snap["extras"]
                  if _extra_key(e) not in base_extra_keys]
    return {"piece_id": snap["piece_id"],
            "new_notes": new_notes, "new_extras": new_extras}


# ------------------------------------------------------------ 读取

def list_comparisons() -> list[dict]:
    out = []
    for row in db.list_comparisons():
        snap_rows = db.get_snapshots(row["id"])
        stale = []
        for sr in snap_rows:
            reasons = _stale_reasons(sr)
            if reasons:
                stale.append({"piece_id": sr["piece_id"], "reasons": reasons})
        out.append({
            "id": row["id"],
            "title": row["title"],
            "baseline_piece": row["baseline_piece"],
            "created_at": row["created_at"],
            "refreshed_at": row["refreshed_at"],
            "member_count": len(snap_rows),
            "stale": bool(stale),
            "stale_members": stale,
        })
    return out


def get_comparison(cmp_id: str) -> dict:
    row = db.get_comparison(cmp_id)
    if not row:
        raise ValueError("对比不存在")
    snap_rows = db.get_snapshots(cmp_id)
    snapshots = [json.loads(r["snapshot_json"]) for r in snap_rows]
    baseline_id = row["baseline_piece"]
    base_snap = next((s for s in snapshots if s["piece_id"] == baseline_id),
                     snapshots[0])

    matrix = build_matrix(snapshots, baseline_id)
    stale, members = [], []
    for sr, snap in zip(snap_rows, snapshots):
        reasons = _stale_reasons(sr)
        is_base = snap["piece_id"] == baseline_id
        member = {
            "piece_id": snap["piece_id"],
            "title": snap["title"],
            "audio_url": snap["audio_url"],
            "is_baseline": is_base,
            "ord": sr["ord"],
            "frozen_version": sr["alignment_version"],
            "fingerprint": sr["fingerprint"],
            "pending_count": len(snap["pending"]),
            "extra_count": len(snap["extras"]),
            "stale": bool(reasons),
            "stale_reasons": reasons,
        }
        if not is_base:
            member.update({
                "stats": build_member_stats(snap, base_snap,
                                            base_snap["fingerprint"]),
                "problem_segments": build_problem_segments(snap, base_snap),
                "regressions": build_regressions(snap, base_snap),
            })
        members.append(member)
        if reasons:
            stale.append({"piece_id": snap["piece_id"], "reasons": reasons})

    return {
        "id": row["id"],
        "title": row["title"],
        "baseline_piece": baseline_id,
        "created_at": row["created_at"],
        "refreshed_at": row["refreshed_at"],
        "stale": bool(stale),
        "stale_members": stale,
        "thresholds": {"min_confidence": C.COMPARE_MIN_CONFIDENCE},
        "members": members,
        "matrix": matrix,
    }

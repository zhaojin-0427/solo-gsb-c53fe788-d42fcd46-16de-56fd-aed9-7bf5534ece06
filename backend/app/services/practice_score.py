"""偏差片段练习谱：片段校验 + 从原 MusicXML 提取整小节生成练习谱。

模型
====
* 在**单次复盘**中，按“展开后的整小节（measure + occurrence 反复轮次）”
  框选若干**连续片段**（按下标顺序、不可重叠）；每段设置循环次数、
  起始速度（BPM）与逐轮增速（每轮 +/− BPM）。
* **生成准入（逐位校验，阻止生成）**：段内每个谱位必须是
  **已确认**结论——存在未处置待确认（``pending_confirmation``）、
  用户已排除（``excluded``）或置信度低于 ``PRACTICE_MIN_CONFIDENCE``
  （``low_confidence``）的谱位时，逐位给出原因并阻止生成；
  每段还必须包含至少一个**已确认的偏差音**（错音 / 漏音，或置信度达标的
  起音偏移 / 时值偏差；段内就近归入的已确认多音也算偏差音）。
* **MusicXML 提取**：系统从原 MusicXML 按反复**展开后的演奏轮次**提取对应
  **整小节**，保留音高、时值、休止、拍号、调号与**片段内**连音（延音线 /
  连音线）；**跨片段连音在片段边界截断并逐处标记**，绝不留下悬空连音。
  每个片段按用户选定顺序、逐轮展开，每轮开头注明**原谱坐标与速度（BPM）**。
* **固化与版本留存**：每次成功生成（创建 / 显式重建）都追加一个**只读版本**：
  独立的 MusicXML 文件、固化清单与数据库指针互不覆盖，任务行只记录当前版本号。
  下载得到的 MusicXML 内嵌来源谱哈希、对齐版本与生成参数。之后来源谱哈希或
  对齐版本变化只把旧版本标记为 ``stale``，**旧稿始终可下载、可回溯**；
  只有用户**显式重建**且校验通过后才产生新版本并切换“当前版本”指针；
  重建失败（含逐位阻塞 / 生成异常）**不新增、不改写任何版本**。
  删除任务时清理全部版本文件与版本行。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET

from .. import config as C
from .. import db
from . import comparison as cmp_service
from . import review

# 阻止生成的逐位原因
BLOCK_PENDING = "pending_confirmation"
BLOCK_EXCLUDED = "excluded"
BLOCK_LOW_CONF = "low_confidence"
BLOCK_NO_DEVIATION = "no_confirmed_deviation"   # 整段无已确认偏差音
BLOCK_REASONS = (BLOCK_PENDING, BLOCK_EXCLUDED, BLOCK_LOW_CONF)

ABNORMAL_TAGS = {"wrong_pitch", "onset_offset", "duration_deviation"}
ABNORMAL_STATUSES = {"wrong", "missing"}


class BlockedError(Exception):
    """存在阻止生成的谱位 / 片段（HTTP 422）。"""

    def __init__(self, message: str, blockers: list, segment_reports: list):
        super().__init__(message)
        self.blockers = blockers
        self.segment_reports = segment_reports


# ============================================================ 谱位状态

def _snapshot_and_notes(piece_id: str):
    """当前已确认对齐 -> (snapshot, {score_index: note})。"""
    snap = cmp_service.build_snapshot(piece_id)
    return snap, {n["score_index"]: n for n in snap["notes"]}


def _blocker_reason(note) -> str | None:
    """单个谱位是否阻止生成：待确认 / 已排除 / 低置信，否则 None。"""
    if note is None:
        return BLOCK_PENDING
    if note.get("shadow_pending"):
        return BLOCK_PENDING
    if note.get("op") == "excluded_score" or note.get("status") == "excluded":
        return BLOCK_EXCLUDED
    conf = note.get("confidence")
    if conf is None or float(conf) < C.PRACTICE_MIN_CONFIDENCE:
        return BLOCK_LOW_CONF
    return None


def _is_confirmed_deviation(note) -> bool:
    """谱位是否为已确认偏差音（置信度达标、非待确认/排除）。"""
    if note is None or _blocker_reason(note) is not None:
        return False
    if note.get("status") in ABNORMAL_STATUSES:
        return True
    return bool(ABNORMAL_TAGS.intersection(note.get("tags") or []))


def _expanded_units(score_notes) -> list[dict]:
    """展开谱音 -> 按展开 beat 排序的整小节（measure + occurrence）序列。"""
    groups: dict[tuple, list] = {}
    for n in score_notes:
        key = (n["measure"], n.get("occurrence", 0) or 0)
        groups.setdefault(key, []).append(n["index"])
    beat_of = {n["index"]: float(n["beat"]) for n in score_notes}
    units = []
    for (measure, occ), indexes in groups.items():
        indexes.sort()
        units.append({
            "measure": measure,
            "occurrence": occ,
            "score_indexes": indexes,
            "beat_start": beat_of[indexes[0]],
        })
    units.sort(key=lambda u: (u["beat_start"], u["measure"], u["occurrence"]))
    for i, u in enumerate(units):
        u["expanded_order"] = i
    return units


# ============================================================ 编辑器数据

def build_editor_data(piece_id: str) -> dict:
    """前端框选片段所需数据：展开小节序列 + 每谱位状态 + 阈值。"""
    piece = db.get_piece(piece_id)
    if not piece:
        raise ValueError("乐曲不存在")
    if piece["status"] != "analyzed":
        raise ValueError("该录音尚未完成分析，不能生成练习谱")
    align = db.get_latest_alignment(piece_id)
    if not align:
        raise ValueError("该录音尚无已确认对齐版本")
    bundle = review.get_bundle(piece_id)
    snap, notes_by_idx = _snapshot_and_notes(piece_id)
    units = _expanded_units(bundle["score_notes"])

    unit_notes = []
    midi_of = {n["index"]: n["midi"] for n in bundle["score_notes"]}
    for u in units:
        notes_out, blockers, dev = [], [], []
        for idx in u["score_indexes"]:
            note = notes_by_idx.get(idx)
            reason = _blocker_reason(note)
            is_dev = _is_confirmed_deviation(note)
            if reason:
                blockers.append({"score_index": idx, "reason": reason})
            if is_dev:
                dev.append(idx)
            notes_out.append({
                "score_index": idx,
                "midi": midi_of.get(idx),
                "status": note.get("status") if note else None,
                "tags": list(note.get("tags") or []) if note else [],
                "confidence": note.get("confidence") if note else None,
                "blocker": reason,
                "is_deviation": is_dev,
            })
        unit_notes.append({
            "measure": u["measure"],
            "occurrence": u["occurrence"],
            "expanded_order": u["expanded_order"],
            "score_indexes": u["score_indexes"],
            "notes": notes_out,
            "blockers": blockers,
            "deviation_indexes": dev,
            "selectable": not blockers,
        })

    # 段内可计入的已确认多音（按邻近谱位）
    extras = [{
        "obs_index": x["obs_index"],
        "near_score_index": x["near_score_index"],
        "midi": x.get("midi"),
        "onset_s": x.get("onset_s"),
    } for x in snap.get("extras", [])
        if x.get("near_score_index") is not None
        and (x.get("confidence") is None
             or float(x["confidence"]) >= C.PRACTICE_MIN_CONFIDENCE)]

    return {
        "piece_id": piece_id,
        "title": piece["title"],
        "alignment_version": align["version"],
        "musicxml_sha256": review._file_hash(piece["musicxml_path"]),  # noqa: SLF001
        "thresholds": {
            "min_confidence": C.PRACTICE_MIN_CONFIDENCE,
            "max_segments": C.PRACTICE_MAX_SEGMENTS,
            "max_loops": C.PRACTICE_MAX_LOOPS,
            "tempo_bpm_min": C.PRACTICE_MIN_TEMPO_BPM,
            "tempo_bpm_max": C.PRACTICE_MAX_TEMPO_BPM,
            "tempo_step_max": C.PRACTICE_MAX_TEMPO_STEP_BPM,
            "tempo_directions": list(C.PRACTICE_TEMPO_DIRECTIONS),
        },
        "measures": unit_notes,
        "extras": extras,
    }


# ============================================================ 片段规范化

def normalize_segments(raw_segments, units) -> list[dict]:
    """校验用户框选的连续（整小节 + 反复轮次）片段与循环/速度参数。

    units: :func:`_expanded_units` 的结果（含 expanded_order）。
    片段**按提交顺序保留**（即练习顺序），只校验连续与互不重叠。
    """
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("至少需要框选一个连续片段")
    if len(raw_segments) > C.PRACTICE_MAX_SEGMENTS:
        raise ValueError(f"一张练习谱最多 {C.PRACTICE_MAX_SEGMENTS} 个片段")
    key2order = {(u["measure"], u["occurrence"]): u["expanded_order"]
                 for u in units}

    out, covered = [], set()
    for k, seg in enumerate(raw_segments):
        ms = _int(seg.get("measure_start"), f"第 {k + 1} 段：起始小节号")
        os_ = _int(seg.get("occurrence_start", 0),
                   f"第 {k + 1} 段：起始反复轮次")
        me = _int(seg.get("measure_end", seg.get("measure_start")),
                  f"第 {k + 1} 段：结束小节号")
        oe = _int(seg.get("occurrence_end",
                          seg.get("occurrence_start", 0)),
                  f"第 {k + 1} 段：结束反复轮次")
        for key in ((ms, os_), (me, oe)):
            if key not in key2order:
                raise ValueError(
                    f"第 {k + 1} 段：展开谱中不存在 小节 {key[0]}"
                    f"（第 {key[1] + 1} 次演奏）")
        a, b = key2order[(ms, os_)], key2order[(me, oe)]
        if a > b:
            raise ValueError(
                f"第 {k + 1} 段：起点（小节 {ms} 第 {os_ + 1} 次）"
                "不得晚于终点")
        orders = set(range(a, b + 1))
        overlap = sorted(covered.intersection(orders))
        if overlap:
            raise ValueError(
                f"第 {k + 1} 段与其他片段重叠（展开顺序位置："
                f"{overlap[:5]}），片段不得重叠")
        covered.update(orders)

        loops = _int(seg.get("loops", 1), f"第 {k + 1} 段：循环次数")
        if not (1 <= loops <= C.PRACTICE_MAX_LOOPS):
            raise ValueError(
                f"第 {k + 1} 段：循环次数须在 1–{C.PRACTICE_MAX_LOOPS} 之间")
        start_tempo = _num(seg.get("start_tempo_bpm"),
                           f"第 {k + 1} 段：起始速度")
        if not (C.PRACTICE_MIN_TEMPO_BPM <= start_tempo
                <= C.PRACTICE_MAX_TEMPO_BPM):
            raise ValueError(
                f"第 {k + 1} 段：起始速度须在 "
                f"{C.PRACTICE_MIN_TEMPO_BPM:g}–"
                f"{C.PRACTICE_MAX_TEMPO_BPM:g} BPM 之间")
        step = _num(seg.get("tempo_increment_bpm", 0.0),
                    f"第 {k + 1} 段：逐轮增速")
        if not (0.0 <= step <= C.PRACTICE_MAX_TEMPO_STEP_BPM):
            raise ValueError(
                f"第 {k + 1} 段：逐轮增速须在 0–"
                f"{C.PRACTICE_MAX_TEMPO_STEP_BPM:g} BPM 之间")
        direction = str(seg.get("tempo_direction", "up"))
        if direction not in C.PRACTICE_TEMPO_DIRECTIONS:
            raise ValueError(
                f"第 {k + 1} 段：速度方向须为 "
                f"{list(C.PRACTICE_TEMPO_DIRECTIONS)} 之一")
        last_tempo = round(
            start_tempo + (loops - 1) * step
            if direction == "up"
            else start_tempo - (loops - 1) * step
            if direction == "down" else start_tempo, 4)
        if not (C.PRACTICE_MIN_TEMPO_BPM <= last_tempo
                <= C.PRACTICE_MAX_TEMPO_BPM):
            raise ValueError(
                f"第 {k + 1} 段：逐轮后的末轮速度 {last_tempo:g} BPM 超出 "
                f"{C.PRACTICE_MIN_TEMPO_BPM:g}–"
                f"{C.PRACTICE_MAX_TEMPO_BPM:g} BPM 范围")

        out.append({
            "measure_start": ms,
            "occurrence_start": os_,
            "measure_end": me,
            "occurrence_end": oe,
            "order_start": a,
            "order_end": b,
            "loops": loops,
            "start_tempo_bpm": start_tempo,
            "tempo_increment_bpm": step,
            "tempo_direction": direction,
        })
    return out


def _num(v, label):
    try:
        return round(float(v), C.ROUND_NDIGITS)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是数字") from exc


def _int(v, label):
    try:
        return int(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是整数") from exc


def round_tempo(seg: dict, loop: int) -> float:
    """第 loop 轮（1 基）的速度 BPM。"""
    d = loop - 1
    if seg["tempo_direction"] == "up":
        v = seg["start_tempo_bpm"] + d * seg["tempo_increment_bpm"]
    elif seg["tempo_direction"] == "down":
        v = seg["start_tempo_bpm"] - d * seg["tempo_increment_bpm"]
    else:
        v = seg["start_tempo_bpm"]
    return round(v, 2)


# ============================================================ 逐位校验

def validate_segments(piece_id: str, segments: list[dict]) -> dict:
    """对规范化后的片段逐谱位检查生成准入；不通过抛 :class:`BlockedError`。

    返回 {"segments": [逐段报告], "blockers": 全部阻止谱位}。
    """
    bundle = review.get_bundle(piece_id)
    units = _expanded_units(bundle["score_notes"])
    score_by_idx = {n["index"]: n for n in bundle["score_notes"]}
    snap, notes_by_idx = _snapshot_and_notes(piece_id)
    extra_near = [x for x in snap.get("extras", [])
                  if x.get("near_score_index") is not None
                  and (x.get("confidence") is None
                       or float(x["confidence"]) >= C.PRACTICE_MIN_CONFIDENCE)]

    reports, all_blockers = [], []
    for k, seg in enumerate(segments):
        indexes = []
        for u in units[seg["order_start"]:seg["order_end"] + 1]:
            indexes.extend(u["score_indexes"])
        blockers = []
        for idx in indexes:
            reason = _blocker_reason(notes_by_idx.get(idx))
            if reason:
                sn = score_by_idx.get(idx)
                item = {
                    "segment": k + 1,
                    "score_index": idx,
                    "measure": sn["measure"] if sn else None,
                    "beat_in_measure": sn["beat_in_measure"] if sn else None,
                    "occurrence": sn["occurrence"] if sn else None,
                    "reason": reason,
                }
                blockers.append(item)
                all_blockers.append(item)
        deviation_indexes = [i for i in indexes
                             if _is_confirmed_deviation(notes_by_idx.get(i))]
        seg_extras = [x for x in extra_near
                      if indexes[0] <= x["near_score_index"] <= indexes[-1]]
        no_dev = not deviation_indexes and not seg_extras
        if no_dev:
            all_blockers.append({
                "segment": k + 1,
                "score_index": None,
                "measure": seg["measure_start"],
                "beat_in_measure": None,
                "occurrence": seg["occurrence_start"],
                "reason": BLOCK_NO_DEVIATION,
            })
        reports.append({
            "segment": k + 1,
            "measure_start": seg["measure_start"],
            "occurrence_start": seg["occurrence_start"],
            "measure_end": seg["measure_end"],
            "occurrence_end": seg["occurrence_end"],
            "score_indexes": indexes,
            "blockers": blockers,
            "deviation_indexes": deviation_indexes,
            "confirmed_extra_obs": [x["obs_index"] for x in seg_extras],
            "has_confirmed_deviation": not no_dev,
            "can_generate": not blockers and not no_dev,
        })

    if all_blockers:
        raise BlockedError(
            "存在阻止生成的谱位（待确认 / 已排除 / 低置信）"
            "或不含已确认偏差音的片段，请逐位处理后再生成",
            all_blockers, reports)
    return {"segments": reports, "blockers": []}


# ============================================================ MusicXML 解析

def _ln(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _ns_of(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _children(el, name):
    return [c for c in list(el) if _ln(c.tag) == name]


def _child(el, name):
    return next((c for c in list(el) if _ln(c.tag) == name), None)


def _source_xml_path(path: str) -> str:
    """压缩 .mxl -> 解压到临时文件并返回路径；普通 XML 原样返回。"""
    if path.lower().endswith(".mxl"):
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rootfile = next(
                (e for e in container.iter()
                 if _ln(e.tag) == "rootfile"), None)
            if rootfile is None:
                raise ValueError("压缩谱 (.mxl) 中找不到 rootfile")
            inner = rootfile.attrib["full-path"]
            fd, tmp = tempfile.mkstemp(suffix=".musicxml")
            with os.fdopen(fd, "wb") as f:
                f.write(z.read(inner))
            return tmp
    return path


def _midi_of_pitch(step, alter, octave):
    if step is None or octave is None:
        return None
    names = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    return 12 * (int(octave.text) + 1) + names.get(step.text.strip()) \
        + (int(round(float(alter.text))) if alter is not None
           and alter.text not in (None, "") else 0)


def _measure_signature(m_el) -> tuple:
    """物理小节内容签名：(音高 MIDI / 休止 / 和弦最高音) 序列。

    只用于把展开后的小节对回物理小节（区分一/二房子等同小节号不同内容），
    单声部前提下签名即“音高序列”；和弦取最高音（与 score_parser 一致）。
    """
    sig = []
    for n in _children(m_el, "note"):
        if _child(n, "chord") is not None:
            continue  # 和弦的后续成员，只保留首个（最高音由 XML 顺序不保证，
                     # 单声部输入中罕见，保持序列对齐即可）
        if _child(n, "rest") is not None:
            sig.append(("rest", None))
            continue
        p = _child(n, "pitch")
        if p is None:
            continue
        sig.append(("note", _midi_of_pitch(_child(p, "step"),
                                           _child(p, "alter"),
                                           _child(p, "octave"))))
    return tuple(sig)


def _m21_expanded_measures(path: str):
    """用 music21 展开反复，返回 [(measure_number, content_signature), ...]。

    惰性导入 music21；无 music21 或展开失败时返回 None，调用方退化为
    按物理小节顺序（不展开）的确定性方案。
    """
    try:
        from music21 import converter  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        score = converter.parse(path)
        parts = list(score.parts) if hasattr(score, "parts") else [score]
        expanded = parts[0].expandRepeats()
    except Exception:  # noqa: BLE001
        return None
    out = []
    for m in expanded.getElementsByClass("Measure"):
        num = m.measureNumber
        if not isinstance(num, int):
            continue
        sig = []
        try:
            flat = m.flatten().notesAndRests
        except Exception:  # noqa: BLE001
            flat = list(m.notesAndRests)
        for el in flat:
            cls = type(el).__name__
            if "Rest" in cls:
                sig.append(("rest", None))
            elif "Chord" in cls:
                top = max(el.pitches, key=lambda p: p.midi)
                sig.append(("note", int(round(float(top.midi)))))
            elif "Note" in cls:
                sig.append(("note", int(round(float(el.pitch.midi)))))
        out.append((num, tuple(sig)))
    return out or None


def build_expansion_map(xml_path: str):
    """反复展开后的小节序列 -> 物理小节。

    返回 (units, physical_measures, ns, warnings)：
    units = [{measure, occurrence, physical_index, signature}, ...]
    （按展开顺序）；physical_measures 为原谱 <measure> 元素（文档顺序）。

    匹配策略：先按小节号把物理小节组织成 FIFO 队列（反复必然重新消费队列），
    再用内容签名校验队首；一/二房子等“同小节号不同内容”由 FIFO 顺序区分。
    签名不符记 warning 但仍按 FIFO 对回（不阻塞提取）。没有 music21 时退化为
    物理顺序（occurrence 全为 0）。
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    ns = _ns_of(root.tag)
    part = next((p for p in list(root)
                 if _ln(p.tag) == "part"), None)
    if part is None:
        raise ValueError("MusicXML 中找不到 <part>")
    physical = _children(part, "measure")
    if not physical:
        raise ValueError("MusicXML 中找不到任何小节")
    sigs = [_measure_signature(m) for m in physical]
    warnings_out = []

    expanded = _m21_expanded_measures(xml_path)
    if expanded is None:
        warnings_out.append("music21 不可用或反复展开失败："
                            "按原谱物理小节顺序提取（不展开反复）")
        units = [{
            "measure": _measure_number(m, i + 1),
            "occurrence": 0,
            "physical_index": i,
            "signature": sigs[i],
        } for i, m in enumerate(physical)]
        return units, physical, ns, warnings_out

    # 物理小节按小节号组织成 FIFO 队列；反复展开会**再次**消费同一物理小节，
    # 因此队列用尽时回到该小节号的物理小节序列从头取用。
    pools: dict[int, list[int]] = {}
    for i, m in enumerate(physical):
        pools.setdefault(_measure_number(m, i + 1), []).append(i)
    ptr = {num: 0 for num in pools}

    units, occ_counter, visited = [], {}, set()
    for num, esig in expanded:
        if num in visited:
            occ_counter[num] = occ_counter.get(num, 0) + 1
        visited.add(num)
        occ = occ_counter.get(num, 0)
        pool = pools.get(num)
        if not pool:
            raise ValueError(f"展开小节 {num} 在原谱中找不到对应小节")
        idx = ptr[num]
        if idx >= len(pool):
            # 反复演奏：重新从该小节号的第一个物理小节消费（常见于普通
            # |: :| 反复）；带第一/第二房子时各小节内容不同、FIFO 在第一次
            # 展开中已按序消费，走到这里同样从头回放并记 warning。
            idx = 0
            ptr[num] = 0
            warnings_out.append(
                f"小节 {num} 第 {occ + 1} 次演奏：按反复记号重新提取"
                "同一物理小节，请核对房子记号")
        pidx = pool[idx]
        ptr[num] += 1
        if esig and sigs[pidx] and esig != sigs[pidx]:
            warnings_out.append(
                f"展开小节 {num}（第 {occ + 1} 次）内容签名与原谱"
                "不完全一致，已按反复顺序对回，请人工核对")
        units.append({
            "measure": num,
            "occurrence": occ,
            "physical_index": pidx,
            "signature": sigs[pidx],
        })
    return units, physical, ns, warnings_out


def _measure_number(m_el, default: int) -> int:
    raw = m_el.attrib.get("number")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ============================================================ 连音截断

def _voice_of(note_el):
    v = _child(note_el, "voice")
    return v.text.strip() if v is not None and v.text else "1"


def _note_label(note_el):
    p = _child(note_el, "pitch")
    if p is None:
        return "rest"
    step = _child(p, "step")
    octv = _child(p, "octave")
    return ((step.text if step is not None else "?")
            + (octv.text if octv is not None else "?"))


def _remove_tie_pairs(note_el, drop_types):
    """删除 note 下指定类型的 <tie> 与 <notations><tied>。返回被删类型集合。"""
    dropped = set()
    for tie in _children(note_el, "tie"):
        if tie.attrib.get("type") in drop_types:
            dropped.add(tie.attrib.get("type"))
            note_el.remove(tie)
    for notations in _children(note_el, "notations"):
        for tied in _children(notations, "tied"):
            if tied.attrib.get("type") in drop_types:
                notations.remove(tied)
        if not list(notations):
            note_el.remove(notations)
    return dropped


def truncate_ties(unit_els, at_in, at_out):
    """在片段 / 循环轮边界截断延音线（tie）。

    * ``at_in``：首小节--删除无段内起点的悬空 stop/continue（进入端）；
    * ``at_out``：末小节--删除无段内终点的悬空 start/continue（离开端）。
    每个练习轮自成闭合，因此循环轮之间相邻的同小节也按两端截断处理。
    返回截断记录列表。
    """
    markers = []
    if not unit_els:
        return markers
    events = []
    for pos, m_el in enumerate(unit_els):
        for n_el in _children(m_el, "note"):
            for tie in _children(n_el, "tie"):
                events.append((pos, n_el, _voice_of(n_el),
                               tie.attrib.get("type")))

    dangling_in, dangling_out = set(), set()
    by_voice: dict[str, list] = {}
    for evt in events:
        by_voice.setdefault(evt[2], []).append(evt)
    for _voice, evts in by_voice.items():
        open_evt = None
        for evt in evts:
            typ = evt[3]
            if typ == "start":
                open_evt = evt
            elif typ in ("continue", "stop"):
                if open_evt is None and at_in and evt[0] == 0:
                    dangling_in.add(id(evt[1]))
                if typ == "stop":
                    open_evt = None
        if open_evt is not None and at_out \
                and open_evt[0] == len(unit_els) - 1:
            dangling_out.add(id(open_evt[1]))

    id2where = {}
    for pos, m_el in enumerate(unit_els):
        for n_el in _children(m_el, "note"):
            id2where[id(n_el)] = (pos, n_el)
    for nid in dangling_in:
        pos, n_el = id2where[nid]
        _remove_tie_pairs(n_el, {"stop", "continue"})
        markers.append({"kind": "tie", "side": "in", "unit_pos": pos,
                        "note": _note_label(n_el), "voice": _voice_of(n_el)})
    for nid in dangling_out:
        pos, n_el = id2where[nid]
        _remove_tie_pairs(n_el, {"start", "continue"})
        markers.append({"kind": "tie", "side": "out", "unit_pos": pos,
                        "note": _note_label(n_el), "voice": _voice_of(n_el)})
    return markers


def truncate_slurs(unit_els):
    """截断跨片段 / 跨轮连音线（slur），绝不留下悬空连音。

    按 (voice, slur number) 把 start / continue / stop 事件线性配对成链：
    段内闭合的链原样保留；只有进入端（无 start）或只有离开端（无 stop）的
    链，删除该链在段内的全部 <slur> 标记并产生截断记录。
    返回截断记录列表。
    """
    markers = []
    chains: dict[tuple, list] = {}
    for pos, m_el in enumerate(unit_els):
        for n_el in _children(m_el, "note"):
            for notations in _children(n_el, "notations"):
                for slur in _children(notations, "slur"):
                    num = slur.attrib.get("number", "1")
                    chains.setdefault((_voice_of(n_el), num), []).append(
                        (pos, n_el, notations, slur,
                         slur.attrib.get("type")))

    for (_voice, num), evts in sorted(
            chains.items(), key=lambda kv: (kv[0][0], _safe_num(kv[0][1]))):
        # 状态机：open_chain 为当前从 start 起尚未闭合的链；pending_in 为
        # 尚未见到 start 时先出现的 continue/stop（进入片段的悬空端）。
        open_chain, pending_in, seen_start, seen_stop = [], [], False, False
        for evt in evts:
            typ = evt[4]
            if typ == "start":
                if open_chain and not seen_stop:
                    # 上一条 start 从未闭合 -> 悬空离开
                    _drop_chain(open_chain, "out", markers)
                open_chain, pending_in = [evt], []
                seen_start, seen_stop = True, False
            elif typ == "continue":
                if seen_start:
                    open_chain.append(evt)
                else:
                    pending_in.append(evt)
            elif typ == "stop":
                if seen_start:
                    open_chain.append(evt)
                    seen_stop = True
                    open_chain = []              # 段内闭合：保留
                else:
                    pending_in.append(evt)
                    _drop_chain(pending_in, "in", markers)
                    pending_in, seen_stop = [], True
        if open_chain and not seen_stop:
            # start（可能含 continue）后没有 stop -> 悬空离开
            _drop_chain(open_chain, "out", markers)
    return markers


def _safe_num(v):
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 1


def _drop_chain(chain, side, markers):
    """删除一条不完整 slur 链在段内的全部 <slur> 标记，并登记截断。"""
    for _pos, n_el, notations, slur, _typ in chain:
        if slur in list(notations):
            notations.remove(slur)
        if not list(notations) and notations in list(n_el):
            n_el.remove(notations)
    ref = chain[0] if side == "in" else chain[-1]
    markers.append({
        "kind": "slur", "side": side, "unit_pos": ref[0],
        "note": _note_label(ref[1]), "voice": _voice_of(ref[1]),
        "number": ref[3].attrib.get("number", "1"),
    })


# ============================================================ 小节克隆 / 标注

def _Q(ns, tag):
    return f"{{{ns}}}{tag}" if ns else tag


def _make_el(ns, tag, attrs=None, text=None):
    el = ET.Element(_Q(ns, tag), attrs or {})
    if text is not None:
        el.text = str(text)
    return el


_ATTR_CARRIED = ("divisions", "key", "time", "clef", "transpose")


def _carried_attributes(physical, ns, upto_index):
    """扫描原谱物理小节 [0..upto]，取最新生效的谱面属性。"""
    state = {}
    for m in physical[:upto_index + 1]:
        attrs = _child(m, "attributes")
        if attrs is None:
            continue
        for name in _ATTR_CARRIED:
            el = _child(attrs, name)
            if el is not None:
                state[name] = copy.deepcopy(el)
    return state


def clone_measure(m_el, ns):
    """深拷贝物理小节并剥离不适合练习谱的元素：

    * 原有 <barline>/<print>/<sound>（终止线与逐轮速度统一重建，避免残留
      反复记号/房子/原速度）；
    * 原有 <direction> 音乐方向（含原速度文字、segno/coda 导航、排练记号）
      ——练习谱每轮开头统一插入自己的坐标/速度标记。

    **保留 <attributes>**：段内拍号 / 调号 / 谱号变化是谱面内容，必须原样保留；
    每轮首小节的生效属性在构建时统一替换为“片段起点携带状态”。
    """
    m = copy.deepcopy(m_el)
    for name in ("barline", "print", "sound", "direction"):
        for el in _children(m, name):
            m.remove(el)
    return m


def insert_attributes(m_el, ns, carried):
    """把携带的拍号/调号/谱号等插到小节最前（练习谱每轮首小节）。"""
    if not carried:
        return
    attrs = _make_el(ns, "attributes")
    for name in _ATTR_CARRIED:
        if name in carried:
            attrs.append(copy.deepcopy(carried[name]))
    m_el.insert(0, attrs)


def replace_first_attributes(m_el, ns, carried):
    """每轮首小节：用片段起点的生效属性统一替换其原 <attributes>，
    保证循环轮开头谱号/拍号/调号完整（段内后续小节的属性变化仍原样保留）。"""
    for el in _children(m_el, "attributes"):
        m_el.remove(el)
    insert_attributes(m_el, ns, carried)


def round_direction(ns, seg_index, loop, loops, tempo_bpm, coord_text,
                    increment_text):
    """每轮开头的方向标记：原谱坐标 + 速度（含可回放的 metronome/sound）。"""
    direction = _make_el(ns, "direction", {"placement": "above"})
    dt1 = _make_el(ns, "direction-type")
    dt1.append(_make_el(ns, "rehearsal",
                        text=f"片段{seg_index} · 第{loop}/{loops}轮"))
    dt2 = _make_el(ns, "direction-type")
    dt2.append(_make_el(ns, "words", text=coord_text))
    dt3 = _make_el(ns, "direction-type")
    dt3.append(_make_el(ns, "words",
                        text=f"速度 {tempo_bpm:g} BPM（{increment_text}）"))
    metro = _make_el(ns, "metronome")
    metro.append(_make_el(ns, "beat-unit", text="quarter"))
    metro.append(_make_el(ns, "per-minute",
                          text=f"{tempo_bpm:g}"))
    dt4 = _make_el(ns, "direction-type")
    dt4.append(metro)
    direction.extend([dt1, dt2, dt3, dt4])
    direction.append(_make_el(ns, "sound", {"tempo": f"{tempo_bpm:g}"}))
    return direction


def truncation_direction(ns, text):
    direction = _make_el(ns, "direction", {"placement": "above"})
    dt = _make_el(ns, "direction-type")
    dt.append(_make_el(ns, "words",
                       {"font-style": "italic", "font-size": "9"}, text))
    direction.append(dt)
    return direction


def final_barline(ns):
    barline = _make_el(ns, "barline", {"location": "right"})
    barline.append(_make_el(ns, "bar-style", text="light-heavy"))
    return barline


# ============================================================ 生成

def _public_params():
    return {
        "practice_algorithm_version": C.PRACTICE_ALGORITHM_VERSION,
        "min_confidence": C.PRACTICE_MIN_CONFIDENCE,
        "tempo_unit": "BPM (quarter note)",
        "tempo_bpm_min": C.PRACTICE_MIN_TEMPO_BPM,
        "tempo_bpm_max": C.PRACTICE_MAX_TEMPO_BPM,
        "max_segments": C.PRACTICE_MAX_SEGMENTS,
        "max_loops": C.PRACTICE_MAX_LOOPS,
    }


def coord_label(unit):
    m, occ = unit["measure"], unit["occurrence"]
    return f"小节 {m}" + (f"（第 {occ + 1} 次演奏）" if occ else "")


def build_practice_musicxml(piece_id: str, segments: list[dict]):
    """提取整小节、按选定顺序逐轮展开，返回 (musicxml_bytes, plan_dict)。"""
    piece = db.get_piece(piece_id)
    if not piece:
        raise ValueError("乐曲不存在")
    xml_path = _source_xml_path(piece["musicxml_path"])
    units, physical, ns, expand_warnings = build_expansion_map(xml_path)
    key2unit = {(u["measure"], u["occurrence"]): u for u in units}

    tree = ET.parse(xml_path)
    src_root = tree.getroot()
    src_ns = _ns_of(src_root.tag)
    if ns != src_ns:
        ns = src_ns

    # 新谱骨架
    new_root = _make_el(ns, "score-partwise", {"version": "3.1"})
    work = _make_el(ns, "work")
    work.append(_make_el(ns, "work-title",
                         text=f"{piece['title']} · 偏差片段练习谱"))
    new_root.append(work)

    part_list = _make_el(ns, "part-list")
    first_score_part = None
    for p in list(src_root):
        if _ln(p.tag) == "part-list":
            first_score_part = next(
                (sp for sp in _children(p, "score-part")), None)
            break
    if first_score_part is not None:
        part_el = copy.deepcopy(first_score_part)
    else:
        part_el = _make_el(ns, "score-part", {"id": "P1"})
        if _child(part_el, "part-name") is None:
            part_el.append(_make_el(ns, "part-name", text="Voice"))
    part_list.append(part_el)
    new_root.append(part_list)
    part_id = part_el.attrib.get("id", "P1")
    new_part = _make_el(ns, "part", {"id": part_id})
    new_root.append(new_part)

    practice_order, all_truncations = [], []
    out_number = 1
    total_rounds = 0

    for si, seg in enumerate(segments):
        a_unit = key2unit[(seg["measure_start"], seg["occurrence_start"])]
        b_unit = key2unit[(seg["measure_end"], seg["occurrence_end"])]
        # 在展开序列中的位置
        a_pos = next(i for i, u in enumerate(units) if u is a_unit)
        b_pos = next(i for i, u in enumerate(units) if u is b_unit)
        seg_units = units[a_pos:b_pos + 1]
        carried = _carried_attributes(physical, ns,
                                      seg_units[0]["physical_index"])
        inc_text = _increment_text(seg)

        for loop in range(1, seg["loops"] + 1):
            total_rounds += 1
            tempo = round_tempo(seg, loop)
            unit_els = [clone_measure(physical[u["physical_index"]], ns)
                        for u in seg_units]

            # 连音截断（每轮自成闭合，跨片段/跨轮连音在进入与离开两端
            # 一律截断并逐处标记，绝不留下悬空连音）
            tie_marks = truncate_ties(unit_els, at_in=True, at_out=True)
            slur_marks = truncate_slurs(unit_els)

            out_start = out_number
            truncations = []
            for pos, (m_clone, u) in enumerate(zip(unit_els, seg_units)):
                if pos == 0:
                    replace_first_attributes(m_clone, ns, carried)
                    coord = coord_label(seg_units[0])
                    if len(seg_units) > 1:
                        coord += f" – {coord_label(seg_units[-1])}"
                    m_clone.insert(
                        len(_children(m_clone, "attributes")),
                        round_direction(ns, si + 1, loop, seg["loops"],
                                        tempo, f"原谱坐标：{coord}",
                                        inc_text))
                # 截断标记方向（放在对应小节）
                for mk in tie_marks + slur_marks:
                    if mk["unit_pos"] == pos:
                        side = "进入处" if mk["side"] == "in" else "离开处"
                        kind = "延音线" if mk["kind"] == "tie" else "连音线"
                        m_clone.append(truncation_direction(
                            ns, f"〔截断标记〕跨片段{kind}于{side}截断"
                                f"（{mk['note']}），不留悬空连音"))
                        truncations.append({
                            "segment": si + 1, "loop": loop,
                            "kind": mk["kind"], "side": mk["side"],
                            "measure": u["measure"],
                            "occurrence": u["occurrence"],
                            "note": mk["note"], "voice": mk["voice"],
                        })
                m_clone.set("number", str(out_number))
                new_part.append(m_clone)
                out_number += 1

            all_truncations.extend(truncations)
            practice_order.append({
                "order": total_rounds,
                "segment": si + 1,
                "loop": loop,
                "loops": seg["loops"],
                "tempo_bpm": tempo,
                "coordinate": coord,
                "source_units": [{"measure": u["measure"],
                                  "occurrence": u["occurrence"]}
                                 for u in seg_units],
                "output_measures": [out_start, out_number - 1],
                "truncations": truncations,
            })

    # 全谱末尾常规终止线
    last_m = _children(new_part, "measure")[-1]
    last_m.append(final_barline(ns))

    plan = {
        "format": "deviation-practice-score-manifest",
        "algorithm_version": C.PRACTICE_ALGORITHM_VERSION,
        "params": _public_params(),
        "expansion_warnings": expand_warnings,
        "segments": [{k: v for k, v in s.items()} for s in segments],
        "practice_order": practice_order,
        "truncated_spanners": all_truncations,
        "expansion": [{"measure": u["measure"], "occurrence": u["occurrence"],
                       "physical_measure_number":
                       physical[u["physical_index"]].attrib.get("number")}
                      for u in units],
    }
    body = _serialize(new_root, ns)
    return body, plan


def _increment_text(seg):
    d = seg["tempo_increment_bpm"]
    if seg["tempo_direction"] == "same" or not d:
        return "恒速"
    arrow = "逐轮增速" if seg["tempo_direction"] == "up" else "逐轮减速"
    return f"{arrow} {d:g} BPM"


def _serialize(root, ns):
    if ns:
        try:
            ET.register_namespace("", ns)
        except ValueError:
            pass
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def freeze_metadata_xml(manifest: dict) -> bytes:
    """把来源谱哈希 / 对齐版本 / 生成参数固化进下载 MusicXML 的
    <identification><miscellany>（与清单内容一致）。"""
    root = ET.fromstring(manifest["_musicxml_bytes"])
    rns = _ns_of(root.tag)
    ident = _child(root, "identification")
    if ident is None:
        ident = _make_el(rns, "identification")
        root.append(ident)
    misc = _child(ident, "miscellany")
    if misc is None:
        misc = _make_el(rns, "miscellany")
        ident.append(misc)
    for key, value in manifest["_freeze_fields"]:
        m = _make_el(rns, "miscellaneous-field", {"name": key}, text=value)
        misc.append(m)
    return _serialize(root, rns)


# ============================================================ 任务编排

def _files_dir():
    return os.path.join(os.environ.get("DATA_DIR", "/data"), "files")


def _file_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prepare(piece_id, raw_segments):
    """规范化 + 逐位校验，返回 (segments, reports)。"""
    bundle = review.get_bundle(piece_id)
    if not bundle:
        raise ValueError("乐曲不存在")
    units = _expanded_units(bundle["score_notes"])
    segments = normalize_segments(raw_segments, units)
    reports = validate_segments(piece_id, segments)
    return segments, reports


def create_practice_score(piece_id: str, title: str | None,
                          raw_segments: list[dict]) -> str:
    """创建练习谱：校验通过即提取生成，成功后固化为 version 1。"""
    piece = db.get_piece(piece_id)
    if not piece:
        raise ValueError("乐曲不存在")
    if piece["status"] != "analyzed":
        raise ValueError("该录音尚未完成分析，不能生成练习谱")
    segments, _reports = _prepare(piece_id, raw_segments)

    task_id = uuid.uuid4().hex[:12]
    default_title = f"《{piece['title']}》· 偏差片段练习谱（{len(segments)} 段）"
    params = _public_params()
    db.create_practice_score(
        task_id, piece_id, title or default_title,
        json.dumps(segments, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")),
        json.dumps(params, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")))

    try:
        _generate_and_commit_version(task_id, piece_id, segments, params)
    except Exception:
        # 生成/落盘失败：未产生任何版本，删除瞬时 draft 任务行
        db.delete_practice_score(task_id)
        raise
    return task_id


def rebuild_practice_score(task_id: str) -> dict:
    """显式重建：按当前来源重新校验/提取，成功后**追加新版本**并切换指针。

    来源变化导致的待确认/低置信等阻塞返回 :class:`BlockedError`（422），
    生成异常一律向上抛出——两者都发生在写入版本之前，因此**不新增、不改写
    任何版本**：旧版本（即使已过期）原样保留、始终可下载，当前版本指针不动。
    """
    row = db.get_practice_score(task_id)
    if not row:
        raise ValueError("练习谱不存在")
    segments = json.loads(row["segments_json"])
    params = json.loads(row["params_json"])
    # _prepare 抛 BlockedError（422）：直接向上传播，尚未触碰任何文件/版本
    segments, _reports = _prepare(row["piece_id"], segments)
    # 生成与固化全部成功后才追加版本；中途失败旧版本与指针不变
    _generate_and_commit_version(task_id, row["piece_id"], segments, params)
    return get_practice_score(task_id)


def _generate_and_commit_version(task_id, piece_id, segments, params):
    """渲染 -> 原子落盘为版本独立文件 -> 追加只读版本并切换当前指针。

    数据库版本写入是本流程最后一步且在单个事务内完成；此前的任何异常
    （含阻塞、解析 / 渲染 / IO 错误）都不会产生新版本或改写当前指针。
    """
    body, plan = build_practice_musicxml(piece_id, segments)

    piece = db.get_piece(piece_id)
    align = db.get_latest_alignment(piece_id)
    source_hash = review._file_hash(piece["musicxml_path"])  # noqa: SLF001
    alignment_version = align["version"] if align else None

    # 版本号先确定（文件名与版本行共用），写文件 -> 原子追加版本行。
    next_version = db.next_practice_version(task_id)
    manifest_json, out_path, final_sha = _build_and_write_version_file(
        task_id, next_version, piece_id, segments, params, body, plan,
        source_hash, alignment_version)
    try:
        db.add_practice_version(task_id, next_version, source_hash,
                                alignment_version, manifest_json, out_path,
                                final_sha)
    except Exception:
        # 文件已原子落盘但版本写入失败：清理孤立文件，保证无“无版本文件”
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise


def _build_and_write_version_file(task_id, next_version, piece_id, segments,
                                  params, body, plan, source_hash,
                                  alignment_version):
    """把固化字段写入 MusicXML，原子落盘到版本独立路径。

    返回 (manifest_json, 落盘路径, 最终文件 sha256)。
    """

    # 下载 MusicXML 内嵌的固化字段；embedded 的内容哈希指“嵌入前正文”，
    # 保证文件与清单可交叉核对（嵌入该字段本身不改变正文音乐内容）。
    content_sha = _file_sha(body)
    freeze_fields = [
        ("source_musicxml_sha256", source_hash or ""),
        ("alignment_version", str(alignment_version or "")),
        ("generation_params", json.dumps(
            params, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))),
        ("segment_count", str(len(segments))),
        ("algorithm_version", C.PRACTICE_ALGORITHM_VERSION),
        ("practice_content_sha256", content_sha),
        ("practice_score_version", str(next_version)),
    ]
    final_body = freeze_metadata_xml({
        "_musicxml_bytes": body,
        "_freeze_fields": freeze_fields,
    })
    final_sha = _file_sha(final_body)

    manifest = {
        **{k: v for k, v in plan.items()},
        "task_id": task_id,
        "version": next_version,
        "piece_id": piece_id,
        "source_musicxml_sha256": source_hash,
        "alignment_version": alignment_version,
        "generated_musicxml_sha256": final_sha,
        "practice_content_sha256": content_sha,
        "frozen_fields": [{"name": k, "value": v} for k, v in freeze_fields],
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))

    os.makedirs(_files_dir(), exist_ok=True)
    out_path = os.path.join(
        _files_dir(), f"practice_{task_id}_v{next_version}.musicxml")
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=_files_dir(),
        prefix=f".practice_{task_id}_v{next_version}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(final_body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, out_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    return manifest_json, out_path, final_sha


# ============================================================ 读取 / 过期

def _version_stale_reasons(vrow, piece_id: str,
                           current_source_hash=None,
                           current_alignment_version=None) -> list[str]:
    """某版本固化的来源谱哈希 / 对齐版本与当前来源相比是否过期。

    来源变化只产生只读过期标记，不改写、不删除该版本。
    可直接传入已算好的当前来源哈希 / 对齐版本以避免逐版本重复读盘。
    """
    if current_source_hash is None or current_alignment_version is None:
        piece = db.get_piece(piece_id)
        if piece is None:
            return ["piece_removed"]
        if current_source_hash is None:
            current_source_hash = review._file_hash(  # noqa: SLF001
                piece["musicxml_path"])
        if current_alignment_version is None:
            latest = db.get_latest_alignment(piece_id)
            current_alignment_version = (latest["version"] if latest else None)
    reasons = []
    if (current_source_hash is not None
            and current_source_hash != vrow["source_musicxml_sha256"]):
        reasons.append("score_changed")
    if current_alignment_version is None:
        reasons.append("alignment_missing")
    elif current_alignment_version != vrow["alignment_version"]:
        reasons.append("alignment_version_changed")
    return reasons


def _version_to_summary(row, vrow, current_source_hash=None,
                        current_alignment_version=None) -> dict:
    """版本列表条目（含过期标记与该版本下载入口）。"""
    plan = json.loads(vrow["manifest_json"])
    available = (vrow["status"] == "built"
                 and os.path.exists(vrow["musicxml_path"]))
    reasons = _version_stale_reasons(
        vrow, row["piece_id"], current_source_hash,
        current_alignment_version)
    return {
        "version": vrow["version"],
        "is_current": vrow["version"] == row["current_version"],
        "status": vrow["status"],
        "stale": bool(reasons),
        "stale_reasons": reasons,
        "source_musicxml_sha256": vrow["source_musicxml_sha256"],
        "alignment_version": vrow["alignment_version"],
        "generated_musicxml_sha256": vrow["musicxml_sha256"],
        "round_count": len(plan.get("practice_order") or []),
        "truncated_spanner_count": len(plan.get("truncated_spanners") or []),
        "created_at": vrow["created_at"],
        "musicxml_available": available,
        "musicxml_url": (
            f"/api/practice-scores/{row['id']}/versions/"
            f"{vrow['version']}/musicxml") if available else None,
        "manifest_url": (
            f"/api/practice-scores/{row['id']}/versions/"
            f"{vrow['version']}/manifest") if available else None,
    }


def get_practice_score(task_id: str) -> dict:
    row = db.get_practice_score(task_id)
    if not row:
        raise ValueError("练习谱不存在")
    return _row_to_dict(row)


def _row_to_dict(row) -> dict:
    piece = db.get_piece(row["piece_id"])
    cur_hash = (review._file_hash(piece["musicxml_path"])  # noqa: SLF001
                if piece else None)
    latest_align = (db.get_latest_alignment(row["piece_id"])
                    if piece else None)
    cur_align = latest_align["version"] if latest_align else None
    cur_vrow = db.get_current_practice_version(row["id"])
    plan = json.loads(cur_vrow["manifest_json"]) if cur_vrow else None
    if cur_vrow:
        reasons = _version_stale_reasons(
            cur_vrow, row["piece_id"], cur_hash, cur_align)
        available = (cur_vrow["status"] == "built"
                     and os.path.exists(cur_vrow["musicxml_path"]))
    else:
        reasons, available = [], False
    rounds = len(plan["practice_order"]) if plan else 0
    versions = [_version_to_summary(row, v, cur_hash, cur_align)
                for v in db.list_practice_versions(row["id"])]
    return {
        "id": row["id"],
        "piece_id": row["piece_id"],
        "piece_title": piece["title"] if piece else None,
        "title": row["title"],
        "status": row["status"],
        "current_version": row["current_version"],
        "version_count": len(versions),
        "versions": versions,
        "stale": bool(reasons),
        "stale_reasons": reasons,
        "segments": json.loads(row["segments_json"]),
        "params": json.loads(row["params_json"]),
        "frozen": {
            "version": cur_vrow["version"] if cur_vrow else None,
            "source_musicxml_sha256": (
                cur_vrow["source_musicxml_sha256"] if cur_vrow else None),
            "current_musicxml_sha256": cur_hash,
            "alignment_version": (
                cur_vrow["alignment_version"] if cur_vrow else None),
            "current_alignment_version": cur_align,
            "generated_musicxml_sha256": (
                cur_vrow["musicxml_sha256"] if cur_vrow else None),
            "algorithm_version": C.PRACTICE_ALGORITHM_VERSION,
        },
        "round_count": rounds,
        "truncated_spanner_count": (len(plan["truncated_spanners"])
                                    if plan else 0),
        "practice_order": plan["practice_order"] if plan else None,
        "built_at": row["built_at"],
        "created_at": row["created_at"],
        "musicxml_available": available,
        "musicxml_url": (
            f"/api/practice-scores/{row['id']}/versions/"
            f"{cur_vrow['version']}/musicxml") if available else None,
        "manifest_url": (
            f"/api/practice-scores/{row['id']}/versions/"
            f"{cur_vrow['version']}/manifest") if cur_vrow else None,
    }


def list_practice_scores(piece_id: str | None = None) -> list[dict]:
    rows = db.list_practice_scores()
    if piece_id:
        rows = [r for r in rows if r["piece_id"] == piece_id]
    return [_row_to_dict(r) for r in rows]


# ------------------------------------------------------------ 版本历史

def list_versions(task_id: str) -> dict:
    """按练习谱查看版本历史（version 降序，当前版本带 is_current）。"""
    row = db.get_practice_score(task_id)
    if not row:
        raise ValueError("练习谱不存在")
    piece = db.get_piece(row["piece_id"])
    cur_hash = (review._file_hash(piece["musicxml_path"])  # noqa: SLF001
                if piece else None)
    latest = db.get_latest_alignment(row["piece_id"]) if piece else None
    cur_align = latest["version"] if latest else None
    items = [_version_to_summary(row, v, cur_hash, cur_align)
             for v in db.list_practice_versions(task_id)]
    return {
        "id": task_id,
        "title": row["title"],
        "current_version": row["current_version"],
        "versions": items,
    }


def _require_version(task_id: str, version: int):
    """取 (任务行, 版本行)；任务不存在 / 版本不存在分别抛 ValueError。"""
    row = db.get_practice_score(task_id)
    if not row:
        raise ValueError("练习谱不存在")
    vrow = db.get_practice_version(task_id, version)
    if not vrow:
        raise LookupError(f"练习谱不存在版本 v{version}")
    return row, vrow


def get_version_detail(task_id: str, version: int) -> dict:
    """单个版本的完整只读详情（固化清单 + 逐轮练习顺序 + 下载入口）。"""
    try:
        row, vrow = _require_version(task_id, version)
    except LookupError as exc:
        raise ValueError(str(exc)) from exc
    piece = db.get_piece(row["piece_id"])
    plan = json.loads(vrow["manifest_json"])
    summary = _version_to_summary(row, vrow)
    summary["stale"] = bool(summary["stale_reasons"])
    return {
        **summary,
        "piece_id": row["piece_id"],
        "piece_title": piece["title"] if piece else None,
        "title": row["title"],
        "segments": plan.get("segments"),
        "params": plan.get("params"),
        "practice_order": plan.get("practice_order"),
        "expansion_warnings": plan.get("expansion_warnings"),
        "truncated_spanners": plan.get("truncated_spanners"),
        "expansion": plan.get("expansion"),
        "manifest": plan,
    }


def get_version_manifest(task_id: str, version: int) -> dict:
    try:
        _row, vrow = _require_version(task_id, version)
    except LookupError as exc:
        raise ValueError(str(exc)) from exc
    return json.loads(vrow["manifest_json"])


def read_version_musicxml(task_id: str, version: int) -> bytes:
    """读取某版本 MusicXML 字节；文件缺失抛 ValueError（HTTP 409）。"""
    try:
        _row, vrow = _require_version(task_id, version)
    except LookupError as exc:
        raise ValueError(str(exc)) from exc
    if not os.path.exists(vrow["musicxml_path"]):
        raise ValueError("该版本练习谱文件缺失")
    with open(vrow["musicxml_path"], "rb") as f:
        return f.read()


def get_manifest(task_id: str) -> dict:
    """当前版本的确定性清单（向后兼容入口）。"""
    row = db.get_practice_score(task_id)
    if not row:
        raise ValueError("练习谱不存在")
    vrow = db.get_current_practice_version(task_id)
    if not vrow:
        raise ValueError("练习谱尚未生成")
    return json.loads(vrow["manifest_json"])


def delete_practice_score(task_id: str) -> bool:
    row = db.get_practice_score(task_id)
    if not row:
        return False
    # 先清理全部版本文件，再由外键级联删除全部版本行
    for v in db.list_practice_versions(task_id):
        path = v["musicxml_path"]
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    db.delete_practice_score(task_id)
    return True

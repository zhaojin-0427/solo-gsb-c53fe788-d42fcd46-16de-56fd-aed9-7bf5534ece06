"""谱面修订迁移：展开反复后的确定性谱位匹配 + 历史产物继承判定。

模型
====
用户选择一条**已分析录音**（其当前 MusicXML）并上传一份**修订版单声部**
MusicXML。系统把两侧谱面都展开反复，按
（原谱小节 measure、反复轮次 occurrence、拍位 beat_in_measure、音高 midi、
时值 dur_q）做**确定性**匹配，逐谱位归入六类：

* ``kept``     保留：谱位（小节/轮次/拍位）一致，音高与时值也一致；
* ``modified`` 修改：谱位一致，但音高或时值发生变化；
* ``moved``    移动：音高与时值完全一致，但出现在另一个谱位
                （在剩余谱音的保序序列对齐中唯一确定）；
* ``deleted``  删除：旧谱有、修订谱没有（且找不到同内容唯一对应）；
* ``added``    新增：修订谱有、旧谱没有；
* ``unmatched``无法匹配：修订谱存在无法安全对回的谱位/单位，不静默强配。

**匹配冲突与多声部歧义必须阻止提交**（预览照做、逐项列出，提交 422）：

* ``polyphony_ambiguous``：修订谱检测到多 part / 和弦（承诺单声部输入）；
* ``duplicate_position``：同一（小节/轮次/拍位）出现多个谱音，匹配键不唯一；
* ``missing_coordinate``：谱音缺原谱小节/拍位坐标，无法定位迁移；
* ``unalignable_measure``：修订谱的展开小节单位无法对回旧谱（改小节号/
  结构重组），该单位内谱音一律不静默迁移；
* ``ambiguous_move``：剩余谱音的保序对齐中存在等代价移动配对，移动对象
  非唯一（DP 次优间隔为 0）。

基于匹配结果生成**迁移预览**，把锚点、对齐快照、未过期对比、处方与
练习谱片段逐项标为 ``inheritable`` 可继承 / ``review`` 需复核 /
``invalid`` 失效并说明原因。**禁止静默改写历史**：旧录音、旧对齐版本与
全部历史产物只读；用户显式确认后才创建新的谱面修订记录与供后续录音
使用的复盘入口（新 piece，预置可继承的 exclude_score 锚点与待接续模板）。
报告（mapping）只含确定性内容（无时间戳/随机项），可导出字节级一致 JSON。
"""

from __future__ import annotations

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
from . import practice_score as ps_service
from . import prescription as prx_service
from . import review
from . import score_parser

# ------------------------------------------------------------ 匹配分类
MATCH_KEPT = "kept"
MATCH_MODIFIED = "modified"
MATCH_MOVED = "moved"
MATCH_DELETED = "deleted"
MATCH_ADDED = "added"
MATCH_UNMATCHED = "unmatched"

# 继承判定
INHERIT = "inheritable"
REVIEW = "review"
INVALID = "invalid"

# 阻止提交的冲突原因
BLK_POLYPHONY = "polyphony_ambiguous"
BLK_DUP_POS = "duplicate_position"
BLK_MISSING_COORD = "missing_coordinate"
BLK_UNALIGNABLE = "unalignable_measure"
BLK_AMBIG_MOVE = "ambiguous_move"

MATCH_LABELS_ZH = {
    MATCH_KEPT: "保留", MATCH_MODIFIED: "修改", MATCH_MOVED: "移动",
    MATCH_DELETED: "删除", MATCH_ADDED: "新增", MATCH_UNMATCHED: "无法匹配",
}
INHERIT_LABELS_ZH = {
    INHERIT: "可继承", REVIEW: "需复核", INVALID: "失效",
}


class PolyphonyError(Exception):
    """修订谱含多声部（多 part / 和弦）歧义：阻止预览与提交。"""

    def __init__(self, message: str, details: list[dict]):
        super().__init__(message)
        self.details = details


class CommitBlockedError(Exception):
    """存在匹配冲突 / 多声部歧义，阻止提交（HTTP 422）。"""

    def __init__(self, message: str, blockers: list[dict]):
        super().__init__(message)
        self.blockers = blockers


# ============================================================ 文件 / 哈希

def _files_dir():
    return os.path.join(os.environ.get("DATA_DIR", "/data"), "files")


def file_sha256(path: str) -> str | None:
    return review._file_hash(path)  # noqa: SLF001


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_zip_mxl(data: bytes) -> bool:
    return data[:2] == b"PK"


def _extract_mxl_xml(data: bytes) -> bytes:
    """从压缩 .mxl 中取出 container.xml 指向的首个 rootfile。"""
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = next(
            (e for e in container.iter()
             if _ln(e.tag) == "rootfile"), None)
        if rootfile is None:
            raise ValueError("压缩谱 (.mxl) 中找不到 rootfile")
        return z.read(rootfile.attrib["full-path"])


def _ln(tag: str) -> str:
    return tag.split("}", 1)[-1]


# ============================================================ 修订谱解析

def _polyphony_scan(data: bytes) -> list[dict]:
    """纯 XML 静态扫描修订谱的多声部歧义：多 part / 和弦。

    返回歧义明细（measure/声部数/和弦音数）。只检查实际发声内容：
    和弦的首个 <note> 带 <chord/> 的后续成员不算新声部，但出现 <chord/>
    本身即“同一拍位多个音”，属多声部歧义（系统承诺单声部）。
    """
    root = ET.fromstring(data)
    parts = [c for c in list(root) if _ln(c.tag) == "part"]
    details: list[dict] = []
    if len(parts) > 1:
        details.append({"kind": "multiple_parts", "part_count": len(parts)})
    for part in parts:
        for m in [c for c in list(part) if _ln(c.tag) == "measure"]:
            mnum = m.attrib.get("number")
            notes = [c for c in list(m) if _ln(c.tag) == "note"]
            voices = set()
            for n in notes:
                voice_el = next(
                    (c for c in list(n) if _ln(c.tag) == "voice"), None)
                voices.add(voice_el.text.strip()
                           if voice_el is not None and voice_el.text else "1")
                if next((c for c in list(n) if _ln(c.tag) == "chord"), None) \
                        is not None:
                    details.append({"kind": "chord",
                                    "measure": mnum})
            if len(voices) > 1:
                details.append({"kind": "multiple_voices",
                                "measure": mnum,
                                "voices": sorted(voices)})
    return details


def parse_revision(data: bytes):
    """解析修订版单声部 MusicXML。

    返回 (score_notes, warnings, revised_hash)。
    多声部歧义（多 part / 和弦 / 小节内多 voice）抛 :class:`PolyphonyError`。
    """
    revised_hash = bytes_sha256(data)
    xml_bytes = _extract_mxl_xml(data) if _is_zip_mxl(data) else data
    poly = _polyphony_scan(xml_bytes)
    if poly:
        raise PolyphonyError(
            "修订版谱面检测到多声部（多声部 / 和弦）歧义：系统仅支持"
            "单声部修订，请拆分为单声部后再上传",
            poly)

    # 落临时文件给 music21（与 score_parser 一致地展开反复）。
    # 压缩 .mxl 先解压为内部 MusicXML 再落盘。
    fd, tmp = tempfile.mkstemp(suffix=".musicxml", dir=_files_dir())
    path = tmp
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(xml_bytes)
        notes, parse_warnings = score_parser.parse_musicxml(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return notes, parse_warnings, revised_hash


# ============================================================ 谱位键 / 单位

def _unit_key(note):
    return (note["measure"], note.get("occurrence", 0) or 0)


def _units_of(notes):
    """展开谱音 -> 按 beat 排序的 (measure, occurrence) 单位序列。"""
    groups: dict[tuple, list[int]] = {}
    for n in notes:
        groups.setdefault(_unit_key(n), []).append(n["index"])
    beat_of = {n["index"]: float(n["beat"]) for n in notes}
    units = []
    for key, idxs in groups.items():
        idxs.sort()
        units.append({"key": key, "indexes": idxs,
                      "beat_start": beat_of[idxs[0]]})
    units.sort(key=lambda u: (u["beat_start"], u["key"][0], u["key"][1]))
    return units


def _lcs_pairs(old_keys, new_keys):
    """两列单位键的最长公共子序列配对（保序、确定性）。

    返回 matched: [(old_pos, new_pos), ...]（按旧序列升序）。
    """
    n, m = len(old_keys), len(new_keys)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if old_keys[i] == new_keys[j]:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    pairs = []
    i = j = 0
    while i < n and j < m:
        if old_keys[i] == new_keys[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _note_coord(note):
    return {
        "measure": note["measure"],
        "beat_in_measure": note["beat_in_measure"],
        "occurrence": note.get("occurrence", 0) or 0,
        "beat_expanded": _r(note["beat"]),
    }


def _r(v, nd=C.ROUND_NDIGITS):
    return None if v is None else round(float(v), nd)


# ============================================================ 确定性匹配

def match_scores(old_notes, new_notes):
    """展开反复后的两侧谱音 -> (matches, blockers)。

    matches: 每条 {"kind", "old": {...}|None, "new": {...}|None,
                   "reasons": [...], "move_cost": float|None}
    blockers: 阻止提交的冲突（多声部歧义由解析阶段先拦截，这里覆盖
              谱位键重复 / 缺坐标 / 单位无法对回 / 移动配对非唯一）。
    """
    old_units = _units_of(old_notes)
    new_units = _units_of(new_notes)
    old_by = {n["index"]: n for n in old_notes}
    new_by = {n["index"]: n for n in new_notes}

    blockers: list[dict] = []

    # ---- 结构性冲突：缺坐标 / 谱位键重复（两侧都查；旧谱来自既有 DB，
    #      正常不会有，但迁移必须显式核实而不是假设）----
    bad_indexes = {"old": set(), "new": set()}
    for side, notes in (("old", old_notes), ("new", new_notes)):
        seen_pos: dict[tuple, int] = {}
        for n in notes:
            if n.get("measure") is None or n.get("beat_in_measure") is None:
                blockers.append({
                    "reason": BLK_MISSING_COORD, "side": side,
                    "score_index": n["index"],
                    "measure": n.get("measure"),
                    "occurrence": n.get("occurrence", 0),
                    "detail": "谱音缺少原谱小节或拍位坐标，无法确定性迁移",
                })
                bad_indexes[side].add(n["index"])
                continue
            pos = (n["measure"], n.get("occurrence", 0) or 0,
                   round(float(n["beat_in_measure"]), 5))
            if pos in seen_pos:
                blockers.append({
                    "reason": BLK_DUP_POS, "side": side,
                    "score_index": n["index"],
                    "measure": pos[0], "occurrence": pos[1],
                    "beat_in_measure": pos[2],
                    "conflict_indexes": [seen_pos[pos], n["index"]],
                    "detail": "同一（小节/反复轮次/拍位）存在多个谱音，"
                              "匹配键不唯一",
                })
                bad_indexes[side].update([seen_pos[pos], n["index"]])
            seen_pos[pos] = n["index"]

    # ---- 全局确定性序列对齐（按展开顺序单调配对）----
    # 合法配对只有三类：
    #   kept     谱位（小节/轮次/拍位）一致且音高时值一致，代价 0；
    #   modified 同谱位但音高或时值变化，代价 REVISION_COST_MODIFIED；
    #   moved    音高时值完全一致但谱位不同，代价 = 单位跨越 + 拍位距离。
    # 未配对旧谱音 = 删除（REVISION_COST_DELETE），未配对新谱音 = 新增
    # （REVISION_COST_INSERT）。代价公开（见 public_params），回溯等代价时
    # 按固定优先级（配对→删→增）确定唯一路径；某配对与“删/增”路径等代价
    # （次优间隔为 0）即该配对非唯一，登记 ambiguous_move 阻止提交。
    clean_old = sorted((n["index"] for n in old_notes
                        if n["index"] not in bad_indexes["old"]),
                       key=lambda i: (float(old_by[i]["beat"]), i))
    clean_new = sorted((n["index"] for n in new_notes
                        if n["index"] not in bad_indexes["new"]),
                       key=lambda i: (float(new_by[i]["beat"]), i))
    pairs, ambig = _global_align(
        clean_old, clean_new, old_by, new_by, old_units, new_units)
    blockers.extend(ambig)

    matches: dict[tuple, dict] = {}

    def add(m):
        if m.get("old") is not None:
            matches[("o", m["old"]["score_index"])] = m
        if m.get("new") is not None:
            matches[("n", m["new"]["score_index"])] = m

    # 结构性冲突谱位直接无法匹配，不参与任何自动对齐
    for idx in sorted(bad_indexes["old"]):
        add(_match(MATCH_UNMATCHED, old_by[idx], None,
                   reasons=["谱位键冲突 / 缺原谱坐标，旧谱音不参与迁移"]))
    for idx in sorted(bad_indexes["new"]):
        add(_match(MATCH_UNMATCHED, None, new_by[idx],
                   reasons=["谱位键冲突 / 缺原谱坐标，修订谱音不参与迁移"]))

    old_consumed, new_consumed = set(bad_indexes["old"]), \
        set(bad_indexes["new"])
    for kind, o_idx, n_idx, cost in pairs:
        add(_pair_match(kind, old_by[o_idx], new_by[n_idx], cost))
        old_consumed.add(o_idx)
        new_consumed.add(n_idx)

    # ---- 未配对：删除 / 新增 ----
    for idx in [n["index"] for n in old_notes]:
        if idx in old_consumed:
            continue
        add(_match(MATCH_DELETED, old_by[idx], None,
                   reasons=["全局对齐中修订谱无对应谱音"]))
        old_consumed.add(idx)
    for idx in [n["index"] for n in new_notes]:
        if idx in new_consumed:
            continue
        add(_match(MATCH_ADDED, None, new_by[idx],
                   reasons=["全局对齐中旧谱无对应谱音"]))
        new_consumed.add(idx)

    # 成对匹配在 matches 中占两个键；按对象去重后确定性排序输出
    unique = {id(m): m for m in matches.values()}
    out = list(unique.values())
    out.sort(key=lambda m: (
        m["old"]["score_index"] if m.get("old") else 10 ** 12,
        m["new"]["score_index"] if m.get("new") else 10 ** 12))
    return out, blockers


def _same_position(on, nn):
    return (on["measure"] == nn["measure"]
            and (on.get("occurrence", 0) or 0)
            == (nn.get("occurrence", 0) or 0)
            and abs(float(on["beat_in_measure"])
                    - float(nn["beat_in_measure"])) < 1e-4)


def _pair_match(kind, on, nn, cost):
    if kind == MATCH_KEPT:
        return _match(MATCH_KEPT, on, nn)
    if kind == MATCH_MODIFIED:
        changes = []
        if abs(float(on["midi"]) - float(nn["midi"])) >= 0.5:
            changes.append(
                f"音高 {_midi_name(on['midi'])}→{_midi_name(nn['midi'])}")
        if abs(float(on["dur_q"]) - float(nn["dur_q"])) \
                >= C.REVISION_DUR_EPS:
            changes.append(
                f"时值 {_q(on['dur_q'])}→{_q(nn['dur_q'])} 四分音符")
        return _match(MATCH_MODIFIED, on, nn,
                      reasons=["同谱位：" + "，".join(changes)])
    return _match(MATCH_MOVED, on, nn,
                  reasons=[f"同内容谱音移动：小节 {on['measure']}"
                           f"（第 {(on.get('occurrence', 0) or 0) + 1} 次）"
                           f"拍 {on['beat_in_measure']} → "
                           f"小节 {nn['measure']}"
                           f"（第 {(nn.get('occurrence', 0) or 0) + 1} 次）"
                           f"拍 {nn['beat_in_measure']}"],
                  move_cost=_r(cost, 4))


def _match(kind, old_note, new_note, reasons=None, move_cost=None):
    return {
        "kind": kind,
        "old": None if old_note is None else _note_view(old_note, "old"),
        "new": None if new_note is None else _note_view(new_note, "new"),
        "reasons": reasons or [],
        "move_cost": move_cost,
    }


def _note_view(note, side):
    return {
        "side": side,
        "score_index": note["index"],
        "midi": _r(note["midi"], 4),
        "midi_name": _midi_name(note["midi"]),
        "beat_expanded": _r(note["beat"]),
        "duration_quarters": _r(note["dur_q"]),
        **_note_coord(note),
    }


def _global_align(old_seq, new_seq, old_by, new_by, old_units, new_units):
    """全局单调序列对齐 DP。

    转移代价：
      * 删除旧音 / 新增新音：REVISION_COST_DELETE / INSERT；
      * 配对：同谱位且同内容 = kept(0)；同谱位音高或时值变 = modified；
        不同谱位但同内容（音高+时值完全一致）= moved（按单位跨越与拍位
        距离计代价）；不同谱位且内容不同 = 禁止配对（INF）。
    回溯等代价时固定优先级 配对→删→增；每个命中配对用“删/增替代”的
    次优间隔 margin 校验唯一性，margin=0 记 ambiguous_move（配对仍输出
    以供预览展示，但随 blockers 阻止提交）。

    返回 (pairs, blockers)，pairs: [(kind, old_idx, new_idx, cost), ...]。
    """
    old_idx_pos = _unit_index_map(old_units)
    new_idx_pos = _unit_index_map(new_units)
    n, m = len(old_seq), len(new_seq)
    INF = float("inf")

    def pair_cost(i, j):
        """合法配对 -> (kind, cost)；不可配对 -> None。"""
        on, nn = old_by[old_seq[i]], new_by[new_seq[j]]
        same_pitch = abs(float(on["midi"]) - float(nn["midi"])) < 0.5
        same_dur = abs(float(on["dur_q"]) - float(nn["dur_q"])) \
            < C.REVISION_DUR_EPS
        if _same_position(on, nn):
            if same_pitch and same_dur:
                return MATCH_KEPT, 0.0
            return MATCH_MODIFIED, C.REVISION_COST_MODIFIED
        if same_pitch and same_dur:
            return MATCH_MOVED, _move_cost(on, nn, old_idx_pos, new_idx_pos)
        return None

    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        dp[i][m] = dp[i + 1][m] + C.REVISION_COST_DELETE
    for j in range(m - 1, -1, -1):
        dp[n][j] = dp[n][j + 1] + C.REVISION_COST_INSERT
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best = dp[i + 1][j] + C.REVISION_COST_DELETE
            best = min(best, dp[i][j + 1] + C.REVISION_COST_INSERT)
            pc = pair_cost(i, j)
            if pc is not None:
                best = min(best, dp[i + 1][j + 1] + pc[1])
            dp[i][j] = best

    pairs, blockers = [], []
    i = j = 0
    while i < n and j < m:
        c_del = dp[i + 1][j] + C.REVISION_COST_DELETE
        c_ins = dp[i][j + 1] + C.REVISION_COST_INSERT
        pc = pair_cost(i, j)
        c_pair = dp[i + 1][j + 1] + pc[1] if pc is not None else INF
        if pc is not None and c_pair <= c_del and c_pair <= c_ins:
            # 局部次优间隔：min(删旧音, 增新音) 与配对路径之差。删旧音后
            # 新音可与后续旧音配对、增新音后旧音可与后续新音配对，这两条
            # 正是“该配对不成立”时的最优替代；间隔 0 即配对非唯一。
            margin = min(c_del, c_ins) - c_pair
            o_idx, n_idx = old_seq[i], new_seq[j]
            if margin <= 1e-9:
                on = old_by[o_idx]
                blockers.append({
                    "reason": BLK_AMBIG_MOVE,
                    "old_score_index": o_idx, "new_score_index": n_idx,
                    "measure": on["measure"],
                    "occurrence": on.get("occurrence", 0),
                    "midi": _r(on["midi"], 4),
                    "midi_name": _midi_name(on["midi"]),
                    "detail": "该配对与删除/新增替代等代价（间隔为 0），"
                              "迁移目标非唯一，必须人工消解",
                })
            pairs.append((pc[0], o_idx, n_idx, pc[1]))
            i += 1
            j += 1
        elif c_del <= c_ins:
            i += 1
        else:
            j += 1
    return pairs, blockers


def _unit_index_map(units):
    return {idx: pos for pos, u in enumerate(units) for idx in u["indexes"]}


def _move_cost(on, nn, old_idx_pos, new_idx_pos):
    """移动代价 = 展开单位跨越 × 权重 + 展开拍位距离 × 权重。"""
    op = old_idx_pos.get(on["index"], -1)
    npos = new_idx_pos.get(nn["index"], -1)
    unit_gap = abs(op - npos)
    beat_gap = abs(float(on["beat"]) - float(nn["beat"]))
    return (C.REVISION_COST_UNIT_PER_GAP * unit_gap
            + C.REVISION_COST_BEAT_PER_Q * beat_gap)


def _midi_name(midi):
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A",
             "A#", "B"]
    m = int(round(float(midi)))
    return f"{names[m % 12]}{m // 12 - 1}"


def _q(v):
    return f"{round(float(v), C.ROUND_NDIGITS):g}"


# ============================================================ 迁移预览

def build_preview(piece_id: str, revised_bytes: bytes,
                  title: str | None = None) -> dict:
    """解析修订谱并生成确定性迁移预览（不落库）。"""
    piece = db.get_piece(piece_id)
    if not piece:
        raise ValueError("乐曲不存在")
    if piece["status"] != "analyzed":
        raise ValueError("该录音尚未完成分析，不能发起谱面修订迁移")
    align = db.get_latest_alignment(piece_id)
    if not align:
        raise ValueError("该录音尚无已确认对齐版本")

    old_rows = db.get_score_notes(piece_id)
    old_notes = [{"index": r["idx"], "midi": float(r["midi"]),
                  "beat": float(r["beat"]), "dur_q": float(r["dur_q"]),
                  "measure": r["measure"],
                  "beat_in_measure": r["beat_in_measure"],
                  "occurrence": r["occurrence"], "part": r["part"]}
                 for r in old_rows]

    new_notes, parse_warnings, revised_hash = parse_revision(
        revised_bytes)
    # 重新连续编号（parse 已编号；保证从 0 连续）
    for i, n in enumerate(new_notes):
        n["index"] = i

    matches, blockers = match_scores(old_notes, new_notes)

    old_to_new = _old_new_index_map(matches)
    impact = build_impact(piece_id, matches, old_to_new)

    source_hash = file_sha256(piece["musicxml_path"])
    # 每条匹配记录只计一次（kept/modified/moved 是成对记录，deleted 只挂旧
    # 侧，added/unmatched 只挂新侧）。
    counts = {k: 0 for k in MATCH_LABELS_ZH}
    for m in matches:
        counts[m["kind"]] += 1

    return {
        "format": "score-revision-migration-preview",
        "algorithm_version": C.REVISION_ALGORITHM_VERSION,
        "params": public_params(),
        "source_piece_id": piece_id,
        "source_title": piece["title"],
        "title": title or f"{piece['title']}（谱面修订）",
        "source_alignment_version": align["version"],
        "source_musicxml_sha256": source_hash,
        "revised_musicxml_sha256": revised_hash,
        "parse_warnings": parse_warnings,
        "can_commit": not blockers,
        "blockers": blockers,
        "summary": {
            "old_note_count": len(old_notes),
            "new_note_count": len(new_notes),
            **counts,
            "blocker_count": len(blockers),
            "inheritable": impact["summary"][INHERIT],
            "review": impact["summary"][REVIEW],
            "invalid": impact["summary"][INVALID],
        },
        "matches": matches,
        "index_map": old_to_new,
        "impact": impact["items"],
        "impact_groups": impact["groups"],
        "history_policy": (
            "旧录音与其全部对齐版本 / 对比快照 / 处方 / 练习谱历史只读，"
            "迁移只追加新记录；确认后创建新的谱面修订记录与复盘入口，"
            "绝不静默改写任何历史产物"),
    }


def _old_new_index_map(matches):
    """旧谱音下标 -> 新谱音下标（保留/修改/移动），删除/无法匹配为 None。"""
    out = {}
    for m in matches:
        if m.get("old") is not None and m.get("new") is not None \
                and m["kind"] in (MATCH_KEPT, MATCH_MODIFIED, MATCH_MOVED):
            out[m["old"]["score_index"]] = m["new"]["score_index"]
    return [{"old_index": k, "new_index": out[k],
             "kind": next(m["kind"] for m in matches
                          if m.get("old") and m["old"]["score_index"] == k)}
            for k in sorted(out)]


# ============================================================ 继承判定

def _worst(statuses):
    if INVALID in statuses:
        return INVALID
    if REVIEW in statuses:
        return REVIEW
    return INHERIT


def _position_status(kind):
    """单个谱位匹配类型 -> 该谱位上历史结论的继承等级。"""
    if kind == MATCH_KEPT:
        return INHERIT
    if kind in (MATCH_MODIFIED, MATCH_MOVED):
        return REVIEW
    return INVALID  # deleted / unmatched


def build_impact(piece_id, matches, old_to_new_map):
    """逐项标注锚点 / 对齐快照 / 未过期对比 / 处方 / 练习谱。"""
    kind_by_old = {m["old"]["score_index"]: m["kind"]
                   for m in matches if m.get("old")}
    new_by_old = {e["old_index"]: (e["new_index"], e["kind"])
                  for e in old_to_new_map}
    # 旧/新展开单位位置 -> 匹配关系（供练习谱按整小节单位映射）
    old_unit_pos, new_unit_pos = _unit_positions(matches)
    unit_map = _map_units(old_unit_pos, new_unit_pos, matches)
    items: list[dict] = []

    # ---- 锚点 ----
    anchors = db.get_anchors(piece_id)
    inherit_anchor_indexes = []
    for a in anchors:
        si, oi = a["score_index"], a["obs_index"]
        if a["kind"] in ("lock", "force_lock"):
            items.append({
                "artifact": "anchor", "id": a["id"], "kind": a["kind"],
                "label": ("🔒 锁定配对" if a["kind"] == "lock"
                          else "✅ 待确认配对")
                         + (f" 谱#{si} ↔ 录#{oi}" if si is not None else ""),
                "status": REVIEW,
                "old_score_index": si, "new_score_index": None,
                "reasons": ["配对绑定旧录音的观测起音，修订后需用新录音"
                            "重新建立；旧锚点保留在只读历史中"],
            })
        elif a["kind"] == "exclude_obs":
            items.append({
                "artifact": "anchor", "id": a["id"], "kind": a["kind"],
                "label": f"🚫 排除观测 录#{oi}", "status": INVALID,
                "old_score_index": si, "new_score_index": None,
                "reasons": ["纯音频观测排除，修订后是另一条录音，无法迁移"],
            })
        elif a["kind"] == "exclude_score":
            kind = kind_by_old.get(si)
            st = _position_status(kind) if kind else INVALID
            ni = new_by_old.get(si, (None,))[0]
            if st == INHERIT:
                reasons = ["谱位保留：排除判定随谱位自动预置到新复盘入口"]
                inherit_anchor_indexes.append(ni)
            elif st == REVIEW:
                reasons = [f"谱位为{MATCH_LABELS_ZH[kind]}：请在新录音复盘时"
                           "复核是否仍需排除"]
            else:
                reasons = [f"谱位{MATCH_LABELS_ZH.get(kind, '已消失')}："
                           "排除判定失去对应谱位"]
            items.append({
                "artifact": "anchor", "id": a["id"], "kind": a["kind"],
                "label": f"🚫 排除乐谱音 谱#{si}", "status": st,
                "old_score_index": si,
                "new_score_index": ni if st == INHERIT else None,
                "reasons": reasons,
            })

    # ---- 对齐快照（只读历史结论）----
    # 该录音参与的每个对比的固化快照都是“旧指纹 + 旧音频”结论。
    cmp_ids = set(db.comparisons_referencing_piece(piece_id))
    unexpired_cmps, stale_cmps = [], []
    for cmp_id in sorted(cmp_ids):
        row = db.get_comparison(cmp_id)
        if not row:
            continue
        snap = db.get_snapshot(cmp_id, piece_id)
        stale_reasons = (cmp_service._stale_reasons(snap)  # noqa: SLF001
                         if snap else ["snapshot_missing"])
        if stale_reasons:
            stale_cmps.append((row, stale_reasons))
        else:
            unexpired_cmps.append(row)
        items.append({
            "artifact": "comparison_snapshot", "id": cmp_id,
            "comparison_id": cmp_id, "title": row["title"],
            "label": f"只读对齐快照：《{row['title']}》",
            "status": INVALID,
            "reasons": ["快照固化的是旧谱指纹与旧录音的逐音结论，"
                        "修订后指纹不同，不能作为新入口结论；"
                        "快照本身只读保留"],
        })

    # ---- 未过期对比（整体对象，可重建）----
    for row in unexpired_cmps:
        items.append({
            "artifact": "comparison", "id": row["id"],
            "comparison_id": row["id"], "title": row["title"],
            "label": f"多次演奏对比：《{row['title']}》",
            "status": REVIEW,
            "reasons": ["对比当前未过期，但成员锚定旧谱；新录音分析完成"
                        "后可显式重建同成员对比（其余成员指纹不变）"],
        })
    for row, reasons in stale_cmps:
        items.append({
            "artifact": "comparison", "id": row["id"],
            "comparison_id": row["id"], "title": row["title"],
            "label": f"多次演奏对比：《{row['title']}》",
            "status": INVALID,
            "reasons": [f"对比已过期（{', '.join(reasons)}）：先在对比页"
                        "显式刷新旧结论，再决定是否对修订入口重建"],
        })

    # ---- 处方 ----
    prx_templates = []
    for prx in db.list_prescriptions():
        if prx["source_piece_id"] != piece_id:
            continue
        st, seg_views, mapped = _prescription_status(
            prx, kind_by_old, new_by_old)
        reason = {
            INHERIT: "全部规则谱段落在保留谱位，可在新入口分析完成后"
                     "一键接续为新处方",
            REVIEW: "部分谱段涉及移动/修改谱位，已按新谱拆分/映射，"
                    "接续前请复核",
            INVALID: "规则谱段引用的谱位在修订谱中已删除/无法匹配，"
                     "处方不能迁移",
        }[st]
        if prx["status"] == "voided":
            st, reason = INVALID, "处方已作废，不迁移（历史只读保留）"
        items.append({
            "artifact": "prescription", "id": prx["id"],
            "prescription_id": prx["id"], "title": prx["title"],
            "label": f"偏差练习处方：《{prx['title']}》"
                     f"（{prx['status']}）",
            "status": st, "reasons": [reason], "segments": seg_views,
        })
        if st in (INHERIT, REVIEW):
            prx_templates.append({
                "kind": "prescription", "source_id": prx["id"],
                "title": prx["title"], "status": st,
                "rules": json.loads(prx["rules_json"]),
                "mapped_segments": mapped,
            })

    # ---- 练习谱 ----
    practice_templates = []
    for task in db.list_practice_scores():
        if task["piece_id"] != piece_id:
            continue
        st, seg_views, mapped = _practice_status(task, unit_map)
        reason = {
            INHERIT: "全部片段单位保留，可在新入口分析完成后一键接续",
            REVIEW: "部分片段涉及移动/修改/新增谱位，已按新单位映射，"
                    "接续前请复核逐位准入",
            INVALID: "片段单位在修订谱中已删除/无法对回，练习谱不能迁移",
        }[st]
        items.append({
            "artifact": "practice_score", "id": task["id"],
            "practice_score_id": task["id"], "title": task["title"],
            "label": f"偏差片段练习谱：《{task['title']}》"
                     f"（当前 v{task['current_version']}）",
            "status": st, "reasons": [reason], "segments": seg_views,
        })
        if st in (INHERIT, REVIEW):
            practice_templates.append({
                "kind": "practice_score", "source_id": task["id"],
                "title": task["title"], "status": st,
                "segments": json.loads(task["segments_json"]),
                "mapped_segments": mapped,
            })

    groups = {}
    for it in items:
        groups.setdefault(it["artifact"], []).append(
            {"id": it["id"], "label": it["label"], "status": it["status"],
             "reasons": it["reasons"]})

    summary = {INHERIT: 0, REVIEW: 0, INVALID: 0}
    for it in items:
        summary[it["status"]] += 1

    return {"items": items, "groups": groups, "summary": summary,
            "_inherit_anchor_indexes": sorted(set(inherit_anchor_indexes)),
            "_templates": {"prescription": prx_templates,
                           "practice_score": practice_templates}}


def _index_range_status(indexes, kind_by_old):
    statuses = []
    for idx in indexes:
        kind = kind_by_old.get(idx)
        statuses.append(_position_status(kind) if kind else INVALID)
    return _worst(statuses)


def _map_range(start, end, new_by_old):
    """旧 [start,end] 连续下标 -> 新下标升序序列（只收保留/修改/移动）。"""
    mapped = sorted(n for i in range(start, end + 1)
                    if (n := new_by_old.get(i, (None,))[0]) is not None)
    return mapped


def _split_contiguous(indexes):
    if not indexes:
        return []
    out, cur = [], [indexes[0]]
    for x in indexes[1:]:
        if x == cur[-1] + 1:
            cur.append(x)
        else:
            out.append(cur)
            cur = [x]
    out.append(cur)
    return out


def _prescription_status(prx, kind_by_old, new_by_old):
    rules = json.loads(prx["rules_json"])
    seg_views, mapped, statuses = [], [], []
    for k, seg in enumerate(rules["segments"]):
        s, e = seg["score_index_start"], seg["score_index_end"]
        st = _index_range_status(range(s, e + 1), kind_by_old)
        statuses.append(st)
        mapped_idx = _map_range(s, e, new_by_old)
        parts = _split_contiguous(mapped_idx)
        mapped.append({"segment": k + 1, "status": st,
                       "old_range": [s, e],
                       "new_ranges": [[p[0], p[-1]] for p in parts]})
        seg_views.append({"segment": k + 1, "status": st,
                         "old_score_index_start": s,
                         "old_score_index_end": e,
                         "new_ranges": [[p[0], p[-1]] for p in parts]})
    return _worst(statuses), seg_views, mapped


def _practice_status(task, unit_map):
    """练习谱按（measure, occurrence）展开单位映射。

    unit_map: {旧展开单位位置: {"new_key":(measure,occ,新位置),
                                "status": inheritable/review/invalid}}
    """
    segments = json.loads(task["segments_json"])
    seg_views, mapped, statuses = [], [], []
    for k, seg in enumerate(segments):
        st, new_keys = _map_practice_segment(seg, unit_map)
        statuses.append(st)
        mapped.append({"segment": k + 1, "status": st,
                       "old_units": [
                           [seg["measure_start"], seg["occurrence_start"]],
                           [seg["measure_end"], seg["occurrence_end"]]],
                       "new_units": new_keys})
        seg_views.append({"segment": k + 1, "status": st,
                          "new_units": new_keys})
    return _worst(statuses), seg_views, mapped


def _map_practice_segment(seg, unit_map):
    """展开单位区间 [order_start..order_end] -> 新单位序列。

    单位消失 -> invalid；单位内有修改/移动谱音 -> review；映射后新单位
    不连续（中间被插入新单位）-> review。
    """
    a, b = seg.get("order_start"), seg.get("order_end")
    if a is None:
        return INVALID, []
    entries, statuses = [], []
    for old_pos in range(a, b + 1):
        info = unit_map.get(old_pos)
        if info is None:
            statuses.append(INVALID)
            continue
        statuses.append(info["status"])
        entries.append(info)
    st = _worst(statuses)
    new_keys = [[e["new_key"][0], e["new_key"][1]] for e in entries]
    if st == INHERIT:
        nps = [e["new_key"][2] for e in entries]
        if any(nps[i + 1] != nps[i] + 1 for i in range(len(nps) - 1)):
            st = REVIEW
    return st, new_keys


def _unit_positions(matches):
    """从匹配结果重建旧/新两侧展开单位及其顺序位置。

    返回 (old_units, new_units)：有序 [(key, [score_index...]), ...]。
    """
    def _collect(side):
        groups: dict[tuple, list[int]] = {}
        beat_of: dict[int, float] = {}
        for m in matches:
            view = m.get(side)
            if view is None:
                continue
            key = (view["measure"], view.get("occurrence", 0) or 0)
            groups.setdefault(key, []).append(view["score_index"])
            beat_of[view["score_index"]] = view["beat_expanded"]
        units = []
        for key, idxs in groups.items():
            idxs.sort()
            units.append((key, idxs, beat_of[idxs[0]]))
        units.sort(key=lambda u: (u[2], u[0][0], u[0][1]))
        return [(u[0], u[1]) for u in units]

    return _collect("old"), _collect("new")


def _map_units(old_units, new_units, matches):
    """旧单位（按展开位置）-> 新单位 + 继承等级。

    沿用与主匹配相同的单位 LCS 配对（measure + occurrence）：
    配对单位按其内谱位 worst 判定（保留可继承；修改/移动需复核）；
    未对上旧单位 -> 无映射（invalid，片段含此单位即失效）。
    """
    kind_by_old = {m["old"]["score_index"]: m["kind"]
                   for m in matches if m.get("old")}
    old_keys = [u[0] for u in old_units]
    new_keys = [u[0] for u in new_units]
    pairs = _lcs_pairs(old_keys, new_keys)
    mapping = {}
    for op, npos in pairs:
        statuses = [_position_status(kind_by_old.get(i))
                    if kind_by_old.get(i) else INVALID
                    for i in old_units[op][1]]
        mapping[op] = {
            "new_key": (new_units[npos][0][0], new_units[npos][0][1], npos),
            "status": _worst(statuses),
        }
    return mapping


def public_params():
    return {
        "revision_algorithm_version": C.REVISION_ALGORITHM_VERSION,
        "match_key": ["measure", "occurrence", "beat_in_measure",
                      "midi", "duration_quarters"],
        "align_cost": {
            "delete": C.REVISION_COST_DELETE,
            "insert": C.REVISION_COST_INSERT,
            "modified_same_position": C.REVISION_COST_MODIFIED,
            "unit_per_gap": C.REVISION_COST_UNIT_PER_GAP,
            "beat_per_quarter": C.REVISION_COST_BEAT_PER_Q,
        },
        "duration_eps_quarters": C.REVISION_DUR_EPS,
    }


# ============================================================ 预览落盘

def create_revision(piece_id: str, revised_bytes: bytes,
                    title: str | None = None) -> dict:
    """上传修订谱 -> 生成预览 -> 原子落盘并创建 preview 记录。

    多声部歧义直接抛 :class:`PolyphonyError`（路由 422）。
    """
    preview = build_preview(piece_id, revised_bytes, title)

    # 统一以解压后的 MusicXML 落盘（.mxl 也规范化为 XML），保证下载媒体类型
    # 与新复盘入口谱文件一致。
    stored_bytes = (_extract_mxl_xml(revised_bytes)
                    if _is_zip_mxl(revised_bytes) else revised_bytes)
    revision_id = uuid.uuid4().hex[:12]
    os.makedirs(_files_dir(), exist_ok=True)
    revised_path = os.path.join(
        _files_dir(), f"revision_{revision_id}_revised.musicxml")
    _atomic_write(revised_path, stored_bytes)

    db.create_score_revision(
        revision_id, piece_id, preview["title"],
        preview["source_musicxml_sha256"],
        preview["revised_musicxml_sha256"],
        preview["source_alignment_version"], revised_path,
        has_blockers=bool(preview["blockers"]),
        blockers_json=_canonical_json(preview["blockers"]),
        mapping_json=_canonical_json(_report_from_preview(preview)))
    return get_revision(revision_id)


def _atomic_write(path: str, data: bytes):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                               prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _report_from_preview(preview: dict) -> dict:
    """确定性迁移报告（无时间戳 / 随机项，可字节级导出）。"""
    return {
        "format": "score-revision-migration-report",
        "algorithm_version": preview["algorithm_version"],
        "params": preview["params"],
        "source_piece_id": preview["source_piece_id"],
        "source_title": preview["source_title"],
        "title": preview["title"],
        "source_alignment_version": preview["source_alignment_version"],
        "source_musicxml_sha256": preview["source_musicxml_sha256"],
        "revised_musicxml_sha256": preview["revised_musicxml_sha256"],
        "parse_warnings": preview["parse_warnings"],
        "can_commit": preview["can_commit"],
        "blockers": preview["blockers"],
        "summary": preview["summary"],
        "matches": preview["matches"],
        "index_map": preview["index_map"],
        "impact": preview["impact"],
        "impact_groups": preview["impact_groups"],
        "history_policy": preview["history_policy"],
        "inheritance_legend": {
            INHERIT: "可继承：谱位/单位保留，结论或规则可随迁移自动接续",
            REVIEW: "需复核：谱位移动/修改或对象已过期，须在新入口人工确认",
            INVALID: "失效：引用谱位删除/无法匹配或绑定旧音频，不迁移"
                     "（历史只读保留）",
        },
        "match_legend": MATCH_LABELS_ZH,
    }


# ============================================================ 读取 / 列表

def get_revision(revision_id: str) -> dict:
    row = db.get_score_revision(revision_id)
    if not row:
        raise ValueError("谱面修订记录不存在")
    report = json.loads(row["mapping_json"])
    source = db.get_piece(row["source_piece_id"])
    new_piece = db.get_piece(row["new_piece_id"]) if row["new_piece_id"] \
        else None
    return {
        "id": row["id"],
        "status": row["status"],
        "source_piece_id": row["source_piece_id"],
        "source_title": source["title"] if source else None,
        "new_piece_id": row["new_piece_id"],
        "new_piece_status": new_piece["status"] if new_piece else None,
        "new_piece_has_audio": bool(new_piece and new_piece["audio_path"])
        if new_piece else False,
        "title": row["title"],
        "source_musicxml_sha256": row["source_musicxml_sha256"],
        "revised_musicxml_sha256": row["revised_musicxml_sha256"],
        "source_alignment_version": row["source_alignment_version"],
        "has_blockers": bool(row["has_blockers"]),
        "blockers": json.loads(row["blockers_json"]),
        "can_commit": row["status"] == "preview"
        and not row["has_blockers"],
        "summary": report["summary"],
        "matches": report["matches"],
        "index_map": report["index_map"],
        "impact": report["impact"],
        "impact_groups": report["impact_groups"],
        "params": report["params"],
        "parse_warnings": report.get("parse_warnings", []),
        "history_policy": report["history_policy"],
        "inherited": json.loads(row["inherited_json"])
        if row["inherited_json"] else None,
        "carryover_templates": json.loads(row["carryover_templates_json"])
        if row["carryover_templates_json"] else None,
        "revised_musicxml_available": os.path.exists(row["revised_path"]),
        "revised_musicxml_url": f"/api/revisions/{row['id']}/revised-musicxml",
        "report_url": f"/api/revisions/{row['id']}/report",
        "created_at": row["created_at"],
        "committed_at": row["committed_at"],
    }


def list_revisions(piece_id: str | None = None) -> list[dict]:
    rows = db.list_score_revisions()
    out = []
    for row in rows:
        if piece_id and row["source_piece_id"] != piece_id \
                and row["new_piece_id"] != piece_id:
            continue
        report = json.loads(row["mapping_json"])
        source = db.get_piece(row["source_piece_id"])
        out.append({
            "id": row["id"],
            "status": row["status"],
            "source_piece_id": row["source_piece_id"],
            "source_title": source["title"] if source else None,
            "new_piece_id": row["new_piece_id"],
            "title": row["title"],
            "has_blockers": bool(row["has_blockers"]),
            "summary": report["summary"],
            "revised_musicxml_sha256": row["revised_musicxml_sha256"],
            "created_at": row["created_at"],
            "committed_at": row["committed_at"],
        })
    return out


def get_report(revision_id: str) -> dict:
    row = db.get_score_revision(revision_id)
    if not row:
        raise ValueError("谱面修订记录不存在")
    report = json.loads(row["mapping_json"])
    report["revision_id"] = row["id"]
    report["status"] = row["status"]
    report["new_piece_id"] = row["new_piece_id"]
    return report


def read_revised_musicxml(revision_id: str) -> bytes:
    row = db.get_score_revision(revision_id)
    if not row:
        raise ValueError("谱面修订记录不存在")
    if not os.path.exists(row["revised_path"]):
        raise ValueError("修订版 MusicXML 文件缺失")
    with open(row["revised_path"], "rb") as f:
        return f.read()


# ============================================================ 确认提交

def commit_revision(revision_id: str) -> dict:
    """用户确认后创建新的谱面修订记录与复盘入口（旧记录只读）。"""
    row = db.get_score_revision(revision_id)
    if not row:
        raise ValueError("谱面修订记录不存在")
    if row["status"] == "committed":
        raise ValueError("该修订已确认提交，记录只读，不能重复提交")
    if row["has_blockers"]:
        blockers = json.loads(row["blockers_json"])
        raise CommitBlockedError(
            "存在匹配冲突 / 多声部歧义，必须先消解后才能提交迁移",
            blockers)

    source = db.get_piece(row["source_piece_id"])
    if not source:
        raise ValueError("源录音已不存在，无法迁移")
    # 来源在预览后发生变化：禁止用过期依据静默迁移
    if file_sha256(source["musicxml_path"]) != row["source_musicxml_sha256"]:
        raise CommitBlockedError(
            "源 MusicXML 在预览生成后已变化，请重新上传修订谱生成预览",
            [{"reason": "source_changed_since_preview"}])
    latest = db.get_latest_alignment(source["id"])
    if not latest or latest["version"] != row["source_alignment_version"]:
        raise CommitBlockedError(
            "源对齐版本在预览生成后已变化，请重新生成预览后再确认",
            [{"reason": "source_alignment_changed_since_preview"}])

    # 重新解析修订谱（确定性核对哈希），写新复盘入口
    with open(row["revised_path"], "rb") as f:
        revised_bytes = f.read()
    if bytes_sha256(revised_bytes) != row["revised_musicxml_sha256"]:
        raise CommitBlockedError(
            "修订谱文件与预览固化时不一致，请重新上传",
            [{"reason": "revised_file_changed_since_preview"}])
    new_notes, warnings, _h = parse_revision(revised_bytes)
    for i, n in enumerate(new_notes):
        n["index"] = i

    new_piece_id = uuid.uuid4().hex[:12]
    new_title = f"{row['title']}"
    # 新入口的谱文件统一保存为展开前 MusicXML 字节（.mxl 也先解压为 XML，
    # 与普通 piece 上传的 .xml 路径一致，便于 score_parser 再次解析）。
    entry_bytes = (_extract_mxl_xml(revised_bytes)
                   if _is_zip_mxl(revised_bytes) else revised_bytes)
    new_xml_path = os.path.join(_files_dir(), f"{new_piece_id}.xml")
    _atomic_write(new_xml_path, entry_bytes)
    db.create_revision_piece(new_piece_id, new_title, new_xml_path,
                             source["id"], revision_id)
    db.save_revision_score_notes(new_piece_id, new_notes, warnings)

    # 重新构建继承判定（与预览一致的确定性结果），取可继承锚点/模板
    old_rows = db.get_score_notes(source["id"])
    old_notes = [{"index": r["idx"], "midi": float(r["midi"]),
                  "beat": float(r["beat"]), "dur_q": float(r["dur_q"]),
                  "measure": r["measure"],
                  "beat_in_measure": r["beat_in_measure"],
                  "occurrence": r["occurrence"], "part": r["part"]}
                 for r in old_rows]
    matches, blockers = match_scores(old_notes, new_notes)
    if blockers:
        # 理论上不会发生（预览已拦截且输入未变），兜底绝不静默写入口
        raise CommitBlockedError("重新匹配出现冲突，已阻止提交", blockers)
    old_to_new = _old_new_index_map(matches)
    impact = build_impact(source["id"], matches, old_to_new)

    # 预置可继承的 exclude_score 锚点到新入口（其余锚点只读留旧入口）
    inherited_anchors = []
    for new_idx in impact["_inherit_anchor_indexes"]:
        db.add_anchor_direct(new_piece_id, "exclude_score", new_idx,
                             note="inherited_from_score_revision")
        inherited_anchors.append({"kind": "exclude_score",
                                  "new_score_index": new_idx})

    templates = impact["_templates"]
    inherited = {
        "anchors": inherited_anchors,
        "inheritable_counts": {
            "anchor": len(inherited_anchors),
            "prescription": len(templates["prescription"]),
            "practice_score": len(templates["practice_score"]),
        },
    }
    db.commit_score_revision(
        revision_id, new_piece_id,
        _canonical_json(inherited), _canonical_json(templates))
    return get_revision(revision_id)


def delete_preview(revision_id: str) -> bool:
    """只允许删除未确认的预览及其修订文件；committed 只读不可删。"""
    row = db.get_score_revision(revision_id)
    if not row:
        return False
    if row["status"] != "preview":
        raise ValueError("已确认的谱面修订记录只读，不能删除")
    try:
        if os.path.exists(row["revised_path"]):
            os.remove(row["revised_path"])
    except OSError:
        pass
    return bool(db.delete_score_revision(revision_id))


# ============================================================ 新入口上传录音

def upload_revision_audio(revision_id: str):
    """为修订后的新复盘入口上传后续录音并后台分析。

    返回 new_piece_id；路由把 WAV 存为 {new_piece_id}.wav 后调用
    :func:`start_audio_analysis`。
    """
    row = db.get_score_revision(revision_id)
    if not row:
        raise ValueError("谱面修订记录不存在")
    if row["status"] != "committed" or not row["new_piece_id"]:
        raise ValueError("修订尚未确认提交，尚无复盘入口")
    piece = db.get_piece(row["new_piece_id"])
    if piece["audio_path"]:
        raise ValueError("该复盘入口已有关联录音，谱面修订入口只接受"
                         "一次后续录音")
    return piece["id"]


def start_audio_analysis(piece_id: str, wav_path: str):
    """与 pieces 路由相同的后台分析（分析完成自动首次对齐）。"""
    from ..services import audio_analyzer
    score_rows = db.get_score_notes(piece_id)
    score_notes = [{"index": r["idx"], "midi": float(r["midi"]),
                    "beat": float(r["beat"]), "dur_q": float(r["dur_q"]),
                    "measure": r["measure"],
                    "beat_in_measure": r["beat_in_measure"],
                    "occurrence": r["occurrence"], "part": r["part"]}
                   for r in score_rows]
    piece = db.get_piece(piece_id)
    try:
        meta = json.loads(piece["warnings_json"] or "{}")
    except (ValueError, TypeError):
        meta = {}
    warnings = meta.get("warnings", [])

    def _worker():
        try:
            obs_notes, duration, peaks, audio_warnings = \
                audio_analyzer.analyze_audio(wav_path)
            db.save_analysis(piece_id, wav_path, duration, score_notes,
                             obs_notes, warnings + audio_warnings, peaks)
            review.recompute(piece_id)
        except Exception as exc:  # noqa: BLE001
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE pieces SET status='failed', error=? WHERE id=?",
                    (str(exc), piece_id))

    import threading
    threading.Thread(target=_worker, daemon=True).start()


# ============================================================ 一键接续

def carryover_context(piece_id: str) -> dict:
    """新复盘入口视角：关联修订记录 + 可接续处方/练习谱模板。"""
    piece = db.get_piece(piece_id)
    if not piece or not piece.get("revision_id"):
        return {"revision_id": None, "templates": []}
    row = db.get_score_revision(piece["revision_id"])
    if not row or not row["carryover_templates_json"]:
        return {"revision_id": piece["revision_id"], "templates": []}
    templates = json.loads(row["carryover_templates_json"])
    analyzed = piece["status"] == "analyzed" and bool(
        db.get_latest_alignment(piece_id))
    out = []
    for kind in ("prescription", "practice_score"):
        for t in templates.get(kind, []):
            out.append({
                "kind": kind, "source_id": t["source_id"],
                "title": t["title"], "status": t["status"],
                "ready": analyzed,
                "not_ready_reason": None if analyzed
                else "新入口尚未完成录音分析，暂无已确认对齐版本",
            })
    return {"revision_id": row["id"], "status": row["status"],
            "analyzed": analyzed, "templates": out}


def apply_carryover(piece_id: str, kind: str, source_id: str) -> dict:
    """分析完成后把可继承/需复核模板显式接续为新入口的处方 / 练习谱。

    必须用户显式触发（一键接续），系统不静默改写；接续产生全新对象，
    源对象与全部历史只读保留。
    """
    piece = db.get_piece(piece_id)
    if not piece or not piece.get("revision_id"):
        raise ValueError("该录音不是谱面修订入口")
    if piece["status"] != "analyzed" or not db.get_latest_alignment(piece_id):
        raise ValueError("新入口尚未完成录音分析，暂无已确认对齐版本")
    row = db.get_score_revision(piece["revision_id"])
    templates = json.loads(row["carryover_templates_json"] or "{}")
    tpl = next((t for t in templates.get(kind, [])
                if t["source_id"] == source_id), None)
    if tpl is None:
        raise ValueError("模板不存在或不可接续（失效项不能接续）")

    if kind == "prescription":
        segments = _carryover_prescription_segments(tpl)
        new_id = prx_service.create_prescription(
            "piece", piece_id,
            f"{tpl['title']}（谱面修订接续）", segments)
        return {"kind": kind, "id": new_id}
    if kind == "practice_score":
        segments = _carryover_practice_segments(tpl)
        new_id = ps_service.create_practice_score(
            piece_id, f"{tpl['title']}（谱面修订接续）", segments)
        return {"kind": kind, "id": new_id}
    raise ValueError("kind 必须为 prescription/practice_score")


def _carryover_prescription_segments(tpl):
    """旧规则段（旧下标区间）-> 新下标连续区间（拆分不连续映射）。"""
    out = []
    for seg, mv in zip(tpl["rules"]["segments"], tpl["mapped_segments"]):
        for a, b in mv["new_ranges"]:
            out.append({
                "score_index_start": a, "score_index_end": b,
                "deviation_categories": seg["deviation_categories"],
                "max_onset_error_s": seg["max_onset_error_s"],
                "max_duration_ratio_dev": seg["max_duration_ratio_dev"],
                "max_abnormal_notes": seg["max_abnormal_notes"],
                "forbid_pending": seg["forbid_pending"],
            })
    return out


def _carryover_practice_segments(tpl):
    """旧练习谱片段（旧展开单位区间）-> 新单位（measure+occurrence）区间。"""
    out = []
    for seg, mv in zip(tpl["segments"], tpl["mapped_segments"]):
        units = mv["new_units"]
        if not units:
            continue
        out.append({
            "measure_start": units[0][0], "occurrence_start": units[0][1],
            "measure_end": units[-1][0], "occurrence_end": units[-1][1],
            "loops": seg["loops"],
            "start_tempo_bpm": seg["start_tempo_bpm"],
            "tempo_increment_bpm": seg["tempo_increment_bpm"],
            "tempo_direction": seg["tempo_direction"],
        })
    return out

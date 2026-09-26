"""偏差练习处方与复测。

模型
====
* **处方（prescription）** 是只读对象：创建时把「谱指纹 + 对齐版本/快照 +
  逐段规则」整体固化。来源分两种：
    - ``piece``：从单次已分析乐曲框选；
    - ``comparison``：从**未过期**的多次演奏对比框选（基线谱指纹）。
* **复测（retest）** 关联同曲后续录音（谱指纹必须一致）。系统按处方谱段
  对目标录音**当次已确认的对齐版本**重新计算确定性结果，并在复测记录中
  **当场固化**目标对齐版本、音频哈希与逐项结果，以及该次复测所依据的
  处方来源固化版本：
    - ``pass`` 通过：所有声明规则满足，且谱段内没有不可判定谱音；
    - ``fail`` 未通过：存在实际违反阈值/类别的规则；
    - ``undeterminable`` 不可判定：无明确失败，但存在待确认（且未被禁止）、
      用户已排除或置信度不足的谱音。
* **两类准入校验彼此独立，不混为一谈**：
    1. *处方来源是否过期*：固化成员中**任一个**的对齐版本/音频/谱文件与
       固化时不一致，即来源过期，**必须显式重绑**（``source_stale``，409）；
    2. *目标当次对齐是否可用*：目标须已完成分析、拥有**当前已确认**对齐
       版本且谱指纹一致。同曲**新目标**（从未列入来源固化成员）**无需
       重绑**即可复测；只有指纹不同（``fingerprint_mismatch``）或目标没有
       已确认对齐（``no_confirmed_alignment``）才拒绝（409）。
* 历史复测永远只读：之后来源或目标再变化，也只影响下一次复测，**绝不
  改写旧结果**。
* 来源过期（对齐版本/音频变化）后只能**显式重绑**：旧复测结果原样保留，
  固化版本历史追加一条。
* 处方状态只按 ``created -> retested -> voided`` 单向推进
  （``created`` 也可直接作废）。创建与复测均要求 ``request_id`` 幂等。
"""

from __future__ import annotations

import json
import uuid

from .. import config as C
from .. import db
from . import comparison as cmp_service
from . import review

# 判定结果
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNDETERMINABLE = "undeterminable"

# 状态机
STATUS_CREATED = "created"
STATUS_RETESTED = "retested"
STATUS_VOIDED = "voided"
STATUS_NEXT = {
    STATUS_CREATED: (STATUS_RETESTED, STATUS_VOIDED),
    STATUS_RETESTED: (STATUS_RETESTED, STATUS_VOIDED),  # 可反复复测
    STATUS_VOIDED: (),
}

# 不可判定原因
UND_PENDING = "pending_confirmation"
UND_EXCLUDED = "excluded"
UND_LOW_CONF = "low_confidence"
UND_NO_NOTE = "no_snapshot_note"

# 复测兼容性拒绝原因
INCOMPAT_FINGERPRINT = "fingerprint_mismatch"
INCOMPAT_NO_ALIGNMENT = "no_confirmed_alignment"
INCOMPAT_PRESCRIPTION_VOIDED = "prescription_voided"

# 候选不可用原因
CANDIDATE_SOURCE_STALE = "source_stale"
CANDIDATE_MEMBER_STALE = "member_stale"
CANDIDATE_NO_ALIGNMENT = "no_confirmed_alignment"


class IncompatibleError(Exception):
    """目标当次对齐不可用（谱指纹不同 / 无已确认对齐版本 / 处方作废）。

    注意：目标是同曲新录音（从未列入来源固化成员）**不**属于不兼容；
    来源固化成员过期属于 :class:`StaleSourceError`，两类校验互不混入。
    """

    def __init__(self, reason: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.extra = extra or {}


class StaleSourceError(Exception):
    """处方来源已过期，需要显式重绑。"""

    def __init__(self, message: str, stale: list[dict]):
        super().__init__(message)
        self.stale = stale


# ============================================================ 固化 / 过期

def _piece_file_hash(piece_id: str, suffix: str):
    import os
    path = os.path.join(os.environ.get("DATA_DIR", "/data"), "files",
                        f"{piece_id}{suffix}")
    return review._file_hash(path)  # noqa: SLF001


def freeze_piece_source(piece_id: str) -> dict:
    """单次乐曲来源：固化最新已确认对齐版本与文件哈希。"""
    piece = db.get_piece(piece_id)
    if not piece:
        raise ValueError("乐曲不存在")
    if piece["status"] != "analyzed":
        raise ValueError(f"《{piece['title']}》尚未完成分析，不能创建处方")
    align = db.get_latest_alignment(piece_id)
    if not align:
        raise ValueError(f"《{piece['title']}》尚无已确认对齐版本")
    bundle = review.get_bundle(piece_id)
    return {
        "source_type": "piece",
        "piece_id": piece_id,
        "title": piece["title"],
        "fingerprint": cmp_service.score_fingerprint(bundle["score_notes"]),
        "members": [{
            "piece_id": piece_id,
            "title": piece["title"],
            "alignment_version": align["version"],
            "musicxml_sha256": _piece_file_hash(piece_id, ".xml"),
            "wav_sha256": _piece_file_hash(piece_id, ".wav"),
        }],
    }


def freeze_comparison_source(cmp_id: str) -> dict:
    """对比来源：只接受**未过期**对比，固化基线指纹与各成员只读快照版本。"""
    cmp_row = db.get_comparison(cmp_id)
    if not cmp_row:
        raise ValueError("对比不存在")
    stale = []
    for sr in db.get_snapshots(cmp_id):
        reasons = cmp_service._stale_reasons(sr)  # noqa: SLF001
        if reasons:
            stale.append({"piece_id": sr["piece_id"], "reasons": reasons})
    if stale:
        raise StaleSourceError(
            "多次演奏对比已过期：请先在对比页“显式刷新”，再创建/重绑处方",
            stale)
    members = []
    for sr in db.get_snapshots(cmp_id):
        members.append({
            "piece_id": sr["piece_id"],
            "is_baseline": bool(sr["is_baseline"]),
            "ord": sr["ord"],
            "title": None,
            "alignment_version": sr["alignment_version"],
            "musicxml_sha256": sr["musicxml_sha256"],
            "wav_sha256": sr["wav_sha256"],
        })
    baseline = next(m for m in members if m["is_baseline"])
    base_sr = next(sr for sr in db.get_snapshots(cmp_id)
                   if sr["piece_id"] == baseline["piece_id"])
    snap = json.loads(base_sr["snapshot_json"])
    for m in members:
        ms = next(s for s in db.get_snapshots(cmp_id)
                  if s["piece_id"] == m["piece_id"])
        m["title"] = json.loads(ms["snapshot_json"])["title"]
    return {
        "source_type": "comparison",
        "comparison_id": cmp_id,
        "comparison_title": cmp_row["title"],
        "baseline_piece": baseline["piece_id"],
        "fingerprint": base_sr["fingerprint"],
        "members": members,
    }


def source_stale(frozen: dict) -> list[dict]:
    """固化来源是否已过期（对齐版本/文件哈希变化）。

    所有固化成员一律与**当前实况**（最新已确认对齐版本 + 文件哈希）比对：
    * piece：最新对齐版本 ≠ 固化版本，或文件哈希变化；
    * comparison：同样按成员实时状态判定（成员被移出对比也算过期）。

    注意：对比页之后即便已“显式刷新”快照，只要本处方没有随之显式重绑，
    固化成员的对齐版本/哈希与实况不一致仍算来源过期。
    """
    stale = []
    if frozen["source_type"] == "comparison":
        cmp_id = frozen["comparison_id"]
        if not db.get_comparison(cmp_id):
            stale.append({"piece_id": None,
                          "reasons": ["comparison_removed"]})
        member_ids = {r["piece_id"] for r in db.get_snapshots(cmp_id)}
        for m in frozen["members"]:
            if m["piece_id"] not in member_ids:
                stale.append({"piece_id": m["piece_id"],
                              "reasons": ["removed_from_comparison"]})
                continue
            stale.extend(_piece_member_stale(m))
        return stale
    for m in frozen["members"]:
        stale.extend(_piece_member_stale(m))
    return stale


def _piece_member_stale(m: dict) -> list[dict]:
    """单个固化成员（按 piece_id）相对实况的过期项。"""
    piece_id = m["piece_id"]
    piece = db.get_piece(piece_id)
    if piece is None:
        return [{"piece_id": piece_id, "reasons": ["piece_removed"]}]
    reasons = []
    latest = db.get_latest_alignment(piece_id)
    if latest is None:
        reasons.append("alignment_missing")
    elif latest["version"] != m["alignment_version"]:
        reasons.append("alignment_version_changed")
    cur_wav = _piece_file_hash(piece_id, ".wav")
    if cur_wav is not None and cur_wav != m["wav_sha256"]:
        reasons.append("audio_changed")
    cur_xml = _piece_file_hash(piece_id, ".xml")
    if cur_xml is not None and cur_xml != m["musicxml_sha256"]:
        reasons.append("score_changed")
    return [{"piece_id": piece_id, "reasons": reasons}] if reasons else []


# ============================================================ 规则校验

def _public_params() -> dict:
    return {
        "rx_algorithm_version": C.RX_ALGORITHM_VERSION,
        "categories": list(C.RX_CATEGORIES),
        "min_confidence": C.RX_MIN_CONFIDENCE,
        "default_max_onset_error_s": C.RX_DEFAULT_MAX_ONSET_S,
        "default_duration_ratio_dev": C.RX_DEFAULT_DURATION_RATIO_DEV,
    }


def normalize_segments(raw_segments, valid_indexes) -> list[dict]:
    """校验并规范化用户框选的连续谱段与逐段规则。"""
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("至少需要框选一个连续谱段")
    if len(raw_segments) > C.RX_MAX_SEGMENTS:
        raise ValueError(f"一张处方最多 {C.RX_MAX_SEGMENTS} 个谱段")
    valid = set(valid_indexes)
    out, covered = [], set()
    for k, seg in enumerate(raw_segments):
        try:
            start = int(seg["score_index_start"])
            end = int(seg["score_index_end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {k + 1} 段：谱段下标必须为整数") from exc
        if start > end:
            raise ValueError(f"第 {k + 1} 段：起点不得大于终点")
        indexes = list(range(start, end + 1))
        bad = [i for i in indexes if i not in valid]
        if bad:
            raise ValueError(
                f"第 {k + 1} 段含不存在的谱音下标：{bad[:5]}")
        overlap = sorted(covered.intersection(indexes))
        if overlap:
            raise ValueError(f"谱段不得重叠，重叠下标：{overlap[:5]}")
        covered.update(indexes)

        cats = seg.get("deviation_categories") or []
        if not isinstance(cats, list) or not cats:
            raise ValueError(f"第 {k + 1} 段：至少声明一个必须消除的偏差类别")
        bad_cats = [c for c in cats if c not in C.RX_CATEGORIES]
        if bad_cats:
            raise ValueError(
                f"第 {k + 1} 段：未知偏差类别 {bad_cats}，"
                f"允许：{list(C.RX_CATEGORIES)}")
        # 去重保序
        cats = list(dict.fromkeys(str(c) for c in cats))

        onset = seg.get("max_onset_error_s", C.RX_DEFAULT_MAX_ONSET_S)
        onset = _num(onset, f"第 {k + 1} 段：最大起音误差")
        if onset < 0:
            raise ValueError(f"第 {k + 1} 段：最大起音误差不能为负")
        dur_dev = seg.get("max_duration_ratio_dev",
                          C.RX_DEFAULT_DURATION_RATIO_DEV)
        dur_dev = _num(dur_dev, f"第 {k + 1} 段：最大时值比例误差")
        if not (0 <= dur_dev <= 1):
            raise ValueError(
                f"第 {k + 1} 段：最大时值比例误差须在 0–1 之间")
        max_abn = seg.get("max_abnormal_notes", 0)
        max_abn = _int(max_abn, f"第 {k + 1} 段：最多异常音数")
        if max_abn < 0:
            raise ValueError(f"第 {k + 1} 段：最多异常音数不能为负")
        if max_abn > len(indexes):
            raise ValueError(
                f"第 {k + 1} 段：最多异常音数不得超过谱段音数 "
                f"{len(indexes)}")
        forbid_pending = bool(seg.get("forbid_pending", False))

        out.append({
            "score_index_start": start,
            "score_index_end": end,
            "deviation_categories": cats,
            "max_onset_error_s": onset,
            "max_duration_ratio_dev": dur_dev,
            "max_abnormal_notes": max_abn,
            "forbid_pending": forbid_pending,
        })
    out.sort(key=lambda s: s["score_index_start"])
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


# ============================================================ 创建 / 重绑

def create_prescription(source_type: str, source_id: str, title: str | None,
                        segments: list[dict]) -> str:
    if source_type == "piece":
        frozen = freeze_piece_source(source_id)
    elif source_type == "comparison":
        frozen = freeze_comparison_source(source_id)
    else:
        raise ValueError("source_type 必须是 piece 或 comparison")

    bundle = review.get_bundle(frozen["baseline_piece"]
                               if source_type == "comparison"
                               else source_id)
    valid_indexes = [n["index"] for n in bundle["score_notes"]]
    rules = normalize_segments(segments, valid_indexes)

    prx_id = uuid.uuid4().hex[:12]
    default_title = (f"{frozen.get('comparison_title')}"
                     if source_type == "comparison"
                     else f"《{frozen['members'][0]['title']}》")
    db.create_prescription(
        prx_id,
        title or f"{default_title} · 偏差练习处方（{len(rules)} 段）",
        source_type,
        source_id if source_type == "piece" else None,
        source_id if source_type == "comparison" else None,
        frozen["fingerprint"],
        json.dumps({"segments": rules, "params": _public_params()},
                   ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")),
        json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")))
    db.add_prescription_source_version(
        prx_id, 0, source_type,
        source_id if source_type == "piece" else None,
        source_id if source_type == "comparison" else None,
        json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")),
        note="created")
    return prx_id


def rebind_prescription(prx_id: str, source_type: str,
                        source_id: str) -> dict:
    """显式重绑过期/更换的来源：重新固化，保留全部旧复测结果。"""
    prx = db.get_prescription(prx_id)
    if not prx:
        raise ValueError("处方不存在")
    if prx["status"] == STATUS_VOIDED:
        raise ValueError("已作废的处方不能重绑")
    if source_type == "piece":
        frozen = freeze_piece_source(source_id)
    elif source_type == "comparison":
        frozen = freeze_comparison_source(source_id)
    else:
        raise ValueError("source_type 必须是 piece 或 comparison")
    if frozen["fingerprint"] != prx["fingerprint"]:
        raise IncompatibleError(
            INCOMPAT_FINGERPRINT,
            "重绑来源的乐谱指纹与处方不一致（非同曲），拒绝重绑",
            {"expected": prx["fingerprint"],
             "actual": frozen["fingerprint"]})
    versions = db.get_prescription_source_versions(prx_id)
    ord_ = (versions[-1]["ord"] + 1) if versions else 1
    frozen_js = json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    db.update_prescription_frozen(prx_id, frozen_js)
    db.add_prescription_source_version(
        prx_id, ord_, source_type,
        source_id if source_type == "piece" else None,
        source_id if source_type == "comparison" else None,
        frozen_js, note="rebind")
    return get_prescription(prx_id)


def void_prescription(prx_id: str) -> dict:
    prx = db.get_prescription(prx_id)
    if not prx:
        raise ValueError("处方不存在")
    if prx["status"] == STATUS_VOIDED:
        return get_prescription(prx_id)
    db.set_prescription_status(prx_id, STATUS_VOIDED, voided=True)
    return get_prescription(prx_id)


# ============================================================ 复测

def _target_snapshot(piece_id: str) -> dict:
    """复测目标当前已确认对齐版本 -> 与对比快照同构的按谱音结果。"""
    return cmp_service.build_snapshot(piece_id)


def check_compatible(prx: dict, target_piece_id: str) -> dict:
    """复测准入：把「处方来源是否过期」与「目标当次对齐是否可用」分开校验。

    1. 处方作废 -> :class:`IncompatibleError`（prescription_voided）；
    2. **处方来源**固化成员任一相对实况过期 -> :class:`StaleSourceError`，
       无论目标是不是固化成员，都必须先显式重绑；
    3. **目标当次**：须已完成分析、存在当前已确认对齐版本、谱指纹一致。
       同曲新目标（从未列入固化成员）在此直接放行——复测时会把它的当次
       对齐版本/音频哈希**当场固化**进复测记录，无需重绑来源。

    返回目标当前快照（按谱音，含当次对齐版本与音频哈希）。
    """
    if prx["status"] == STATUS_VOIDED:
        raise IncompatibleError(
            INCOMPAT_PRESCRIPTION_VOIDED, "处方已作废，不能再提交复测")
    piece = db.get_piece(target_piece_id)
    if not piece:
        raise ValueError("复测录音不存在")
    if piece["status"] != "analyzed":
        raise ValueError("复测录音尚未完成分析")

    # 校验一：处方来源是否过期（与目标是谁无关，固化成员任一过期即拦截）
    frozen = json.loads(prx["frozen_json"])
    stale = source_stale(frozen)
    if stale:
        raise StaleSourceError(
            "处方来源已过期（对齐版本/音频与固化时不一致）："
            "旧复测结果保留，请先显式重绑处方来源后再复测",
            stale)

    # 校验二：目标当次对齐是否可用
    align = db.get_latest_alignment(target_piece_id)
    if not align:
        raise IncompatibleError(
            INCOMPAT_NO_ALIGNMENT,
            f"《{piece['title']}》尚无已确认对齐版本，不能复测："
            "请先完成分析并确认对齐结果",
            {"target_piece_id": target_piece_id})
    bundle = review.get_bundle(target_piece_id)
    fingerprint = cmp_service.score_fingerprint(bundle["score_notes"])
    if fingerprint != prx["fingerprint"]:
        raise IncompatibleError(
            INCOMPAT_FINGERPRINT,
            f"乐谱指纹不一致：《{piece['title']}》与处方不是同曲"
            "（或反复展开结果不同），拒绝复测",
            {"expected": prx["fingerprint"], "actual": fingerprint})

    snap = _target_snapshot(target_piece_id)
    return snap


def run_retest(prx_id: str, target_piece_id: str) -> dict:
    """对同曲、兼容的目标录音执行一次确定性复测并持久化。

    复测记录**当场固化**：目标当次对齐版本、目标音频哈希、逐项判定结果，
    以及所依据的处方来源固化版本（source_version_id + 版本摘要）。此后
    来源或目标再变化只会产生新的复测，绝不改写本条记录。
    """
    prx = db.get_prescription(prx_id)
    if not prx:
        raise ValueError("处方不存在")
    snap = check_compatible(prx, target_piece_id)
    align = db.get_latest_alignment(target_piece_id)
    rules = json.loads(prx["rules_json"])
    result = evaluate(rules["segments"], snap)
    result["params"] = rules["params"]

    ord_ = db.next_retest_ord(prx_id)
    retest_id = uuid.uuid4().hex[:12]
    versions = db.get_prescription_source_versions(prx_id)
    source_row = versions[-1] if versions else None
    source_version_id = source_row["id"] if source_row else None
    source_frozen = json.loads(source_row["frozen_json"]) if source_row \
        else json.loads(prx["frozen_json"])
    result_full = {
        "format": "deviation-prescription-retest",
        "algorithm_version": C.RX_ALGORITHM_VERSION,
        "prescription_id": prx_id,
        "fingerprint": prx["fingerprint"],
        "target": {
            "piece_id": target_piece_id,
            "title": snap["title"],
            "alignment_version": align["version"],
            "wav_sha256": snap["wav_sha256"],
            "musicxml_sha256": snap.get("musicxml_sha256"),
        },
        # 所依据的来源版本：行 id 外键 + 内容摘要双固化；
        # 之后显式重绑/目标重算都不会回写本条结果。
        "source_basis": {
            "source_version_id": source_version_id,
            "source_version_ord": source_row["ord"] if source_row else None,
            "source_type": source_frozen["source_type"],
            "fingerprint": source_frozen["fingerprint"],
            "members": [{
                "piece_id": m["piece_id"],
                "alignment_version": m["alignment_version"],
                "wav_sha256": m.get("wav_sha256"),
            } for m in source_frozen["members"]],
        },
        **result,
    }
    db.create_retest(
        retest_id, prx_id, ord_, target_piece_id, snap["title"],
        align["version"], snap["wav_sha256"], source_version_id,
        json.dumps(result_full, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")),
        result_full["overall"])
    db.set_prescription_status(prx_id, STATUS_RETESTED,
                               latest_retest_id=retest_id)
    return get_retest_detail(retest_id)


# ============================================================ 确定性判定

def _r(v, nd=C.ROUND_NDIGITS):
    return None if v is None else round(float(v), nd)


def evaluate(segments: list[dict], snap: dict) -> dict:
    """按处方谱段对目标快照逐音、逐规则计算确定性结果。

    纯函数：相同规则 + 相同快照 => 相同结果（无时间戳/随机项）。
    """
    notes_by_idx = {n["score_index"]: n for n in snap["notes"]}
    seg_results, overall = [], VERDICT_PASS
    any_fail = False
    any_und = False
    for seg in segments:
        sr = _evaluate_segment(seg, notes_by_idx)
        # 多音（不占据谱音行的额外观测）按邻近谱位归入谱段，可能翻转判定
        attach_extra_evidence(sr, seg, snap)
        seg_results.append(sr)
        if sr["verdict"] == VERDICT_FAIL:
            any_fail = True
        elif sr["verdict"] == VERDICT_UNDETERMINABLE:
            any_und = True
    if any_fail:
        overall = VERDICT_FAIL
    elif any_und:
        overall = VERDICT_UNDETERMINABLE

    return {"overall": overall, "segments": seg_results}


def _evaluate_segment(seg, notes_by_idx):
    start, end = seg["score_index_start"], seg["score_index_end"]
    cats = set(seg["deviation_categories"])
    evidences = []
    und_indexes = set()
    und_reasons = {}

    # ---- 逐谱音证据 ----
    for idx in range(start, end + 1):
        note = notes_by_idx.get(idx)
        ev, und = _classify_evidence(idx, note, seg)
        evidences.append(ev)
        if und:
            und_indexes.add(idx)
            und_reasons.setdefault(idx, und)

    # ---- 规则逐项（实际值 / 阈值 / 证据 / 失败原因）----
    rules_out, rule_failed = [], False

    def add_rule(key, label, actual, threshold, violated, evidence, reason):
        nonlocal rule_failed
        passed = not violated
        if violated:
            rule_failed = True
        rules_out.append({
            "rule": key,
            "label": label,
            "actual": actual,
            "threshold": threshold,
            "passed": passed,
            "verdict": VERDICT_FAIL if violated else VERDICT_PASS,
            "evidence_score_indexes": evidence,
            "failure_reason": None if passed else reason,
        })

    # 1) 必须消除的偏差类别：每类实际异常音数（阈值 0）
    for cat in seg["deviation_categories"]:
        ev_idx = [e["score_index"] for e in evidences
                  if cat in e["categories"]]
        add_rule(
            f"eliminate:{cat}",
            f"必须消除 {CATEGORY_LABELS.get(cat, cat)}",
            {"count": len(ev_idx)},
            {"max_count": 0},
            bool(ev_idx), ev_idx,
            f"{CATEGORY_LABELS.get(cat, cat)}仍出现 {len(ev_idx)} 处"
            if ev_idx else None)

    # 2) 最大起音误差（仅声明 onset_offset 时生效）
    if "onset_offset" in cats:
        over = [e for e in evidences
                if e["onset_offset_s"] is not None
                and abs(e["onset_offset_s"]) > seg["max_onset_error_s"]]
        worst = max((abs(e["onset_offset_s"]) for e in evidences
                     if e["onset_offset_s"] is not None), default=None)
        add_rule(
            "max_onset_error",
            "最大起音误差",
            {"max_abs_onset_offset_s": _r(worst, 4),
             "over_count": len(over)},
            {"max_abs_onset_offset_s": seg["max_onset_error_s"]},
            bool(over), [e["score_index"] for e in over],
            "起音偏差超阈："
            + "、".join(f"#{e['score_index']}({_r(abs(e['onset_offset_s']),3)}s)"
                       for e in over[:8])
            if over else None)

    # 3) 最大时值误差（仅声明 duration_deviation 时生效）
    if "duration_deviation" in cats:
        over = [e for e in evidences
                if e["duration_ratio"] is not None
                and abs(e["duration_ratio"] - 1.0)
                > seg["max_duration_ratio_dev"]]
        worst = max((abs(e["duration_ratio"] - 1.0) for e in evidences
                     if e["duration_ratio"] is not None), default=None)
        add_rule(
            "max_duration_error",
            "最大时值比例误差",
            {"max_abs_ratio_dev": _r(worst, 4), "over_count": len(over)},
            {"max_abs_ratio_dev": seg["max_duration_ratio_dev"]},
            bool(over), [e["score_index"] for e in over],
            "时值偏差超阈："
            + "、".join(f"#{e['score_index']}(Δ{_r(abs(e['duration_ratio']-1),3)})"
                       for e in over[:8])
            if over else None)

    # 4) 最多异常音数（声明类别的异常音总数；未声明类别不计入）
    abnormal_idx = [e["score_index"] for e in evidences
                    if cats.intersection(e["categories"])]
    add_rule(
        "max_abnormal_notes",
        "最多异常音数",
        {"count": len(abnormal_idx),
         "by_category": {c: sum(1 for e in evidences if c in e["categories"])
                         for c in seg["deviation_categories"]}},
        {"max_count": seg["max_abnormal_notes"]},
        len(abnormal_idx) > seg["max_abnormal_notes"], abnormal_idx,
        f"声明类别异常音 {len(abnormal_idx)} 个，超过上限 "
        f"{seg['max_abnormal_notes']}"
        if len(abnormal_idx) > seg["max_abnormal_notes"] else None)

    # 5) 是否禁止待确认
    pending_idx = sorted(i for i, r in und_reasons.items()
                         if r == UND_PENDING)
    if seg["forbid_pending"]:
        add_rule(
            "forbid_pending",
            "禁止待确认",
            {"pending_count": len(pending_idx)},
            {"max_pending": 0},
            bool(pending_idx), pending_idx,
            f"存在 {len(pending_idx)} 个未处置待确认/歧义谱音"
            if pending_idx else None)

    # ---- 谱段总判定 ----
    if rule_failed:
        verdict = VERDICT_FAIL
    elif und_indexes:
        verdict = VERDICT_UNDETERMINABLE
    else:
        verdict = VERDICT_PASS

    coord = _segment_coord(start, end, notes_by_idx)
    return {
        "score_index_start": start,
        "score_index_end": end,
        "measure_start": coord["measure_start"],
        "measure_end": coord["measure_end"],
        "occurrence": coord["occurrence"],
        "rule": seg,
        "verdict": verdict,
        "rules": rules_out,
        "undeterminable": [
            {"score_index": i, "reason": und_reasons[i]}
            for i in sorted(und_indexes)],
        "evidence": evidences,
    }


def _segment_coord(start, end, notes_by_idx):
    a = notes_by_idx.get(start) or notes_by_idx.get(end)
    b = notes_by_idx.get(end) or notes_by_idx.get(start)
    return {
        "measure_start": a.get("measure") if a else None,
        "measure_end": b.get("measure") if b else None,
        "occurrence": (a.get("occurrence", 0) if a else 0),
    }


def _classify_evidence(idx, note, seg):
    """单个谱音 -> (证据 dict, 不可判定原因或 None)。"""
    base = {"score_index": idx,
            "measure": note.get("measure") if note else None,
            "beat_in_measure": note.get("beat_in_measure") if note else None,
            "occurrence": note.get("occurrence", 0) if note else 0,
            "midi": note.get("midi") if note else None}
    if note is None:
        base.update({"categories": [], "status": None, "op": None,
                     "confidence": None, "onset_s": None,
                     "onset_offset_s": None, "duration_ratio": None,
                     "obs_index": None, "pitch_delta": None})
        return base, UND_NO_NOTE

    base.update({
        "status": note.get("status"),
        "op": note.get("op"),
        "confidence": note.get("confidence"),
        "obs_index": note.get("obs_index"),
        "onset_s": note.get("onset_s"),
        "onset_offset_s": note.get("onset_offset_s"),
        "duration_ratio": note.get("duration_ratio"),
        "pitch_delta": note.get("pitch_delta"),
    })

    # 未处置待确认阴影 -> 不可判定（若处方禁止则另由规则判失败）
    if note.get("shadow_pending"):
        base["categories"] = []
        base["pending_reason"] = note.get("shadow_pending_reason")
        base["pending_obs_index"] = note.get("shadow_pending_obs")
        return base, UND_PENDING

    # 用户排除的谱音 -> 不可判定
    if note.get("op") == "excluded_score" or note.get("status") == "excluded":
        base["categories"] = []
        return base, UND_EXCLUDED

    cats = []
    # 漏音
    if note.get("status") == "missing":
        cats.append("missing")
    # 错音
    if note.get("status") == "wrong":
        cats.append("wrong_pitch")

    # 起音 / 时值：以处方阈值现场重判（阈值可逐段收紧）
    oo = note.get("onset_offset_s")
    if oo is not None and abs(float(oo)) > seg["max_onset_error_s"]:
        cats.append("onset_offset")
    dr = note.get("duration_ratio")
    if dr is not None and abs(float(dr) - 1.0) > seg["max_duration_ratio_dev"]:
        cats.append("duration_deviation")

    base["categories"] = cats

    # 已确认配对但置信度不足：类别仍然报出（供“最多异常音数”），
    # 但整段不能直接判通过 -> 不可判定。
    conf = note.get("confidence")
    if conf is None or float(conf) < C.RX_MIN_CONFIDENCE:
        return base, UND_LOW_CONF
    return base, None


# 多音证据：多音（不占据谱音行的额外观测）按邻近谱位归入所属谱段，
# 仅当该段声明了 extra 类别时影响判定；它们作为额外证据行追加。
def attach_extra_evidence(seg_result: dict, seg: dict, snap: dict) -> None:
    s, e = seg_result["score_index_start"], seg_result["score_index_end"]
    extras = [x for x in snap.get("extras", [])
              if x.get("near_score_index") is not None
              and s <= x["near_score_index"] <= e]
    seg_result["extra_observations"] = [{
        "obs_index": x["obs_index"],
        "near_score_index": x["near_score_index"],
        "onset_s": x.get("onset_s"),
        "midi": x.get("midi"),
    } for x in extras]
    if "extra" not in seg["deviation_categories"] or not extras:
        return
    extra_idx = [x["near_score_index"] for x in extras]
    extra_failed = False
    for rule in seg_result["rules"]:
        if rule["rule"] == "eliminate:extra":
            rule["actual"]["count"] = len(extras)
            rule["actual"]["extra_observations"] = len(extras)
            rule["passed"] = False
            rule["verdict"] = VERDICT_FAIL
            rule["evidence_score_indexes"] = sorted(
                set(rule["evidence_score_indexes"] + extra_idx))
            rule["failure_reason"] = f"出现 {len(extras)} 个额外音（多音）"
            extra_failed = True
        elif rule["rule"] == "max_abnormal_notes":
            bc = rule["actual"]["by_category"]
            bc["extra"] = bc.get("extra", 0) + len(extras)
            rule["actual"]["count"] += len(extras)
            rule["evidence_score_indexes"] = sorted(
                set(rule["evidence_score_indexes"] + extra_idx))
            if rule["actual"]["count"] > rule["threshold"]["max_count"]:
                rule["passed"] = False
                rule["verdict"] = VERDICT_FAIL
                rule["failure_reason"] = (
                    f"声明类别异常音 {rule['actual']['count']} 个，超过上限 "
                    f"{rule['threshold']['max_count']}")
                extra_failed = True
    if extra_failed:
        seg_result["verdict"] = VERDICT_FAIL


CATEGORY_LABELS = {
    "wrong_pitch": "错音",
    "missing": "漏音",
    "extra": "多音",
    "onset_offset": "起音偏移",
    "duration_deviation": "时值偏差",
}


# ============================================================ 读取

def _timeline(prx: dict) -> list[dict]:
    """处方时间线：创建 / 历次复测 / 重绑 / 作废，统一按时间排序。"""
    events = [{
        "type": "created", "at": prx["created_at"],
        "text": "创建处方（固化谱指纹、对齐版本与规则）",
    }]
    for v in db.get_prescription_source_versions(prx["id"]):
        if v["ord"] == 0:
            continue
        frozen = json.loads(v["frozen_json"])
        if frozen["source_type"] == "comparison":
            ref = frozen.get("comparison_title", frozen["comparison_id"])
        else:
            ref = frozen["members"][0]["title"]
        events.append({
            "type": "rebind", "at": v["created_at"], "ord": v["ord"],
            "text": f"显式重绑来源（{ref}），旧复测结果保留",
        })
    for r in db.list_retests(prx["id"]):
        events.append({
            "type": "retest", "at": r["created_at"], "ord": r["ord"],
            "retest_id": r["id"], "overall": r["overall"],
            "text": f"第 {r['ord']} 次复测：《{r['target_title']}》"
                    f"（对齐 v{r['target_version']}）→ "
                    + {"pass": "通过", "fail": "未通过",
                       "undeterminable": "不可判定"}[r["overall"]],
        })
    if prx["status"] == STATUS_VOIDED and prx["voided_at"]:
        events.append({"type": "voided", "at": prx["voided_at"],
                       "text": "处方已作废"})
    events.sort(key=lambda x: x["at"])
    return events


def get_prescription(prx_id: str) -> dict:
    prx = db.get_prescription(prx_id)
    if not prx:
        raise ValueError("处方不存在")
    frozen = json.loads(prx["frozen_json"])
    rules = json.loads(prx["rules_json"])
    stale = source_stale(frozen)
    retests = [_retest_brief(r) for r in db.list_retests(prx_id)]
    return {
        "id": prx["id"],
        "title": prx["title"],
        "status": prx["status"],
        "source_type": prx["source_type"],
        "source_piece_id": prx["source_piece_id"],
        "comparison_id": prx["comparison_id"],
        "fingerprint": prx["fingerprint"],
        "segments": rules["segments"],
        "params": rules["params"],
        "frozen": _frozen_public(frozen),
        "source_stale": bool(stale),
        "source_stale_members": stale,
        "created_at": prx["created_at"],
        "voided_at": prx["voided_at"],
        "retest_count": len(retests),
        "retests": retests,
        "timeline": _timeline(prx),
    }


def _frozen_public(frozen: dict) -> dict:
    out = {"source_type": frozen["source_type"],
           "fingerprint": frozen["fingerprint"],
           "members": frozen["members"]}
    if frozen["source_type"] == "comparison":
        out["comparison_id"] = frozen["comparison_id"]
        out["comparison_title"] = frozen.get("comparison_title")
        out["baseline_piece"] = frozen.get("baseline_piece")
    return out


def _retest_brief(row: dict) -> dict:
    result = json.loads(row["result_json"])
    return {
        "id": row["id"],
        "ord": row["ord"],
        "target_piece_id": row["target_piece_id"],
        "target_title": row["target_title"],
        "target_version": row["target_version"],
        "target_wav_sha256": row["target_wav_sha256"],
        "overall": row["overall"],
        "created_at": row["created_at"],
        "segment_verdicts": [
            {"score_index_start": s["score_index_start"],
             "score_index_end": s["score_index_end"],
             "verdict": s["verdict"]}
            for s in result["segments"]],
    }


def get_retest_detail(retest_id: str) -> dict:
    row = db.get_retest(retest_id)
    if not row:
        raise ValueError("复测不存在")
    result = json.loads(row["result_json"])
    return {
        "id": row["id"],
        "prescription_id": row["prescription_id"],
        "ord": row["ord"],
        "target_piece_id": row["target_piece_id"],
        "target_title": row["target_title"],
        "target_version": row["target_version"],
        "target_wav_sha256": row["target_wav_sha256"],
        "overall": row["overall"],
        "created_at": row["created_at"],
        "result": result,
    }


def list_prescriptions() -> list[dict]:
    out = []
    for prx in db.list_prescriptions():
        frozen = json.loads(prx["frozen_json"])
        stale = source_stale(frozen)
        latest = db.get_retest(prx["latest_retest_id"]) \
            if prx["latest_retest_id"] else None
        out.append({
            "id": prx["id"],
            "title": prx["title"],
            "status": prx["status"],
            "source_type": prx["source_type"],
            "source_piece_id": prx["source_piece_id"],
            "comparison_id": prx["comparison_id"],
            "fingerprint": prx["fingerprint"][:16],
            "segment_count": len(json.loads(prx["rules_json"])["segments"]),
            "source_stale": bool(stale),
            "source_stale_members": stale,
            "retest_count": len(db.list_retests(prx["id"])),
            "latest_overall": latest["overall"] if latest else None,
            "created_at": prx["created_at"],
            "voided_at": prx["voided_at"],
        })
    return out


def candidate_targets(prx_id: str) -> list[dict]:
    """列出可用于复测的同曲录音及其准入状态（供前端选择）。

    每条候选同时给出两个互相独立的状态：
    * ``source_stale``：**处方来源**是否有任一固化成员过期（全局，所有候选
      相同；过期时必须先显式重绑）；若是冻结成员本身过期，该候选还带
      ``member_stale`` 标记；
    * 目标当次是否可复测：已完成分析、谱指纹一致、有已确认对齐版本。
      同曲新目标（从未列入固化成员）``compatible=true``，**不需要重绑**。
    """
    prx = db.get_prescription(prx_id)
    if not prx:
        raise ValueError("处方不存在")
    frozen = json.loads(prx["frozen_json"])
    frozen_ids = {m["piece_id"] for m in frozen["members"]}
    stale = source_stale(frozen)
    stale_map = {s["piece_id"]: s["reasons"] for s in stale}
    source_stale_flag = bool(stale)
    out = []
    for p in db.list_pieces():
        piece = db.get_piece(p["id"])
        if piece["status"] != "analyzed":
            continue
        bundle = review.get_bundle(p["id"])
        fp = cmp_service.score_fingerprint(bundle["score_notes"])
        if fp != prx["fingerprint"]:
            continue
        align = db.get_latest_alignment(p["id"])
        member_reasons = stale_map.get(p["id"])
        if not align:
            compatible = False
            reason = CANDIDATE_NO_ALIGNMENT
        elif source_stale_flag:
            # 来源任一固化成员过期：一律先重绑；成员自身过期的候选标明
            compatible = False
            reason = (CANDIDATE_MEMBER_STALE if member_reasons
                      else CANDIDATE_SOURCE_STALE)
        else:
            # 来源新鲜：同曲目标（含从未固化的新目标）凭当次对齐即可复测
            compatible = True
            reason = None
        out.append({
            "piece_id": p["id"],
            "title": p["title"],
            "current_version": align["version"] if align else None,
            "frozen_version": next(
                (m["alignment_version"] for m in frozen["members"]
                 if m["piece_id"] == p["id"]), None),
            "in_frozen_source": p["id"] in frozen_ids,
            "source_stale": source_stale_flag,
            "member_stale_reasons": member_reasons,
            "compatible": compatible,
            "reason": reason,
        })
    return out

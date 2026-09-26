"""复盘编排：锚点状态 -> 增量重算 -> 结果持久化 / bundle / 确定性导出。"""

from __future__ import annotations

import hashlib
import json

from .. import config as C
from .. import db
from . import aligner

# 进程内片段 DP 缓存（按 piece 隔离），配合 DB 中锚点实现增量重算。
_REGION_CACHES: dict[str, dict] = {}


def _cache_for(piece_id: str):
    return _REGION_CACHES.setdefault(piece_id, {})


def invalidate(piece_id: str):
    _REGION_CACHES.pop(piece_id, None)


# ------------------------------------------------------------ 重算

def recompute(piece_id: str) -> int:
    """按当前锚点集合重算并持久化新版本，返回 version。"""
    s_rows = db.get_score_notes(piece_id)
    o_rows = db.get_obs_notes(piece_id)
    score_notes = [{"index": r["idx"], "midi": float(r["midi"]),
                    "beat": float(r["beat"]), "dur_q": float(r["dur_q"])}
                   for r in s_rows]
    obs_notes = [{"index": r["idx"],
                  "midi": (float(r["midi"]) if r["midi"] is not None
                           else None),
                  "onset": float(r["onset"]),
                  "duration": float(r["duration"]),
                  "state": r["state"]}
                 for r in o_rows]

    anchors = db.get_anchors(piece_id)
    locks, forced, excl_s, excl_o = [], [], set(), set()
    for a in anchors:
        if a["kind"] == "lock":
            locks.append((a["score_index"], a["obs_index"]))
        elif a["kind"] == "force_lock":
            # 用户在待确认中显式确认的歧义配对（可含 poly/silent 观测）
            forced.append((a["score_index"], a["obs_index"]))
        elif a["kind"] == "exclude_score":
            excl_s.add(a["score_index"])
        elif a["kind"] == "exclude_obs":
            excl_o.add(a["obs_index"])

    result = aligner.align(score_notes, obs_notes,
                           locks=locks,
                           forced_locks=forced,
                           excluded_score=excl_s,
                           excluded_obs=excl_o,
                           cache=_cache_for(piece_id))
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return db.save_alignment(piece_id, payload)


# ------------------------------------------------------------ bundle

def get_bundle(piece_id: str):
    piece = db.get_piece(piece_id)
    if not piece:
        return None
    s_rows = db.get_score_notes(piece_id)
    o_rows = db.get_obs_notes(piece_id)
    anchors = db.get_anchors(piece_id)
    align_row = db.get_latest_alignment(piece_id)
    result = json.loads(align_row["result_json"]) if align_row else None
    meta = json.loads(piece["warnings_json"] or "{}")

    return {
        "piece": {
            "id": piece["id"],
            "title": piece["title"],
            "status": piece["status"],
            "error": piece["error"],
            "duration_s": piece["duration_s"],
            "audio_url": f"/files/{piece['id']}.wav"
            if piece["audio_path"] else None,
            "warnings": meta.get("warnings", []),
            "peaks": meta.get("peaks", []),
        },
        "score_notes": [_score_dto(r) for r in s_rows],
        "obs_notes": [_obs_dto(r) for r in o_rows],
        "anchors": [_anchor_dto(a) for a in anchors],
        "alignment": {
            "version": align_row["version"] if align_row else None,
            "alpha": result["alpha"] if result else None,
            "beta": result["beta"] if result else None,
            "entries": result["entries"] if result else [],
            "pending": result["pending"] if result else [],
            "tempo_curve": result["tempo_curve"] if result else [],
            "cache": result.get("cache") if result else None,
        },
    }


def _score_dto(r):
    return {
        "index": r["idx"], "midi": r["midi"], "beat": r["beat"],
        "dur_q": r["dur_q"], "measure": r["measure"],
        "beat_in_measure": r["beat_in_measure"],
        "occurrence": r["occurrence"], "part": r["part"],
    }


def _obs_dto(r):
    return {
        "index": r["idx"], "midi": r["midi"], "onset": r["onset"],
        "duration": r["duration"], "state": r["state"],
        "voiced_ratio": r["voiced_ratio"],
        "poly_ratio": r["poly_ratio"],
        "onset_strength": r["onset_strength"],
    }


def _anchor_dto(a):
    return {
        "id": a["id"], "kind": a["kind"],
        "score_index": a["score_index"],
        "obs_index": a["obs_index"], "note": a["note"],
    }


# ------------------------------------------------------------ 待确认处置

def resolve_pending(piece_id: str, obs_index: int, action: str,
                    score_index=None, note=""):
    """处置歧义观测：
    action=match   -> 锁定到指定乐谱音（转为 lock 锚点）；
    action=extra   -> 确认为额外音（exclude_obs 语义：不配对但保留记录）；
    action=ignore  -> 排除该观测。
    """
    o_rows = {r["idx"]: r for r in db.get_obs_notes(piece_id)}
    if obs_index not in o_rows:
        raise ValueError("观测下标不存在")

    if action == "match":
        if score_index is None:
            raise ValueError("match 处置必须提供 score_index")
        s_rows = {r["idx"]: r for r in db.get_score_notes(piece_id)}
        if score_index not in s_rows:
            raise ValueError("乐谱下标不存在")
        db.add_anchor(piece_id, "force_lock", int(score_index),
                      int(obs_index), note or "resolved_pending")
    elif action in ("extra", "ignore"):
        # extra/ignore 都令该观测不参与自动配对；区别写入 note 供导出说明
        db.add_anchor(piece_id, "exclude_obs", None, int(obs_index),
                      note or f"resolved_pending:{action}")
    else:
        raise ValueError(f"未知处置动作 {action}")


# ------------------------------------------------------------ 确定性导出

def build_export(piece_id: str) -> dict:
    bundle = get_bundle(piece_id)
    if not bundle:
        raise ValueError("乐曲不存在")
    piece = bundle["piece"]

    xml_hash = _file_hash(_full_path(piece["id"], ".xml"))
    wav_hash = _file_hash(_full_path(piece["id"], ".wav"))

    s_by_i = {n["index"]: n for n in bundle["score_notes"]}
    o_by_i = {n["index"]: n for n in bundle["obs_notes"]}

    note_results = []
    for e in bundle["alignment"]["entries"]:
        sn = s_by_i.get(e["score_index"])
        on = o_by_i.get(e["obs_index"])
        note_results.append({
            "op": e["op"],
            "status": e["status"],
            "tags": e["tags"],
            "confidence": e["confidence"],
            "score": None if sn is None else {
                "index": sn["index"],
                "midi": _r(sn["midi"]),
                "measure": sn["measure"],
                "beat_in_measure": sn["beat_in_measure"],
                "occurrence": sn["occurrence"],
                "beat_expanded": _r(sn["beat"]),
                "duration_quarters": _r(sn["dur_q"]),
            },
            "audio": None if on is None else {
                "index": on["index"],
                "midi": _r(on["midi"]),
                "onset_s": _r(on["onset"]),
                "duration_s": _r(on["duration"]),
                "state": on["state"],
                "voiced_ratio": on["voiced_ratio"],
                "poly_ratio": on["poly_ratio"],
            },
            "metrics": {
                "pitch_delta_semitones": e["pitch_delta"],
                "onset_offset_s": e["onset_offset"],
                "predicted_onset_s": e["predicted_onset"],
                "expected_duration_s": e["expected_duration_s"],
                "duration_ratio": e["duration_ratio"],
                "duration_deviation_s": e["duration_deviation_s"],
            },
            "basis": e["basis"],
        })

    stats = _stats(note_results)

    return {
        "format": "score-audio-review",
        "algorithm_version": C.ALGORITHM_VERSION,
        "params": _params_snapshot(),
        "piece": {
            "id": piece["id"],
            "title": piece["title"],
            "alignment_version": bundle["alignment"]["version"],
            "audio_duration_s": piece["duration_s"],
            "warnings": piece["warnings"],
        },
        "inputs": {
            "musicxml_sha256": xml_hash,
            "wav_sha256": wav_hash,
        },
        "tempo": {
            "alpha_seconds_per_quarter": _r(bundle["alignment"]["alpha"]),
            "beta_offset_s": _r(bundle["alignment"]["beta"]),
            "local_curve": bundle["alignment"]["tempo_curve"],
        },
        "anchors": bundle["anchors"],
        "pending_confirmation": bundle["alignment"]["pending"],
        "note_results": note_results,
        "summary": stats,
    }


def _params_snapshot():
    return {
        "pitch_per_semitone": C.COST_PITCH_PER_SEMI,
        "pitch_max": C.COST_PITCH_MAX,
        "pitch_match_max_semitones": C.PITCH_MATCH_MAX_SEMI,
        "delete_cost": C.COST_DELETE,
        "insert_cost": C.COST_INSERT,
        "insert_pending_cost": C.COST_INSERT_PENDING,
        "onset_per_sec": C.COST_ONSET_PER_SEC,
        "onset_max": C.COST_ONSET_MAX,
        "pitch_wrong_semitones": C.PITCH_WRONG_SEMITONES,
        "onset_offset_s": C.ONSET_OFFSET_S,
        "duration_ratio_low": C.DURATION_RATIO_LOW,
        "duration_ratio_high": C.DURATION_RATIO_HIGH,
        "voiced_ratio_silent": C.VOICED_RATIO_SILENT,
        "poly_ratio_ambig": C.POLY_RATIO_AMBIG,
    }


def _stats(note_results):
    def count(status):
        return sum(1 for n in note_results if n["status"] == status)
    total_score = sum(1 for n in note_results if n["score"] is not None)
    return {
        "score_notes_total": total_score,
        "correct": count("correct"),
        "wrong_pitch": count("wrong"),
        "missing": count("missing"),
        "extra": count("extra"),
        "onset_offset": sum(1 for n in note_results
                            if "onset_offset" in n["tags"]),
        "duration_deviation": sum(1 for n in note_results
                                  if "duration_deviation" in n["tags"]),
        "pending": count("pending"),
        "excluded": count("excluded"),
    }


def _r(v, nd=C.ROUND_NDIGITS):
    return None if v is None else round(float(v), nd)


def _full_path(piece_id, suffix):
    import os
    base = os.environ.get("DATA_DIR", "/data")
    return os.path.join(base, "files", f"{piece_id}{suffix}")


def _file_hash(path):
    import os
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

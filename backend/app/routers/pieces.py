"""乐曲相关 API：上传、状态轮询、复盘 bundle、锚点、待确认、导出。"""

from __future__ import annotations

import json
import os
import threading
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from .. import db
from ..services import audio_analyzer, review, score_parser

router = APIRouter(prefix="/api")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
FILES_DIR = os.path.join(DATA_DIR, "files")

# 后台分析任务（单进程部署；状态同样落库，重启后可从 pieces.status 恢复）
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# ------------------------------------------------------------ 上传/分析

@router.post("/pieces")
async def create_piece(
    title: str = Form(...),
    musicxml: UploadFile = File(...),
    audio: UploadFile = File(...),
):
    piece_id = uuid.uuid4().hex[:12]
    os.makedirs(FILES_DIR, exist_ok=True)
    xml_path = os.path.join(FILES_DIR, f"{piece_id}.xml")
    wav_path = os.path.join(FILES_DIR, f"{piece_id}.wav")
    _save_upload(musicxml, xml_path, allowed=(".xml", ".musicxml", ".mxl"))
    _save_upload(audio, wav_path, allowed=(".wav",))

    db.create_piece(piece_id, title, xml_path)

    # 先同步解析谱面（快，且错误要立即返回给上传者）
    try:
        score_notes, warnings = score_parser.parse_musicxml(xml_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"MusicXML 解析失败：{exc}") from exc

    with _jobs_lock:
        _jobs[piece_id] = {"status": "running", "error": None,
                           "warnings": warnings, "score_notes": score_notes}
    threading.Thread(target=_analyze_worker,
                     args=(piece_id, wav_path, score_notes, warnings),
                     daemon=True).start()
    return {"id": piece_id, "status": "running"}


def _save_upload(up: UploadFile, dest: str, allowed):
    name = (up.filename or "").lower()
    if not name.endswith(allowed):
        raise HTTPException(400, f"{name} 类型不被接受，允许：{allowed}")
    with open(dest, "wb") as f:
        f.write(up.file.read())


def _analyze_worker(piece_id, wav_path, score_notes, warnings):
    try:
        obs_notes, duration, peaks, audio_warnings = audio_analyzer.analyze_audio(
            wav_path)
        all_warnings = warnings + audio_warnings
        db.save_analysis(piece_id, wav_path, duration, score_notes,
                         obs_notes, all_warnings, peaks)
        # 自动首次对齐
        version = review.recompute(piece_id)
        with _jobs_lock:
            _jobs[piece_id] = {"status": "done", "error": None,
                                "version": version}
    except Exception as exc:  # noqa: BLE001
        with db.get_conn() as conn:
            conn.execute("UPDATE pieces SET status='failed', error=? "
                         "WHERE id=?", (str(exc), piece_id))
        with _jobs_lock:
            _jobs[piece_id] = {"status": "failed", "error": str(exc)}


@router.get("/jobs/{piece_id}")
def job_status(piece_id: str):
    with _jobs_lock:
        job = _jobs.get(piece_id)
    piece = db.get_piece(piece_id)
    if not piece:
        raise HTTPException(404, "乐曲不存在")
    if job:
        return {"status": job["status"], "error": job["error"]}
    # 进程重启后回落到库状态
    return {"status": "done" if piece["status"] == "analyzed"
            else piece["status"], "error": piece["error"]}


# ------------------------------------------------------------ 浏览

@router.get("/pieces")
def list_pieces():
    return db.list_pieces()


@router.get("/pieces/{piece_id}/bundle")
def bundle(piece_id: str):
    data = review.get_bundle(piece_id)
    if not data:
        raise HTTPException(404, "乐曲不存在")
    return data


# ------------------------------------------------------------ 锚点/重算

@router.post("/pieces/{piece_id}/anchors")
def add_anchor(piece_id: str, body: dict):
    if not db.get_piece(piece_id):
        raise HTTPException(404, "乐曲不存在")
    kind = body.get("kind")
    if kind not in ("lock", "exclude_score", "exclude_obs"):
        raise HTTPException(400, "kind 必须为 lock/exclude_score/exclude_obs")
    si = body.get("score_index")
    oi = body.get("obs_index")
    if kind == "lock" and (si is None or oi is None):
        raise HTTPException(400, "lock 需要 score_index 与 obs_index")
    db.add_anchor(piece_id, kind, si, oi, body.get("note", ""))
    try:
        version = review.recompute(piece_id)
    except ValueError as exc:
        # 非法锚点（如锁定歧义观测、非单调）回滚，不留脏数据
        if kind == "lock":
            with db.get_conn() as conn:
                conn.execute(
                    "DELETE FROM anchors WHERE piece_id=? AND kind='lock' "
                    "AND score_index=? AND obs_index=?",
                    (piece_id, si, oi))
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "version": version}


@router.delete("/anchors/{anchor_id}")
def delete_anchor(anchor_id: int, piece_id: str):
    if not db.get_piece(piece_id):
        raise HTTPException(404, "乐曲不存在")
    removed = db.delete_anchor(piece_id, anchor_id)
    if not removed:
        raise HTTPException(404, "锚点不存在或不属于该乐曲")
    version = review.recompute(piece_id)
    return {"ok": True, "version": version}


@router.post("/pieces/{piece_id}/recompute")
def manual_recompute(piece_id: str):
    if not db.get_piece(piece_id):
        raise HTTPException(404, "乐曲不存在")
    return {"ok": True, "version": review.recompute(piece_id)}


# ------------------------------------------------------------ 待确认

@router.post("/pieces/{piece_id}/resolve")
def resolve_pending(piece_id: str, body: dict):
    try:
        review.resolve_pending(piece_id,
                               int(body["obs_index"]),
                               body["action"],
                               body.get("score_index"),
                               body.get("note", ""))
        version = review.recompute(piece_id)
    except KeyError as exc:
        raise HTTPException(400, f"缺少字段 {exc}") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "version": version}


# ------------------------------------------------------------ 导出

@router.get("/pieces/{piece_id}/export")
def export(piece_id: str):
    try:
        payload = review.build_export(piece_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    # sort_keys + 固定分隔符 + 无空白差异 => 字节级确定性 JSON
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     indent=2, separators=(",", ": "))
    fname = f"review_{piece_id}_v{payload['piece']['alignment_version']}.json"
    return Response(
        content=raw,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

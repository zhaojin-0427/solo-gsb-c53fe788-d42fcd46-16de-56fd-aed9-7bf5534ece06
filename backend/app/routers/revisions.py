"""谱面修订迁移 API。

  POST /api/pieces/{piece_id}/revisions
       上传修订版**单声部** MusicXML，展开反复后生成确定性迁移预览
       （保留/移动/修改/删除/新增/无法匹配 + 逐项继承判定）；
       多声部歧义 -> 422 polyphony_ambiguous；含匹配冲突的预览照样保存，
       但 can_commit=false。
  GET  /api/revisions[?piece_id=...]   谱面修订记录列表
  GET  /api/revisions/{id}             迁移预览 / 已提交记录详情
  POST /api/revisions/{id}/commit      用户确认：创建新谱面修订记录与供后续
                                       录音使用的复盘入口（request_id 幂等）；
                                       匹配冲突 / 来源已变化 -> 422 阻止提交
  DELETE /api/revisions/{id}           仅可删除未确认预览（committed 只读）
  GET  /api/revisions/{id}/report      导出确定性迁移报告 JSON
  GET  /api/revisions/{id}/revised-musicxml  下载该修订版 MusicXML
  POST /api/revisions/{id}/audio       为新复盘入口上传后续 WAV（后台分析）
  GET  /api/pieces/{piece_id}/revision-context 新入口的接续模板清单
  POST /api/pieces/{piece_id}/carryover 分析完成后显式一键接续处方/练习谱
"""

from __future__ import annotations

import json
import os
import shutil

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from .. import db
from ..services import practice_score as ps
from ..services import revision as rv

router = APIRouter(prefix="/api")


def _get_or_404(revision_id: str):
    try:
        return rv.get_revision(revision_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _blocked(exc: rv.CommitBlockedError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc), "reason": "migration_blocked",
                 "blockers": exc.blockers})


# ------------------------------------------------------------ 上传 / 预览

@router.post("/pieces/{piece_id}/revisions")
async def create_revision(piece_id: str,
                          musicxml: UploadFile = File(...),
                          title: str | None = Form(None)):
    if not db.get_piece(piece_id):
        raise HTTPException(404, "乐曲不存在")
    name = (musicxml.filename or "").lower()
    if not name.endswith((".xml", ".musicxml", ".mxl")):
        raise HTTPException(400, "只接受 .xml/.musicxml/.mxl 修订谱")
    data = await musicxml.read()
    try:
        rev = rv.create_revision(piece_id, data, title)
    except rv.PolyphonyError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "reason": rv.BLK_POLYPHONY,
                     "polyphony": exc.details})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return rev


# ------------------------------------------------------------ 列表 / 详情

@router.get("/revisions")
def list_revisions(piece_id: str | None = None):
    return rv.list_revisions(piece_id)


@router.get("/revisions/{revision_id}")
def get_revision(revision_id: str):
    return _get_or_404(revision_id)


# ------------------------------------------------------------ 确认提交

@router.post("/revisions/{revision_id}/commit")
def commit_revision(revision_id: str, body: dict | None = None):
    if not db.get_score_revision(revision_id):
        raise HTTPException(404, "谱面修订记录不存在")
    body = body or {}
    request_id = body.get("request_id")
    if not request_id or not isinstance(request_id, str):
        raise HTTPException(400, "必须提供 request_id 用于幂等提交")

    replay = db.get_revision_idempotent(request_id)
    if replay is not None:
        if replay["revision_id"] != revision_id:
            raise HTTPException(409, "request_id 已用于其他谱面修订提交")
        return JSONResponse(
            status_code=replay["status_code"],
            content=json.loads(replay["response_json"]))

    try:
        data = rv.commit_revision(revision_id)
    except rv.CommitBlockedError as exc:
        resp = _blocked(exc)
        db.save_revision_idempotent(
            request_id, revision_id, resp.status_code,
            resp.body.decode("utf-8"))
        return resp
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    raw = json.dumps(data, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    db.save_revision_idempotent(request_id, revision_id, 200, raw)
    return JSONResponse(content=json.loads(raw))


# ------------------------------------------------------------ 删除预览

@router.delete("/revisions/{revision_id}")
def delete_revision(revision_id: str):
    try:
        if not rv.delete_preview(revision_id):
            raise HTTPException(404, "谱面修订记录不存在")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


# ------------------------------------------------------------ 报告 / 文件

@router.get("/revisions/{revision_id}/report")
def revision_report(revision_id: str):
    try:
        payload = rv.get_report(revision_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     indent=2, separators=(",", ": "))
    return Response(
        content=raw, media_type="application/json; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="revision_{revision_id}_report.json"'})


@router.get("/revisions/{revision_id}/revised-musicxml")
def revised_musicxml(revision_id: str, download: int = 1):
    try:
        raw = rv.read_revised_musicxml(revision_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    disp = "attachment" if download else "inline"
    return Response(
        content=raw,
        media_type="application/vnd.recordare.musicxml+xml",
        headers={"Content-Disposition":
                 f'{disp}; filename="revision_{revision_id}_revised.musicxml"'})


# ------------------------------------------------------------ 新入口录音

@router.post("/revisions/{revision_id}/audio")
async def upload_revision_audio(revision_id: str,
                                audio: UploadFile = File(...)):
    row = db.get_score_revision(revision_id)
    if not row:
        raise HTTPException(404, "谱面修订记录不存在")
    if not (audio.filename or "").lower().endswith(".wav"):
        raise HTTPException(400, "只接受 WAV 录音")
    try:
        piece_id = rv.upload_revision_audio(revision_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    data_dir = os.environ.get("DATA_DIR", "/data")
    files_dir = os.path.join(data_dir, "files")
    os.makedirs(files_dir, exist_ok=True)
    wav_path = os.path.join(files_dir, f"{piece_id}.wav")
    with open(wav_path, "wb") as f:
        shutil.copyfileobj(audio.file, f)
    rv.start_audio_analysis(piece_id, wav_path)
    return {"piece_id": piece_id, "status": "running"}


# ------------------------------------------------------------ 接续模板

@router.get("/pieces/{piece_id}/revision-context")
def revision_context(piece_id: str):
    if not db.get_piece(piece_id):
        raise HTTPException(404, "乐曲不存在")
    return rv.carryover_context(piece_id)


@router.post("/pieces/{piece_id}/carryover")
def carryover(piece_id: str, body: dict):
    if not db.get_piece(piece_id):
        raise HTTPException(404, "乐曲不存在")
    kind = body.get("kind")
    source_id = body.get("source_id")
    if kind not in ("prescription", "practice_score") or not source_id:
        raise HTTPException(400,
                            "kind 必须为 prescription/practice_score 且"
                            "提供 source_id")
    try:
        return rv.apply_carryover(piece_id, kind, source_id)
    except ps.BlockedError as exc:
        # 练习谱接续时逐位准入不通过（新录音该段无已确认偏差音等）：
        # 返回与练习谱创建一致的 422 逐位原因，不生成任何新对象
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "reason": "blocked_positions",
                     "blockers": exc.blockers, "segments": exc.segment_reports})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

"""偏差片段练习谱 API（在单次复盘中按展开小节/反复轮次框选片段）。

  GET  /api/pieces/{piece_id}/practice/editor-data
       框选用数据：展开小节（measure+occurrence）、每谱位状态 / 偏差标记、
       待确认 / 已排除 / 低置信逐位提示与阈值
  POST /api/pieces/{piece_id}/practice-scores
       创建练习谱：逐位校验通过才提取整小节生成并固化为 **version 1**；
       存在阻止生成的谱位 / 无已确认偏差音的片段 -> 422 + 逐位原因
  GET  /api/practice-scores[?piece_id=...]   练习谱列表（当前版本 / 过期 /
                                             版本数 / 下载能力）
  GET  /api/practice-scores/{id}             详情（当前版本 + 练习顺序）
  POST /api/practice-scores/{id}/rebuild     显式重建（校验通过才**追加新版本**
                                             并切换当前指针；阻塞 422 时不新增
                                             不改写版本，旧稿保留可下载）
  GET  /api/practice-scores/{id}/versions    版本历史（逐版本固化来源/哈希/
                                             过期标记/下载入口）
  GET  /api/practice-scores/{id}/versions/{version}
       单个版本详情（固化清单 + 逐轮坐标速度）
  GET  /api/practice-scores/{id}/versions/{version}/musicxml
       下载 / 预览**任一历史版本**练习谱 MusicXML
  GET  /api/practice-scores/{id}/versions/{version}/manifest
       任一历史版本的确定性清单
  GET  /api/practice-scores/{id}/musicxml    当前版本下载（向后兼容入口）
  GET  /api/practice-scores/{id}/manifest    当前版本清单（向后兼容入口）
  DELETE /api/practice-scores/{id}           删除练习谱与**全部版本**文件
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from .. import db
from ..services import practice_score as ps

router = APIRouter(prefix="/api")


def _get_or_404(task_id: str):
    try:
        return ps.get_practice_score(task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _require_current_version(task_id: str):
    """(任务行, 当前版本行)；无版本时 409。"""
    row = db.get_practice_score(task_id)
    if not row:
        raise HTTPException(404, "练习谱不存在")
    vrow = db.get_current_practice_version(task_id)
    if not vrow:
        raise HTTPException(409, "练习谱尚未生成")
    return row, vrow


def _version_or_404(task_id: str, version: int):
    try:
        return ps._require_version(task_id, version)  # noqa: SLF001
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _blocked(exc: ps.BlockedError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc),
                 "reason": "blocked_positions",
                 "blockers": exc.blockers,
                 "segments": exc.segment_reports})


def _musicxml_response(task_id: str, version: int, raw: bytes,
                       download: int):
    disp = "attachment" if download else "inline"
    fname = f"practice_{task_id}_v{version}.musicxml"
    return Response(
        content=raw,
        media_type="application/vnd.recordare.musicxml+xml",
        headers={"Content-Disposition": f'{disp}; filename="{fname}"'})


# ------------------------------------------------------------ 编辑器数据

@router.get("/pieces/{piece_id}/practice/editor-data")
def editor_data(piece_id: str):
    if not db.get_piece(piece_id):
        raise HTTPException(404, "乐曲不存在")
    try:
        return ps.build_editor_data(piece_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


# ------------------------------------------------------------ 创建

@router.post("/pieces/{piece_id}/practice-scores")
def create_practice_score(piece_id: str, body: dict | None = None):
    body = body or {}
    if not db.get_piece(piece_id):
        raise HTTPException(404, "乐曲不存在")
    try:
        task_id = ps.create_practice_score(
            piece_id, body.get("title") or None,
            body.get("segments") or [])
    except ps.BlockedError as exc:
        return _blocked(exc)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _get_or_404(task_id)


# ------------------------------------------------------------ 读取

@router.get("/practice-scores")
def list_practice_scores(piece_id: str | None = None):
    return ps.list_practice_scores(piece_id)


@router.get("/practice-scores/{task_id}")
def get_practice_score(task_id: str):
    return _get_or_404(task_id)


# ------------------------------------------------------------ 显式重建

@router.post("/practice-scores/{task_id}/rebuild")
def rebuild_practice_score(task_id: str):
    if not db.get_practice_score(task_id):
        raise HTTPException(404, "练习谱不存在")
    try:
        return ps.rebuild_practice_score(task_id)
    except ps.BlockedError as exc:
        # 来源变化引入待确认/低置信等：阻止生成，不新增/不改写任何版本，
        # 旧版本（含过期稿）保留可下载
        return _blocked(exc)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ------------------------------------------------------------ 版本历史

@router.get("/practice-scores/{task_id}/versions")
def list_practice_versions(task_id: str):
    try:
        return ps.list_versions(task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/practice-scores/{task_id}/versions/{version}")
def get_practice_version(task_id: str, version: int):
    _version_or_404(task_id, version)
    try:
        return ps.get_version_detail(task_id, version)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/practice-scores/{task_id}/versions/{version}/musicxml")
def version_musicxml(task_id: str, version: int, download: int = 1):
    _version_or_404(task_id, version)
    try:
        raw = ps.read_version_musicxml(task_id, version)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _musicxml_response(task_id, version, raw, download)


@router.get("/practice-scores/{task_id}/versions/{version}/manifest")
def version_manifest(task_id: str, version: int):
    _version_or_404(task_id, version)
    try:
        payload = ps.get_version_manifest(task_id, version)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     indent=2, separators=(",", ": "))
    fname = f"practice_{task_id}_v{version}_manifest.json"
    return Response(
        content=raw, media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ------------------------------------------------------------ 下载（当前版本）

@router.get("/practice-scores/{task_id}/musicxml")
def practice_musicxml(task_id: str, download: int = 1):
    _row, vrow = _require_current_version(task_id)
    path = vrow["musicxml_path"]
    if not path or not os.path.exists(path):
        raise HTTPException(409, "练习谱尚未生成")
    with open(path, "rb") as f:
        raw = f.read()
    return _musicxml_response(task_id, vrow["version"], raw, download)


@router.get("/practice-scores/{task_id}/manifest")
def practice_manifest(task_id: str):
    _row, vrow = _require_current_version(task_id)
    payload = json.loads(vrow["manifest_json"])
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     indent=2, separators=(",", ": "))
    fname = f"practice_{task_id}_v{vrow['version']}_manifest.json"
    return Response(
        content=raw, media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ------------------------------------------------------------ 删除

@router.delete("/practice-scores/{task_id}")
def delete_practice_score(task_id: str):
    if not ps.delete_practice_score(task_id):
        raise HTTPException(404, "练习谱不存在")
    return {"ok": True}

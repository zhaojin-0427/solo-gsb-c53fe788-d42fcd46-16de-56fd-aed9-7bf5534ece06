"""最佳片段合成轨 API。

挂载在对比资源下：
  POST   /api/comparisons/{cmp_id}/synthesis        新建合成任务（规划）
  GET    /api/synthesis/{task_id}                   读取规划 / 固化结果 / 过期标记
  PUT    /api/synthesis/{task_id}/overrides         钉住 / 禁用（重新规划，失效旧 WAV）
  POST   /api/synthesis/{task_id}/rebuild           显式重建并固化 WAV + 清单
  GET    /api/synthesis/{task_id}/wav               试听 / 下载合成 WAV
  GET    /api/synthesis/{task_id}/manifest          确定性清单 JSON
  DELETE /api/synthesis/{task_id}                   删除任务与 WAV
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from .. import db
from ..services import synthesis as syn

router = APIRouter(prefix="/api")


def _get_or_404(task_id: str):
    try:
        return syn.get_task(task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _conflict(msg: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": msg, **extra})


# ------------------------------------------------------------ 新建（挂在对比下）

@router.post("/comparisons/{cmp_id}/synthesis")
def create_synthesis(cmp_id: str, body: dict | None = None):
    body = body or {}
    if not db.get_comparison(cmp_id):
        raise HTTPException(404, "对比不存在")
    try:
        task_id = syn.create_task(cmp_id, body.get("title") or None,
                                  body.get("overrides") or {})
    except syn.StaleError as exc:
        return _conflict(str(exc), stale_members=exc.stale_members)
    except ValueError as exc:
        return _conflict(str(exc))
    return _get_or_404(task_id)


# ------------------------------------------------------------ 读取

@router.get("/synthesis/{task_id}")
def get_synthesis(task_id: str):
    return _get_or_404(task_id)


@router.get("/comparisons/{cmp_id}/synthesis")
def get_synthesis_by_comparison(cmp_id: str):
    if not db.get_comparison(cmp_id):
        raise HTTPException(404, "对比不存在")
    data = syn.get_task_by_comparison(cmp_id)
    return data  # 无任务时返回 null（前端据此显示"＋ 创建合成轨"）


# ------------------------------------------------------------ 钉住 / 禁用

@router.put("/synthesis/{task_id}/overrides")
def put_overrides(task_id: str, body: dict):
    if not db.get_synthesis_task(task_id):
        raise HTTPException(404, "合成任务不存在")
    try:
        return syn.update_overrides(task_id, body)
    except syn.StaleError as exc:
        return _conflict(str(exc), stale_members=exc.stale_members)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ------------------------------------------------------------ 显式重建

@router.post("/synthesis/{task_id}/rebuild")
def rebuild_synthesis(task_id: str):
    if not db.get_synthesis_task(task_id):
        raise HTTPException(404, "合成任务不存在")
    try:
        return syn.rebuild(task_id)
    except syn.BlockedError as exc:
        # 任一区间无合格来源 -> 阻止导出，返回逐位原因
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "blocked": exc.blocked})
    except syn.StaleError as exc:
        return _conflict(str(exc), stale_members=exc.stale_members)
    except ValueError as exc:
        return _conflict(str(exc))


# ------------------------------------------------------------ WAV / 清单

@router.get("/synthesis/{task_id}/wav")
def synthesis_wav(task_id: str, download: int = 0):
    task = db.get_synthesis_task(task_id)
    if not task:
        raise HTTPException(404, "合成任务不存在")
    if task["status"] != "built" or not task["wav_path"] \
            or not os.path.exists(task["wav_path"]):
        raise HTTPException(409, "尚未固化 WAV：请先解除阻塞并显式重建")
    with open(task["wav_path"], "rb") as f:
        raw = f.read()
    disp = "attachment" if download else "inline"
    fname = f"best_take_{task_id}.wav"
    return Response(
        content=raw, media_type="audio/wav",
        headers={"Content-Disposition": f'{disp}; filename="{fname}"'})


@router.get("/synthesis/{task_id}/manifest")
def synthesis_manifest(task_id: str):
    task = db.get_synthesis_task(task_id)
    if not task:
        raise HTTPException(404, "合成任务不存在")
    if not task["manifest_json"]:
        raise HTTPException(409, "尚未固化：请先显式重建")
    payload = json.loads(task["manifest_json"])
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     indent=2, separators=(",", ": "))
    fname = f"best_take_{task_id}_manifest.json"
    return Response(
        content=raw, media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ------------------------------------------------------------ 删除

@router.delete("/synthesis/{task_id}")
def delete_synthesis(task_id: str):
    if not syn.delete_task(task_id):
        raise HTTPException(404, "合成任务不存在")
    return {"ok": True}

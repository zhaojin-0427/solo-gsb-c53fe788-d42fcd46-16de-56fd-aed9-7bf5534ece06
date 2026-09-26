"""多次演奏对比 API：创建、列表、读取、显式刷新、删除。"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from .. import db
from ..services import comparison as cmp_service

router = APIRouter(prefix="/api/comparisons")


@router.post("")
def create_comparison(body: dict):
    try:
        member_ids = body["piece_ids"]
        baseline_id = body["baseline_piece"]
    except (KeyError, TypeError) as exc:
        raise HTTPException(400, f"缺少字段 {exc}") from exc
    if not isinstance(member_ids, list) or not isinstance(baseline_id, str):
        raise HTTPException(400, "piece_ids 必须为数组，baseline_piece 为字符串")
    try:
        cmp_id = cmp_service.create_comparison(
            [str(p) for p in member_ids], baseline_id,
            body.get("title") or None)
    except ValueError as exc:
        # 指纹不一致 / 成员未完成 / 数量不合法等 -> 409/400 由文案说明
        raise HTTPException(409, str(exc)) from exc
    return {"id": cmp_id, **_safe_get(cmp_id)}


@router.get("")
def list_comparisons():
    return cmp_service.list_comparisons()


@router.get("/{cmp_id}")
def get_comparison(cmp_id: str):
    try:
        return cmp_service.get_comparison(cmp_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{cmp_id}/refresh")
def refresh_comparison(cmp_id: str):
    try:
        data = cmp_service.refresh_comparison(cmp_id)
    except ValueError as exc:
        if db.get_comparison(cmp_id) is None:
            raise HTTPException(404, str(exc)) from exc
        raise HTTPException(409, str(exc)) from exc
    return data


@router.delete("/{cmp_id}")
def delete_comparison(cmp_id: str):
    # 先删合成轨 WAV 文件（DB 行会随外键级联删除）
    task = db.get_synthesis_task_by_comparison(cmp_id)
    if task and task["wav_path"] and os.path.exists(task["wav_path"]):
        os.remove(task["wav_path"])
    if not db.delete_comparison(cmp_id):
        raise HTTPException(404, "对比不存在")
    return {"ok": True}


def _safe_get(cmp_id: str) -> dict:
    try:
        return cmp_service.get_comparison(cmp_id)
    except ValueError as exc:  # 创建后理论上立即可读
        raise HTTPException(500, str(exc)) from exc

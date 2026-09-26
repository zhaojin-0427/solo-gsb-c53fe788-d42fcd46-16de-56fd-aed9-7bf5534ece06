"""偏差练习处方与复测 API。

  POST /api/prescriptions                 创建处方（只读固化；request_id 幂等）
  GET  /api/prescriptions                 处方列表
  GET  /api/prescriptions/{id}            处方详情（时间线 / 固化版本 / 复测）
  POST /api/prescriptions/{id}/retests    提交复测（request_id 幂等；
                                           来源过期/指纹不符/无已确认对齐/
                                           已作废均返回 409 并回放首次响应）
  GET  /api/prescriptions/{id}/retests    复测历史
  GET  /api/retests/{id}                  单次复测完整结果（实际值/阈值/证据）
  GET  /api/prescriptions/{id}/candidates 可关联的同曲录音及准入状态
  POST /api/prescriptions/{id}/rebind     显式重绑过期来源（保留旧结果）
  POST /api/prescriptions/{id}/void       作废（终态）
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from .. import db
from ..services import prescription as prx

router = APIRouter(prefix="/api")


def _get_or_404(prx_id: str):
    try:
        return prx.get_prescription(prx_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _incompat(exc: prx.IncompatibleError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "reason": exc.reason, **exc.extra})


def _stale(exc: prx.StaleSourceError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "reason": "source_stale",
                 "stale_members": exc.stale})


def _replay_or_none(request_id: str, scope: str, prx_id: str | None = None):
    """命中幂等记录时回放首次响应（404/作用域不符按冲突处理）。"""
    row = db.get_idempotent(request_id)
    if row is None:
        return None
    if row["scope"] != scope:
        raise HTTPException(
            409, f"request_id {request_id} 已用于其他类型操作")
    if prx_id is not None and row["prescription_id"] != prx_id:
        raise HTTPException(
            409, f"request_id {request_id} 已用于其他处方的{scope}操作")
    return JSONResponse(
        status_code=row["status_code"],
        content=json.loads(row["response_json"]))


def _store_idempotent(request_id: str, scope: str, prx_id: str,
                      status_code: int, payload: dict):
    db.save_idempotent(
        request_id, scope, prx_id, status_code,
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   default=str))


def _require_request_id(body: dict) -> str:
    request_id = body.get("request_id")
    if not request_id or not isinstance(request_id, str):
        raise HTTPException(400, "必须提供 request_id 用于幂等")
    return request_id


# ------------------------------------------------------------ 创建

@router.post("/prescriptions")
def create_prescription(body: dict):
    request_id = _require_request_id(body)
    replay = _replay_or_none(request_id, "create_prescription")
    if replay is not None:
        return replay

    source_type = body.get("source_type")
    source_id = body.get("source_id")
    if source_type not in ("piece", "comparison") or not isinstance(
            source_id, str):
        raise HTTPException(400, "source_type 必须为 piece/comparison，"
                                "并提供对应 source_id")
    try:
        prx_id = prx.create_prescription(
            source_type, source_id, body.get("title") or None,
            body.get("segments") or [])
    except prx.StaleSourceError as exc:
        resp = _stale(exc)
        _persist_response(resp, request_id, "create_prescription", None)
        return resp
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    data = prx.get_prescription(prx_id)
    _store_idempotent(request_id, "create_prescription", prx_id, 200, data)
    return data


def _persist_response(resp: JSONResponse, request_id: str, scope: str,
                      prx_id: str | None):
    """把首次确定性响应（含 409）记入幂等表，重复提交原样回放。"""
    _store_idempotent(request_id, scope, prx_id, resp.status_code,
                      json.loads(resp.body))


# ------------------------------------------------------------ 读取

@router.get("/prescriptions")
def list_prescriptions():
    return prx.list_prescriptions()


@router.get("/prescriptions/{prx_id}")
def get_prescription(prx_id: str):
    return _get_or_404(prx_id)


@router.get("/prescriptions/{prx_id}/candidates")
def candidates(prx_id: str):
    if not db.get_prescription(prx_id):
        raise HTTPException(404, "处方不存在")
    try:
        return prx.candidate_targets(prx_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


# ------------------------------------------------------------ 复测

@router.post("/prescriptions/{prx_id}/retests")
def create_retest(prx_id: str, body: dict):
    if not db.get_prescription(prx_id):
        raise HTTPException(404, "处方不存在")
    request_id = _require_request_id(body)
    replay = _replay_or_none(request_id, "retest", prx_id)
    if replay is not None:
        return replay

    target = body.get("target_piece_id")
    if not isinstance(target, str):
        raise HTTPException(400, "必须提供 target_piece_id")
    try:
        data = prx.run_retest(prx_id, target)
    except prx.StaleSourceError as exc:
        # 来源固化成员过期：必须显式重绑。该确定性拒绝同样幂等回放。
        resp = _stale(exc)
        _persist_response(resp, request_id, "retest", prx_id)
        return resp
    except prx.IncompatibleError as exc:
        resp = _incompat(exc)
        _persist_response(resp, request_id, "retest", prx_id)
        return resp
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _store_idempotent(request_id, "retest", prx_id, 200, data)
    return data


@router.get("/prescriptions/{prx_id}/retests")
def list_retests(prx_id: str):
    if not db.get_prescription(prx_id):
        raise HTTPException(404, "处方不存在")
    return [prx.get_retest_detail(r["id"])
            for r in db.list_retests(prx_id)]


@router.get("/retests/{retest_id}")
def get_retest(retest_id: str):
    try:
        return prx.get_retest_detail(retest_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


# ------------------------------------------------------------ 状态推进

@router.post("/prescriptions/{prx_id}/rebind")
def rebind(prx_id: str, body: dict | None = None):
    body = body or {}
    if not db.get_prescription(prx_id):
        raise HTTPException(404, "处方不存在")
    source_type = body.get("source_type")
    source_id = body.get("source_id")
    if source_type not in ("piece", "comparison") or not isinstance(
            source_id, str):
        raise HTTPException(400, "source_type 必须为 piece/comparison，"
                                "并提供对应 source_id")
    try:
        return prx.rebind_prescription(prx_id, source_type, source_id)
    except prx.StaleSourceError as exc:
        return _stale(exc)
    except prx.IncompatibleError as exc:
        return _incompat(exc)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/prescriptions/{prx_id}/void")
def void_prescription(prx_id: str):
    if not db.get_prescription(prx_id):
        raise HTTPException(404, "处方不存在")
    try:
        return prx.void_prescription(prx_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

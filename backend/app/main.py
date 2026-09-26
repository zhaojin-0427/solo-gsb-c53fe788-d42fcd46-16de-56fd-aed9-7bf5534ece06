"""FastAPI 入口：API 路由 + 录音文件静态服务 + 前端静态页面。"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db
from .routers import comparisons as comparisons_router
from .routers import pieces as pieces_router
from .routers import practice_scores as practice_scores_router
from .routers import prescriptions as prescriptions_router
from .routers import revisions as revisions_router
from .routers import synthesis as synthesis_router

DATA_DIR = os.environ.get("DATA_DIR", "/data")
FILES_DIR = os.path.join(DATA_DIR, "files")
FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "/app/frontend")

os.makedirs(FILES_DIR, exist_ok=True)

app = FastAPI(title="演奏录音与乐谱偏差复盘台", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"])

app.include_router(pieces_router.router)
app.include_router(comparisons_router.router)
app.include_router(synthesis_router.router)
app.include_router(prescriptions_router.router)
app.include_router(practice_scores_router.router)
app.include_router(revisions_router.router)


@app.on_event("startup")
def _startup():
    db.init_db()


@app.get("/api/health")
def health():
    return {"ok": True}


# 录音文件：StaticFiles 支持 Range，供 <audio> seek
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

# 前端必须最后挂载（/ 兜底）
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True),
              name="frontend")

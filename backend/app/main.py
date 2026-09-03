# -*- coding: utf-8 -*-
"""FastAPI 入口。

启动(在 backend 目录下):
    python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import agro, securities
from app.db import get_session

app = FastAPI(title="stock-value-analysis API", version="0.1.0")

# 局域网部署 + Vue3 开发服务器跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(securities.router)
app.include_router(agro.router)


class HealthOut(BaseModel):
    status: str
    db: str
    time: str


@app.get("/api/health", response_model=HealthOut)
def health(db: Session = Depends(get_session)):
    db.execute(select(func.now()))
    return HealthOut(status="ok", db="ok", time=datetime.now().isoformat(timespec="seconds"))


# 生产前端:npm run build 产物存在时直接托管(本机一体服务 :8000;hash 路由无需 fallback)
_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")

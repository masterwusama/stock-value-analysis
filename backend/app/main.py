# -*- coding: utf-8 -*-
"""FastAPI 入口。

启动(在 backend 目录下):
    python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import agro, commodity, etf, forecast, pick, research, securities, sentiment
from app.db import get_session

app = FastAPI(title="stock-value-analysis API", version="0.1.0")

# gzip 压缩：API JSON（列表 75KB→约 15KB）与静态 JS/CSS/HTML（1MB 主包虽有按需分包，
# 97KB 入口仍可再压 3 倍）——手机/Tailscale 远程访问的传输量直降 3~4 倍
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 局域网部署 + Vue3 开发服务器跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(securities.router)
app.include_router(pick.router)
app.include_router(agro.router)
app.include_router(commodity.router)
app.include_router(forecast.router)
app.include_router(sentiment.router)
app.include_router(research.router)
app.include_router(etf.router)


class HealthOut(BaseModel):
    status: str
    db: str
    time: str


@app.get("/api/health", response_model=HealthOut)
def health(db: Session = Depends(get_session)):
    db.execute(select(func.now()))
    return HealthOut(status="ok", db="ok", time=datetime.now().isoformat(timespec="seconds"))


# 生产前端:npm run build 产物存在时直接托管(本机一体服务 :8000;hash 路由无需 fallback)
# index.html 必须禁缓存:它引用带内容哈希的 chunk,浏览器缓存旧 index.html 就会加载旧
# chunk——每次发版后用户看到的都是旧前端,还必须手动强刷(2026-09-19 实测踩坑)。
# 带哈希的 assets/ 反过来要长缓存:内容变哈希就变,URL 即版本,immutable 让
# Tailscale/手机端的重复访问省掉每个 JS/CSS 一次协商往返(2026-10-28)。
class _NoCacheIndex(StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        p = getattr(resp, "path", "") or ""
        if p.endswith("index.html"):
            resp.headers["Cache-Control"] = "no-cache"
        elif ("assets" in Path(p).parts
              and Path(p).suffix.lower() in (".js", ".css", ".woff2", ".png", ".svg")):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", _NoCacheIndex(directory=_DIST, html=True), name="frontend")

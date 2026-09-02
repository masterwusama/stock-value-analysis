# -*- coding: utf-8 -*-
"""数据库引擎与会话工厂。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session():
    """FastAPI 依赖:请求级会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

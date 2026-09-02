# -*- coding: utf-8 -*-
"""应用配置:从 backend/.env 读取数据库连接等配置。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://root@localhost:3306/db_va?charset=utf8mb4",
)
BOND_10Y: float = float(os.getenv("BOND_10Y", "0.017"))
LEGACY_DATA_DIR: Path = Path(os.getenv("LEGACY_DATA_DIR", ""))

# 市场枚举与货币映射(与原 fetch_data.py 口径一致)
MARKETS = ("A", "HK", "US")
MARKET_CURRENCY = {"A": "CNY", "HK": "HKD", "US": "USD"}

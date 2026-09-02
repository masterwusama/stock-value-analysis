# -*- coding: utf-8 -*-
"""P0:在 db_va 中创建全部表,并将 DDL 存档到 docs/schema.sql。

用法(在 backend 目录下):
    python -m scripts.create_tables             # 增量创建(已存在的表不动)
    python -m scripts.create_tables --recreate  # 先删除全部表再重建(清空数据)
"""
import argparse
import sys
from io import StringIO
from pathlib import Path

from sqlalchemy import create_mock_engine

from app.db import engine
from app.models import Base

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DOC = BACKEND_DIR.parent / "docs" / "schema.sql"


def dump_ddl() -> str:
    """用 mock engine 导出 CREATE TABLE 语句存档。"""

    def dump(sql, *args, **kwargs):
        out.write(str(sql.compile(dialect=engine.dialect)).strip() + ";\n\n")

    out = StringIO()
    mock = create_mock_engine("mysql+pymysql://", executor=dump)
    Base.metadata.create_all(mock, checkfirst=False)
    return out.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recreate", action="store_true", help="先删除全部表再重建(清空数据)")
    args = ap.parse_args()
    if args.recreate:
        Base.metadata.drop_all(engine)
        print("已删除全部旧表")
    Base.metadata.create_all(engine)
    tables = sorted(Base.metadata.tables)
    print(f"已创建 {len(tables)} 张表: {', '.join(tables)}")

    SCHEMA_DOC.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_DOC.write_text(
        "-- db_va 全库 DDL(由 app/models/entities.py 导出,勿手改)\n\n"
        + dump_ddl(),
        encoding="utf-8",
    )
    print(f"DDL 已存档: {SCHEMA_DOC}")


if __name__ == "__main__":
    sys.exit(main())

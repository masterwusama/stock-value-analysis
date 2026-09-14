# -*- coding: utf-8 -*-
"""P0:在 db_va 中创建全部表,并将 DDL 存档到 docs/schema.sql。

建表之外还做一件容易漏的事：给**已存在**的表补新增列与新增索引（sync_schema）。
metadata.create_all 只对缺失的表生效，改了 entities.py 后跑它是静默无动作的，
于是新字段抓得到、算得出、就是进不了库（列表页整列 -，回灌报 1054 Unknown column）。
用法(在 backend 目录下):
    python -m scripts.create_tables             # 增量创建 + 补列补索引(已存在的列不动)
    python -m scripts.create_tables --recreate  # 先删除全部表再重建(清空数据)
"""
import argparse
import sys
from io import StringIO
from pathlib import Path

from sqlalchemy import create_mock_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateColumn

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


def sync_schema(db: Engine) -> list[str]:
    """给已存在的表补缺失列与缺失索引，返回执行过的动作（人读文本）。

    只做加法：不改类型、不删列、不碰主键与唯一约束。那些改动得先评估数据怎么搬，
    不该被一个脚本悄悄做掉（改类型请走 §12 的重建 + 全量回灌）。
    """
    insp = inspect(db)
    actions: list[str] = []
    with db.begin() as conn:
        for tbl in Base.metadata.sorted_tables:
            if not insp.has_table(tbl.name):
                continue  # 整张表缺失由 create_all 负责，这里不半建
            exist_cols = {c["name"] for c in insp.get_columns(tbl.name)}
            for col in tbl.columns:
                if col.name in exist_cols:
                    continue
                # CreateColumn 产出的就是「`列名` 类型」一段，可直接拼进 ADD COLUMN
                ddl = str(CreateColumn(col).compile(dialect=db.dialect)).strip()
                conn.execute(text(f"ALTER TABLE {tbl.name} ADD COLUMN {ddl}"))
                actions.append(f"+ column {tbl.name}.{col.name}")
            exist_idx = {i["name"] for i in insp.get_indexes(tbl.name)}
            for idx in tbl.indexes:
                if idx.name in exist_idx:
                    continue
                cols = ", ".join(f"`{c.name}`" for c in idx.columns)
                conn.execute(text(f"CREATE INDEX {idx.name} ON {tbl.name} ({cols})"))
                actions.append(f"+ index {tbl.name}.{idx.name}")
    return actions


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

    # 补结构放在建表之后、存档之前：docs/schema.sql 要与真库当前结构一致
    for act in sync_schema(engine):
        print(f"补结构: {act}")

    SCHEMA_DOC.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_DOC.write_text(
        "-- db_va 全库 DDL(由 app/models/entities.py 导出,勿手改)\n\n"
        + dump_ddl(),
        encoding="utf-8",
    )
    print(f"DDL 已存档: {SCHEMA_DOC}")


if __name__ == "__main__":
    sys.exit(main())

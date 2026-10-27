# -*- coding: utf-8 -*-
"""投研库接口:研报索引与正文渲染。

研报是 git 版本化的 Markdown 文档,存 docs/research/{code}-{名称}-{YYYYMM}.md,
frontmatter 承载结构化元数据(契约见 docs/设计-投研库模块.md)。本路由只做
解析/渲染/索引三件事,不落库、零迁移;内容为仓库自有文档,服务端渲染 HTML
后由前端注入(v-html),信任源可控。

依赖 markdown>=3.4 / pyyaml>=6(纯 Python)。
"""
import re
from datetime import date as date_type
from pathlib import Path

import markdown as md_lib
import yaml
from fastapi import APIRouter, HTTPException
from markdown.extensions.toc import TocExtension, slugify_unicode
from pydantic import BaseModel

router = APIRouter(prefix="/api/research", tags=["research"])

# 仓库根 = backend/app/api/research.py 上跳三级(与 main.py 定位 frontend/dist 同款)
RESEARCH_DIR = Path(__file__).resolve().parents[3] / "docs" / "research"

# slug 白名单:字母数字下划线连字符点 + 中文;显式排除路径分隔与父目录
_SLUG_RE = re.compile(r"^[\w\u4e00-\u9fff.-]+$")

_MD_EXTS = ["tables", "fenced_code", TocExtension(slugify=slugify_unicode)]


class ReportMeta(BaseModel):
    slug: str
    code: str
    name: str
    title: str
    type: str = "deep"
    date: str
    status: str = "final"
    conclusion: str = ""
    price_ref: float | None = None
    val_low: float | None = None
    val_high: float | None = None
    val_method: str = ""
    tags: list[str] = []
    summary: str = ""


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """--- yaml ---\n正文。无 frontmatter 或解析失败时按无元数据处理,不让单篇坏文件拖垮列表。
    utf-8-sig:Windows 记事本保存会带 BOM,\ufeff--- 会让 startswith("---") 判假。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)   # 开头 --- 固定,只需按闭合分隔符切一刀
    if len(parts) < 2:
        return {}, text
    try:
        meta = yaml.safe_load(parts[0][3:]) or {}
    except yaml.YAMLError:
        return {}, text
    return meta if isinstance(meta, dict) else {}, parts[1].lstrip("\n")


def _norm(meta: dict) -> dict:
    """frontmatter 归一化:YAML 会把 2026-10-02 解析成 date 对象,统一转 ISO 字符串;
    数值字段容忍误写成字符串(转不动就原样留着,详情页照显;列表侧由 _load_meta
    的兜底捕获,坏值文件不进列表)。"""
    if isinstance(meta.get("date"), date_type):
        meta["date"] = meta["date"].isoformat()
    for k in ("price_ref", "val_low", "val_high"):
        v = meta.get(k)
        if isinstance(v, str):
            try:
                meta[k] = float(v) if v.strip() else None
            except ValueError:
                pass
    if meta.get("tags") is None:
        meta["tags"] = []
    elif isinstance(meta.get("tags"), str):
        meta["tags"] = [t.strip() for t in meta["tags"].split(",") if t.strip()]
    return meta


def _load_meta(path: Path) -> ReportMeta | None:
    try:
        meta, _body = _split_frontmatter(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return None
    # code/name/title/date 是契约必填项;缺任一则列表里无意义,跳过。
    # 其余任何坏值(类型错、数值字段写不成 float、pydantic 校验不过)同样只废这一篇——
    # 语义就是「跳过坏文件」,except 范围必须与之一致,否则单篇坏文件打挂整个列表
    try:
        if not all(meta.get(k) for k in ("code", "name", "title", "date")):
            return None
        fields = {k: _norm(meta).get(k, d) for k, d in ReportMeta(slug="", code="", name="", title="", date="").model_dump().items()}
        fields["slug"] = path.stem
        return ReportMeta(**fields)
    except Exception:  # noqa: BLE001
        return None


@router.get("/list", response_model=list[ReportMeta])
def list_reports():
    """全部研报元数据,date 倒序(最新在前;同日按文件名稳定排序)。_ 开头是模板,不算研报。"""
    if not RESEARCH_DIR.is_dir():
        return []
    metas = [m for p in sorted(RESEARCH_DIR.glob("[!_]*.md")) if (m := _load_meta(p))]
    metas.sort(key=lambda m: (m.date, m.slug), reverse=True)
    return metas


# ---------------------------------------------------------------------------
# 研报覆盖缓存:证券列表/详情页挂「有研报」标记用。9 份文件的 frontmatter 解析
# 不到 1ms,但列表页每请求都要——按目录 mtime 缓存,内容不变零解析。
# ---------------------------------------------------------------------------
_cov_cache: tuple[tuple, dict] = ((0, 0.0), {})


def coverage() -> dict:
    """{code: {slug, title, conclusion, date}};目录无变化时直接返回缓存。
    stamp 用 (文件数, 最大 mtime):只看 mtime 时删除「非最新」的文件不会
    失效缓存,已删研报的「有研报」徽章会残留。"""
    global _cov_cache
    if not RESEARCH_DIR.is_dir():
        return {}
    try:
        stamp = (len(list(RESEARCH_DIR.glob("*.md"))),
                 max(p.stat().st_mtime for p in RESEARCH_DIR.glob("*.md")))
    except ValueError:  # 空目录
        return {}
    if stamp != _cov_cache[0]:
        cov = {}
        for m in list_reports():
            # 同一 code 存在多份研报时(如 603599-...-202610 与 -202610-2),
            # 个股联动取 date 最新的那份;date 相同则让后遍历者不覆盖。
            old = cov.get(m.code)
            if old is None or (m.date or "") >= (old.get("date") or ""):
                cov[m.code] = {"slug": m.slug, "title": m.title,
                               "conclusion": m.conclusion, "date": m.date}
        _cov_cache = (stamp, cov)
    return _cov_cache[1]


@router.get("/report/{slug}")
def get_report(slug: str):
    if not _SLUG_RE.match(slug) or ".." in slug:
        raise HTTPException(status_code=404, detail="不存在的研报")
    path = RESEARCH_DIR / f"{slug}.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="不存在的研报")
    meta, body = _split_frontmatter(path.read_text(encoding="utf-8-sig"))
    # 每请求新建实例:Markdown 实例官方声明非线程安全,sync def 会在线程池里并发,
    # 单例 + reset 只解决状态累积,防不了并发 convert 互相污染(HTML/toc 错乱)
    md = md_lib.Markdown(extensions=_MD_EXTS)
    html = md.convert(body)
    defaults = ReportMeta(slug=slug, code="", name="", title="", date="").model_dump()
    # toc_tokens 是 h1 嵌 h2 的树;阅读器侧栏要的是章节平铺(取 h2/h3,缩进区分)
    flat = []

    def _flatten(tokens, level):
        for t in tokens:
            if level >= 2:
                flat.append({"id": t["id"], "name": t["name"], "level": level})
            _flatten(t.get("children", []), level + 1)

    _flatten(md.toc_tokens, 1)
    return {"meta": {k: _norm(meta).get(k, v) for k, v in defaults.items()}, "html": html, "toc": flat}

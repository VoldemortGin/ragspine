"""叙事批量入库编排：文件夹 / 文件列表 -> 抽取 -> 切块 -> 块库（幂等、可 dry-run）。

流程（仿 src/ragspine/ingestion/structured/ingestion.py 的编排与报告模式）：
    文件 hash -> 与登记台账比对（未变化即 skipped，幂等）
      -> narrative_extract 抽取（无文本 -> no_text；异常 -> failed，不中断整批）
      -> chunk_document 切块（元数据逐块继承）
      -> chunk_store.replace_doc_chunks 批量写入（重入走既有版本化语义）
      -> 登记台账更新。dry_run=True 时报告完整但块库 / 台账零写入。

元数据两层来源：
    1) 调用方显式 per-doc 映射（meta_by_doc，按文件名键入）—— 优先；
       允许字段见 ALLOWED_META_KEYS，未知字段直接 ValueError（防笔误静默丢失）。
    2) 缺省时仅从文件名启发式提取 period（显式 FY2024 / 2025H1 / 2025Q1 模式，
       裸年份不算、多个不同期间视为歧义留空）；topic/entity 等**绝不猜测，留空就是留空**。

登记台账说明：chunk_store（B 线，只读）只有块级版本化、没有文件 hash 登记表，
故本模块在**同一个 sqlite 库**内自建 narrative_doc 表（doc_id -> file_hash）做
「未变化即跳过」的比对，不触碰 src/ragspine/retrieval/chunking/chunk_store.py 也不动 Excel 线的 manifest。
"""

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ragspine.common.company_profile import load_company_profile
from ragspine.common.glossary import normalize_period
from ragspine.common.sensitivity import classify_sensitivity
from ragspine.extraction.extractors.pptx_styled_extractor import compute_file_hash
from ragspine.ingestion.narrative.narrative_extract import (
    SUPPORTED_SUFFIXES,
    extract_narrative,
)
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta, chunk_document

# 模块级 home 公司 profile（沿用 glossary/intent/query_tools 的 env-aware 装载模式，
# 测试可 monkeypatch 它演示运行期换部署）。敏感度分级器从 _PROFILE.sensitivity 取规则。
_PROFILE = load_company_profile()

# 每文件状态取值。
STATUS_INGESTED = "ingested"   # 已入库（dry-run 下表示「将要入库」）。
STATUS_SKIPPED = "skipped"     # hash 未变化，幂等跳过。
STATUS_FAILED = "failed"       # 读取 / 抽取失败（error 记录原因，不中断整批）。
STATUS_NO_TEXT = "no_text"     # 抽不出叙事文本（如全扫描 PDF），不落库。

# per-doc 元数据映射允许的字段（DocumentMeta 维度 + valid_as_of / title）。
ALLOWED_META_KEYS = {
    "title", "topic", "entity", "geography", "period",
    "language", "sensitivity", "valid_as_of",
}

# 文件名 period 启发式：只认显式模式，裸年份（如日期 2026-06-11）不算。
_FY_RE = re.compile(r"(?i)FY[\s_-]?(20\d{2})[\s_-]?(H[12]|Q[1-4])?")
_HQ_RE = re.compile(r"(?i)(20\d{2})[\s_-]?(H[12]|Q[1-4])")


@dataclass
class FileReport:
    """单文件入库结果。

    字段语义约定：
        path:            源文件路径（字符串）。
        doc_id:          源文件名（= 块的 doc_id，血缘根）。
        status:          'ingested' / 'skipped' / 'failed' / 'no_text'。
        n_chunks:        本次产出（或 dry-run 下将产出）的 chunk 数。
        n_skipped_pages: 抽取时跳过的无文本层页数（扫描页）。
        file_hash:       源文件内容 hash（读取失败时为 None）。
        error:           失败原因（status='failed' 时非 None）。
        warnings:        抽取告警（逐页跳过原因等）。
    """

    path: str
    doc_id: str
    status: str
    n_chunks: int = 0
    n_skipped_pages: int = 0
    file_hash: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class NarrativeIngestReport:
    """一批叙事入库的汇总报告。"""

    dry_run: bool = False
    files: list[FileReport] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """各状态文件数（汇总展示用）。"""
        out = {s: 0 for s in (STATUS_INGESTED, STATUS_SKIPPED, STATUS_FAILED, STATUS_NO_TEXT)}
        for f in self.files:
            out[f.status] += 1
        return out


def period_from_filename(name: str) -> str:
    """从文件名启发式提取 period（glossary 规范形）；提不出 / 歧义返回 ''。

    只认显式模式：FY2024（含 FY2024H1 / FY2025Q1 变体）、2025H1、2025Q1，
    大小写不敏感，部件间允许单个 空格/下划线/连字符。裸年份不算（日期类
    文件名误报风险）。命中多个**不同**期间视为歧义，留空——绝不猜测。
    """
    candidates: set[str] = set()
    for m in _FY_RE.finditer(name):
        token = m.group(1) + (m.group(2) or "")
        parsed = normalize_period(token)
        if parsed is not None:
            candidates.add(parsed[1])
    for m in _HQ_RE.finditer(name):
        parsed = normalize_period(m.group(1) + m.group(2))
        if parsed is not None:
            candidates.add(parsed[1])
    return candidates.pop() if len(candidates) == 1 else ""


def ingest_narrative(
    inputs: str | Path | list[str | Path],
    store: ChunkStore,
    *,
    meta_by_doc: dict[str, dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> NarrativeIngestReport:
    """批量叙事入库编排，返回逐文件汇总报告。

    参数：
        inputs:      文件夹（递归收 pptx/pdf，忽略隐藏与 Office 临时文件）
                     或文件路径列表（顺序保留）。
        store:       ChunkStore 实例（已 init_schema）。
        meta_by_doc: 可选 per-doc 元数据映射 {文件名: {topic/entity/.../valid_as_of}}；
                     未知字段 ValueError。
        dry_run:     True 时完整跑抽取与切块并产出报告，但块库 / 台账零写入。

    行为见模块 docstring：hash 幂等跳过、单文件失败不中断整批、no_text 不落库。
    """
    meta_by_doc = meta_by_doc or {}
    for doc_id, meta in meta_by_doc.items():
        unknown = set(meta) - ALLOWED_META_KEYS
        if unknown:
            raise ValueError(
                f"meta_by_doc[{doc_id!r}] 含未知字段 {sorted(unknown)}；"
                f"允许字段：{sorted(ALLOWED_META_KEYS)}"
            )

    report = NarrativeIngestReport(dry_run=dry_run)
    registry = sqlite3.connect(store.db_path)
    try:
        _ensure_registry(registry)
        for path in _resolve_inputs(inputs):
            report.files.append(
                _ingest_one(path, store, registry, meta_by_doc, dry_run)
            )
    finally:
        registry.close()
    return report


# ---------------------------------------------------------------------------
# 内部：单文件流程 / 输入解析 / 登记台账
# ---------------------------------------------------------------------------

def _ingest_one(
    path: Path,
    store: ChunkStore,
    registry: sqlite3.Connection,
    meta_by_doc: dict[str, dict[str, Any]],
    dry_run: bool,
) -> FileReport:
    """单文件：hash 比对 -> 抽取 -> 切块 -> 写入；任何异常落进 failed 报告。"""
    doc_id = path.name
    rep = FileReport(path=str(path), doc_id=doc_id, status=STATUS_FAILED)

    try:
        rep.file_hash = compute_file_hash(path)
    except OSError as exc:
        rep.error = f"{type(exc).__name__}: {exc}"
        return rep

    if _registered_hash(registry, doc_id) == rep.file_hash:
        rep.status = STATUS_SKIPPED
        return rep

    try:
        doc = extract_narrative(path)
    except Exception as exc:  # noqa: BLE001 —— 单文件失败不中断整批
        rep.error = f"{type(exc).__name__}: {exc}"
        return rep

    rep.n_skipped_pages = doc.skipped_pages
    rep.warnings = list(doc.warnings)
    if not doc.segments:
        rep.status = STATUS_NO_TEXT
        return rep

    meta = meta_by_doc.get(doc_id, {})
    # 敏感度：人工显式标注优先；缺省时确定性自动分级（漏标 = 泄露，故 fail-safe）。
    sensitivity = meta.get("sensitivity")
    if sensitivity is None:
        sensitivity = classify_sensitivity(doc_id, doc.to_text(), _PROFILE.sensitivity)
    doc_meta = DocumentMeta(
        doc_id=doc_id,
        title=meta.get("title", path.stem),
        topic=meta.get("topic", ""),
        entity=meta.get("entity", ""),
        geography=meta.get("geography", ""),
        period=meta.get("period") or period_from_filename(doc_id),
        language=meta.get("language", ""),
        sensitivity=sensitivity,
    )
    chunks = chunk_document(doc.to_text(), doc_meta)
    rep.n_chunks = len(chunks)
    rep.status = STATUS_INGESTED
    if dry_run:
        return rep

    store.replace_doc_chunks(doc_id, chunks, valid_as_of=meta.get("valid_as_of", ""))
    _register_doc(registry, doc_id, rep.file_hash, str(path), len(chunks))
    return rep


def _resolve_inputs(inputs: str | Path | list[str | Path]) -> list[Path]:
    """输入解析：文件夹递归收支持类型（排序、忽略隐藏 / '~$' 临时文件），列表原序。"""
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    paths: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            paths.extend(sorted(
                f for f in p.rglob("*")
                if f.is_file()
                and f.suffix.lower() in SUPPORTED_SUFFIXES
                and not f.name.startswith(("~$", "."))
            ))
        else:
            paths.append(p)
    return paths


def _ensure_registry(conn: sqlite3.Connection) -> None:
    """建叙事文档登记台账表（幂等；与 narrative_chunk 同库不同表）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS narrative_doc (
            doc_id      TEXT PRIMARY KEY,
            file_hash   TEXT NOT NULL,
            source_path TEXT NOT NULL DEFAULT '',
            n_chunks    INTEGER NOT NULL DEFAULT 0,
            ingested_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _registered_hash(conn: sqlite3.Connection, doc_id: str) -> str | None:
    """取某文档已登记的 file_hash；未登记返回 None。"""
    row = conn.execute(
        "SELECT file_hash FROM narrative_doc WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    return row[0] if row is not None else None


def _register_doc(
    conn: sqlite3.Connection, doc_id: str, file_hash: str, source_path: str, n_chunks: int
) -> None:
    """登记 / 更新一个文档的入库版本（hash 比对的依据）。"""
    conn.execute(
        """
        INSERT INTO narrative_doc (doc_id, file_hash, source_path, n_chunks, ingested_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (doc_id) DO UPDATE SET
            file_hash = excluded.file_hash,
            source_path = excluded.source_path,
            n_chunks = excluded.n_chunks,
            ingested_at = excluded.ingested_at
        """,
        (doc_id, file_hash, source_path, n_chunks,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

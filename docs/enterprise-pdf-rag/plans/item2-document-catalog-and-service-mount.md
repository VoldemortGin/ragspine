# 方案：第 2 项 —— 文档目录（DocumentCatalog）与通用只读服务挂载（MountedDocument）

仓库 `/Users/linhan/startup/spine/ragspine`，分支 `feat/generic-document-service`（HEAD `b91205e`）。本文件是只读设计，不含任何已落盘改动。所有行号均核对自该 HEAD。

---

## 0. 目标与与第 3 项的接口边界（已定死，双方按此设计）

| 产出 | 位置 | 第 3 项如何消费 |
|---|---|---|
| `DocumentCatalog` / `CatalogEntry` | `src/enterprise_pdf_rag/adapters/document_catalog.py` | `scan_catalog(...)` → 过滤 `retrieval_status == "ready"` 的 entry |
| `MountedDocument` / `mount_document` | 同上 | `mount_document(entry, embedder=<显式 EmbeddingPort>)` → `search(query, *, limit)` / `resolve(hit)`；**不直接碰 store** |
| HTTP `/v1/documents*` | `src/enterprise_pdf_rag/adapters/http/documents.py` | 第 3 项不依赖 HTTP；它在 Python 层直接用上面两个对象 |

**给第 3 项的两点对齐说明**：
1. 接口草案写的 `search(query, top_k)`，实际按既有 `ProcessingRetrieval.search(publication, query, *, limit=5)`（`processing_retrieval.py:234`）与 `ProcessingSearchRequest.limit`（`processing_schemas.py:40`）命名为 `search(query: str, *, limit: int = 5)`；`top_k` 即 `limit`。
2. `resolve(hit) -> RetrievalContext`（`processing/retrieval.py:143-155`，frozen dataclass）：`context.description.text`（description）、`context.ir`（qualified_ir / typed IR）、`context.member.source_svg: AssetRef`（SVG 证据引用，字节用 `MountedDocument.read_asset(ref)` 取）、`context.qualification`（资格回执，`LiteralQualification | FigureQualification`）、`context.scope`、`context.member.page_index`（0-based，物理页 = +1）。hit 结构 `PinnedRetrievalHit(snapshot_id, member_id, score)`（`processing/retrieval.py:99-107`），**无 page 字段**，页号从 `resolve` 取。

---

## 1. 现状事实（设计依据，函数/行号级）

### 1.1 存储与指针
- `LocalDocumentStore(root, *, activate_on_publish=True)`（`document_store.py:18`）；内容寻址 `root/objects/sha256/<digest>`；指针 **`current-manifest`**（`:81/:86`，纯文本 64-hex + `\n`）；`load_current() -> DocumentSnapshot`（`:85`）、`load(manifest_id)`（`:88`，逐资产校验 sha/长度）；损坏 → `ValueError`（`"Stored artifact digest mismatch; source review is unavailable"` `:51` 等）。`DocumentManifest.filename`、`manifest.source.sha256`、页数 `len(manifest.pages)`（`documents/models.py:93-105`）。
- `ProcessingStore(root)`（`processing_store.py:32`）；内部 `self.assets = LocalDocumentStore(root, activate_on_publish=False)`（`:34`）；指针 **`current-processing`**（`:72/:166`）；`load_current() -> tuple[str, ProcessingManifest]`（`:165`）；`load(snapshot_id)`（`:90`，若 `manifest.retrieval` 非空则连带 `load_retrieval` 并交叉校验 scope/member/lineage）；`load_retrieval(publication) -> tuple[RetrievalPlan, RetrievalIndex]`（`:128`，含每个 member 的 `(description_sha256, fingerprint, dims, vector)` 与索引一致性校验，`"Index vector does not match its actual embedding artifact"` `:162`）。**没有** `snapshots/`、`retrieval/` 目录，全部在 `objects/sha256/`。
- `ProcessingManifest.scope: ProcessingScope(source_manifest_id, source_sha256, source_page_count, selected_page_indices)`（`processing/models.py:30-53`）；`manifest.retrieval: RetrievalPublication | None`（`snapshot_id, plan, index, dependencies`，`:170-175`）——**member_count / fingerprint / dims 不在 RetrievalPublication 上**，必须 `load_retrieval` 后从 `plan.members[*].embedding_fingerprint / .embedding_dimensions / len(plan.members)` 取。
- `validate_processing_source(*, sources, artifacts, manifest, plan)`（`source_publication.py:24-30`），零模型。

### 1.2 检索与指纹
- `ProcessingRetrieval(sources, outputs, embedder)`（`processing_retrieval.py:76-84`，构造只存引用，**零调用**）；`search(publication, query, *, limit=5) -> tuple[PinnedRetrievalHit, ...]`（`:234`）内已有守卫：`member.embedding_fingerprint != self.embedder.fingerprint` → `ValueError("Query embedding provider differs from the pinned index")`（`:242-245`）；维度不符 → `ValueError(...)`（`:251`）；**每次 search 恰好一次 `embed_query`**。`resolve(publication, hit)`（`:265`）→ 模块函数 `resolve_processing_context(sources, outputs, publication, hit)`（`:287-302`，docstring 明示零模型）。
- `EmbeddingPort`（`figures/ports.py:40-47`，runtime_checkable）：`fingerprint: str` 属性、`embed_description`、`embed_query`。`LocalEmbeddingAdapter(config, *, sender=None)`（`local_models.py:88`）指纹 `f"local-http/{config.model}"`（`:102-104`）；`load_local_model_config("embedding")` 在 `providers.py:102-111`，读 `EMBEDDING_BASE_URL/MODEL/API_KEY`（无 `APP_` 前缀，强制 loopback）。`ProviderConfigurationError`/`ProviderRequestError` 都是 `ValueError` 子类（`providers.py:18/:22`）。
- 离线替身：`OfflineDescriptionEmbedder()`（`adapters/offline.py:97`，指纹 `"offline-demo/token-hash-64-v1"`，64 维）；`RecordingEmbedding`（**测试内定义**：`tests/enterprise_pdf_rag/processing/test_persistent_retrieval.py:52-63`，指纹 `"test-explicit-offline-vector-v1"`，2 维）。

### 1.3 发布与入库
- `DraftPublication`（`draft_publication.py:146-160`）：`source_sha256, source_manifest_id, processing_id, published_processing_id, retrieval_snapshot_id, member_count, embedding_dimensions, source_store, processing_store, current_processing_id, source_activated, indexed=True, activated=True, retrieval_status="ready"`；`BoundaryModel` 在 `adapters/http/schemas.py:15-16`（`extra="forbid", frozen=True, strict=True, str_strip_whitespace=True`）。
- `ingest_pdf` 输出根：`(output_dir or get_settings().data_dir / "ingestion") / <pdf sha256> / {source, processing}`（`pdf_ingestion.py:143-150`）；source store 以 `activate_on_publish=False` 打开。`data/ingestion` 当前**不存在**。

### 1.4 HTTP 与配置
- `app.py`：`create_app(*, mode)`（offline-demo）、`create_configured_app()`（`:69-87`）按 `get_settings().execution_mode` 分发；`aia-source-review` 分支写死 `ProcessingStore(PROCESSING_OUTPUT)`（`processing_runtime.py:16` = `AIA_OUTPUT / "pages-001-020"`）、哨兵 `PROCESSING_OUTPUT / "current-processing"`、`LocalDocumentStore(AIA_OUTPUT)`（`aia_ingestion.py:19` = `DATA_DIR/output/aia-2026-interim`）；embedder 缺配置时 `ProviderConfigurationError` → `None`（`:80-83`）。
- `create_aia_app(store, *, spec=AIA_SPEC, processing=None, embedder=None)`（`aia_review.py:64`）：`_ensure_source(snapshot, spec)`（`:49-61`）**只接受 AIA 样本**；含 `include_router(create_processing_router(store, processing, processed[0], embedder=embedder))`（无 prefix）；`ValueError/OSError` → 409（`:85-91`）。
- `create_processing_router(sources, outputs, processing_id, *, embedder=None)`（`processing_review.py:109-115`）：单 `processing_id` 闭包；路由 `/v1/processing/status|manifest|review/{relative:path}|search|context`；内嵌 `create_chart_qa_router`（`/v1/queries`，绝对路径）；`checked()`（`:128-145`）重载并比对 pinned manifest，`ValueError/OSError` → 409；search：`processing_id` 不符或 `retrieval is None` → 409，`embedder is None` → 503，`ProviderRequestError` → 503；`processing_status(processing_id, manifest) -> ProcessingStatusResponse`（`:67`）为纯函数可复用；review 白名单正则（`:158-165`）为 AIA 布局专属。
- `Settings`（`core/settings.py:64`）：`env_prefix="APP_"`、yaml `config/enterprise-pdf-rag/settings.yaml`（现为 `{}`）、不读 `.env`；字段 `is_debug, beartype_on, execution_mode: Literal["unconfigured","offline-demo","production","aia-source-review"], data_dir, log_dir`；`field_validator("data_dir","log_dir")` 解析相对路径；`get_settings()` 有 `lru_cache`，测试改 env 后须 `get_settings.cache_clear()`。非 frozen。
- `cli.py` `serve`：仅 `--host/--port`，`create_configured_app()` 无参 → `uvicorn.run`（`:308-312`）。
- 测试：`httpx2.ASGITransport + AsyncClient`（不用 `TestClient`）；autouse `no_network`（`tests/enterprise_pdf_rag/conftest.py:15-20`）；app factory 全链路模式见 `tests/enterprise_pdf_rag/documents/test_source_search.py:118-140`（`monkeypatch.setattr(app_module, "AIA_OUTPUT"/...)` + `get_settings.cache_clear()`）；CLI 层替换 embedder：`monkeypatch.setattr("enterprise_pdf_rag.cli.LocalEmbeddingAdapter", lambda config: OfflineDescriptionEmbedder())`（`test_generic_publication_e2e.py:210-213`）。
- 门：`check_conformance`（绝对导入、禁 `from __future__ import annotations`、`adapters/` 免封闭白名单）；`check_schema` 的 `CONTRACTS`（`scripts/enterprise_pdf_rag/check_schema.py:60-82`）登记制 ⇄ `docs/enterprise-pdf-rag/schemas/<name>.json`（无导出脚本）；`scripts/check_doc_drift.py` 要求 `src/enterprise_pdf_rag/CLAUDE.md` 的 `verified-against: 407849e` 在改包内代码后 bump 到新 HEAD；ruff 严格集（ANN/BLE/T20…）与 mypy strict 覆盖 `src|tests|scripts/enterprise_pdf_rag`。
- 数据现场：AIA 是**扁平命名目录**——source store = `data/output/aia-2026-interim/`（`current-manifest` = `e702bf1c…`），processing store = 其子目录 `pages-001-020/`（`current-processing` = `a7384f0c…`，manifest.retrieval.snapshot_id = `f59d2308…`）；与通用 `<root>/<sha>/{source,processing}` 布局不同。

---

## 2. 设计

### 2.1 配置（约束 4：配置隔离）

`src/enterprise_pdf_rag/core/settings.py` 只加三处（保持 beartype 叶子、无一方 import）：

```python
execution_mode: Literal[
    "unconfigured", "offline-demo", "production", "aia-source-review", "document-catalog"
] = "unconfigured"

# 通用入库/目录根；None → data_dir / "ingestion"（与 ingest_pdf 现默认一致）
ingestion_dir: Path | None = None                       # APP_INGESTION_DIR
# 兼容根：每项是一个 processing store 根，其父目录即 source store 根（AIA 现场布局）
legacy_document_roots: tuple[Path, ...] = ()           # APP_LEGACY_DOCUMENT_ROOTS='["/abs/.../pages-001-020"]'

@field_validator("ingestion_dir")           # 与 data_dir 同法：expanduser + 相对 ROOT_DIR + resolve；None 原样
@field_validator("legacy_document_roots")   # 逐项同上
@property
def ingestion_root(self) -> Path:
    return self.ingestion_dir if self.ingestion_dir is not None else self.data_dir / "ingestion"
```

- env 惯例沿用 `APP_*`；tuple 由 pydantic-settings 从 JSON 字符串解析，也可写进 `settings.yaml`。
- `adapters/pdf_ingestion.py:144` 改一行：`get_settings().data_dir / "ingestion"` → `get_settings().ingestion_root`，保证 `ingest` 默认输出与 catalog 扫描根是同一个配置。
- 不同文档的 store 互不可见：catalog 每项只持有自己的两条绝对路径；`MountedDocument` 只打开这两条；跨文档 hit 由 `resolve_member` 的 `"Retrieval hit belongs to another semantic snapshot"` 拒绝。

### 2.2 `src/enterprise_pdf_rag/adapters/document_catalog.py`（新）

模块 docstring：`"""Read-only catalog of published documents and pinned read-only mounts; no model calls."""`

#### 边界模型（`BoundaryModel` 风格，与 `DraftPublication` 同基类）

```python
from typing import Literal

from enterprise_pdf_rag.adapters.http.schemas import BoundaryModel

type CatalogOrigin = Literal["ingestion", "legacy"]
type CatalogRetrievalStatus = Literal["ready", "not_indexed", "corrupt"]

class CatalogEntry(BoundaryModel):
    document_id: str                       # ingestion 布局 = 子目录名(64-hex source sha)；legacy = scope.source_sha256，不可读时用 source 根目录名
    origin: CatalogOrigin
    source_store: str                      # 绝对路径
    processing_store: str                  # 绝对路径
    retrieval_status: CatalogRetrievalStatus
    reason: str | None = None              # corrupt / not_indexed 的原因，ready 时 None
    # 以下字段 retrieval_status == "ready" 时保证非 None；corrupt 时能读到多少填多少
    source_sha256: str | None = None
    source_manifest_id: str | None = None
    current_processing_id: str | None = None
    retrieval_snapshot_id: str | None = None
    member_count: int | None = None
    embedding_fingerprint: str | None = None
    embedding_dimensions: tuple[int, ...] | None = None
    document_label: str | None = None      # source manifest.filename
    source_page_count: int | None = None
    selected_physical_pages: tuple[int, ...] | None = None
    source_activated: bool | None = None   # source 根的 current-manifest == source_manifest_id

class DocumentCatalog(BoundaryModel):
    catalog_policy: Literal["published-current-processing-with-retrieval-v1"] = (
        "published-current-processing-with-retrieval-v1"
    )
    ingestion_root: str
    legacy_roots: tuple[str, ...]
    documents: tuple[CatalogEntry, ...]    # 按 document_id 排序，含 corrupt / not_indexed
    unpublished: tuple[str, ...]           # ingestion 子目录有 store 但无 current-processing（draft），只列 id 不解析

    @property
    def ready(self) -> tuple[CatalogEntry, ...]: ...
    def entry(self, document_id: str) -> CatalogEntry | None: ...
```

状态含义（与"只列指针存在且 retrieval 非空"的口径对齐，但**不隐藏**）：
- 无 `current-processing` 指针的 ingestion 子目录 → 不成为 entry，只进 `unpublished`（它们是 draft，不是已发布文档）。
- 指针存在、`load_current`/`load_retrieval`/`validate_processing_source`/source 校验任一抛 `ValueError | OSError` → `corrupt` + `reason=str(error)`。
- 指针存在且可读，但 `manifest.retrieval is None` → `not_indexed`（`publish_draft` 不会产生此态，只有手工/历史指针会；标出而非伪装成 corrupt）。
- legacy 根缺指针 → `corrupt`（显式配置的根缺指针是配置错误，不能静默跳过）。
- 同一 `document_id` 出现两次（如同一 PDF 既在 ingestion 又在 legacy）→ 后者标 `corrupt("duplicate document id …")`，fail closed 不择一。

#### 扫描（零模型、零写）

```python
def scan_catalog(ingestion_root: Path, *, legacy_roots: Sequence[Path] = ()) -> DocumentCatalog
```
1. `ingestion_root` 不存在 → `documents=()`（不是错误；`data/ingestion` 今天就不存在）。
2. 对 `sorted(ingestion_root.iterdir())` 中 `is_dir()` 且名字全匹配 `^[0-9a-f]{64}$` 的子目录：`source = child/"source"`，`processing = child/"processing"`；无 `processing/"current-processing"` → `unpublished += (child.name,)`；否则 `_inspect(document_id=child.name, source_root=source, processing_root=processing, origin="ingestion", expected_sha=child.name)`。
3. 对每个 `legacy_roots` 项：`processing_root = root.resolve()`，`source_root = processing_root.parent`，`_inspect(document_id=None, ..., origin="legacy", expected_sha=None)`。
4. 去重 + 排序 → `DocumentCatalog`。

```python
def _inspect(*, document_id, source_root, processing_root, origin, expected_sha) -> CatalogEntry:
    try:
        outputs = ProcessingStore(processing_root)
        sources = LocalDocumentStore(source_root, activate_on_publish=False)
        processing_id, manifest = outputs.load_current()                 # 指针→ID→完整校验
        scope = manifest.scope
        if expected_sha is not None and scope.source_sha256 != expected_sha:
            raise ValueError("Document directory name does not match the processing scope source sha256")
        snapshot = sources.load(scope.source_manifest_id)               # 按 pinned ID，不按 current-manifest
        if snapshot.manifest.source.sha256 != scope.source_sha256:
            raise ValueError("Source manifest does not carry the processing scope source sha256")
        if manifest.retrieval is None:
            return CatalogEntry(..., retrieval_status="not_indexed", reason="current processing has no retrieval publication", ...)
        plan, _index = outputs.load_retrieval(manifest.retrieval)      # 指纹/维度/向量一致性（既有守卫）
        validate_processing_source(sources=sources, artifacts=outputs.assets, manifest=manifest, plan=plan)
        fingerprints = {m.embedding_fingerprint for m in plan.members}
        if len(fingerprints) != 1:
            raise ValueError("Retrieval snapshot mixes embedding providers")
        return CatalogEntry(document_id=document_id or scope.source_sha256, ..., retrieval_status="ready",
                            current_processing_id=processing_id, retrieval_snapshot_id=manifest.retrieval.snapshot_id,
                            member_count=len(plan.members), embedding_fingerprint=fingerprints.pop(),
                            embedding_dimensions=tuple(sorted({m.embedding_dimensions for m in plan.members})),
                            document_label=snapshot.manifest.filename, source_page_count=scope.source_page_count,
                            selected_physical_pages=scope.physical_pages,
                            source_activated=_pointer(source_root / "current-manifest") == scope.source_manifest_id)
    except (ValueError, OSError) as error:        # pydantic.ValidationError / JSONDecodeError 都是 ValueError；FileNotFoundError 是 OSError
        return CatalogEntry(document_id=document_id or source_root.name, ..., retrieval_status="corrupt",
                            reason=str(error) or type(error).__name__)
```
- 只 `read_text` 指针、`load`/`get` 资产；不调用 `publish`/`activate`/`save_draft`/`_write_pointer`。
- 不捕获更宽的 `Exception`（ruff BLE001；且真 bug 应冒泡）。
- 成本：`load_retrieval` 会读索引 JSON（AIA 约 10 MB），仅在启动扫描一次，可接受。

#### 挂载（约束 1/2/3）

```python
class QueryEmbeddingUnavailable(RuntimeError):
    """The mount has no query embedder; evidence reads still work."""

def mount_document(entry: CatalogEntry, *, embedder: EmbeddingPort | None) -> MountedDocument
```
- `embedder` 为 keyword-only、**无默认值**：调用方必须显式给出（真实 adapter、离线替身或 `None`）。`None` = 只读证据挂载（`resolve`/`status` 可用，`search` 抛 `QueryEmbeddingUnavailable`），对应现有"缺配置时来源读取继续、search 503"的行为。
- 校验顺序：
  1. `entry.retrieval_status != "ready"` 或关键字段为 None → `ValueError(f"Catalog entry {id} is not mountable: {reason}")`（corrupt/not_indexed 可见不可挂载）。
  2. `embedder is not None and entry.embedding_fingerprint != embedder.fingerprint` → `ValueError("Embedding provider differs from the published index; refusing to mount")`（绝不静默用错模型；`LocalEmbeddingAdapter.fingerprint` 是配置字符串，比对不触发任何请求）。
  3. `outputs.load(entry.current_processing_id)`（**按 ID 而非指针**：挂载后指针再移动也不影响本 mount，快照不可变）；`manifest.retrieval is None or snapshot_id != entry.retrieval_snapshot_id` → `ValueError("Pinned retrieval snapshot differs from the catalog entry")`。
  4. `plan, _ = outputs.load_retrieval(publication)`；再比一次 `{m.embedding_fingerprint} == {embedder.fingerprint}`（防 entry 被人为构造）。
  5. `validate_processing_source(...)`。
- 返回 `MountedDocument(entry, sources, outputs, pinned_manifest, publication, ProcessingRetrieval(sources, outputs, embedder) if embedder else None)`。构造全程零 `embed_*` 调用。

```python
class MountedDocument:                      # 普通类，属性私有，只暴露方法/只读属性；不暴露 store
    document_id / processing_id / snapshot_id / embedding_fingerprint: str  (property)
    entry: CatalogEntry                      (property)
    def manifest(self) -> ProcessingManifest:        # = checked()：outputs.load(pinned_id) 并与 pinned 比对；不同 → ValueError("Immutable processing manifest changed")
    def search(self, query: str, *, limit: int = 5) -> tuple[PinnedRetrievalHit, ...]:
        self.manifest(); if self._retrieval is None: raise QueryEmbeddingUnavailable(...)
        return self._retrieval.search(self._publication, query, limit=limit)      # 恰好一次 embed_query；既有指纹/维度守卫
    def resolve(self, hit: PinnedRetrievalHit) -> RetrievalContext:
        self.manifest(); return resolve_processing_context(self._sources, self._outputs, self._publication, hit)  # 零模型
    def read_asset(self, ref: AssetRef) -> bytes:    # 供第 3 项取 SVG 证据字节：先 outputs.assets.get(ref)，FileNotFoundError 再 sources.get(ref)；两处都做 digest 校验。实现时用 e2e 断言确认 member.source_svg 的归属，勿猜。
```

```python
class MountedCatalog:                        # 普通类；供 HTTP 与第 3 项一次性挂载全部 ready 项
    catalog: DocumentCatalog
    embedding_fingerprint: str | None
    documents: Mapping[str, MountedDocument]     # document_id → mount
    failures: Mapping[str, str]                  # ready 但拒绝挂载的 document_id → 原因（如指纹不符）

def mount_catalog(catalog: DocumentCatalog, *, embedder: EmbeddingPort | None) -> MountedCatalog
```
逐个 `mount_document`，`ValueError | OSError` 记进 `failures`，不中断其它文档、不抛出、不隐藏。

### 2.3 HTTP 通用化

#### `src/enterprise_pdf_rag/adapters/http/catalog_schemas.py`（新，公开契约 `document-catalog-v1`）

```python
class DocumentListItem(BoundaryModel):        # CatalogEntry 全部字段 + 挂载态
    ... CatalogEntry 字段 ...
    mounted: bool
    mount_error: str | None = None
class DocumentListResponse(BoundaryModel):
    schema_version: Literal["document-catalog-v1"] = "document-catalog-v1"
    ingestion_root: str
    legacy_roots: tuple[str, ...]
    embedding_configured: bool
    embedding_fingerprint: str | None
    documents: tuple[DocumentListItem, ...]
    unpublished: tuple[str, ...]
class DocumentDetailResponse(BoundaryModel):
    schema_version: Literal["document-catalog-v1"] = "document-catalog-v1"
    document: DocumentListItem
    status: ProcessingStatusResponse | None      # 已挂载时 = processing_status(processing_id, mount.manifest())
class DocumentSearchRequest(BoundaryModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=100)
class DocumentSearchResponse(BoundaryModel):
    document_id: str
    processing_id: str
    snapshot_id: str
    hits: tuple[PinnedRetrievalHit, ...]
class DocumentContextRequest(BoundaryModel):
    hit: RetrievalHitInput                        # 复用 processing_review.RetrievalHitInput
class DocumentContextResponse(BoundaryModel):
    document_id: str
    processing_id: str
    context: RetrievalContext
```

#### `src/enterprise_pdf_rag/adapters/http/documents.py`（新）

```python
def create_documents_router(mounted: MountedCatalog) -> APIRouter
def create_documents_app(catalog: DocumentCatalog, *, embedder: EmbeddingPort | None) -> FastAPI
```

| method | path | 行为 | 状态码 |
|---|---|---|---|
| GET | `/v1/documents` | `DocumentListResponse`（catalog 全部项 + `mounted`/`mount_error`） | 200 |
| GET | `/v1/documents/{document_id}` | 详情；已挂载附 `ProcessingStatusResponse` | 200 / 404 未知 id / 409 pinned manifest 变化 |
| GET | `/v1/documents/{document_id}/manifest` | `ProcessingSnapshotResponse(snapshot_id=processing_id, manifest=mount.manifest())`（复用既有 schema） | 200 / 404 / 409 |
| POST | `/v1/documents/{document_id}/search` | `mount.search(body.query, limit=body.limit)` | 200；404 未知；409 entry 非 ready / 未挂载（附 reason）；503 `QueryEmbeddingUnavailable` 或 `ProviderRequestError`；409 其它 `ValueError/OSError` |
| POST | `/v1/documents/{document_id}/context` | `mount.resolve(PinnedRetrievalHit(**body.hit))` | 200；404；409（含跨文档 hit：`"belongs to another semantic snapshot"`） |

- 映射规则与 `processing_review.py` 一致：损坏/不一致 → 409，模型不可用/失败 → 503，不存在 → 404。`ProviderRequestError` 是 `ValueError` 子类，必须**先于** `ValueError` 捕获（现有 search 路由 `:224-226` 亦如此）。
- `create_documents_app`：`mounted = mount_catalog(catalog, embedder=embedder)`；`FastAPI(title="Enterprise PDF RAG — document catalog", version="0.1.0.dev0")`；`include_router`；注册 `ValueError`/`OSError` → 409 JSON handler（同 `aia_review.py:85-91` 写法）。
- **不**把 `/v1/processing/review/{relative:path}` 与 `/v1/queries`（chart-qa）参数化到本切片：前者白名单正则是 AIA 布局专属（`page-\d{3}`、`chart-qa-v2-evaluations/…`），后者需要按文档的 `ChartQAService`，而 ADR 0010 明确通用入口不承诺任意图表资格。作为后续项记录。

#### `src/enterprise_pdf_rag/adapters/http/app.py` 改动

`create_configured_app()` 新增分支（放在 `aia-source-review` 分支之后、`return create_app(...)` 之前）：
```python
if configured == "document-catalog":
    settings = get_settings()
    catalog = scan_catalog(settings.ingestion_root, legacy_roots=settings.legacy_document_roots)
    try:
        embedder = LocalEmbeddingAdapter(load_local_model_config("embedding"))   # 显式构造一次，共享给全部 mount
    except ProviderConfigurationError:
        embedder = None
    return create_documents_app(catalog, embedder=embedder)
```
- `aia-source-review` 分支**本轮不动**（运行中的 8766/8767、`webui_preview.py:249` 的 profile 白名单、`compose.yaml:15`、`backend.Dockerfile:13` 都绑它）；AIA 常量 import 仍保留给该分支。移除属于第 3 项完成后的服务切换。
- 启动零模型连接：`LocalEmbeddingAdapter` 构造与 `mount_catalog` 都不发请求；指纹比对只用配置字符串。

#### 门与文档同步
- `scripts/enterprise_pdf_rag/check_schema.py` `CONTRACTS` 增 `"document-catalog-v1": (DocumentListResponse, DocumentDetailResponse, DocumentSearchRequest, DocumentSearchResponse, DocumentContextRequest, DocumentContextResponse)`；生成 `docs/enterprise-pdf-rag/schemas/document-catalog-v1.json`（无导出脚本，一次性：`uv run --locked python -c 'import json; from enterprise_pdf_rag.adapters.http.catalog_schemas import *; print(json.dumps({m.__name__: m.model_json_schema() for m in (...)}, indent=2, ensure_ascii=False))' > docs/enterprise-pdf-rag/schemas/document-catalog-v1.json`，具体格式以 `check_schema.py:87-95` 的比对方式为准，先看现有 json 的缩进/排序再生成）。
- `src/enterprise_pdf_rag/CLAUDE.md`：`adapters/` 一行补 `document_catalog.py (catalog / read-only mount)`；`verified-against` bump 到新 HEAD（否则 `scripts/ci.sh` 第 2 步红）。

### 2.4 AIA 样本的兼容方式（二选一 → 选 A）

**A（推荐）：`APP_LEGACY_DOCUMENT_ROOTS='["/Users/linhan/startup/spine/ragspine/data/output/aia-2026-interim/pages-001-020"]'`。** 规则只有一条：legacy 项是 processing store 根，父目录是 source store 根。`document_id` 从 manifest `scope.source_sha256` 读出（不写死 `df9023…`），label 从 source manifest `filename` 读出，页数从 scope 读出。零写入 `data/`、跨平台、默认 `()` 不含 AIA（约束 5）。
**B（拒绝）：在 `data/ingestion/<aia-sha>/` 放 `source`/`processing` 符号链接或搬目录。** 会改动受保护的运行现场（交接明确"不要清理或覆盖"），Windows 符号链接需特权，且 `objects/sha256` 硬链接语义在搬移后不可控。

### 2.5 约束逐条落实

| 约束 | 落实点 |
|---|---|
| 1 不可变 | catalog 只 `read_text` 指针 + `load`/`get`；mount 按 `current_processing_id` **ID** 打开（非指针）；`LocalDocumentStore(..., activate_on_publish=False)`；模块内不出现 `publish`/`activate`/`save_draft`/`_write_pointer`；测试用指针字节 + `objects/sha256` 目录列表前后哈希断言零写 |
| 2 损坏证据拒绝 | 沿用 `ProcessingStore.load/load_retrieval`、`LocalDocumentStore.load/get`、`validate_processing_source` 的 `ValueError`；catalog 转为 `corrupt+reason`（可见）；`mount_document` 对非 ready 抛 `ValueError`；HTTP 409；每次请求 `mount.manifest()` 重校验 pinned manifest |
| 3 无隐式模型调用 | `scan_catalog` 无 embedder 参数；`mount_document(embedder=...)` keyword-only 必填；`mount_catalog`/构造零 `embed_*`；app factory 显式 `LocalEmbeddingAdapter(load_local_model_config("embedding"))` 一次共享；测试注入 `OfflineDescriptionEmbedder`/`RecordingEmbedding`；指纹不符 → `ValueError`/409 拒绝挂载，另有 `ProcessingRetrieval.search` 既有二次守卫 |
| 4 配置隔离 | `Settings.ingestion_dir`(`APP_INGESTION_DIR`)、`legacy_document_roots`(`APP_LEGACY_DOCUMENT_ROOTS`)、`execution_mode="document-catalog"`；`EMBEDDING_*` 仍由 `providers.py` 独立读取；每文档只开自己的两条 store |
| 5 按 ID 工作 | 新模块零 AIA 常量、零文件名/页数/SHA 字面量；AIA 仅作为 legacy 根被配置发现 |

---

## 3. 既有文件最小改动清单

| 文件 | 改动 |
|---|---|
| `src/enterprise_pdf_rag/core/settings.py` | `execution_mode` Literal 增 `"document-catalog"`；新增 `ingestion_dir`、`legacy_document_roots` 字段与 validator；`ingestion_root` property |
| `src/enterprise_pdf_rag/adapters/pdf_ingestion.py:144` | `get_settings().data_dir / "ingestion"` → `get_settings().ingestion_root` |
| `src/enterprise_pdf_rag/adapters/http/app.py` | `create_configured_app` 增 `document-catalog` 分支（约 10 行）+ 2 个 import |
| `scripts/enterprise_pdf_rag/check_schema.py` | `CONTRACTS` 登记 `document-catalog-v1` + import |
| `docs/enterprise-pdf-rag/schemas/document-catalog-v1.json` | 新增（生成） |
| `src/enterprise_pdf_rag/CLAUDE.md` | 目录树一行 + `verified-against` bump |
| `docs/enterprise-pdf-rag/adr/0011-document-catalog-and-read-only-mount.md` | 新 ADR（决定、被拒方案 B、不承诺项） |
| `docs/enterprise-pdf-rag/testing-and-ingestion.md` | 新节"文档目录与按文档搜索"：env、`serve` 用法、curl 示例、AIA legacy 根示例 |
| `docs/enterprise-pdf-rag/CLAUDE_HANDOFF.md` | 第 2 项标记完成 + 证据路径 |

**不改**：`processing_review.py`、`aia_review.py`、`draft_publication.py`、`processing_retrieval.py`、`processing_store.py`、`document_store.py`、`cli.py`（`serve` 仍无参，配置走 env/yaml）、`webui_gate.py`/`webui_preview.py`（WebUI 切换属第 3 项）。

新增：`adapters/document_catalog.py`、`adapters/http/catalog_schemas.py`、`adapters/http/documents.py`、三个测试文件。

---

## 4. 测试计划（全离线，`tests/enterprise_pdf_rag/adapters/`）

公共手法：复用 `test_generic_publication_e2e.py` 的 `_ingest_generic_semantics(tmp_path, monkeypatch, ...)`（`:78-104`，内部 `authored_pdf` + `_text_partition_sender` 桩 + `ingest_pdf(stage="semantics", max_live_calls=3, output_dir=tmp_path/"ingestion")`）→ `qualify_draft` → `index_draft(embedder=OfflineDescriptionEmbedder())` → `publish_draft(processing_id=indexed.indexed_processing_id)`。若跨模块私有 import 不便，把这两个 helper 抽到 `tests/enterprise_pdf_rag/adapters/generic_publication_helpers.py`，e2e 文件改两行 import（可选，实施时定）。

### 4.1 `test_document_catalog.py`
fixture `two_published(tmp_path, monkeypatch) -> tuple[Path, DraftPublication, DraftPublication]`：在 `tmp_path/"ingestion"` 下发布 `label="Meridian revenue"`（3 页）与 `label="Orion expense"`（2 页）两份程序化 PDF；再 ingest 第三份但不 index/publish（draft）。

| 用例 | 断言 |
|---|---|
| `test_scan_lists_two_ready_documents_and_one_draft` | `len(catalog.ready)==2`，按 id 排序；每项 `source_sha256==document_id`、`current_processing_id==pub.current_processing_id`、`retrieval_snapshot_id==pub.retrieval_snapshot_id`、`member_count==pub.member_count`、`embedding_fingerprint=="offline-demo/token-hash-64-v1"`、`embedding_dimensions==(64,)`、`source_store/processing_store` 为绝对路径且等于 pub 的；`unpublished==(第三份 sha,)` |
| `test_scan_missing_root_is_empty` | 不存在的根 → `documents==()`，不抛 |
| `test_scan_marks_corrupt_pointer_visible_and_unmountable` | 把 A 的 `current-processing` 改成不存在的 64-hex（或非法文本）→ 该项 `retrieval_status=="corrupt"`、`reason` 非空、其它项仍 ready；`mount_document(entry, embedder=...)` → `pytest.raises(ValueError, match="not mountable")` |
| `test_scan_marks_tampered_index_corrupt` | 用 `test_draft_publication.py:341-390` 手法伪造 manifest 并把指针指向伪造 sha → corrupt，reason 含既有消息片段 |
| `test_scan_marks_directory_name_mismatch_corrupt` | 把 A 的整个 `<sha>/` 目录复制/重命名为另一个 64-hex 名 → corrupt("does not match") |
| `test_scan_is_read_only` | 扫描前后：两指针文件字节、`objects/sha256` 目录名集合、`current-manifest` 存在性完全相同 |
| `test_mount_rejects_wrong_embedder_fingerprint` | `mount_document(entryA, embedder=RecordingEmbedding())` → `ValueError` match `"provider"`；不产生任何 `embed_*` 调用（用计数包装器） |
| `test_mount_and_search_stay_within_one_document` | `mA = mount_document(entryA, embedder=Offline())`，`hits = mA.search("revenue", limit=5)`：全部 `hit.snapshot_id == entryA.retrieval_snapshot_id`；`ctx = mA.resolve(hits[0])`，`"Meridian" in ctx.description.text`；`mA.resolve(hitB)` → `ValueError` match `"another semantic snapshot"` |
| `test_mount_makes_no_embedding_calls_and_search_makes_one` | 计数包装 `OfflineDescriptionEmbedder`：mount 后 0 次，一次 search 后 `embed_query==1`、`embed_description==0`，resolve 不增加 |
| `test_mount_pins_snapshot_after_pointer_moves` | 挂载 A 后把 A 的 `current-processing` 改指到伪造/其它 id：`mA.search` 仍工作（pinned ID 未损坏）；重新 `scan_catalog` 得到新状态 |
| `test_mount_without_embedder_serves_evidence_only` | `mount_document(entryA, embedder=None)`：`resolve` 可用，`search` → `QueryEmbeddingUnavailable` |
| `test_mount_catalog_records_failures` | 一份 ready + 一份 corrupt + 错指纹 → `documents` 只含可挂载项，`failures` 含另两项原因 |

### 4.2 `test_documents_http.py`（`httpx2.ASGITransport`）
| 用例 | 断言 |
|---|---|
| `test_list_documents` | `GET /v1/documents` 200：2 ready+mounted、1 corrupt+`mounted=False`；`embedding_configured=True`、`embedding_fingerprint` |
| `test_detail_unknown_404` / `test_detail_corrupt_200_with_reason` | 404 / 200 且 `document.retrieval_status=="corrupt"`、`status is None` |
| `test_search_corrupt_409` | 409，body 含 reason |
| `test_search_and_context_roundtrip` | search 200 → 取 hits[0] → context 200，`context.member.member_id` 一致，`processing_id` 与 catalog 一致 |
| `test_context_cross_document_409` | 用 B 的 hit 打 A 的 context → 409 |
| `test_search_without_embedder_503_but_list_and_context_work` | `create_documents_app(catalog, embedder=None)` |
| `test_search_wrong_fingerprint_is_unmounted_409` | `embedder=RecordingEmbedding()` → 列表 `mount_error` 含 provider，search 409 |
| `test_configured_app_document_catalog_mode` | `monkeypatch.setenv("APP_EXECUTION_MODE","document-catalog")`、`APP_INGESTION_DIR=tmp`、`EMBEDDING_*` 三项、`monkeypatch.setattr(app_module,"LocalEmbeddingAdapter", lambda config: OfflineDescriptionEmbedder())`、`get_settings.cache_clear()`；列表 200；`finally: cache_clear()`。另一参数化：删 `EMBEDDING_*` → 列表 200、search 503 |
| `test_configured_app_legacy_root_env` | `APP_LEGACY_DOCUMENT_ROOTS='["<tmp>/x/processing"]'`（把一份已发布文档的 `processing` 目录当 legacy 根，父目录须含 `current-manifest`，故构造时把 `source` 内容放到父目录或用 `publish --activate-source` 后的布局）→ 被发现，`origin=="legacy"` |

### 4.3 `test_document_catalog_aia_smoke.py`（真实 AIA store，只读；不存在则 skip）
```python
_AIA_PROCESSING = PROCESSING_OUTPUT            # 仅测试引用 AIA 常量，产品代码不引用
pytestmark = pytest.mark.skipif(not (_AIA_PROCESSING / "current-processing").is_file(), reason="AIA sample store absent")
```
- `scan_catalog(tmp_path/"empty", legacy_roots=(_AIA_PROCESSING,))` → 恰 1 项 `ready`、`origin=="legacy"`、`document_id == manifest.scope.source_sha256`（从 store 读，不写死）、`member_count == len(plan.members)`（与 `outputs.load_retrieval` 对照，不写死 189）、`embedding_fingerprint.startswith("local-http/")`。
- 只读：扫描 + `mount_document(entry, embedder=RecordingEmbedding())`（预期 `ValueError` 指纹不符，证明**不调模型也能拒绝**）前后，`current-manifest`/`current-processing` 字节与 `objects/sha256` 条目数不变。
- 不构造 `LocalEmbeddingAdapter`（`no_network` 且无隧道）。

---

## 5. 分阶段实施清单

### 阶段 1：纯 Python 层（catalog + mount + 配置）
1. `core/settings.py` 三处改动；`pdf_ingestion.py:144` 一行。
2. 新建 `adapters/document_catalog.py`（模型、`scan_catalog`、`mount_document`、`MountedDocument`、`mount_catalog`、`QueryEmbeddingUnavailable`）。
3. 新建 `tests/enterprise_pdf_rag/adapters/test_document_catalog.py` 与 `test_document_catalog_aia_smoke.py`（先 RED 后 GREEN）。
4. 验证：
   ```sh
   cd /Users/linhan/startup/spine/ragspine
   uv run --locked python -m pytest tests/enterprise_pdf_rag -q
   uv run --locked python -m mypy                                  # ci.sh 第 3 步
   uv run --locked python -m ruff check . && uv run --locked python -m ruff format --check .   # 第 4 步
   git status --short data/                                        # 必须为空；两指针 shasum 前后一致
   ```

### 阶段 2：HTTP 与 app factory
1. 新建 `adapters/http/catalog_schemas.py`、`adapters/http/documents.py`；`app.py` 增分支。
2. `check_schema.py` 登记 + 生成 `docs/enterprise-pdf-rag/schemas/document-catalog-v1.json`。
3. 新建 `tests/enterprise_pdf_rag/adapters/test_documents_http.py`。
4. 验证：阶段 1 三条 +
   ```sh
   uv run --locked python scripts/enterprise_pdf_rag/check_conformance.py .   # ci.sh 第 9 步四条
   uv run --locked python scripts/enterprise_pdf_rag/check_architecture.py
   uv run --locked python scripts/enterprise_pdf_rag/check_schema.py
   uv run --locked python scripts/enterprise_pdf_rag/check_drift.py
   APP_EXECUTION_MODE=document-catalog APP_LEGACY_DOCUMENT_ROOTS='["'"$PWD"'/data/output/aia-2026-interim/pages-001-020"]' \
     uv run --locked python -c 'from enterprise_pdf_rag.adapters.http.app import create_configured_app; app=create_configured_app(); print([r.path for r in app.routes])'
   ```
   （最后一条是本机只读 smoke：无 `EMBEDDING_*` 时应正常起 app、AIA 项 `mounted=False`/evidence-only；不启动 uvicorn、不占端口。）

### 阶段 3：文档与完整门
1. ADR 0011、`testing-and-ingestion.md` 新节、`CLAUDE_HANDOFF.md` 第 2 项状态、`src/enterprise_pdf_rag/CLAUDE.md` 树 + `verified-against` bump。
2. 验证：`bash scripts/ci.sh` 全绿；`uv run --locked python scripts/check_doc_drift.py --quiet`；`git status --short data/` 为空。
3. 不 commit / 不 push，除非用户明确授权（交接第 5 项）。

---

## 6. 需要拍板的设计点（≤2）

1. **AIA 兼容方式**：推荐 A —— `APP_LEGACY_DOCUMENT_ROOTS`（每项为 processing 根，父目录为 source 根），默认空；拒绝 B（symlink/搬目录进 `data/ingestion/<sha>/`，会动受保护现场且跨平台脆弱）。
2. **执行模式并存**：推荐新增 `execution_mode="document-catalog"` 并**保留** `aia-source-review` 分支原样（WebUI gate、`webui_preview.py` profile 白名单、compose/Dockerfile 均绑它，替换属于第 3 项后的服务切换）；不推荐现在就删 AIA 分支或把 `/v1/documents` 塞进 `create_aia_app`。

已按默认假设写入方案（无需拍板，但如不同意请指出）：`not_indexed` 作为第三种可见不可挂载状态；`mount_document(embedder=None)` 允许"仅证据挂载"以保持现有"缺配置时 context 仍可用、search 503"行为；review 静态文件路由与 `/v1/queries` 不在本切片参数化。

---

## 7. 非目标 / 风险
- 不实现自然语言回答、rerank、WebUI 文档选择、上传端点、review HTML 通用白名单、任意图表资格。
- 真实 `LocalEmbeddingAdapter` 下的在线 search 仍需隧道环境，本切片只能离线证明指纹拒绝与零调用；不能据此宣称新文档"已可在线检索"。
- `scan_catalog` 对每个已发布文档读一次索引 JSON；文档数量大时启动变慢，可后续加惰性 `load_retrieval`（本切片不做）。
- 若实现时发现 `member.source_svg` 归属于 source store 而非 processing assets，`read_asset` 的回退顺序按 e2e 断言调整，不猜。

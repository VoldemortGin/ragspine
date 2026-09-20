> 实施分支 `feat/visual-objects`（2026-09-21 落地）。本文件是**方案原文**，只在顶部加了这行注记；实现与方案的偏离、最终口径与遗留以 [ADR 0015](../adr/0015-diagram-and-formula-retrievable.md) 与 [交接文档](../CLAUDE_HANDOFF.md) 为准。

# 方案：让 `ObjectKind.FORMULA` 对象可检索（token 序列 IR + 无模型来源资格 + 检索期投影 + 可引用路径）

仓库 `/Users/linhan/startup/spine/ragspine`，基线 `main` = `c15525b`（`feat/page-metadata` 已 fast-forward 合并；ADR 0013 / policy v4 已在 main）。只读设计，不改仓库。
所有路径相对 `src/enterprise_pdf_rag/`，除非写明 `tests/`、`scripts/`、`docs/`。行号一律以 `git show c15525b:<path> | cat -n` 为准。
与图表方案共用**同一次 policy v5 升级**（§4.2），两份方案落地时只改一次 `_POLICY`。

---

## 0. 目标与不变量

**目标（可验证）**：一个由 partition 判为 `Formula` 的版面对象，在**不调用任何模型**的前提下，被拆成逐字来自 span 的 token 序列，分数线 / 根号来自 `get_cdrawings()` 真实路径，上下标附证明强度；全部证明成立才产出 `qualified_ir` / `qualified_description` / `qualification` 三个 stage，进入 `eligibility()`、索引投影、context block 与 claim 校验；任何一个 token / 路径 / 脚本无法证明 → 整个公式 PENDING，不放行，诊断串逐字。

| 不变量（简报 §"不变量"） | 本方案落点 |
|---|---|
| 逐字证据 | 每个 `FormulaToken.text == span.text[char_start:char_end]`；每个 span 的 token 子串按序拼接 == 去空白后的 span 全文（§2.3 tiling 闭合规则）；`readable` / `linear` 只由 token 文本 + 固定连接词 / 固定语法拼出，不含任何模型字符串 |
| fail closed | `check_formula` 返回 `ir=None` 的任一原因（§3.3 诊断表）→ `qualification` stage 为 `UNAVAILABLE`，不写 `qualified_*`；`eligibility` 于是拒绝（"required qualification stages are incomplete" 之前先被 kind-特定理由拦下） |
| 零人工 | 资格判定是纯函数 `processing/formula_rules.py::check_formula`，输入只有 pdfspine 观测（§2.4），没有 fixture 凭据、没有人工登记 |
| LLM 只在构建阶段 | `visual_semantics` 的两路 VLM 保持原样，只作为 lineage；回答链 `_verify_formula` 只比字符串（§4.4） |
| same-SVG two branches | IR 分支（`ir`，VLM）与 description 分支（`description`，VLM）不变、互不为输入；资格分支 `formula_observation → qualified_ir/qualified_description` 无模型，独立于两路 VLM，VLM `source_literal` 只做"一致 / 不一致 / 不可用"的信息性记录（§3.2） |
| 快照不可变 | description 资产字节不动；新增的是新 stage（`qualified_description` 是新资产），检索文本是 `member_index_text` 的检索期投影（§4.2） |
| 旧快照可挂载 | policy v5 只在 `member_text` 读取侧分支；`PROJECTED_CHART_POLICIES` / `CONTEXTUAL_POLICIES` 把 v4 加进去；没有任何代码按 policy 拒收（ADR 0012:143-146、0013:117-118） |
| import 白名单 | **纠偏**：`scripts/enterprise_pdf_rag/check_architecture.py:8-16` 规定 `processing/` **只允许标准库**（只有 `answers` 额外允许 pydantic）。因此 §2 的所有 `processing/` 新模型都是 `dataclass(frozen=True, slots=True)`，序列化用 `TypeAdapter` 只在 `adapters/` 里做（与 `table_models.py` / `typed_ir.py` 同款）。pdfspine 只在 `adapters/pdfspine_formula.py` import |

---

## 1. 现状事实

### 1.1 事实表（仓库，`c15525b`）

| 路径:行号 | 签名 / 要点 |
|---|---|
| `processing/typed_ir.py:310-318` | `FormulaIR(object_id, source: SourceAnchor, source_literal: str\|None, latex: str\|None, source_span_ids, diagnostics, verification=PENDING)` —— 无 token / 结构字段 |
| `processing/typed_ir.py:98` | `type TypedIR = TextIR \| ListIR \| TableIR \| ChartIR \| DiagramIR \| ImageIR \| FormulaIR \| GroupIR` |
| `processing/typed_ir.py:101-109` | `ObjectDescription(object_id, source, source_span_ids, text, producer, confidence, verification)` —— 视觉 description 与表格 description 共用此类 |
| `adapters/visual_semantics.py:301-327` | `VisualSemanticAdapter.infer(*, page, item, native_svg) -> VisualInference`；只收 IMAGE/DIAGRAM/FORMULA |
| `adapters/visual_semantics.py:449-471` | Formula 映射：`literal = "".join(element.text for element in elements)`，`FormulaIR(..., literal, dto.latex, _source_ids(elements), (...,"Normalized form state=..."), Verification.PENDING)` —— 位置参数构造，新增默认字段不影响 |
| `adapters/visual_semantics.py:113-226` | `_prepare`：元素 id = `"obs-"+sha256(f"{span_id}:{start}:{end}")[:16]`（151），一个 span 一个 observation（去首尾空白），**不切子串** |
| `adapters/visual_semantics.py:61-76` | `VisualInference.description_index_eligible: bool = False` 空挂钩，无生产消费者 |
| `adapters/semantic_objects.py:139-141` | 分发第一跳：TEXT/LIST/GROUP → `ProcessingObjectAdapter` |
| `adapters/semantic_objects.py:176-183` | 非文字对象先落 `native_crop`、`source_text`；CHART → `_chart`，TABLE → `_table` |
| `adapters/semantic_objects.py:184-223` | IMAGE/DIAGRAM/FORMULA 唯一路径：`VisualSemanticAdapter.infer` → 落 `svg`/`model_render`/`model_view`/`ir(_raw)`/`description(_raw)` |
| **`adapters/semantic_objects.py:217-222`** | `qualification` 恒 `writer.diagnostic("qualification", "Visual semantics are source-bound model inferences; an independent field/relationship verifier is not available for this object.")` → `StageState.UNAVAILABLE`（`_Writer.diagnostic` 默认 `failed=False`，47-98） |
| `adapters/semantic_objects.py:225-304` | `_table`：`self.sources.load(page.source_manifest_id)` → `self.sources.get(source.manifest.source)` 取 **PDF 字节** → `PdfspineTableAdapter().extract(pdf, page=, item=)`；成功后 `ir` + `source_table_description` + `LiteralQualification` —— **无模型资格落盘的既有模板** |
| `adapters/semantic_objects.py:490-508` | `_unavailable`：prepare 失败的兜底（`svg` + `ir` FAILED + `description`/`qualification` UNAVAILABLE） |
| `adapters/pdfspine_tables.py:53-88` | `pdfspine.open(stream=pdf, filetype="pdf")` → `load_page` → 校验 `rotation==0` 与 `rect==(0,0,w,h)` → `finally: document.close()`；`_validate_input`(90-108) 校验 sha256 / kind / bbox / span 归属 |
| `adapters/pdfspine_document.py:66-96` | span id 规则：`occurrence = f"{source_digest}:{page_index}:{block_index}:{line_index}:{span_index}"`, `span_id = f"span-v1-{sha256(occurrence).hexdigest()}"`；只投影 `text/bbox/origin/font/size/dir` 六字段（`_Span` 39-64） |
| `adapters/pdfspine_document.py:106` | `page.get_text("dict")`；`_extract_page` 99-124 拒绝旋转页 / 非默认 cropbox |
| `adapters/pdfspine_figure.py:32-42` | `_Drawing(BaseModel, extra="forbid")`：`type, rect, color, fill, width, dashes, closePath(alias close_path), even_odd, items` —— 与 pdfspine 0.11.0 `get_cdrawings()` 的 9 个键一一对应 |
| `adapters/pdfspine_figure.py:91-98` | `_top_left(bounds, height)` / `_point(value, height)`：bottom-left → top-left 翻转（`height - y`） |
| `adapters/source_objects.py:20-23,117-143` | `_LITERAL_PRODUCER = "exact-source-transcription-v1"`、`_LITERAL_CONFIDENCE = Confidence(None, "deterministic source occurrence transcription; no semantic inference")`；`source_table_description` 产 `ObjectDescription(..., "\n".join(span texts), producer, confidence, Verification.VERIFIED)` |
| `adapters/literal_qualification.py:28-152` | `validate_literal_member(sources, assets, scope, member)`：重读回执 + description + sidecar，逐字段比对；`131-132: raise ValueError("Unsupported qualification for this object kind")` —— FORMULA 若走这里会撞 |
| `adapters/literal_qualification.py:99-107` | SVG crop 逐字节重算比对（`crop_native_svg(...) == assets.get(member.source_svg)`） |
| `adapters/chart_publication.py:39-70` | `_ChartReceipt` / `ChartPublicationReceipt(schema_version="source-chart-qualification-v1")`（pydantic strict/frozen/forbid），`parse_chart_receipt` |
| `adapters/chart_publication.py:81-159` | `resolve_chart_member`："No model/network calls: reconstruct source, then repeat the scoped proof"，lineage 闭包精确相等（116-127），重算后 `!= expected → raise`（149-159） |
| `adapters/processing_retrieval.py:60-74` | `_POLICY = "...-v4"`, `PROJECTED_CHART_POLICIES = {v4, v3, donut-bar-v2}`, `CONTEXTUAL_POLICIES = {v4}` |
| `adapters/processing_retrieval.py:77-99` | `member_text(assets, plan, member, context=None)`：非 CHART 一律 `ObjectDescription.text`；CHART 且 policy ∈ PROJECTED → `member_index_text(chart, body)`；policy ∈ CONTEXTUAL → 加页头 |
| **`adapters/processing_retrieval.py:101-130`** | `eligibility(record)`：kind 白名单 5 类（107-113）+ 四 stage 全 SUCCEEDED（116-129）；拒绝串 `f"{record.kind.value} objects are not retrievable"`（114） |
| `adapters/processing_retrieval.py:144-256` | `build(scope, records, contexts=None)`：158-162 必需 stage 元组（CHART 用 `qualified_*`）；173-203 CHART lineage；217-221 `text = contextual_index_text(member_index_text(checked_ir, checked_description.text), ...)` |
| `adapters/processing_retrieval.py:347-356` | `_qualified`：CHART → `validate_retrieval_chart_member`，否则 `validate_literal_member` |
| `adapters/processing_retrieval.py:359-374` | `resolve_processing_context`：同样二分 |
| `adapters/source_publication.py:76-83` | `validate_processing_source` 对 plan 成员同样二分（CHART / 其它） |
| `adapters/processing_store.py:115-126` | 挂载校验 `stages.get("qualified_ir", stages.get("ir"))` / `stages.get("qualified_description", stages.get("description"))` —— 新 kind 用 `qualified_*` 命名即兼容 |
| `adapters/processing_store.py:230-244` | `processing_assets` 收集**所有** object stage artifact —— 新 stage 无需改动 |
| `adapters/draft_publication.py:38-79` | `qualify_draft` 只调 `eligibility`，`kinds` / `skipped_reasons` 计数 —— 新 kind 自动出现在 CLI `qualify` 输出 |
| `processing/index_text.py:85-89` | `member_index_text(ir, description_text)`：`isinstance(ir, ChartIR)` 才投影 |
| `processing/context_builder.py:21-35` | `BlockKind` 5 个；`_KIND_OF_OBJECT` 5 项 |
| `processing/context_builder.py:70-122` | `ContextBlock` + `prompt_text()`（可引用路径唯一渲染处；表格行 116、文字行 118） |
| `processing/context_builder.py:163-224` | `build_context_block`；224 `raise ValueError(f"{type(ir).__name__} members are not supported as answer context")` |
| `processing/retrieval.py:143-155` | `RetrievalContext(snapshot_id, member, ir: TypedIR, description: ObjectDescription\|TextDescription, qualification: LiteralQualification\|FigureQualification)` + `.scope` |
| `answers/models.py:18-22` | `ClaimKind`：QUOTE / CELL / CHART_VALUE |
| `answers/models.py:109-119` | `ClaimCitation(member_id, kind: BlockKind, page_index, field_path, evidence_ids, bbox\|None, quote, chart_citation=None, page_title=None)` |
| `answers/prompt.py:17-23` | `ModelClaim.kind: Literal["quote", "cell", "chart_value"]` |
| `answers/prompt.py:41-56` | `SYSTEM_RULES` 第 1 条只写了三种路径句式 |
| `answers/verify.py:61-70` | `_PATH_PREFIX`（kind → **单个**前缀串，`startswith` 用）/ `_BLOCK_KINDS` |
| `answers/verify.py:81-82` | `_norm(text) = " ".join(text.split()).casefold()` —— **casefold** 对公式不适用（`x` ≠ `X`） |
| `answers/verify.py:107-134` | `_verify_quote`：找 span → 子串判定 → `VerifiedClaim(..., ClaimCitation(...))` —— `_verify_formula` 的结构范本 |
| `answers/verify.py:293-342` | `verify_claims`：320-326 kind/前缀闸门；327-337 三路分发 |
| `adapters/http/chat_schemas.py:66-76,93-99` | `ClaimCitationOut.kind: BlockKind`、`ClaimOut.kind: ClaimKind` 直接暴露枚举 → 枚举扩成员会改 `rag-chat-v1` 公开 schema |
| `scripts/enterprise_pdf_rag/check_schema.py:103,107-119` | `"rag-chat-v1": (RagChatRequest, ModelList, RagCompletionResponse, RagCompletionChunk)`，与 `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` 全等比对，**无 `--write`**，需手动重生成 |
| `scripts/enterprise_pdf_rag/check_architecture.py:8-27` | `figures/documents/processing/answers` 纯域；`EXTRA_ALLOWED = {"enterprise_pdf_rag.answers": {"pydantic"}}`；禁 `os/pathlib/io/...` |
| `tests/enterprise_pdf_rag/adapters/test_pdf_ingestion.py:47-71` | `authored_pdf(path, *, page_count, label, embedded_font=False, table_page=False)`：pdfspine `new_page(240,160)` + `insert_font(fontbuffer=authored-donut-ascii.ttf)` + `insert_text((x,y), text, fontsize=, fontname=)` + `draw_line` |
| `tests/.../generic_publication_helpers.py:68-128` | `text_partition_sender(calls, *, table_caption=False)`：解析 prompt 里 `"Source text observations:\n"` 之后的 JSON，按 bbox 分区，回 OpenAI chat 形状 |
| `tests/.../generic_publication_helpers.py:131-171` | `ingest_generic_semantics(...)`：monkeypatch `enterprise_pdf_rag.adapters.json_completion._send_once`；`ingest_pdf(stage="semantics", max_live_calls=page_count)` |
| `tests/.../test_visual_semantics.py:429-439` | formula DTO 样例：`{"schema_version":"formula-observations-v1","svg_digest":…,"source_literal_element_ids":[…],"normalization_state":"inferred","latex":"A+B","confidence":"medium","diagnostics":[]}` |
| `tests/.../answers/fake_llm.py:25,54-66` | `scripted_client(cache_dir, script)`、`answered(answer, *claims)`、`chart_claim(member_id, point_id, text)` |
| `tests/.../answers/store_mounted_document.py:32-60` | `StoreMountedDocument(sources, outputs, *, processing_id, embedder)` —— 用真实 store 满足 `MountedDocument` |
| `adapters/json_completion.py:233-262,305-318` | payload 里**没有 `task` 名**；mock sender 只能靠 prompt 哨兵串分支（`"Source text observations:"` / `"Return formula-observations-v1"` / `"Return visual-description-v1"` / `"Question:"`） |
| `pyproject.toml:84` | `reportlab>=4.0` 在 `[project.optional-dependencies].dev` → 测试可用 reportlab 5.0.1 写 `Ts` |

### 1.2 pdfspine 0.11.0 能力核实（来自 `pdfspine-capabilities.md`，本方案依赖的 7 条）

| # | 事实 | 对本方案的用法 |
|---|---|---|
| P1 | `get_text("dict")` span 键：`ascender, bbox, color, ctm, declared_size, descender, dir, flags, font, matrix, origin, quad, rendered_size, seq, size, text, text_matrix`；仓库目前只消费 6 个 | `adapters/pdfspine_formula.py` 重新打开 PDF 读 `text_matrix / ctm / flags / size / origin` |
| P2 | `rise = (page_height - origin[1]) - text_matrix[5]` 精确等于 PDF `Ts`（实测 +5.0 / −3.0 / 0.0）；**仅当 `ctm == (1,0,0,1,0,0)` 且 `dir == (1,0)`**（旋转落在 ctm，text_matrix 退化为单位阵） | `ScriptEvidence.rise`；ctm 非单位阵 → `rise=None`，text_rise 证明不可用 |
| P3 | `flags & 1`（superscript 位）是"基线比同行高"的启发式：同字号只抬基线也标 1；`Ts -3` 的下标 flags=0；**无 subscript 位** | 只记录为 `ScriptEvidence.superscript_flag`，**永不作判定依据** |
| P4 | `get_text("rawdict")` 的 span 无 `text` 键，多 `chars`：每字符 `bbox / quad / origin / matrix / c / seq / synthetic` | token 子串 bbox = 该字符区间 bbox 并集；`"".join(c)` 必须等于 sidecar 的 `TextSpan.text` |
| P5 | `get_cdrawings()` 键固定 9 个：`closePath, color, dashes, even_odd, fill, items, rect, type, width`；`items` 元素 `('l',p0,p1)` / `('re',(x0,y0,x1,y1))` / `('c',p0,p1,p2,p3)`；**坐标 bottom-left**（`get_drawings()` 才是 top-left，span 是 top-left） | 复用 `pdfspine_figure._top_left/_point` 的翻转逻辑；分数线 = `type='s'` + 单条 `'l'` 水平 + `width<=2`；根号 = 3–4 条 `'l'` 且末段水平最长 |
| P6 | SVG `<text>`/`<path>` 无 id；`<path d>` 与 `get_cdrawings()` 顺序、坐标一一对应 | `PathEvidence.path_index` = `get_cdrawings()` 下标（= SVG `<path>` 出现序）；不解析 SVG |
| P7 | `get_paint_profile()` 有 `Ts` 算子原文但 `Tj` 字符串操作数不暴露，无法绑到文字 | 不用；P2 已是逐 span 的原文字段 |

结论 (a)（笔记 §6a）原文要点：上下标**不用 flags 用 text_matrix 差**；rise=0 的排版式上下标只能靠"字号比 + 基线偏移"推断，**不能逐字证明**，必须标 derived 并把三项数值落盘；分数线来自 `get_cdrawings` 水平线，"这是分数线"是推断但顶点是原文；根号多为字形 `√`（U+221A），画成路径时才找 `'c'+'l'`。

### 1.3 真实样本（`real-samples.md` §C）

- AIA 1–20 页：`layout.json` **0 个 `"kind":"Formula"`**，`layout.raw.json` 同样 0；全 71 页 `text.json` 里 `=`、希腊字母、上标数字、`×÷√≈` 命中数**全为 0**。
- 91 个"像公式"的 span 全是百分比 / 增幅（Chart 49、Text 36、Group 5、List 1），无一是被误判的公式。
- ⇒ 本方案在真实样本上**零覆盖**，只能靠 §6.1 合成夹具验证；§6.4 给"若 data/ 里出现 Formula 对象就跑校验器"的只读 smoke 骨架。

### 1.4 `feat/page-metadata`（`page-metadata-branch.md` §5）相关冲突面

`adapters/processing_retrieval.py`（policy 常量 + `member_text` + `build`）、`processing/index_text.py`、`answers/models.py`（四处分散改动）本分支刚改过；`verify.py` / `context_builder.py` / `semantic_objects.py` 本分支未动。本方案基于合并后的 `c15525b` 继续，不 rebase。

### 1.5 调研纠偏（影响设计）

| 假设 | 实际 | 影响 |
|---|---|---|
| `processing/` 可用 pydantic | `check_architecture.py:16` 只给 `answers` 开 pydantic | §2 全用 dataclass；`__post_init__` 做校验 |
| `authored_pdf` 能写 `Ts` | pdfspine `Page.insert_text(point, text, *, fontname, fontsize, color, fontfile, oc)`（`document.pyi:711-722`）**无 rise 参数**；`TextWriter.append` 也无 | 真 `Ts` 夹具用 reportlab（dev 依赖，笔记 probe 已验证 `setRise`）；pdfspine 夹具做 derived 上标（§6.1 两种都给） |
| pdfspine `insert_text` 输出的 span `ctm` 是单位阵 | 未实测（笔记只测了 reportlab） | §7 阶段 0 先跑探针；若 `ctm` 为 y 翻转阵，则 pdfspine 夹具的上标只能 derived（规则本身不受影响：derived 判定只用 `origin/size/bbox`，与 ctm 无关） |
| mock sender 能按 task 分支 | payload 无 task | 按 prompt 哨兵串分支（§6.3） |
| `_PATH_PREFIX` 一 kind 一前缀 | FORMULA 需要 `formula.` 与 `tokens.` 两个 | 改成 tuple，`str.startswith(tuple)` 原生支持 |

---

## 2. 数据模型与模块

新文件 4 个（2 纯 + 2 adapter），改既有 `typed_ir.py` 1 处。

```
processing/formula_models.py      纯 stdlib：Token/Structure/Observation/Receipt 值对象
processing/formula_rules.py       纯 stdlib：tiling 闭合、脚本证明、路径几何、线性化、可读转写、check_formula
processing/typed_ir.py            FormulaIR 增默认字段（tokens/structures/linear/readable/proof_level）
adapters/pdfspine_formula.py      SDK 边界：重开 PDF，产 FormulaSourceObservation
adapters/formula_qualification.py 编排 + 回执 + 重放校验 validate_formula_member
```

### 2.1 `processing/formula_models.py`（新，纯 stdlib）

```python
"""Source-proven formula tokens: every token quotes a span range; every structure quotes a path."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Literal

from enterprise_pdf_rag.documents.models import AssetRef, Bounds
from enterprise_pdf_rag.figures.models import SourceAnchor


class TokenRole(StrEnum):
    OPERAND = "operand"      # 拉丁字母词，如 ROE / Net / profit
    NUMBER = "number"        # 数字串，含 . , 如 1,234.5
    OPERATOR = "operator"    # + - − × ÷ * / ·
    RELATION = "relation"    # = < > ≤ ≥ ≈ ≠ ≡ →
    GREEK = "greek"          # U+0370–U+03FF
    UNIT = "unit"            # % ‰
    BRACKET = "bracket"      # ( ) [ ] { }
    RADICAL = "radical"      # √ U+221A（字形根号）


class ScriptPosition(StrEnum):
    BASE = "base"
    SUPERSCRIPT = "superscript"
    SUBSCRIPT = "subscript"


class StructureKind(StrEnum):
    FRACTION = "fraction"
    SQRT = "sqrt"


type ScriptProof = Literal["text_rise", "derived"]
type ProofLevel = Literal["full", "literal"]
Matrix = tuple[float, float, float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class ScriptEvidence:
    """The three source numbers a script decision is made from; superscript_flag is recorded only."""

    rise: float | None            # (page_height - origin_y) - text_matrix[5]; None unless ctm identity & dir (1,0)
    size_ratio: float             # token run size / base run size
    baseline_offset: float        # base origin_y - run origin_y (top-left frame; > 0 = raised)
    superscript_flag: bool        # pdfspine flags & 1 — heuristic, never decisive


@dataclass(frozen=True, slots=True)
class FormulaToken:
    index: int
    text: str
    source_span_id: str
    char_start: int
    char_end: int
    bbox: Bounds
    role: TokenRole
    script: ScriptPosition = ScriptPosition.BASE
    script_proof: ScriptProof | None = None
    base_token_index: int | None = None
    script_evidence: ScriptEvidence | None = None

    def __post_init__(self) -> None:
        if not self.text or self.text != self.text.strip() or not self.source_span_id:
            raise ValueError("Formula tokens quote a non-empty, unpadded span substring")
        if not 0 <= self.char_start < self.char_end or self.char_end - self.char_start != len(self.text):
            raise ValueError("Formula token offsets must cover exactly its text")
        x0, y0, x1, y1 = self.bbox
        if not all(isfinite(v) for v in self.bbox) or x0 >= x1 or y0 >= y1:
            raise ValueError("Formula token bbox must be finite with positive area")
        is_base = self.script is ScriptPosition.BASE
        if is_base != (self.script_proof is None) or is_base != (self.base_token_index is None):
            raise ValueError("Script tokens carry a proof and a base; base tokens carry neither")
        if not is_base and (self.script_evidence is None or self.base_token_index == self.index):
            raise ValueError("Script tokens carry their evidence and a distinct base")


@dataclass(frozen=True, slots=True)
class PathEvidence:
    path_index: int                        # get_cdrawings() 下标 = SVG <path> 出现序
    kind: Literal["line", "rect", "polyline"]
    points: tuple[Point, ...]              # page top-left points（已由 bottom-left 翻转）
    width: float

    def __post_init__(self) -> None:
        if self.path_index < 0 or len(self.points) < 2 or not all(
            isfinite(c) for p in self.points for c in p
        ):
            raise ValueError("Path evidence needs a nonnegative index and finite points")


@dataclass(frozen=True, slots=True)
class FormulaStructure:
    kind: StructureKind
    path: PathEvidence
    first: tuple[int, ...]                 # fraction: numerator token indices; sqrt: radicand
    second: tuple[int, ...] = ()           # fraction: denominator; sqrt: ()
    radical_token_index: int | None = None # sqrt drawn as glyph √ + overline

    def __post_init__(self) -> None:
        if not self.first or (self.kind is StructureKind.FRACTION) != bool(self.second):
            raise ValueError("A fraction has both sides; a sqrt has only a radicand")
        members = (*self.first, *self.second)
        if len(set(members)) != len(members) or any(i < 0 for i in members):
            raise ValueError("Structure members are unique token indices")


# —— pdfspine 观测（adapter 产出、纯规则消费、重放时逐字节比对）——

@dataclass(frozen=True, slots=True)
class ObservedChar:
    text: str
    bbox: Bounds


@dataclass(frozen=True, slots=True)
class ObservedRun:
    """One pdfspine span with exactly the fields the proof reads; ids follow pdfspine_document."""

    span_id: str
    text: str
    bbox: Bounds
    origin: Point
    size: float
    font: str
    direction: Point
    ctm: Matrix
    text_matrix: Matrix
    flags: int
    chars: tuple[ObservedChar, ...]


@dataclass(frozen=True, slots=True)
class ObservedPath:
    path_index: int
    paint: str                               # "s" | "f" | "fs"
    width: float
    closed: bool
    items: tuple[tuple[str, tuple[Point, ...]], ...]   # ("l",(p0,p1)) / ("re",(p0,p1)) / ("c",(p0..p3)) top-left


@dataclass(frozen=True, slots=True)
class FormulaSourceObservation:
    schema_version: str                      # "formula-source-observation-v1"
    sdk: str                                 # f"pdfspine/{pdfspine.__version__}"
    source_sha256: str
    page_index: int
    page_height: float
    bbox: Bounds
    runs: tuple[ObservedRun, ...]            # 只含 item.source_span_ids，按内容流顺序
    paths: tuple[ObservedPath, ...]          # 只含 rect 落在 bbox 内（0.5pt 容差）的路径


# —— 资格回执（processing 侧值对象；adapters 用 pydantic 外壳落盘）——

@dataclass(frozen=True, slots=True)
class FormulaQualification:
    """Qualifies exact token transcription and path-backed structure only; never a financial relation."""

    object_id: str
    source: SourceAnchor
    source_manifest_id: str
    source_span_ids: tuple[str, ...]
    ir: AssetRef
    description: AssetRef
    source_svg: AssetRef
    observation: AssetRef
    proof_level: ProofLevel
    token_count: int
    structure_count: int
    derived_script_token_indices: tuple[int, ...]
    model_literal_agreement: Literal["agrees", "disagrees", "unavailable"]
    lineage: tuple[AssetRef, ...] = ()       # 构建时 SUCCEEDED 的 ir / description / model_view
    scope: str = "formula-source-tokens-v1"
    method: str = "exact-span-tiling+path-geometry+text-rise-v1"

    def __post_init__(self) -> None:
        if (self.proof_level == "full") != (not self.derived_script_token_indices):
            raise ValueError("Full proof means no derived script; literal proof means at least one")
        if self.token_count < 1 or self.structure_count < 0 or not self.source_span_ids:
            raise ValueError("A qualified formula has tokens and source occurrences")
```

### 2.2 `processing/typed_ir.py::FormulaIR` 扩展（310-318）

```python
@dataclass(frozen=True, slots=True)
class FormulaIR:
    object_id: str
    source: SourceAnchor
    source_literal: str | None
    latex: str | None
    source_span_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]
    verification: Verification = Verification.PENDING
    # —— 无模型资格分支填充；模型分支（visual_semantics）保持默认值 ——
    tokens: tuple[FormulaToken, ...] = ()
    structures: tuple[FormulaStructure, ...] = ()
    linear: str | None = None
    readable: str | None = None
    proof_level: ProofLevel | None = None

    def __post_init__(self) -> None:
        qualified = bool(self.tokens)
        if qualified != (self.linear is not None) or qualified != (self.readable is not None):
            raise ValueError("Token IR carries its linear and readable forms; model IR carries neither")
        if qualified != (self.proof_level is not None):
            raise ValueError("Proof level accompanies tokens only")
        if self.verification is Verification.VERIFIED and self.proof_level != "full":
            raise ValueError("Only a fully proven formula is verified")
        if tuple(t.index for t in self.tokens) != tuple(range(len(self.tokens))):
            raise ValueError("Token indices are dense and ordered")
        indices = {t.index for t in self.tokens}
        for token in self.tokens:
            if token.base_token_index is not None and (
                token.base_token_index not in indices
                or self.tokens[token.base_token_index].script is not ScriptPosition.BASE
            ):
                raise ValueError("A script token attaches to an existing base token")
        for structure in self.structures:
            if not set((*structure.first, *structure.second)) <= indices:
                raise ValueError("Structure members are existing tokens")
```

- 旧 `ir` 资产（模型分支 JSON，无新键）经 `TypeAdapter(FormulaIR).validate_json` 仍可解析（dataclass 默认值）。
- `visual_semantics.py:477-488` 位置参数构造不变。
- import：`from enterprise_pdf_rag.processing.formula_models import FormulaStructure, FormulaToken, ProofLevel, ScriptPosition`（`formula_models` 只依赖 documents/figures，无环）。

### 2.3 `processing/formula_rules.py`（新，纯 stdlib）

常量（口径全部显式，测试逐条正反例）：

```python
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
RISE_EPSILON = 1e-3                 # pt；|rise| 低于此视为 0
DERIVED_MAX_SIZE_RATIO = 0.8        # 派生脚本：字号 ≤ 0.8 × 基座字号
DERIVED_MIN_SUPER_SHIFT = 0.15      # × 基座字号：基线抬高至少此值 → 派生上标
DERIVED_MIN_SUB_SHIFT = 0.10        # × 基座字号：基线下降至少此值 → 派生下标
ADJACENCY_GAP = 0.5                 # × 基座字号：脚本 run 左缘与基座 token 右缘的最大间距（可略重叠 -0.2×size）
RULE_MAX_SLOPE = 0.5                # pt：分数线/根号横线 |y0-y1| 上限
RULE_MAX_WIDTH = 2.0                # pt：线宽上限（矩形形式则高度上限）
X_OVERLAP_TOLERANCE = 1.0           # pt：成员 token 中心 x 必须落在 [x0-tol, x1+tol]
VERTICAL_REACH = 1.5                # × 成员 token 最大字号：成员到横线的最大竖直距离
BASELINE_CLUSTER = 0.3              # × 字号：同一行基座 token 的 origin_y 容差
PATH_INSIDE_TOLERANCE = 0.5         # pt：路径 rect 落在对象 bbox 内（与 pdfspine_tables._contains 同值）
```

字符分类与切分：

```python
_OPERATORS = "+-−×÷*/·"
_RELATIONS = "=<>≤≥≈≠≡→"
_BRACKETS = "()[]{}"
_UNITS = "%‰"
_RADICAL = "√"
_MERGEABLE = {TokenRole.OPERAND, TokenRole.NUMBER, TokenRole.GREEK}


def role_of(char: str) -> TokenRole | None:
    """None means whitespace (a gap); everything printable gets exactly one role."""
    if char.isspace():
        return None
    if char in _OPERATORS: return TokenRole.OPERATOR
    if char in _RELATIONS: return TokenRole.RELATION
    if char in _BRACKETS: return TokenRole.BRACKET
    if char in _UNITS: return TokenRole.UNIT
    if char in _RADICAL: return TokenRole.RADICAL
    if char.isdigit() or char in ".,": return TokenRole.NUMBER
    if "Ͱ" <= char <= "Ͽ": return TokenRole.GREEK
    return TokenRole.OPERAND


def tile_run(text: str) -> tuple[tuple[int, int, TokenRole], ...]:
    """Maximal same-role runs (single-char for operator/relation/bracket/radical); whitespace is a gap."""
    pieces: list[tuple[int, int, TokenRole]] = []
    i = 0
    while i < len(text):
        role = role_of(text[i])
        if role is None:
            i += 1
            continue
        j = i + 1
        if role in _MERGEABLE:
            while j < len(text) and role_of(text[j]) is role:
                j += 1
        pieces.append((i, j, role))
        i = j
    return tuple(pieces)


def check_tiling(text: str, pieces: Sequence[tuple[int, int]]) -> str | None:
    """Closure rule: ordered, non-overlapping substrings whose concatenation is the span text
    with every whitespace run removed; nothing else is skipped. Returns a diagnostic or None."""
    last = 0
    for start, end in pieces:
        if start < last or start >= end or end > len(text):
            return "formula_span_pieces_overlap_or_disordered"
        if text[last:start].strip():
            return f"formula_span_not_tiled:{text[last:start]!r}"
        last = end
    if text[last:].strip():
        return f"formula_span_not_tiled:{text[last:]!r}"
    if "".join(text[s:e] for s, e in pieces) != "".join(text.split()):
        return "formula_span_tiling_mismatch"
    return None
```

`check_tiling` 就是"token 允许引用 span 子串，但子串拼接必须逐字等于 span 文本"的校验规则：重放时对每个 span 用其 token 的 `(char_start, char_end)` 再跑一次，`None` 才通过。

脚本证明：

```python
def rise_of(run: ObservedRun, page_height: float) -> float | None:
    if run.ctm != IDENTITY or run.direction != (1.0, 0.0):
        return None
    return (page_height - run.origin[1]) - run.text_matrix[5]


def script_of(
    run: ObservedRun, base: FormulaToken | None, base_run: ObservedRun | None, page_height: float
) -> tuple[ScriptPosition, ScriptProof | None, ScriptEvidence | None, str | None]:
    """text_rise proof when the PDF really used Ts; otherwise size-ratio + baseline-shift → derived;
    otherwise base. The last element is a diagnostic that withholds the formula."""
    rise = rise_of(run, page_height)
    if rise is not None and abs(rise) < RISE_EPSILON:
        rise = 0.0
    if base is None or base_run is None:
        if rise:
            return ScriptPosition.BASE, None, None, f"formula_script_without_base:{run.span_id}"
        return ScriptPosition.BASE, None, None, None
    evidence = ScriptEvidence(
        rise, run.size / base_run.size, base_run.origin[1] - run.origin[1], bool(run.flags & 1)
    )
    if rise:                                            # PDF 原文 Ts ≠ 0：可逐字证明
        position = ScriptPosition.SUPERSCRIPT if rise > 0 else ScriptPosition.SUBSCRIPT
        return position, "text_rise", evidence, None
    if evidence.size_ratio <= DERIVED_MAX_SIZE_RATIO:    # 排版式：只能推断
        if evidence.baseline_offset >= DERIVED_MIN_SUPER_SHIFT * base_run.size:
            return ScriptPosition.SUPERSCRIPT, "derived", evidence, None
        if -evidence.baseline_offset >= DERIVED_MIN_SUB_SHIFT * base_run.size:
            return ScriptPosition.SUBSCRIPT, "derived", evidence, None
    return ScriptPosition.BASE, None, None, None
```

基座选择 `base_for(run, tokens_so_far, runs)`：在已产出的 BASE token 里取**最靠右且满足** `-0.2*run.size <= run.bbox[0] - token.bbox[2] <= ADJACENCY_GAP * base_run.size` 且 `|base_run.origin_y - run.origin_y| <= 2 * base_run.size` 的那个；无则 `None`。一个 run 内的所有 token 共享同一 script 结论（run 就是 PDF 里的一段同字号同 Ts 文本）。

结构证明：

```python
def horizontal_rules(paths: Sequence[ObservedPath]) -> tuple[tuple[ObservedPath, float, float, float], ...]:
    """(path, y, x0, x1) for every stroked single 'l' with |dy| <= RULE_MAX_SLOPE and width <= RULE_MAX_WIDTH,
    plus filled/stroked 're' whose height <= RULE_MAX_WIDTH (y = mid)."""


def fraction_of(rule, tokens) -> tuple[FormulaStructure | None, str | None]:
    y, x0, x1 = rule[1:]
    def over_x(t): return x0 - X_OVERLAP_TOLERANCE <= (t.bbox[0] + t.bbox[2]) / 2 <= x1 + X_OVERLAP_TOLERANCE
    above = tuple(t.index for t in tokens if over_x(t) and t.bbox[3] <= y + RULE_MAX_SLOPE
                  and y - t.bbox[3] <= VERTICAL_REACH * size_of(t))
    below = tuple(t.index for t in tokens if over_x(t) and t.bbox[1] >= y - RULE_MAX_SLOPE
                  and t.bbox[1] - y <= VERTICAL_REACH * size_of(t))
    straddle = any(over_x(t) and t.bbox[1] < y < t.bbox[3] for t in tokens)
    if straddle or not above or not below:
        return None, f"formula_fraction_line_unpaired:{rule[0].path_index}"
    return FormulaStructure(StructureKind.FRACTION, path_evidence(rule[0]), above, below), None
```

- 根号（字形）：`RADICAL` token + 一条横线满足 `|x0 - radical.bbox[2]| <= 2.0` 且 `|y - radical.bbox[1]| <= 2.0` → 被开方项 = 横线下方、x 重叠的 token；缺横线 → `formula_radical_without_overline:<span_id>`。
- 根号（路径）：`paint=="s"`，`items` 全为 `'l'`，3–4 段，末段水平（`|dy|<=RULE_MAX_SLOPE`）且为最长段 → 末段当横线；被开方项同上；`PathEvidence.kind="polyline"`。
- **未被任何结构消费的路径 → `formula_unexplained_path:<path_index>`**（含填充路径、贝塞尔、虚线）。一条画在公式里却解释不了的线可能是下划线 / 删除线，改变含义，fail closed。
- 一个 token 属于两个结构 → `formula_token_in_two_structures:<index>`。
- 行数：所有不在结构内的 BASE token 的 `origin_y` 必须落在同一簇（`BASELINE_CLUSTER × size`），否则 `formula_multiline_unsupported`。

线性化与可读转写：

```python
def linearize(tokens, structures) -> str:
    """LaTeX-style subset: top-level items in x order; scripts as ^{…} / _{…}; \frac{…}{…}; \sqrt{…}.
    Symbols stay verbatim Unicode (no \alpha, no \times); words inside one group join with '\ '."""


_RELATION_WORDS = {"=": "等于", "<": "小于", ">": "大于", "≤": "小于等于", "≥": "大于等于",
                   "≈": "约等于", "≠": "不等于", "≡": "恒等于", "→": "推出"}
_OPERATOR_WORDS = {"+": "加", "-": "减", "−": "减", "×": "乘以", "*": "乘以", "·": "乘以", "÷": "除以", "/": "除以"}


def readable_text(tokens, structures) -> str:
    """Same tree as linearize; fraction → '<num> 除以 <den>'（多项加括号）; sqrt → '<radicand> 的平方根';
    superscript text_rise → '<base> 的 <sup> 次方'; superscript derived → '<base> 上标 <sup>';
    subscript (either proof) → '<base> 下标 <sub>'; other tokens verbatim; single spaces."""
```

排序：顶层项（不在结构内的 BASE token + 结构本身，结构位置取其路径 `x0`）按 `bbox[0]`/`x0` 升序；脚本 token 附着到 `base_token_index`，同基座同位置多个脚本按 x 序空格连接。

总入口：

```python
@dataclass(frozen=True, slots=True)
class FormulaCheck:
    ir: FormulaIR | None
    diagnostics: tuple[str, ...]           # ir is None ⇔ at least one diagnostic names the refusal


def check_formula(observation: FormulaSourceObservation, *, object_id: str, anchor: SourceAnchor) -> FormulaCheck:
    """Pure, deterministic, replayable. Tokens ← runs (tiling + char bboxes); scripts ← rise / derived;
    structures ← rules; then every path must be consumed and every base token on one baseline."""
```

产出的 `FormulaIR`：`source_literal = "\n".join(run.text for run in runs)`（span 序逐字），`latex=None`（模型 latex 只是 lineage），`source_span_ids` = token 出现序去重，`diagnostics = ("Tokens quote span substrings verbatim; structures quote get_cdrawings paths; derived scripts are typographic inferences", f"proof_level={level}")`，`verification = VERIFIED if level == "full" else PENDING`，`proof_level = "full" | "literal"`。

### 2.4 `adapters/pdfspine_formula.py`（新，SDK 边界）

```python
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class _RawChar(BaseModel):
    model_config = ConfigDict(extra="ignore")
    c: str
    bbox: tuple[float, float, float, float]


class _RawSpan(BaseModel):                      # rawdict span：无 text 键，有 chars
    model_config = ConfigDict(extra="ignore")
    bbox: tuple[float, float, float, float]
    origin: tuple[float, float]
    size: float
    font: str
    flags: int
    ctm: tuple[float, float, float, float, float, float]
    text_matrix: tuple[float, float, float, float, float, float]
    dir: tuple[float, float] | None = None
    chars: list[_RawChar]


class _Drawing(BaseModel):                      # 与 pdfspine_figure._Drawing 同形（9 个键，extra="forbid"）
    ...


def observe_formula(pdf: bytes, *, page: PageInput, item: LayoutObject) -> FormulaSourceObservation:
    """Re-open the pinned PDF and quote, for the object's own spans, the fields the proof needs.
    Raises ValueError when the PDF, page geometry or any owned span differs from the sidecar."""
    _validate_input(pdf, page=page, item=item)     # 抄 PdfspineTableAdapter._validate_input，kind 换 FORMULA
    document = pdfspine.open(stream=pdf, filetype="pdf")
    try:
        source_page = document.load_page(page.page_index)     # 越界/旋转/rect 校验同 pdfspine_tables.py:57-66
        stored = {span.span_id: span for span in page.text.spans}
        wanted = set(item.source_span_ids)
        runs: list[ObservedRun] = []
        raw = source_page.get_text("rawdict")
        for block_index, block in enumerate(raw["blocks"]):
            if block.get("type") != 0:
                continue
            for line_index, line in enumerate(block["lines"]):
                for span_index, payload in enumerate(line["spans"]):
                    occurrence = f"{page.source_sha256}:{page.page_index}:{block_index}:{line_index}:{span_index}"
                    span_id = f"span-v1-{sha256(occurrence.encode()).hexdigest()}"   # == pdfspine_document.py:83-84
                    if span_id not in wanted:
                        continue
                    span = _RawSpan.model_validate(payload)
                    text = "".join(char.c for char in span.chars)
                    known = stored[span_id]
                    if (text, span.bbox, span.origin, span.font, span.size) != (
                        known.text, known.bbox, known.origin, known.font, known.size
                    ):
                        raise ValueError("Formula source occurrence differs from the pinned text sidecar")
                    runs.append(ObservedRun(span_id, text, span.bbox, span.origin, span.size, span.font,
                                            span.dir or tuple(line.get("dir", (1.0, 0.0))), span.ctm,
                                            span.text_matrix, span.flags,
                                            tuple(ObservedChar(c.c, c.bbox) for c in span.chars)))
        if {run.span_id for run in runs} != wanted:
            raise ValueError("Formula object owns a source occurrence the PDF does not print")
        paths: list[ObservedPath] = []
        for index, drawing in enumerate(source_page.get_cdrawings()):        # bottom-left → 翻转
            d = _Drawing.model_validate(drawing)
            if not _contains(item.bbox, _top_left(d.rect, page.height), tolerance=PATH_INSIDE_TOLERANCE):
                continue
            paths.append(ObservedPath(index, d.type, d.width, d.close_path,
                                      tuple(_item(entry, page.height) for entry in d.items)))
        return FormulaSourceObservation("formula-source-observation-v1", f"pdfspine/{pdfspine.__version__}",
                                        page.source_sha256, page.page_index, page.height, item.bbox,
                                        tuple(runs), tuple(paths))
    finally:
        document.close()
```

`_item(entry, height)`：`('l', p0, p1)` → `("l", (flip(p0), flip(p1)))`；`('re', (x0,y0,x1,y1))` → `("re", (tl0, tl1))`；`('c', …)` → `("c", 四点)`；其它首元素 → `raise ValueError("Unsupported drawing item")`（fail closed）。`_top_left`/`_point`/`_contains` 直接复制 `pdfspine_figure.py:91-98` 与 `pdfspine_tables.py:27-38`（私有函数，不跨模块 import）。

### 2.5 `adapters/formula_qualification.py`（新，编排 + 回执 + 重放）

```python
FORMULA_SCOPE = "formula-source-tokens-v1"
FORMULA_PRODUCER = "exact-formula-transcription-v1"
FORMULA_CONFIDENCE = Confidence(
    None,
    "deterministic formula token transcription; structure from source paths; typographic scripts marked derived when unproven",
)
FORMULA_RECEIPT_SCHEMA = "source-formula-qualification-v1"


class FormulaPublicationReceipt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["source-formula-qualification-v1"] = "source-formula-qualification-v1"
    qualification: FormulaQualification


@dataclass(frozen=True, slots=True)
class FormulaQualificationResult:
    observation: FormulaSourceObservation
    ir: FormulaIR | None
    description: ObjectDescription | None
    agreement: Literal["agrees", "disagrees", "unavailable"]
    diagnostics: tuple[str, ...]


def literal_agreement(model_ir: FormulaIR | None, proven: FormulaIR) -> Literal["agrees", "disagrees", "unavailable"]:
    """Informational only: the model's cited literal vs the proven span concatenation, whitespace-folded."""
    if model_ir is None or model_ir.source_literal is None:
        return "unavailable"
    fold = lambda s: "".join(s.split())
    return "agrees" if fold(model_ir.source_literal) == fold(proven.source_literal or "") else "disagrees"


def qualify_formula(pdf: bytes, *, page: PageInput, item: LayoutObject, model_ir: FormulaIR | None) -> FormulaQualificationResult:
    observation = observe_formula(pdf, page=page, item=item)
    anchor = SourceAnchor(page.source_sha256, page.source_sha256, page.page_index, item.bbox)
    check = check_formula(observation, object_id=item.object_id, anchor=anchor)
    if check.ir is None:
        return FormulaQualificationResult(observation, None, None, "unavailable", check.diagnostics)
    description = ObjectDescription(item.object_id, anchor, check.ir.source_span_ids, check.ir.readable,
                                    FORMULA_PRODUCER, FORMULA_CONFIDENCE, Verification.VERIFIED)
    return FormulaQualificationResult(observation, check.ir, description,
                                      literal_agreement(model_ir, check.ir), check.diagnostics)


def check_model_description(description: ObjectDescription | None, ir: FormulaIR) -> tuple[str, ...]:
    """Optional VLM description check: every symbol-like string (non-space run containing a relation /
    operator / digit / Greek char) in the model text must be a verbatim token text; diagnostics only."""


def validate_formula_member(
    sources: LocalDocumentStore, assets: LocalDocumentStore, scope: ProcessingScope, member: RetrievalMember
) -> tuple[FormulaIR, ObjectDescription, FormulaQualification]:
    """No model/network calls: re-observe the pinned PDF, repeat check_formula, compare everything."""
    receipt = FormulaPublicationReceipt.model_validate_json(assets.get(member.qualification), strict=True).qualification
    ir = TypeAdapter(FormulaIR).validate_json(assets.get(member.ir), strict=True)
    description = TypeAdapter(ObjectDescription).validate_json(assets.get(member.description), strict=True)
    if (receipt.object_id, receipt.source_manifest_id, receipt.ir, receipt.description, receipt.source_svg, receipt.scope) != (
        member.object_id, scope.source_manifest_id, member.ir, member.description, member.source_svg, FORMULA_SCOPE
    ):
        raise ValueError("Formula qualification does not match its retrieval member")
    if description.producer != FORMULA_PRODUCER or description.confidence != FORMULA_CONFIDENCE:
        raise ValueError("Formula qualification requires the deterministic transcription producer")
    source = sources.load(scope.source_manifest_id)                       # 同 literal_qualification.py:44-49
    if source.manifest.source.sha256 != scope.source_sha256 or member.page_index not in scope.selected_page_indices:
        raise ValueError("Formula projection is outside the pinned source scope")
    source_page = source.manifest.pages[member.page_index]
    page = PageInput(scope.source_manifest_id, scope.source_sha256, member.page_index, source_page.width,
                     source_page.height, source_page.svg, read_text_sidecar(sources, source, member.page_index))
    item = LayoutObject(member.object_id, ObjectKind.FORMULA, receipt.source.bbox, receipt.source_span_ids,
                        "replay", Confidence(None, "replay of a pinned formula object"))
    observation = observe_formula(sources.get(source.manifest.source), page=page, item=item)
    if observation != TypeAdapter(FormulaSourceObservation).validate_json(assets.get(receipt.observation), strict=True):
        raise ValueError("Formula source observation differs from the pinned PDF")
    check = check_formula(observation, object_id=member.object_id, anchor=receipt.source)
    expected_description = None if check.ir is None else ObjectDescription(
        member.object_id, receipt.source, check.ir.source_span_ids, check.ir.readable,
        FORMULA_PRODUCER, FORMULA_CONFIDENCE, Verification.VERIFIED)
    if check.ir != ir or expected_description != description or (
        receipt.proof_level, receipt.token_count, receipt.structure_count, receipt.derived_script_token_indices
    ) != (ir.proof_level, len(ir.tokens), len(ir.structures),
          tuple(t.index for t in ir.tokens if t.script_proof == "derived")):
        raise ValueError("Formula projection or receipt differs from independent source qualification")
    expected_crop = crop_native_svg(sources.get(source_page.svg).decode(), width=source_page.width,
                                    height=source_page.height, bbox=receipt.source.bbox).encode()
    if assets.get(member.source_svg) != expected_crop:                    # 同 literal_qualification.py:99-107
        raise ValueError("Formula SVG crop does not derive from the pinned source page and anchor")
    if set(member.lineage_refs) != {receipt.observation, *receipt.lineage} or len(member.lineage_refs) != 1 + len(receipt.lineage):
        raise ValueError("Formula publication requires its observation and recorded model lineage")
    return ir, description, receipt
```

`model_literal_agreement` 在重放时**不重算**（模型分支可能不在 lineage），只要求回执值 ∈ 三枚举（pydantic 已保证）。

### 2.6 description 分支：确定性优先，VLM 可选

- **落盘方式（推荐：新 stage，不做检索期投影）**。`qualified_description` 是新的内容寻址资产，产 `ObjectDescription(text=readable, producer="exact-formula-transcription-v1", VERIFIED)`；原 VLM `description` stage 字节不动，仍是 lineage。理由：(1) ADR 0012:129-133 / 0013:115-116 禁止改写的是**既有** description 资产，新建资产不违反；(2) `processing_store.load:115-126` 的 `qualified_description` 优先规则让挂载校验零改动；(3) 图表已有同名先例，`eligibility` 的必需元组直接复用；(4) 若只做检索期投影，则 context block 的 `description_text` 仍是模型文本，回答链会把未证明的文字放进 prompt。
- 模板（`readable_text`，§2.3）：关系式 "A 等于 B"、分数 "X 除以 Y"、text_rise 上标 "X 的 n 次方"、derived 上标 "X 上标 n"、下标 "X 下标 n"、根号 "X 的平方根"。连接词是固定表，符号逐字。
- VLM description（若 SUCCEEDED）经 `check_model_description` 得诊断串，写进 `qualification_exclusions`（沿用图表 stage 名，`semantic_objects.py:556-562` 先例）；**不影响放行**。

---

## 3. stage 产出与资格判定

### 3.1 stage 表（FORMULA 对象，`SemanticObjectAdapter.process` 之后）

| stage | 产出者 | 状态 | 内容 |
|---|---|---|---|
| `native_crop`, `source_text` | 既有 176-179 | SUCCEEDED | 不变 |
| `svg`, `model_render`, `model_view` | 既有 192-196 | SUCCEEDED | 不变 |
| `ir(_raw)`, `description(_raw)` | 既有 197-216（VLM 两路） | SUCCEEDED / FAILED | 不变，PENDING |
| **`formula_observation`** | 新 `_formula(...)` | SUCCEEDED（观测本身总能落盘）或 FAILED（PDF/几何/归属不一致，`observe_formula` 抛 ValueError） | `TypeAdapter(FormulaSourceObservation).dump_json` |
| **`qualified_ir`** | 新 | 仅证明成立时 SUCCEEDED | `TypeAdapter(FormulaIR).dump_json(result.ir)` |
| **`qualified_description`** | 新 | 同上 | `TypeAdapter(ObjectDescription).dump_json(result.description)` |
| **`qualification`** | 新 | SUCCEEDED（回执）或 UNAVAILABLE（诊断） | `FormulaPublicationReceipt(qualification=FormulaQualification(...)).model_dump_json().encode()` |
| **`qualification_exclusions`** | 新（可选） | SUCCEEDED | `json.dumps({"model_literal_agreement": ..., "model_description_diagnostics": [...], "diagnostics": [...]})` |

`semantic_objects.py:217-222` 的硬编码诊断被替换为：

```python
        model_ir = result.ir if isinstance(result.ir, FormulaIR) else None
        if item.kind is ObjectKind.FORMULA:
            return self._formula(page, item, writer, stages, model_ir, result.description)
        stages.append(writer.diagnostic("qualification", "Visual semantics are source-bound model inferences; an independent field/relationship verifier is not available for this object."))
        return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
```

（IMAGE/DIAGRAM 保持原诊断；图表方案在同一处为 DIAGRAM 开分支。）

```python
    def _formula(self, page, item, writer, stages, model_ir, model_description) -> ObjectProcessingRecord:
        source = self.sources.load(page.source_manifest_id)
        try:
            result = qualify_formula(self.sources.get(source.manifest.source), page=page, item=item, model_ir=model_ir)
        except ValueError as error:
            stages.extend((writer.diagnostic("formula_observation", str(error), failed=True),
                           writer.diagnostic("qualification", "Formula source could not be re-observed: " + str(error))))
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        observation = writer.save("formula_observation", TypeAdapter(FormulaSourceObservation).dump_json(result.observation))
        stages.append(observation)
        if result.ir is None or result.description is None:
            stages.append(writer.diagnostic("qualification", "Formula qualification withheld: " + "; ".join(result.diagnostics)))
            return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages))
        by_name = {stage.stage: stage for stage in stages}
        lineage = tuple(_ref(by_name[name]) for name in ("ir", "description", "model_view")
                        if name in by_name and by_name[name].state is StageState.SUCCEEDED)
        ir = writer.save("qualified_ir", TypeAdapter(FormulaIR).dump_json(result.ir))
        desc = writer.save("qualified_description", TypeAdapter(ObjectDescription).dump_json(result.description))
        receipt = FormulaQualification(
            item.object_id, result.ir.source, page.source_manifest_id, result.ir.source_span_ids,
            _ref(ir), _ref(desc), _ref(by_name["svg"]), _ref(observation),
            result.ir.proof_level, len(result.ir.tokens), len(result.ir.structures),
            tuple(t.index for t in result.ir.tokens if t.script_proof == "derived"),
            result.agreement, lineage,
        )
        stages.extend((ir, desc,
                       writer.save("qualification", FormulaPublicationReceipt(qualification=receipt).model_dump_json().encode()),
                       writer.save("qualification_exclusions", json.dumps({
                           "model_literal_agreement": result.agreement,
                           "model_description_diagnostics": list(check_model_description(model_description, result.ir)),
                           "diagnostics": list(result.diagnostics)}).encode())))
        return ObjectProcessingRecord(item.object_id, item.kind, tuple(stages),
                                      qualified_claim_count=len(result.ir.tokens))
```

- **不看 `qualification_policy`**：与 Table 一样（`_table` 无论 policy 都做逐字转录资格），因为无模型、无歧义；`pdf_ingestion.py:173` 的 `policy="none"` 下公式即可放行。
- **模型两路失败不阻断**：`qualified_*` 完全确定性；模型分支只进 `lineage`。与图表"两路都要"的差异在于图表的 qualified 产物是**投影模型输出**，公式的 qualified 产物**不含任何模型字节**，因此 ADR 0002 "no description-only fallback" 不适用（没有 model description 被放行）。
- `stage 指纹`：`_Writer.save` 的指纹含 `stage` 名 + `producer` + `item`（47-72 行），新 stage 名天然独立缓存。

### 3.2 两级证明

| 级别 | 条件 | `qualified_ir.verification` | `proof_level` | 放行 |
|---|---|---|---|---|
| **VERIFIED / full** | 全部 token 通过 tiling 闭合 + 字符 bbox；全部结构有路径证据且几何成立；全部路径被消费；脚本全是 `text_rise` 或 BASE；单行 | `VERIFIED` | `"full"` | 是 |
| **literal**（存在 derived 脚本） | 同上，但 ≥1 token 的 `script_proof == "derived"` | `PENDING`（与 `TableIR` 恒 PENDING 同理：文字逐字已证，**关系**未证） | `"literal"` | **是**（推荐，§8 拍板点 1） |
| 拒绝 | 任一诊断（§3.3） | — | — | 否：无 `qualified_*`，`qualification` UNAVAILABLE |

### 3.3 诊断串（逐字；`qualification` stage 的 diagnostic = `"Formula qualification withheld: " + "; ".join(...)`）

| 诊断 | 触发 |
|---|---|
| `formula_span_transform_unsupported:<span_id>` | 不是 `dir == (1,0)`（旋转/竖排）。ctm 非单位阵**不拒绝**，只让 rise 不可证 |
| `formula_char_count_mismatch:<span_id>` | `len(chars) != len(text)`（连字/合成字符）→ 无法给子串 bbox |
| `formula_span_not_tiled:<piece>` / `formula_span_tiling_mismatch` / `formula_span_pieces_overlap_or_disordered` | tiling 闭合失败 |
| `formula_script_without_base:<span_id>` | `Ts ≠ 0` 但左侧无可附着的基座 token |
| `formula_fraction_line_unpaired:<path_index>` | 横线一侧无 token，或有 token 跨线 |
| `formula_radical_without_overline:<span_id>` | `√` 字形没有右上横线 |
| `formula_unexplained_path:<path_index>` | bbox 内任何未被结构消费的路径（含填充、贝塞尔、虚线） |
| `formula_token_in_two_structures:<index>` | 结构成员重叠 |
| `formula_multiline_unsupported` | 顶层 BASE token 基线不在一簇 |
| `formula_no_tokens` | 对象无任何非空白字符 |
| `formula_merged_span_unsplittable:<span_id>` | 见 §3.4 |

### 3.4 pdfspine 降级判定规则

| 情形 | 判定 | 证明强度标注 |
|---|---|---|
| `ctm == I` 且 `dir == (1,0)`，`Ts ≠ 0` | `script_of` 走 text_rise 分支 | `script_proof="text_rise"`，`ScriptEvidence.rise` = 原文差值 |
| `ctm ≠ I`（`q … cm … Q` 施加变换） | `rise_of` 返回 `None`；只做 derived 判定（`origin/size/bbox` 已是页面坐标，与 ctm 无关） | 最多 `derived`；`ScriptEvidence.rise=None` |
| 排版式上下标（`Tm/Td` 移基线，rise=0，小字号） | `size_ratio <= 0.8` 且基线偏移达阈值 → derived；否则 BASE | `derived`，三项数值落盘可复核 |
| `flags & 1` 为 1 但基线/字号不满足 | 仍 BASE | `superscript_flag=True` 仅记录 |
| span 已被合并成整行且脚本与基座**同字号同基线**（如 `"x2"` 一段 12pt） | tiling 会切成 `x` / `2` 两个 BASE token（逐字仍成立），但脚本关系**无证据** | 放行为 BASE-BASE，`linear = "x 2"`，readable "x 2"——不猜次方。**注意**：若同一 run 里出现 `Ts` 变化，pdfspine 一定切成新 span（Ts 是 span 属性），所以"合并 span 含真上标"不会发生；真正无法切分的只有排版式且同字号的情况，此时无任何几何信号，按 BASE 处理是唯一诚实结论 |
| span 内混有非公式文字（如 `"where x ="`） | tiling 把 `where` 切成 OPERAND token，放行，`readable = "where x 等于 …"` | 由 partition 负责把说明文字排除在 Formula 区域外；本方案不猜 |
| 分数线画成两条短线拼接 / 贝塞尔 | 两条线各自 `fraction_of`；若各自都能配对则两个 fraction（错误但逐字）；不能配对 → `formula_fraction_line_unpaired` 或 `formula_unexplained_path` | 拒绝 |
| 根号 / 求和 / 积分为字体字形（`√ ∑ ∫`） | `√` 有专门规则；`∑ ∫ ∏` 归 OPERAND token 逐字放行，其上下限若为 `Ts`/小字号则按脚本规则；无"大算符"语义（非目标） | — |

---

## 4. 索引 / 检索 / 回答链接入点

### 4.1 eligibility（`adapters/processing_retrieval.py:101-130`）

```python
def eligibility(record: ObjectProcessingRecord) -> tuple[bool, str | None]:
    if record.kind not in (ObjectKind.TEXT, ObjectKind.LIST, ObjectKind.GROUP, ObjectKind.TABLE, ObjectKind.CHART, ObjectKind.FORMULA):
        return False, f"{record.kind.value} objects are not retrievable"
    stages = {stage.stage: stage for stage in record.stages}
    required = (("qualified_ir", "qualified_description", "qualification", "svg")
                if record.kind in (ObjectKind.CHART, ObjectKind.FORMULA)
                else ("ir", "description", "qualification", "svg"))
    if any(name not in stages or stages[name].state is not StageState.SUCCEEDED for name in required):
        if record.kind is ObjectKind.TABLE:
            return False, "Table transcription is not verified; only verified tables are retrievable"
        if record.kind is ObjectKind.FORMULA:
            return False, "Formula tokens are not source-proven; only proven formulas are retrievable"
        return False, "required qualification stages are incomplete"
    return True, None
```

`build()` 158-162 的 `required` 同步改为 `record.kind in (CHART, FORMULA)`；173-203 之后加 FORMULA lineage：

```python
            elif record.kind is ObjectKind.FORMULA:
                receipt = FormulaPublicationReceipt.model_validate_json(self.outputs.assets.get(qualification), strict=True).qualification
                lineage = (receipt.observation, *receipt.lineage)
                produced = {stage.artifact for stage in record.stages if stage.state is StageState.SUCCEEDED and stage.artifact is not None}
                if not set(lineage) <= produced:
                    raise ValueError("Qualified formula lineage is outside its processing object stages")
```

**与 diagram 方案叠加**：diagram 方案对同一处写的是 `record.kind in (CHART, DIAGRAM)`；两份都合入后，kind 白名单是 `(TEXT, LIST, GROUP, TABLE, CHART, DIAGRAM, FORMULA)`，`required` 的条件是 `record.kind in (CHART, DIAGRAM, FORMULA)`，kind 特定拒绝串三条并列（TABLE / DIAGRAM / FORMULA）。`build()` 158-162 的 `required` 同理。

`_qualified`(347-356) / `resolve_processing_context`(359-374) / `source_publication.validate_processing_source`(79-83) 各加一分支 `if member.kind is ObjectKind.FORMULA: validate_formula_member(...)`（合并后是 CHART / DIAGRAM / FORMULA / 其余四路）；`_qualified` 返回注解联合类型加 `FormulaIR` / `FormulaQualification`。`processing/retrieval.py:149` 的 `qualification` 联合类型加 `FormulaQualification`（终态 `LiteralQualification | FigureQualification | DiagramQualification | FormulaQualification`）；**`.scope`（151-155）不改**——只有 `FigureQualification` 走 `semantic_scope` 特判，`DiagramQualification` / `FormulaQualification` 的字段名都叫 `scope`，天然落在末尾的 `return self.qualification.scope`（与 diagram 方案 §5 的写法统一，§10）。

### 4.2 索引文本投影 + policy v5（与图表方案共用同一次升级）

`processing/index_text.py`（85-89 之前插入）：

```python
def formula_index_text(formula: FormulaIR, *, fallback: str) -> str:
    """readable + linear + every distinct token text; a model-only FormulaIR keeps its description."""
    if not formula.tokens or formula.linear is None or formula.readable is None:
        return fallback
    parts = [formula.readable, formula.linear, "formula"]
    parts.extend(dict.fromkeys(token.text for token in formula.tokens))
    return " ".join(part.strip() for part in parts if part.strip())


def member_index_text(ir: TypedIR, description_text: str) -> str:
    if isinstance(ir, ChartIR):
        return chart_index_text(ir, fallback=description_text)
    if isinstance(ir, FormulaIR):
        return formula_index_text(ir, fallback=description_text)
    return description_text
```

（图表方案在同一函数加 `DiagramIR` 分支；合并后 `member_index_text` 的终态是 `ChartIR` → `DiagramIR` → `FormulaIR` 三个 `isinstance` 分支 + `return description_text` 兜底，三份方案各自只追加一支。）"可引用判据是内容属性不是 Verification"：这里用 `tokens` 非空，与 ADR 0012:139-142 一致。

policy v5 四步（`processing_retrieval.py:60-74, 77-99`）。**这四个常量由 diagram 方案唯一编辑**（同属 ADR 0015 的一次升级，见 §10）；本方案只在 `member_text` 的 `else` 分支追加一支门，复用 diagram 建好的 `VISUAL_PROJECTION_POLICIES`，**不新建 `FORMULA_PROJECTION_POLICIES`**。常量区终态（照抄自 diagram 方案 §4.2，此处只为对表）：

```python
_POLICY = "source-transcription-and-scoped-chart-qualification-v5"
PROJECTED_CHART_POLICIES = frozenset({
    _POLICY,
    "source-transcription-and-scoped-chart-qualification-v4",
    "source-transcription-and-scoped-chart-qualification-v3",
    "source-transcription-donut-and-displayed-bar-v2",
})
CONTEXTUAL_POLICIES = frozenset({_POLICY, "source-transcription-and-scoped-chart-qualification-v4"})
# Snapshots whose Diagram / Formula members embed their qualified-IR projection (ADR 0015).
VISUAL_PROJECTION_POLICIES = frozenset({_POLICY})


def member_text(assets, plan, member, context=None) -> str:
    payload = assets.get(member.description)
    if member.kind is ObjectKind.CHART:
        body = TypeAdapter(TextDescription).validate_json(payload).text
        if plan.qualification_policy in PROJECTED_CHART_POLICIES:
            body = member_index_text(TypeAdapter(ChartIR).validate_json(assets.get(member.ir)), body)
    else:
        body = TypeAdapter(ObjectDescription).validate_json(payload).text
        if member.kind is ObjectKind.FORMULA and plan.qualification_policy in VISUAL_PROJECTION_POLICIES:
            body = member_index_text(TypeAdapter(FormulaIR).validate_json(assets.get(member.ir)), body)
    if plan.qualification_policy not in CONTEXTUAL_POLICIES:
        return body
    return contextual_index_text(body, context)
```

旧快照：v1–v4 快照里**不存在** FORMULA 成员（eligibility 拒绝过），所以 `VISUAL_PROJECTION_POLICIES` 门只对新快照生效；v4 快照进 `CONTEXTUAL_POLICIES` 与 `PROJECTED_CHART_POLICIES` 后照旧打分。`snapshot_id` 含 policy 串（`processing/retrieval.py:71-80`）→ 重新 `index + publish` 即迁移；embedding 缓存指纹含 `sha256(text)`（258-273）→ 不复用旧向量。

### 4.3 context block（`processing/context_builder.py`）

```python
class BlockKind(StrEnum):
    ...
    FORMULA = "formula"

_KIND_OF_OBJECT = {..., ObjectKind.FORMULA: BlockKind.FORMULA}


@dataclass(frozen=True, slots=True)
class FormulaTokenEvidence:
    index: int
    text: str
    role: str
    script: str
    proof: str | None
    source_span_id: str
    bbox: Bounds


class ContextBlock:
    ...
    formula_linear: str | None = None
    formula_readable: str | None = None
    formula_proof_level: str | None = None
    formula_tokens: tuple[FormulaTokenEvidence, ...] = ()
```

`prompt_text()`（109 行 `elif self.kind is BlockKind.TABLE` 之前插入）：

```python
        elif self.kind is BlockKind.FORMULA:
            lines.append(f"formula proof_level={self.formula_proof_level}")
            lines.append(f"formula.linear: {self.formula_linear}")
            lines.append(f"formula.readable: {self.formula_readable}")
            lines.extend(
                f"tokens.{token.index}: {token.text}  (role={token.role}, script={token.script}, proof={token.proof or 'none'})"
                for token in self.formula_tokens
            )
```

渲染示例（§6.1 夹具）：

```
[member 3f…] kind=formula page_index=2 scope=formula-source-tokens-v1 verification=verified
formula proof_level=full
formula.linear: ROE = \frac{Net\ profit}{Equity}
formula.readable: ROE 等于 Net profit 除以 Equity
tokens.0: ROE  (role=operand, script=base, proof=none)
tokens.1: =  (role=relation, script=base, proof=none)
tokens.2: Net  (role=operand, script=base, proof=none)
tokens.3: profit  (role=operand, script=base, proof=none)
tokens.4: Equity  (role=operand, script=base, proof=none)
```

`build_context_block`（224 行 raise 之前）：

```python
    if isinstance(ir, FormulaIR):
        if member.kind is not ObjectKind.FORMULA:
            raise ValueError("Retrieval member kind does not match its typed IR")
        if not ir.tokens:
            raise ValueError("Formula members need their proven token IR")
        return ContextBlock(*common, BlockKind.FORMULA, member.page_index, context.scope, ir.verification,
                            context.description.text,
                            formula_linear=ir.linear, formula_readable=ir.readable, formula_proof_level=ir.proof_level,
                            formula_tokens=tuple(FormulaTokenEvidence(t.index, t.text, t.role.value, t.script.value,
                                                                      t.script_proof, t.source_span_id, t.bbox)
                                                 for t in ir.tokens))
```

可引用路径：`formula.linear`（值 = `linear` 整串）、`formula.readable`（值 = `readable` 整串）、`tokens.<i>`（值 = 该 token 的 `text`）。token 文本不含空白（word 级切分），所以 `tokens.<i>: <text>  (…)` 用两个空格 + 括号分隔注释，模型引用括号前的文本。

### 4.4 verify（`answers/verify.py`）+ 六处联动

| # | 位置 | 改动 |
|---|---|---|
| 1 | `answers/models.py:18-22` `ClaimKind` | 加 `FORMULA = "formula"` |
| 2 | `answers/prompt.py:21` `ModelClaim.kind` | `Literal["quote", "cell", "chart_value", "formula"]` |
| 3 | `answers/prompt.py:45-49` `SYSTEM_RULES` 第 1 条 | 追加：`"kind `formula` uses `formula.linear` or `formula.readable` and `text` is exactly that line after the colon, or `tokens.<index>` and `text` is exactly the token printed before the parenthesised annotation.\n"` |
| 4 | `answers/verify.py:61-65` `_PATH_PREFIX` | 值改 tuple：`QUOTE: ("fragments.",), CELL: ("cells.",), CHART_VALUE: ("points.",), FORMULA: ("formula.", "tokens.")`；320-322 的 `claim.field_path.startswith(_PATH_PREFIX[kind])` 无需改（`str.startswith` 接受 tuple）。**本方案是唯一把这张表改成 tuple 的方案**：若 diagram 方案已合入，同一次改动把它的两行一并写成 `DIAGRAM_NODE: ("nodes.",)` / `DIAGRAM_EDGE: ("edges.",)`（§10） |
| 5 | `answers/verify.py:66-70` `_BLOCK_KINDS` | 加 `ClaimKind.FORMULA: {BlockKind.FORMULA}` |
| 6 | `processing/context_builder.py:29-35` `_KIND_OF_OBJECT` | 加 FORMULA（§4.3） |

新函数（放在 `_verify_cell` 之后，165 行后）：

```python
_TOKEN_PATH_RE = re.compile(r"tokens\.(?P<index>\d+)")


def _exact(text: str) -> str:
    """Whitespace-folded but case-preserving: formula symbols are case-sensitive."""
    return " ".join(text.split())


def _verify_formula(claim: ModelClaim, block: ContextBlock) -> VerifiedClaim | RejectedClaim:
    if claim.field_path in ("formula.linear", "formula.readable"):
        expected = block.formula_linear if claim.field_path == "formula.linear" else block.formula_readable
        if expected is None:
            return _reject(claim, AbstainReason.VALUE_UNAVAILABLE, "formula line is unavailable")
        if not claim.text.strip() or _exact(claim.text) != _exact(expected):
            return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claim text differs from the formula line")
        citation = ClaimCitation(block.member_id, block.kind, block.page_index, claim.field_path,
                                 tuple(dict.fromkeys(t.source_span_id for t in block.formula_tokens)), None, expected)
    else:
        match = _TOKEN_PATH_RE.fullmatch(claim.field_path)
        if match is None:
            return _reject(claim, AbstainReason.MODEL_OUTPUT_INVALID, "formula claims cite formula.linear, formula.readable or tokens.<index>")
        index = int(match.group("index"))
        token = next((t for t in block.formula_tokens if t.index == index), None)
        if token is None:
            return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "cited token is not in the block")
        if _exact(claim.text) != _exact(token.text):
            return _reject(claim, AbstainReason.CLAIM_NOT_IN_EVIDENCE, "claim text differs from token")
        citation = ClaimCitation(block.member_id, block.kind, block.page_index, claim.field_path,
                                 (token.source_span_id,), token.bbox, token.text)
    return VerifiedClaim(claim.claim_id, ClaimKind.FORMULA, claim.text, None, None, (citation,))
```

`verify_claims` 327-337 分发加一支：`elif kind is ClaimKind.FORMULA: outcome = _verify_formula(claim, block)`（放在 CELL 之后、chart 之前）。`prose_grounded`(345-366) 不改：token 里的数字通过 `_numbers(cited.quote)` 进入允许集。

### 4.5 chat 引用字段

`adapters/http/chat_schemas.py:66-76,93-99` 直接暴露 `BlockKind` / `ClaimKind` 枚举，无需改代码；但公开 schema `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` 的两个 enum 会多出 `"formula"`，`check_schema.py` 会红 → 重生成（§7 阶段 3 命令）。`ClaimCitationOut.bbox` 对 `formula.linear` 引用为 `null`，对 `tokens.<i>` 为 token bbox；`evidence_ids` 是 span id。图表座位（`answer_service.select_context:72-101`）不给公式（非目标）。

---

## 5. 既有文件最小改动清单

| 文件 | 函数 / 常量 | 改动 | 行号（c15525b） |
|---|---|---|---|
| `processing/typed_ir.py` | `FormulaIR` | 加 5 个默认字段 + `__post_init__`；import `formula_models` | 310-318；import 区 3-13 |
| `adapters/semantic_objects.py` | `process` 视觉路径末尾 | 217-222 替换为 FORMULA 分支调用 `self._formula`；新增 `_formula` 方法；import `FormulaIR`, `FormulaSourceObservation`, `FormulaQualification`, `FormulaPublicationReceipt`, `qualify_formula`, `check_model_description`, `StageState` | 217-222；新方法插在 `_table`(225-304) 之后 |
| `adapters/processing_retrieval.py` | `_POLICY` / `PROJECTED_CHART_POLICIES` / `CONTEXTUAL_POLICIES` / `VISUAL_PROJECTION_POLICIES` | **本方案不编辑这四个常量**（由 diagram 方案在 ADR 0015 的同一次升级里建好）；本方案只复用 `VISUAL_PROJECTION_POLICIES` | 60-74 |
| 同上 | `member_text` | FORMULA 投影分支 | 77-99 |
| 同上 | `eligibility` | 白名单加 FORMULA；required 元组条件；kind 特定拒绝串 | 107-113, 116-120, 124-129 |
| 同上 | `build` | required 条件；FORMULA lineage 分支 | 158-162, 173-203 |
| 同上 | `_qualified` / `resolve_processing_context` | FORMULA → `validate_formula_member` | 347-356, 368-374 |
| `adapters/source_publication.py` | `validate_processing_source` | 三分支 | 79-83 |
| `processing/retrieval.py` | `RetrievalContext.qualification` 类型与 `.scope` | 加 `FormulaQualification` | 149, 151-155 |
| `processing/index_text.py` | 新 `formula_index_text`；`member_index_text` | 加分支；import `FormulaIR` | 85-89 |
| `processing/context_builder.py` | `BlockKind` / `_KIND_OF_OBJECT` / 新 `FormulaTokenEvidence` / `ContextBlock` 4 字段 / `prompt_text` / `build_context_block` | §4.3 | 21-35, 59-68 之后, 79-85, 108-109 之前, 223-224 之前 |
| `answers/models.py` | `ClaimKind` | 加 FORMULA | 18-22 |
| `answers/prompt.py` | `ModelClaim.kind`、`SYSTEM_RULES` | §4.4 | 21, 45-49 |
| `answers/verify.py` | `_PATH_PREFIX`、`_BLOCK_KINDS`、新 `_exact`/`_TOKEN_PATH_RE`/`_verify_formula`、`verify_claims` | §4.4 | 61-70, 165 之后, 327-331 |
| `docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` | `BlockKind`/`ClaimKind` enum | 重生成 | — |
| `tests/enterprise_pdf_rag/adapters/test_pdf_ingestion.py` | `authored_pdf` | 加 `formula_page: bool = False, formula_rule: bool = True` + `_draw_formula(page, fontname, *, rule=True)`；三份方案的统一终态签名见 diagram 方案 §6.1（`table_page: bool \| TableSpec` / `diagram_page` / `formula_page` 三者互斥、都画在最后一页） | 47-71 |
| `tests/enterprise_pdf_rag/adapters/generic_publication_helpers.py` | `text_partition_sender` / `ingest_generic_semantics` | 加 `formula_page` 分区 + 视觉两路应答（prompt 哨兵分支，与 diagram 的两条哨兵分支并列在**同一个** `text_partition_sender` 里，三份方案都不新建独立 sender）；`max_live_calls = page_count + 2 × 视觉对象数`（公式页 2 个区域 → 加 4） | 68-171 |
| `src/enterprise_pdf_rag/CLAUDE.md` | processing/ 行、"Same-SVG two branches" 段 | 提 `formula_rules.py` 与 FORMULA 投影 | 42-46, 99-106 |
| `docs/enterprise-pdf-rag/adr/0015-diagram-and-formula-retrievable.md`（新） | — | **与图表方案合写同一份 ADR 0015**（一次 policy v5 升级 + 视觉对象的无模型资格），已拍板不再各写一份；表格网格证明是独立的 **ADR 0014**（含 ADR 0011 Rejected alternative 的旁注）| — |
| `docs/enterprise-pdf-rag/CLAUDE_HANDOFF.md`、`CHANGELOG.md` | — | 记录 | — |

**不改**：`adapters/visual_semantics.py`、`visual_semantic_schemas.py`（VLM 分支原样）、`processing_store.py`（`qualified_*` 兼容 + `processing_assets` 通吃）、`draft_publication.py`、`cli.py`、`hybrid_search.py`、`document_catalog.py`、`answer_service.py`、`chat_schemas.py`、`literal_qualification.py`。

**与其它两份方案的共享 / 冲突文件**：`processing_retrieval.py`（policy 常量、`member_text`、`eligibility`、`build`、`_qualified`）、`index_text.py::member_index_text`、`context_builder.py`（`BlockKind`/`_KIND_OF_OBJECT`/`prompt_text`/`build_context_block`）、`verify.py`（三张表 + 分发）、`prompt.py`、`answers/models.py::ClaimKind`、`semantic_objects.py:217-222`、`retrieval.py::RetrievalContext`、`source_publication.py`、`rag-chat-v1.json`、两个测试 helper。建议合并顺序：先图表方案（Diagram 分支、v5 常量），本方案在其上加 FORMULA 分支，只追加行不改图表行。

---

## 6. 测试计划

### 6.1 离线夹具

**(a) pdfspine 夹具（derived 上标 + 分数线）** —— 扩展 `authored_pdf`：

```python
# tests/enterprise_pdf_rag/adapters/test_pdf_ingestion.py
# 公式页几何（240x160，top-left；insert_text 的 point 是基线原点）
FORMULA_FRACTION_BBOX = (18.0, 56.0, 126.0, 100.0)      # ROE = Net profit / Equity
FORMULA_POWER_BBOX = (150.0, 62.0, 172.0, 86.0)         # x² (derived)
FORMULA_RULE = ((60.0, 78.0), (120.0, 78.0))             # 分数线端点，width 0.8


def _draw_formula(page: pdfspine.Page, fontname: str) -> None:
    page.insert_text((20, 82), "ROE =", fontsize=12, fontname=fontname)        # 基线 y=82
    page.insert_text((62, 74), "Net profit", fontsize=11, fontname=fontname)  # 分子，bbox.y1 ≈ 76.5 < 78
    page.draw_line(*FORMULA_RULE, width=0.8)                                  # 分数线 y=78
    page.insert_text((72, 94), "Equity", fontsize=11, fontname=fontname)       # 分母，bbox.y0 ≈ 84 > 78
    page.insert_text((152, 82), "x", fontsize=12, fontname=fontname)           # 基座
    page.insert_text((160, 76), "2", fontsize=7, fontname=fontname)            # 小字号 + 基线抬 6pt → derived 上标


def authored_pdf(path, *, page_count, label, embedded_font=False, table_page=False, formula_page=False) -> Path:
    ...
            if table_page and number == page_count - 1:
                _draw_table(page, fontname)
            if formula_page and number == page_count - 1:
                _draw_formula(page, fontname)
```

预期：`size_ratio = 7/12 ≈ 0.58 <= 0.8`，`baseline_offset = 82 - 76 = 6 >= 0.15 * 12 = 1.8` → `derived` SUPERSCRIPT；分数线 `('l',(60,82),(120,82))`（bottom-left y = 160-78）翻转后 y=78；"Net"/"profit" 中心 x ∈ [59,121]，`bbox.y1 <= 78.5`；"Equity" `bbox.y0 >= 77.5`。字体用 `authored-donut-ascii.ttf`（渲染侧拒绝未解析 `<text>`，`figure_reasoning.py:258-266`）。

**(b) reportlab 夹具（真 `Ts` 上标，`proof_level="full"`）** —— 新 helper `tests/enterprise_pdf_rag/adapters/formula_fixture.py`：

```python
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from enterprise_pdf_rag.core.settings import ROOT_DIR

_FONT = ROOT_DIR / "tests/enterprise_pdf_rag/fixtures/authored-donut-ascii.ttf"
PAGE = (240.0, 160.0)                       # 与 authored_pdf 同尺寸；reportlab 是 bottom-left


def rise_formula_pdf(path: Path, *, with_fraction: bool = True) -> Path:
    """`ROE = Net profit / Equity` with a drawn fraction rule, and `x²` written with a real `Ts`."""
    if "Authored" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Authored", str(_FONT)))
    c = canvas.Canvas(str(path), pagesize=PAGE)
    h = PAGE[1]
    if with_fraction:
        t = c.beginText(20, h - 82); t.setFont("Authored", 12); t.textOut("ROE ="); c.drawText(t)
        t = c.beginText(62, h - 74); t.setFont("Authored", 11); t.textOut("Net profit"); c.drawText(t)
        c.setLineWidth(0.8); c.line(60, h - 78, 120, h - 78)
        t = c.beginText(72, h - 94); t.setFont("Authored", 11); t.textOut("Equity"); c.drawText(t)
    t = c.beginText(152, h - 82); t.setFont("Authored", 12); t.textOut("x")
    t.setFont("Authored", 7); t.setRise(5); t.textOut("2"); t.setRise(0)              # → `5 Ts`
    c.drawText(t)
    c.showPage(); c.save()
    return path
```

预期（笔记 1.3 实测同构）：`'2'` 的 `origin=(…, 77.0)`、`text_matrix[5] = 78.0`（= h-82）、`rise = (160-77) - 78 = 5.0` → `text_rise`。

**(c) 观测夹具（纯规则单测用，不经 PDF）**：`tests/enterprise_pdf_rag/processing/formula_observation_fixtures.py` 提供 `run(span_id, text, *, origin, size, rise=0.0, ctm=IDENTITY, chars=None)` 与 `line(index, y, x0, x1, width=0.8)` 两个 builder，直接构造 `FormulaSourceObservation`。

### 6.2 单元测试（文件 → 用例名 → 断言）

`tests/enterprise_pdf_rag/processing/test_formula_rules.py`（纯，无 pdfspine）：

| 用例 | 正 / 反 | 断言 |
|---|---|---|
| `test_tiling_splits_a_merged_span_and_closes_verbatim` | 正 | `"ROE ="` → `[(0,3,OPERAND),(4,5,RELATION)]`，`check_tiling` 为 None |
| `test_tiling_rejects_a_gap_that_is_not_whitespace` | 反 | pieces 漏掉 `"="` → `formula_span_not_tiled:'='` |
| `test_tiling_rejects_overlapping_or_disordered_pieces` | 反 | `formula_span_pieces_overlap_or_disordered` |
| `test_number_run_keeps_thousands_separator_and_decimal_point` | 正 | `"1,234.5%"` → NUMBER `1,234.5` + UNIT `%` |
| `test_greek_and_operator_roles` | 正 | `"α+β"` → GREEK, OPERATOR, GREEK |
| `test_text_rise_superscript_is_proven` | 正 | `rise=5` → SUPERSCRIPT/`text_rise`，evidence.rise==5.0 |
| `test_text_rise_subscript_is_proven` | 正 | `rise=-3` → SUBSCRIPT/`text_rise` |
| `test_text_rise_without_a_base_withholds` | 反 | `formula_script_without_base:<id>` |
| `test_non_identity_ctm_disables_rise_but_allows_derived` | 正 | `ctm=(1,0,0,-1,0,160)`：`rise is None`，小字号抬基线 → derived |
| `test_small_raised_run_is_derived_superscript` | 正 | 7/12，offset 6 → derived SUPERSCRIPT；`superscript_flag` 只记录 |
| `test_small_lowered_run_is_derived_subscript` | 正 | offset −2 (≥ 0.10×12) → derived SUBSCRIPT |
| `test_same_size_raised_run_stays_base` | 反 | 12/12 且 flags=1 → BASE（flags 不决定） |
| `test_merged_same_size_span_x2_yields_two_base_tokens` | 边界 | `"x2"` 一段 12pt → `x`,`2` 均 BASE，`linear == "x 2"` |
| `test_fraction_rule_pairs_numerator_and_denominator` | 正 | 结构 `FRACTION`，first=(2,3) second=(4,)，`PathEvidence.path_index==0` |
| `test_fraction_rule_missing_withholds_structure_and_paths_must_be_explained` | 反 | 无横线 → 无结构，`linear == "ROE = Net profit Equity"`；`formula_multiline_unsupported`（Net/Equity 不同基线） |
| `test_fraction_rule_above_both_operands_is_unpaired` | 反 | 横线在分子上方 → `formula_fraction_line_unpaired:0` |
| `test_rule_too_thick_or_sloped_is_unexplained` | 反 | width 3 → `formula_unexplained_path:0` |
| `test_filled_triangle_in_bbox_is_unexplained` | 反 | `paint="f"` → `formula_unexplained_path:<i>` |
| `test_sqrt_glyph_with_overline` | 正 | `√` + 横线 → `SQRT`, radical_token_index |
| `test_sqrt_glyph_without_overline_withholds` | 反 | `formula_radical_without_overline` |
| `test_sqrt_drawn_as_polyline` | 正 | 3 段 `'l'` 末段水平最长 → `kind="polyline"` |
| `test_token_in_two_structures_withholds` | 反 | 两条横线共享 token → `formula_token_in_two_structures` |
| `test_linearize_latex_subset_keeps_symbols_verbatim` | 正 | `ROE = \frac{Net\ profit}{Equity}`；`α×β` 不映射为 `\alpha\times\beta` |
| `test_readable_wording_distinguishes_proven_and_derived_superscript` | 正 | text_rise → "x 的 2 次方"；derived → "x 上标 2"；subscript → "x 下标 i" |
| `test_check_formula_full_and_literal_levels` | 正 | full ⇒ VERIFIED；含 derived ⇒ PENDING + `proof_level="literal"` |
| `test_check_formula_is_deterministic` | 正 | 同一观测两次 `check_formula` 结果 `==` |
| `test_formula_ir_post_init_rules` | 反 | tokens 非空但 linear None / VERIFIED 但 literal / 脚本指向非 BASE → ValueError |

`tests/enterprise_pdf_rag/adapters/test_pdfspine_formula.py`：

| 用例 | 断言 |
|---|---|
| `test_observe_formula_quotes_matrices_chars_and_flipped_paths`（夹具 a） | runs 覆盖 3 个 span；`ctm`/`text_matrix` 在场；`paths[0].items == (("l",((60,78),(120,78))),)`（翻转正确） |
| `test_observe_formula_rise_from_reportlab_ts`（夹具 b） | `rise_of(run_2) == 5.0` |
| `test_observe_formula_refuses_span_drift` | 篡改 sidecar 文本 → `ValueError("Formula source occurrence differs …")` |
| `test_observe_formula_refuses_wrong_pdf_or_kind` | sha 不符 / kind TABLE → ValueError |
| `test_observe_formula_ignores_paths_outside_bbox` | 表格线在 bbox 外不入 `paths` |

`tests/enterprise_pdf_rag/adapters/test_formula_qualification.py`：

| 用例 | 断言 |
|---|---|
| `test_qualify_formula_fixture_a_is_literal_level` | `proof_level=="literal"`，`derived_script_token_indices==(6,)`，description.text == readable，producer/confidence 常量 |
| `test_qualify_formula_fixture_b_is_full_level` | `verification is VERIFIED` |
| `test_literal_agreement_three_states` | 模型 `source_literal` 一致 / 不一致 / None |
| `test_check_model_description_flags_symbols_not_in_tokens` | "ROE = Net profit ÷ Equity" 里 `÷` 不在 tokens → 诊断 |
| `test_validate_formula_member_replays_and_refuses_drift` | 用 §6.3 发布产物：正常通过；改 PDF 字节 / 改 `qualified_ir` 一个 token 文本 / 改 lineage → 三种 ValueError 逐字 |

`tests/enterprise_pdf_rag/processing/test_index_text.py`（+2）：`test_formula_members_index_readable_linear_and_tokens`、`test_model_only_formula_ir_keeps_description_text`。
`tests/enterprise_pdf_rag/processing/test_context_builder.py`（+2）：`test_formula_member_renders_linear_readable_and_token_paths`（逐行断言 §4.3 示例）、`test_model_only_formula_ir_is_refused`（`tokens=()` → "need their proven token IR"）。**注意 `test_kind_mismatch_and_unsupported_ir_are_refused`（`:273-281`）的"不支持 IR"反例**：基线用的是 `DiagramIR`，diagram 方案已把它换成 `ImageIR`；本方案放行 `FormulaIR`，所以该反例只能是 `ImageIR`，本方案不得把它改回 `FormulaIR`（§10）。
`tests/enterprise_pdf_rag/answers/test_verify.py`（+5）：`test_formula_linear_claim_verifies_verbatim_only`（大小写差异被拒：`_exact` 不 casefold）、`test_formula_token_claim_cites_span_and_bbox`、`test_formula_unknown_token_index_is_rejected`、`test_formula_path_prefix_mismatch_is_invalid_output`（`points.x` 配 formula kind）、`test_prose_number_from_formula_token_is_grounded`。
`tests/enterprise_pdf_rag/adapters/test_draft_publication.py`（+1）：`qualify_draft` 的 `skipped_reasons` 含 `"Formula tokens are not source-proven; only proven formulas are retrievable": 1`（用夹具 a 去掉分数线的变体）。
`tests/enterprise_pdf_rag/processing/test_retrieval_snapshot.py`（+1）：policy 串改 v5 后 snapshot id 变化且旧 id 仍可 `load`。

### 6.3 e2e（`tests/enterprise_pdf_rag/adapters/test_formula_publication_e2e.py`）

sender 扩展（`generic_publication_helpers.py`）：

```python
FORMULA_REGIONS = {"fraction": FORMULA_FRACTION_BBOX, "power": FORMULA_POWER_BBOX}


def text_partition_sender(calls, *, table_caption=False, formula_page=False):
    def sender(url, *, api_key, payload, timeout):
        calls.append(payload)
        body = json.loads(payload)
        content = body["messages"][1]["content"]
        prompt = content[0]["text"] if isinstance(content, list) else content
        if "Return formula-observations-v1" in prompt:          # 视觉 IR 分支
            digest = prompt.split('"svg_digest":"', 1)[1].split('"', 1)[0]
            ids = list(dict.fromkeys(re.findall(r"obs-[0-9a-f]{16}", prompt)))
            return _chat({"schema_version": "formula-observations-v1", "svg_digest": digest,
                          "source_literal_element_ids": ids, "normalization_state": "inferred",
                          "latex": "ROE = \\frac{Net profit}{Equity}", "confidence": "medium", "diagnostics": []})
        if "Return visual-description-v1" in prompt:             # 视觉 description 分支
            digest = ...; ids = ...
            return _chat({"schema_version": "visual-description-v1", "svg_digest": digest,
                          "text": "A formula defining ROE as Net profit over Equity.",
                          "evidence": {"element_ids": ids, "confidence": "0.5"}, "diagnostics": []})
        assert "Source text observations:" in prompt             # layout 分支（原逻辑）
        observations = json.loads(prompt.split("Source text observations:\n", 1)[1])
        regions = []
        if formula_page:
            for name, bbox in FORMULA_REGIONS.items():
                owned = [o for o in observations if _inside(o["bbox"], bbox)]
                if owned:
                    regions.append(_region(f"formula-{name}", "Formula", list(bbox), [str(o["id"]) for o in owned]))
                    observations = [o for o in observations if o not in owned]
        ... # 原 table / body 逻辑
```

`ingest_generic_semantics(..., formula_page=False)`：`authored_pdf(..., formula_page=formula_page)`，`max_live_calls=page_count + (4 if formula_page else 0)`（两个 Formula 区域 × 两路）。

用例：

1. `test_formula_pdf_ingest_qualify_index_publish_retrieve_offline`：
   - `ingest.failed_stage_count == 0`；两个 FORMULA record 的 stage 集合含 `formula_observation / qualified_ir / qualified_description / qualification / qualification_exclusions`；fraction 对象 `qualified_ir.verification is VERIFIED`？——**否**：夹具 a 的 fraction 区域无脚本 → `full`/VERIFIED；power 区域 derived → `literal`/PENDING。逐条断言。
   - `qualify_draft(...).kinds == {"Formula": 2, "Text": 3}`；`plan.qualification_policy == "…-v5"`。
   - `index_draft` 后 `member_text(...)` 对 fraction 成员 == `"<header>\nROE 等于 Net profit 除以 Equity ROE = \frac{Net\ profit}{Equity} formula ROE = Net profit Equity"`（按 §4.2 拼接规则逐字）。
   - `ProcessingRetrieval.search(publication, "ROE 怎么算")`（`OfflineDescriptionEmbedder`）命中 fraction 成员；`resolve` → `RetrievalContext.qualification` 是 `FormulaQualification`；`build_context_block` 渲染与 §4.3 示例逐行相等。
2. `test_formula_answer_cites_linear_and_rejects_non_verbatim`：
   - `StoreMountedDocument(sources, outputs, processing_id=published.published_processing_id, embedder=OfflineDescriptionEmbedder())` + `AnswerService({sha: document}, scripted_client(...))`；script 从 prompt 用 `_MEMBER_LINE` 取 `kind=formula` 成员 id，返回 `answered("ROE = \\frac{Net\\ profit}{Equity}", ModelClaim(claim_id="c1", member_id=m, kind="formula", field_path="formula.linear", text="ROE = \\frac{Net\\ profit}{Equity}"))` → `status is ANSWERED`，`claims[0].citations[0].evidence_ids` 为 3 个 span id，`llm_live_calls == 1`。
   - 反例：`text="ROE = Net profit / Equity"` → `ABSTAINED`，`abstain_reason is CLAIM_NOT_IN_EVIDENCE`，`rejected[0].detail == "claim text differs from the formula line"`。
   - `tokens.4` + `text="Equity"` → ANSWERED，citation.bbox 非空。
3. `test_formula_without_rule_is_withheld_and_stays_out`：`authored_pdf` 变体不画分数线（加 `formula_rule=False` 参数）→ `qualification.state is UNAVAILABLE` 且 diagnostic 含 `formula_multiline_unsupported`；`qualify_draft().skipped_reasons` 含公式拒绝串；`index_draft().member_count` 不含公式。
4. `test_formula_qualifies_when_model_branches_are_budget_exhausted`：`max_live_calls=page_count`（视觉两路 `call_budget_exhausted` → `ir`/`description` FAILED）→ `qualified_*` 仍 SUCCEEDED，`receipt.lineage == ()`, `model_literal_agreement == "unavailable"`；发布后 `validate_formula_member` 通过。
5. `test_rise_formula_pdf_reaches_full_proof`（夹具 b，reportlab）：power 对象 `proof_level == "full"`，`tokens[1].script_proof == "text_rise"`，readable 含 "x 的 2 次方"。

### 6.4 真实样本只读 smoke

AIA 1–20 页无 Formula（§1.3），所以只能是"若出现就跑"的骨架。`scripts/enterprise_pdf_rag/formula_smoke.py`（只读，不写任何 store）：

```python
"""Read-only: run the formula proof over every Formula object of a saved processing id."""

import argparse, json, sys
from pathlib import Path

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.aia_ingestion import read_text_sidecar
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.formula_qualification import qualify_formula
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.processing.models import ObjectKind, PageInput, PagePartition


def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--source-store", type=Path, required=True)
    p.add_argument("--processing-store", type=Path, required=True); p.add_argument("--processing-id", required=True)
    a = p.parse_args()
    sources = LocalDocumentStore(a.source_store, activate_on_publish=False)
    outputs = ProcessingStore(a.processing_store)
    manifest = outputs.load(a.processing_id)
    source = sources.load(manifest.scope.source_manifest_id)
    pdf = sources.get(source.manifest.source)
    seen = 0
    for record in manifest.pages:
        if record.partition.artifact is None:
            continue
        partition = TypeAdapter(PagePartition).validate_json(outputs.assets.get(record.partition.artifact))
        items = {item.object_id: item for item in partition.objects if item.kind is ObjectKind.FORMULA}
        if not items:
            continue
        sp = source.manifest.pages[record.page_index]
        page = PageInput(manifest.scope.source_manifest_id, manifest.scope.source_sha256, record.page_index,
                         sp.width, sp.height, sp.svg, read_text_sidecar(sources, source, record.page_index))
        for item in items.values():
            seen += 1
            try:
                result = qualify_formula(pdf, page=page, item=item, model_ir=None)
                print(json.dumps({"page": record.page_index + 1, "object": item.object_id,
                                  "proof_level": None if result.ir is None else result.ir.proof_level,
                                  "linear": None if result.ir is None else result.ir.linear,
                                  "diagnostics": list(result.diagnostics)}, ensure_ascii=False))
            except ValueError as error:
                print(json.dumps({"page": record.page_index + 1, "object": item.object_id, "error": str(error)}))
    print(f"formula objects seen: {seen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

配套 `tests/enterprise_pdf_rag/adapters/test_formula_aia_smoke.py`：`pytest.mark.skipif(not (ROOT_DIR / "data/output/aia-2026-interim/pages-001-020").exists())`，运行同逻辑，断言"seen == 0 或每个对象都得到 JSON 行且无 traceback"（与 `test_document_catalog_aia_smoke.py` 同款）。

---

## 7. 分阶段实施清单与验证命令

（全部从仓库根运行；`PY=.venv/bin/python`）

**阶段 0 —— 探针（0.25 人日）**
- 在 scratchpad 写 `probe_authored_formula.py`：用 §6.1(a) 画页 → `page.get_text("rawdict")` 打印各 span 的 `ctm / text_matrix / origin / size / flags / len(chars)`，`get_cdrawings()` 打印分数线；再用 §6.1(b) 打印 `'2'` 的 rise。
- 验证：`$PY <scratchpad>/probe_authored_formula.py` 输出里 (a) 的 `'2'` `rise==0`（或 ctm 非单位阵）、字号 7；(b) 的 `'2'` `rise==5.0`。据此确认 §2.3 常量与 §6.1 坐标；若 (a) 的 `len(chars) != len(text)`，改夹具字体。

**阶段 1 —— 纯规则（2 人日）**
- 新增 `processing/formula_models.py`、`processing/formula_rules.py`；改 `processing/typed_ir.py::FormulaIR`。
- 先写 `test_formula_rules.py`（§6.2 全部 27 条）→ 红 → 实现 → 绿。
- 验证：`$PY -m pytest tests/enterprise_pdf_rag/processing/test_formula_rules.py tests/enterprise_pdf_rag/processing/test_visual_ir.py tests/enterprise_pdf_rag/adapters/test_visual_semantics.py -q`；`$PY scripts/enterprise_pdf_rag/check_architecture.py`（processing 纯 stdlib）；`$PY -m mypy`；`$PY -m ruff check . && $PY -m ruff format --check .`。

**阶段 2 —— adapter 观测 / 资格 / 重放 / stage 接线（2 人日）**
- 新增 `adapters/pdfspine_formula.py`、`adapters/formula_qualification.py`；改 `semantic_objects.py`（`_formula`）；夹具 (a)(b)(c)。
- 验证：`$PY -m pytest tests/enterprise_pdf_rag/adapters/test_pdfspine_formula.py tests/enterprise_pdf_rag/adapters/test_formula_qualification.py tests/enterprise_pdf_rag/processing/test_semantic_stages.py -q`；`$PY scripts/enterprise_pdf_rag/check_conformance.py .`（pdfspine 只在 adapters）。

**阶段 3 —— 检索 / 上下文 / 回答链 / policy v5（1.5 人日；与图表方案共用 policy 改动）**
- 改 `processing_retrieval.py`、`source_publication.py`、`retrieval.py`、`index_text.py`、`context_builder.py`、`answers/models.py`、`prompt.py`、`verify.py`；重生成 schema：
  ```
  $PY - <<'EOF'
  import json, sys; sys.path.insert(0, "scripts/enterprise_pdf_rag")
  from check_schema import CONTRACTS, ROOT
  name = "rag-chat-v1"
  actual = {m.__name__: m.model_json_schema() for m in CONTRACTS[name]}
  (ROOT / "docs/enterprise-pdf-rag/schemas" / f"{name}.json").write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n")
  EOF
  ```
  （先 `git diff docs/enterprise-pdf-rag/schemas/rag-chat-v1.json` 核对只多了 `"formula"` 两处。）
- 验证：`$PY -m pytest tests/enterprise_pdf_rag/processing/test_index_text.py tests/enterprise_pdf_rag/processing/test_context_builder.py tests/enterprise_pdf_rag/answers -q`；`$PY scripts/enterprise_pdf_rag/check_schema.py`；`$PY -m pytest tests/enterprise_pdf_rag/processing/test_retrieval_snapshot.py tests/enterprise_pdf_rag/adapters/test_document_catalog.py tests/enterprise_pdf_rag/adapters/test_chat_http.py -q`（旧快照仍可挂载）。

**阶段 4 —— e2e + smoke（1 人日）**
- 扩 `generic_publication_helpers.py`；新 `test_formula_publication_e2e.py`（5 条）；`scripts/enterprise_pdf_rag/formula_smoke.py` + `test_formula_aia_smoke.py`。
- 验证：`$PY -m pytest tests/enterprise_pdf_rag/adapters/test_formula_publication_e2e.py tests/enterprise_pdf_rag/adapters/test_generic_publication_e2e.py -q`；`$PY scripts/enterprise_pdf_rag/formula_smoke.py --source-store data/output/aia-2026-interim/pages-001-020/source --processing-store data/output/aia-2026-interim/pages-001-020/processing --processing-id <current-processing>` → `formula objects seen: 0`。

**阶段 5 —— 文档与门禁（0.5 人日）**
- ADR **0015**（与 diagram 方案合写同一份）、`src/enterprise_pdf_rag/CLAUDE.md` 42-46 / 99-106、`CLAUDE_HANDOFF.md`、`CHANGELOG.md`。
- 验证：`scripts/ci.sh` 全绿（含 `check_doc_drift.py`、`check_drift.py`）；`$PY -m pytest tests/ -q -m "not gpu and not docling and not network"` 通过数 = 基线 + 新增用例数。

合计 **≈ 7.25 人日**（单人）；若图表方案先合入，本方案阶段 3 的 policy v5 部分可省约 0.5 人日。

---

## 8. 需要拍板的点（3 个）

1. **derived 上下标是否放行？** —— **推荐放行为 `literal` 级**（`proof_level="literal"`，`qualified_ir.verification=PENDING`，token 带 `script_proof="derived"` + 三项数值，`readable` 用"上标"而非"次方"，context block 逐 token 打印 `proof=derived`）。理由：(a) pdfTeX / InDesign / Word 导出的 PDF 几乎都用 `Tm/Td` 移基线而非 `Ts`，只认 `text_rise` 会让功能在真实文档上死掉；(b) 逐字不变量仍成立——token 文本、bbox、字号、基线全是原文，只有"上标关系"是推断，且被显式标记、可复核、不进"次方"措辞；(c) 与 `TableIR` 恒 PENDING 但成员可检索的既有判例同构（ADR 0011：文字逐字已证，关系未证）。**不推荐**在 `linear` 里加特殊标记：`linear` 保持合法 LaTeX 子集，便于模型逐字引用和与 VLM `latex` 做一致性记录；标记通过 `proof_level`、token 表和 `readable` 措辞承载。备选：只放行 `full` —— 更保守，但合成夹具之外基本无覆盖。

2. **`linear` 用 LaTeX 子集还是自定义线性语法？** —— **推荐 LaTeX 子集**（`\frac{}{}`、`\sqrt{}`、`^{}`、`_{}`、组内词用 `\ `），符号一律保留 Unicode 原文（不做 `\alpha`/`\times` 映射，否则"映射"本身成了非逐字改写）。理由：可读、模型可稳定引用、与 `FormulaObservationsDTO.latex` 同域便于 `literal_agreement` 扩展。备选：S 表达式 `(= ROE (frac (Net profit) Equity))` —— 更无歧义但对用户和模型都不友好。

3. **token 是否允许引用 span 子串？** —— **推荐允许**，以 §2.3 `check_tiling` 闭合规则为硬约束（有序、不重叠、拼接 == 去空白 span 全文、跳过的只能是空白），并要求 `rawdict` 字符数 == 文本长度以给子串精确 bbox。理由：pdfspine 会把 `"ROE ="`、`"x="` 合成一个 span，不允许子串则几乎所有公式都会 fail closed。备选：整 span 为 token —— 实现更简单，但只能覆盖"每个符号独立 span"的极少数排版。

---

## 9. 非目标与风险

**非目标**：多行 / 对齐式公式（`formula_multiline_unsupported`）；矩阵、大括号分段函数；求和 / 积分 / 乘积的上下限语义（`∑ ∫ ∏` 只作 OPERAND token）；LaTeX 命令级归一化（`\alpha`）；公式求值或数值关系校验（`require_financial_qualification` 仍拒绝 formula scope）；给公式检索座位；改 VLM 两路。

| 风险 | 表现 | 缓解 |
|---|---|---|
| 多行公式 / 矩阵 | 顶层 token 不在一条基线 | 明确拒绝 `formula_multiline_unsupported`；矩阵的括号是字形或路径，路径版会触发 `formula_unexplained_path` |
| 大算符（∑ ∫）画成路径或用 CM/Symbol 字体 | 路径 → unexplained 拒绝；字体字形 → `text` 可能是私用区字符或 `""` | 拒绝是正确行为；诊断里带 span id；将来可为 CM 字体加 unicode 映射表（需字体名白名单） |
| 上下标同时（`x_i^2`） | 两个脚本 run 附着同一基座 | 支持：`base_token_index` 相同、位置不同；`linear = x_{i}^{2}`；测试加一条 |
| Symbol / Math 字体的 unicode 映射错误 | `√` 显示正确但 `chars[].c` 为其它码位 | 逐字规则只信 `c`；错码 → 角色判成 OPERAND，不会伪造 `√`；风险是漏识别根号，非误识别 |
| span 被切成单字符（每字一 span） | tiling 每 span 一个 token，`Net` 变 `N`,`e`,`t` | 逐字仍成立但 readable 难看；可在 §2.3 加"同基线、同字号、字符间距 < 0.1×size 的相邻 OPERAND span 合并"规则——**本期不做**（合并是推断） |
| pdfspine 版本升级改坐标系 / 键名 | `observe_formula` 抛错或 `FormulaSourceObservation` 不等 | 观测 JSON 记 `sdk` 版本；重放逐字节比对 → 直接拒绝挂载，不会静默错读（与图表 `svg` 逐字节比对同一态度）；升级时需重新 `semantics → index → publish` |
| `get_cdrawings()` 的 `('re', …)` 分数线 | 有些排版器用极扁矩形画线 | `horizontal_rules` 已收 `re` 且高度 ≤ 2pt |
| 分数线与下划线混淆 | 下划线在文字下方且下方无 token | `formula_fraction_line_unpaired` → 拒绝（fail closed） |
| `Ts` 用于非脚本用途（整段抬升） | 整行 rise≠0 且无基座 | `formula_script_without_base` → 拒绝 |
| 中文连接词与英文语料混排影响 BM25 | 词法通道以 token 文本为主命中 | 投影同时含 `readable`、`linear`、token 文本；连接词表可改，不影响资产 |
| `rag-chat-v1.json` 公开契约变化 | 枚举新增值 | 向后兼容（新增枚举值）；阶段 3 重生成并在 CHANGELOG 记录 |

---

## 10. 交叉修订记录

三份方案（`diagram-retrievable.md` / `formula-retrievable.md` / `table-grid-verification.md`）写完后做了一次交叉一致性核对，本节逐条记录**本文件**被改了什么、为什么。总览与合并顺序见同目录 `README.md`。

### 10.1 已修订（逐条）

| # | 位置 | 改了什么 | 为什么 |
|---|---|---|---|
| 1 | §4.1 末尾 | ①新增「与 diagram 方案叠加」：合并后 `eligibility` 的 kind 白名单是 7 项，两处 `required` 条件是 `record.kind in (CHART, DIAGRAM, FORMULA)`，kind 特定拒绝串三条并列（TABLE / DIAGRAM / FORMULA）；`_qualified` / `resolve_processing_context` / `source_publication` 是四路分派。②`processing/retrieval.py` 的 `.scope`（151-155）从"加 `isinstance(..., FormulaQualification)` 分支"改为**不改** | 本方案原文写 `in (CHART, FORMULA)`、diagram 方案写 `in (CHART, DIAGRAM)`，需要给出终态；`.scope` 只有 `FigureQualification` 需要特判，`FormulaQualification.scope` 天然命中末尾的 `return self.qualification.scope`，diagram 方案已按"不改"写，两份统一 |
| 2 | §4.2 policy 段 | ①把"两份方案只做一次"改为明确归属：`_POLICY` / `PROJECTED_CHART_POLICIES` / `CONTEXTUAL_POLICIES` / `VISUAL_PROJECTION_POLICIES` 四个常量**由 diagram 方案唯一编辑**，本方案只追加 `member_text` 的一支门、复用同一个 `VISUAL_PROJECTION_POLICIES`；②常量块注释 `ADR 0014` → `ADR 0015`；③把 `"…-v4"` / `"…-v3"` 的省略写法展开成完整字面量 | ADR 编号统一为 **0014 = 表格网格证明，0015 = Diagram + Formula 合写**；常量区必须单点编辑，否则两份方案会互相覆盖；省略号写法在实施时容易抄错 |
| 3 | §4.2 `member_index_text` 段末 | 写明终态是 `ChartIR` → `DiagramIR` → `FormulaIR` 三个 `isinstance` 分支 + 兜底，各方案只追加一支 | 两份方案各自展示的函数体只含自己那一支，合并时容易互相覆盖 |
| 4 | §4.4 表第 4 行（`_PATH_PREFIX`） | 注明**本方案是唯一把这张表改成 tuple 的方案**，若 diagram 已合入则同一次把它的两行写成 `("nodes.",)` / `("edges.",)` | diagram 方案按"一 kind 一串"写，本方案改表结构，必须说明谁负责转换 |
| 5 | §5 改动清单 `processing_retrieval.py` 常量行 | 改为"**本方案不编辑这四个常量**，只复用 `VISUAL_PROJECTION_POLICIES`" | 同 2 |
| 6 | §5 改动清单 ADR 行 | `docs/enterprise-pdf-rag/adr/0014-*.md`（"合写或各一份，推荐合写"）→ **`0015-diagram-and-formula-retrievable.md`，已拍板合写**；并写明表格方案是独立的 ADR 0014（含 ADR 0011 旁注） | 表格方案原本写的是"0014 表格 / 0015 diagram / 0016 formula"，与本方案的"0014 合写"矛盾；统一后不存在 ADR 0016 |
| 7 | §5 改动清单两条测试 helper 行 | ①`authored_pdf` 明确加 `formula_page` + `formula_rule`，并指向 diagram 方案 §6.1 的三份统一终态签名（`table_page: bool \| TableSpec` / `diagram_page` / `formula_page` 三者互斥）；②`generic_publication_helpers.py` 明确"三份方案都不新建独立 sender，哨兵分支都挂在同一个 `text_partition_sender` 上"，`max_live_calls` 口径统一为 `page_count + 2 × 视觉对象数`（本方案的公式页 2 个区域 → 加 4） | 三份方案给了三种互不兼容的 `authored_pdf` 签名；diagram 方案原本要新建 `diagram_sender`，而 `ingest_generic_semantics` 只 monkeypatch 一个 `_send_once`，多个 sender 无法共存 |
| 8 | §6.2 `test_context_builder.py` 那行 | 加一句：`test_kind_mismatch_and_unsupported_ir_are_refused`（`:273-281`）的"不支持 IR"反例最终必须是 **`ImageIR`**，本方案不得改回 `FormulaIR` | 基线反例是 `DiagramIR`，diagram 方案把它换成 `ImageIR`；本方案放行 `FormulaIR`，若换成 `FormulaIR` 该回归会直接失效 |
| 9 | §7 阶段 5 | ADR 0014（合写或独立）→ ADR **0015**（与 diagram 合写） | 同 6 |

### 10.2 核对过、确认无冲突（未改动）

- **`processing/` 的 import 约束**：已用 `git show c15525b:scripts/enterprise_pdf_rag/check_architecture.py` 与 `check_conformance.py` 复核，本方案 §1.5 的纠偏是对的 —— `PACKAGES = (figures, documents, processing, answers)` 只允许标准库 + 这四个纯域包，`EXTRA_ALLOWED` 只给 `answers` 开 pydantic，另外禁 `os/pathlib/io/socket/sqlite3/subprocess/http/urllib/importlib` 与 `open/eval/exec/__import__`。**核查结论：diagram 方案与表格方案放进 `processing/` 的新模块（`diagram_models.py`、`diagram_description.py`、`table_grid_proof.py`、`geometry.py` 扩展、`table_models.py` 证据类型）全部是 dataclass + stdlib，没有一处用 pydantic，无需改写**；三份方案的 pydantic 回执壳都在 `adapters/`。
- **policy 串取值**：三份一致，均为 `"source-transcription-and-scoped-chart-qualification-v5"`。
- **枚举新值互不重名**：`ClaimKind` = 本方案 `formula` + diagram 的 `diagram_node` / `diagram_edge`；`BlockKind` = 本方案 `formula` + diagram 的 `diagram`；表格方案两个枚举都不加值。
- **`ContextBlock` 新字段**：本方案 `formula_linear` / `formula_readable` / `formula_proof_level` / `formula_tokens`，diagram 的 `nodes` / `edges`，表格的 `grid_verification` —— 全带默认值、纯追加、无重名。
- **`semantic_objects.py:217-222`**：终态三路（FORMULA → `self._formula`、DIAGRAM → diagram 方案的 `qualify_diagram`、其余 → 原逐字诊断），diagram 方案已同步写明。
- **`answers/prompt.py` / `answers/models.py`**：`kind` 的 `Literal` 三份合计 +3 值；表格方案给 `ModelClaim` 加 `row/col/header`、给 `ClaimCitation` 加 4 个字段 —— 与本方案互不覆盖。
- **`draft_publication.py`**：三份都不改，`DraftQualification.qualification_policy` 保持 `...-v2`（表格方案 §3.4 已把原来悬而未决的"若升 v3"拍死为不升）。

### 10.3 发现但**未**修改的遗留问题

1. **`verify.py` 里三种文本比对口径并存**：本方案新增 `_exact`（whitespace-fold、**不** casefold），diagram 方案的 `_verify_diagram_node/_edge` 沿用 `_norm`（casefold），表格方案的 `header` 用裸 `==`。三者各自有理由（公式符号大小写敏感 / 自然语言 label 与 QUOTE、CELL 对齐 / 表头逐字），但同一个文件里会有三种口径 —— 建议在 ADR 0014 / 0015 里各写明本 kind 的口径，本次不擅自统一。
2. **`adapters/` 内私有助手的复用风格不一致**：本方案明确"复制 `pdfspine_figure._top_left` / `pdfspine_tables._contains`，不跨模块 import 私有名"；diagram 方案则直接从 `donut_geometry` import `_matrix` / `_polygon` / `_compose`。两种都过得了门，未统一。
3. **`processing_export.py` 的 coverage 列**：diagram 方案为 DIAGRAM 留了可选的新列，本方案没提 —— 合并后 Formula 对象会被计进 `source_transcription_qualified`。只影响审阅产物统计，未改。
4. **真实样本零覆盖**：§1.3 已实测 AIA 1–20 页 0 个 Formula 对象，本方案只能靠 §6.1 合成夹具验证，§6.4 的 smoke 是"若出现就跑"的骨架。这不是与另两份方案的矛盾，但它是本方案最大的未决风险，README §5 已登记。

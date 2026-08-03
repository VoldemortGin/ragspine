"""LightRAG（HKUDS/LightRAG）公开 API 形状克隆：既有 LightRAG 调用方零改动迁移。

用法与 LightRAG 一致，只换 import：

    from ragspine.compat.lightrag import LightRAG, QueryParam

    rag = LightRAG(working_dir="./ws")
    rag.insert("一段材料")
    answer = rag.query("问题", param=QueryParam(mode="hybrid"))

本模块【薄】——只做签名翻译，全部实际工作回调 `ragspine.RAGSpine` facade，绝不重写检索或
编排逻辑。同为形状克隆的先例见 `service/api/dify_public.py`（Dify）与家族内 pdfspine 的
`fitz.py` shim。

**语义不等价之处（刻意点明，绝不假装等价）**

- `query()` 按 LightRAG 返回**裸字符串**，这会吞掉来源。家族「答案可溯源」是代码级不变量，
  故另开 `query_with_sources()` 返回完整 `AgentResult`（answer + route + sources）。
  迁移者若在意溯源，用后者。
- `QueryParam.mode` 的五档在 RAGSpine 里映射为「是否允许走图路径」，而非 LightRAG 的
  实体级/主题级双层检索：`naive` / `local` → 关图（确定性双通道）；`global` / `hybrid` /
  `mix` → 开图（`graph="auto"`，仅当问题命中全局线索时才真正走图）。RAGSpine 的图层用的是
  确定性连通分量而非 Leiden 分层社区，故 `global` 与 LightRAG 的 global **召回口径不同**。
- `initialize_storages()` 是幂等 no-op：RAGSpine 的工作区在构造时即就绪，无需异步预热。
- 异步变体（`ainsert` / `aquery`）是同步实现的直接包装——RAGSpine 引擎本身是同步的，此处
  不引入伪并发。
- LightRAG 特有的构造参数（`llm_model_func` / `embedding_func` / `chunk_token_size` 等）
  被**容忍并忽略**：RAGSpine 的模型与切块经受控配置与 `RetrievalPreset` 选型，不由这些
  回调决定。忽略而非报错，是为了让迁移代码能先跑起来。
- `insert()` 的裸文本会落成工作区内 `lightrag_inserts/<sha256 前缀>.txt` 再走正规摄入管线，
  从而获得真实 `doc_id` + locator 血缘（内容相同 → 文件名相同 → 摄入层按 hash 幂等跳过）。
  绝不把无来源的悬空文本塞进块库。
"""

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from ragspine.agent.agent import AgentResult
from ragspine.agent.llm_provider import LLMProvider
from ragspine.session import RAGSpine

__all__ = ["VALID_MODES", "LightRAG", "QueryParam"]

# LightRAG 的五档检索模式（原样保留名字，语义映射见模块 docstring）。
VALID_MODES = ("local", "global", "hybrid", "naive", "mix")

# 需要开图的模式（其余走确定性双通道）。
_GRAPH_MODES = frozenset({"global", "hybrid", "mix"})

# 裸文本落盘的子目录名。
_INSERT_DIRNAME = "lightrag_inserts"


@dataclass
class QueryParam:
    """LightRAG `QueryParam` 的字段子集；未知字段容忍并忽略。"""

    mode: str = "mix"
    only_need_context: bool = False
    # LightRAG 还有 top_k / max_token_for_* 等调参字段：接受但不生效（RAGSpine 的检索
    # 参数由 RetrievalPreset 决定）。用 extras 兜住，避免迁移代码传参即崩。
    extras: dict[str, Any] = field(default_factory=dict)


class LightRAG:
    """LightRAG 形状的 RAGSpine 门面。

    Args:
        working_dir: 工作区目录（对应 LightRAG 的同名参数）。
        provider: 可选 `LLMProvider`；缺省用 RAGSpine 的离线 `MockProvider`。
        **ignored: LightRAG 特有构造参数，容忍并忽略（见模块 docstring）。
    """

    def __init__(
        self,
        working_dir: str | Path = "./ragspine_workspace",
        *,
        provider: LLMProvider | None = None,
        **ignored: Any,
    ) -> None:
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self._insert_dir = self.working_dir / _INSERT_DIRNAME
        self._insert_dir.mkdir(parents=True, exist_ok=True)
        self._provider = provider
        # 两个 facade 按图开关分列：mode 决定用哪个。共享同一工作区目录（同一批底层库文件），
        # 故切换模式不会分裂索引。惰性构造，避免未用到的那半付出初始化代价。
        self._facades: dict[bool, RAGSpine] = {}
        self._ignored_kwargs = dict(ignored)

    # ------------------------------------------------------------------ 生命周期

    def _facade(self, *, graph: bool) -> RAGSpine:
        existing = self._facades.get(graph)
        if existing is not None:
            return existing
        created = RAGSpine.local(
            self.working_dir,
            provider=self._provider,
            graph="auto" if graph else "off",
        )
        self._facades[graph] = created
        return created

    def close(self) -> None:
        """关闭已打开的底层工作区（LightRAG 无此方法，属附加便利）。"""
        for facade in self._facades.values():
            facade.close()
        self._facades.clear()

    def __enter__(self) -> "LightRAG":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    async def initialize_storages(self) -> None:
        """LightRAG 要求的异步预热；此处是幂等 no-op（工作区构造即就绪）。"""
        return None

    async def finalize_storages(self) -> None:
        """LightRAG 的收尾钩子；此处关闭工作区。"""
        self.close()

    # ------------------------------------------------------------------ 写入

    def inserted_documents(self) -> list[Path]:
        """按文件名升序列出经 `insert()` 落盘的文档（确定性，便于断言与审计）。"""
        return sorted(self._insert_dir.glob("*.txt"))

    def insert(self, input: str | list[str]) -> None:
        """插入一段或多段裸文本（LightRAG 同名方法）。

        每段落成 `<working_dir>/lightrag_inserts/<sha256 前缀>.txt` 后走正规摄入管线，
        故拿到真实血缘；内容相同则文件名相同，摄入层按 hash 幂等跳过。
        """
        texts = [input] if isinstance(input, str) else list(input)
        facade = self._facade(graph=True)
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            path = self._insert_dir / f"{digest}.txt"
            if not path.exists():
                path.write_text(text, encoding="utf-8")
            facade.ingest(path)

    async def ainsert(self, input: str | list[str]) -> None:
        """`insert` 的异步包装（引擎本身同步，此处不引入伪并发）。"""
        await asyncio.sleep(0)
        self.insert(input)

    # ------------------------------------------------------------------ 查询

    @staticmethod
    def _resolve_mode(param: QueryParam | None) -> str:
        mode = (param or QueryParam()).mode
        if mode not in VALID_MODES:
            raise ValueError(f"unknown query mode {mode!r}; expected one of {list(VALID_MODES)}")
        return mode

    def query_with_sources(self, query: str, param: QueryParam | None = None) -> AgentResult:
        """带血缘的查询出口——LightRAG 的 `query()` 签名吞来源，故另开此法。"""
        mode = self._resolve_mode(param)
        return self._facade(graph=mode in _GRAPH_MODES).ask(query)

    def query(self, query: str, param: QueryParam | None = None) -> str:
        """LightRAG 同名方法：返回裸字符串（来源被签名吞掉，见 `query_with_sources`）。"""
        return self.query_with_sources(query, param).answer

    async def aquery(self, query: str, param: QueryParam | None = None) -> str:
        """`query` 的异步包装。"""
        await asyncio.sleep(0)
        return self.query(query, param)

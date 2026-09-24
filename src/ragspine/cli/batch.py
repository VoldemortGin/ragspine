"""``ragspine batch``：题集批量问答 / retrieval-only 评测的编排（resume、并发、落盘）。

纯逻辑（题集、判定、指标、单题检索）在 ``ragspine.eval.retrieval_only``；这里只做编排：

- ``--retrieval-only``：整批经 ``RAGSpine.open_retriever()``（与 ``ask`` 同一套守卫与检索组装）只跑检索，
  每个 worker 各开一个检索器；不带 ask 的 entity/period 意图过滤。
- 端到端：每题调 ``RAGSpine.ask``（每次调用自开 store 与检索器），不注入 retriever、不改 agent。
- ``results.jsonl`` 每完成一题追加一行（写入加锁）；``--resume`` 跳过已成功的 id（出错的重跑），
  再用全量记录（同 id 取最后一条）重新生成 ``summary.md``。
- 答案与命中块文本只进这两个评测产物，绝不送进 observability trace（trace 只记计数）。

检索配置由 workspace + ``--profile`` 经 ``RAGSpine.local`` 决定；``RAGSpine.local`` 不读 ``RAGSPINE_*``
环境变量，所以真实模型（Qwen embedding / reranker）用 ``--embedding`` / ``--reranker`` /
``--persist-vectors`` 指定；local-http 适配器自己读进程环境里的 ``EMBEDDING_*`` / ``RERANK_*``
（先 ``source data/local-models/local-models.env``）。
"""

import argparse
import json
import logging
import queue
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ragspine.agent.agent import NarrativeRetriever
from ragspine.agent.llm_provider import LLMProvider, MockProvider
from ragspine.common.observability.trace import TRACE_LOGGER_NAME
from ragspine.config import RAGSpineConfig
from ragspine.eval.retrieval_only import (
    BatchQuestion,
    QuestionSetError,
    content_hit,
    hit_page,
    judge_hits,
    load_questions,
    recall_ks,
    retrieval_metrics,
    retrieve_hits,
)
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.session import RAGSpine

DEFAULT_OUT_ROOT = Path("data") / "output" / "batch"
RESULTS_FILE = "results.jsonl"
SUMMARY_FILE = "summary.md"
SETTINGS_FILE = "run_settings.json"
MODE_RETRIEVAL = "retrieval-only"
MODE_ASK = "ask"
# claude-cli provider 自带的并发上限（agent/claude_cli_provider.py 的 DEFAULT_CLAUDE_CLI_CONCURRENCY）。
_CLAUDE_CLI_NOTE = "claude-cli provider 自带并发上限（默认 4 个 `claude -p` 子进程），`--concurrency` 超过 4 时多出的请求会排队。"

# MockProvider 收到翻译请求会原样返回问题：auto 在 mock 下等于没翻译。
_MOCK_TRANSLATION_NOTE = (
    "query_translation=auto 但 provider 是 mock：MockProvider 对翻译请求原样返回问题，"
    "本次跨语言翻译实际没有生效；要评测真实翻译效果请换成真实 provider（anthropic / claude-cli）。"
)

Record = dict[str, Any]


class BatchError(Exception):
    """前置条件不满足（缺 workspace / 缺块库 / 题集非法 / 配置非法）：报错到 stderr、返回 2。"""


def _clip(text: str, limit: int = 240) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 前置检查与装配
# ---------------------------------------------------------------------------


def _check_workspace(workspace: Path, *, retrieval_only: bool) -> None:
    """缺 workspace / 缺知识库 / retrieval-only 下没有叙事块：明确报错，绝不静默建空库。"""
    if not workspace.is_dir():
        raise BatchError(
            f"workspace 不存在：{workspace}（先用 `ragspine ingest <文档> --workspace ...` 建库）"
        )
    db_path = workspace / RAGSpineConfig().storage.knowledge_db
    if not db_path.is_file():
        raise BatchError(f"workspace 里没有知识库：{db_path}（先用 `ragspine ingest` 入库）")
    if retrieval_only:
        store = ChunkStore(db_path)
        try:
            store.init_schema()
            n_chunks = store.count()
        finally:
            store.close()
        if n_chunks == 0:
            raise BatchError(
                f"workspace 里没有叙事块：{db_path}（先用 `ragspine ingest` 入库文档再跑 --retrieval-only）"
            )


def _make_provider(name: str) -> LLMProvider:
    if name == "anthropic":
        from ragspine.agent.llm_provider import AnthropicProvider

        return AnthropicProvider()
    if name == "claude-cli":
        from ragspine.agent import claude_cli_provider

        provider: LLMProvider = claude_cli_provider.ClaudeCliProvider()
        return provider
    return MockProvider()


def _retrieval_overrides(args: argparse.Namespace) -> dict[str, object] | None:
    retrieval = {
        key: value
        for key, value in (("embedding", args.embedding), ("reranker", args.reranker))
        if value is not None
    }
    config: dict[str, object] = {}
    if retrieval:
        config["retrieval"] = retrieval
    if args.persist_vectors:
        config["storage"] = {"persist_vectors": True}
    return config or None


def _open_rag(args: argparse.Namespace, provider: LLMProvider) -> RAGSpine:
    try:
        rag = RAGSpine.local(
            args.workspace,
            provider=provider,
            preset=args.profile,
            config=_retrieval_overrides(args),
        )
    except ValueError as exc:
        raise BatchError(
            f"检索配置非法：{exc}\n（embedding≠none 需要 `--profile balanced` 或 `--profile quality`）"
        ) from exc
    if args.contextual_index is not None:
        # 标题进索引不在 RAGSpineConfig / preset 里（各 profile 均为 off）：与 run_nl_gold_ragspine 同一做法，
        # 只覆盖检索预设这一项；BM25 与向量索引文本都跟随它，economy 下同样生效（只影响 BM25）。
        rag.retrieval = rag.retrieval.with_overrides(contextual_index=args.contextual_index)
    if args.query_translation is not None:
        # 跨语言查询翻译同样只在检索预设里（各 profile 默认 auto）：只覆盖这一项。
        rag.retrieval = rag.retrieval.with_overrides(query_translation=args.query_translation)
    return rag


class _VectorChannelCapture(logging.Handler):
    """旁听装配时的 ``narrative.vector_channel`` trace（只含计数与原因码），得到实际的向量通道状态。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.fields: dict[str, object] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "op", None) == "narrative.vector_channel":
            self.fields = {
                key: getattr(record, key, None)
                for key in ("vector_channel", "vector_reason", "n_vectors")
            }


def _vector_status(rag: RAGSpine, captured: Mapping[str, object] | None) -> str:
    persist = f"persist_vectors={rag.retrieval.persist_vectors}"
    if captured is not None:
        reason = captured.get("vector_reason")
        detail = f"{reason}, " if reason else ""
        return f"{captured.get('vector_channel')}（{detail}n_vectors={captured.get('n_vectors')}；{persist}）"
    if rag.retrieval.retrieval_mode == "economy" or rag.retrieval.embedding == "none":
        return f"bm25_only（未开向量通道；{persist}）"
    return f"hybrid（{rag.retrieval.embedding} 向量，检索时现算；{persist}）"


def _precheck(rag: RAGSpine) -> str:
    """开跑前按 ask 的守卫（关闭 / 索引兼容）组装一次检索器：装配失败即报错，不让每题都记 error。

    返回实际的向量通道状态（持久化向量库为空 / 没有后端时会退化成纯 BM25，配置值不代表实际）。
    """
    logger = logging.getLogger(TRACE_LOGGER_NAME)
    capture = _VectorChannelCapture()
    previous_level = logger.level
    logger.addHandler(capture)
    if not logger.isEnabledFor(logging.INFO):
        logger.setLevel(logging.INFO)
    try:
        with rag.open_retriever():
            pass
    except Exception as exc:  # noqa: BLE001 — 装配 / 兼容性失败都是前置条件不满足
        raise BatchError(f"workspace 检索器装配失败：{_error_text(exc)}") from exc
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
    return _vector_status(rag, capture.fields)


def _out_dir(args: argparse.Namespace) -> Path:
    if args.out is not None:
        out = Path(args.out)
    elif args.resume:
        raise BatchError("--resume 需要用 --out 指明要续跑的输出目录")
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = DEFAULT_OUT_ROOT / f"{Path(args.questions).stem}-{stamp}"
    if (out / RESULTS_FILE).exists() and not args.resume:
        raise BatchError(
            f"输出目录已有结果：{out / RESULTS_FILE}（续跑请加 --resume，或换一个 --out）"
        )
    out.mkdir(parents=True, exist_ok=True)
    return out


def run_settings(
    args: argparse.Namespace, *, contextual_index: str, query_translation: str
) -> dict[str, object]:
    """决定结果口径的运行配置（--limit / --concurrency 不在内：续跑时可以改）。

    ``contextual_index`` / ``query_translation`` 记实际生效值（未传参数时取预设值：off / auto），
    显式传入与预设相同的值视为一致。
    """
    return {
        "questions": str(Path(args.questions).resolve()),
        "workspace": str(Path(args.workspace).resolve()),
        "mode": MODE_RETRIEVAL if args.retrieval_only else MODE_ASK,
        "top_k": args.top_k if args.retrieval_only else None,
        "profile": args.profile,
        "provider": args.provider,
        "embedding": args.embedding,
        "reranker": args.reranker,
        "persist_vectors": bool(args.persist_vectors),
        "contextual_index": contextual_index,
        "query_translation": query_translation,
    }


def _pin_settings(out: Path, settings: Mapping[str, object], *, resume: bool) -> None:
    """首跑写下运行配置；续跑时与之比对，不一致即报错（免得两种口径的记录混在一个目录）。"""
    path = out / SETTINGS_FILE
    if resume and (out / RESULTS_FILE).exists():
        try:
            pinned = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BatchError(
                f"无法读取 {path}（{exc}），不能确认续跑配置一致；请换一个 --out 重跑"
            ) from exc
        if not isinstance(pinned, dict):
            raise BatchError(
                f"{path} 不是 JSON 对象（可能被改坏），不能确认续跑配置一致；请换一个 --out 重跑"
            )
        changed = sorted(
            key for key in {*pinned, *settings} if pinned.get(key) != settings.get(key)
        )
        if changed:
            detail = ", ".join(f"{k}: {pinned.get(k)!r} → {settings.get(k)!r}" for k in changed)
            raise BatchError(f"续跑配置与 {path} 不一致（{detail}）；请换一个 --out 重跑")
        return
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 单题
# ---------------------------------------------------------------------------


def _base_record(question: BatchQuestion, mode: str) -> Record:
    return {
        "id": question.id,
        "question": question.question,
        "mode": mode,
        "expected": question.expected,
        "page_groups": [sorted(group) for group in question.page_groups],
        "doc": list(question.doc),
        "extra": dict(question.extra),
    }


def _retrieval_record(
    question: BatchQuestion, retriever: NarrativeRetriever, *, top_k: int
) -> Record:
    started = time.perf_counter()
    error: str | None = None
    hits: list[dict[str, object]] = []
    try:
        hits = retrieve_hits(retriever, question.question, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 — 单题失败记为未命中，整批继续
        error = _error_text(exc)
    judged = judge_hits(question, hits)
    record = _base_record(question, MODE_RETRIEVAL)
    record.update(
        basis=judged.basis,
        rank=judged.rank,
        page_rank=judged.page_rank,
        hits=[
            {
                "rank": i + 1,
                "doc_id": str(hit.get("doc_id") or ""),
                "locator": str(hit.get("source_locator") or ""),
                "page": key[1] if (key := hit_page(hit)) is not None else None,
                "hit": flag,
                "text": _clip(str(hit.get("text") or "")),
            }
            for i, (hit, flag) in enumerate(zip(hits, judged.hit_flags, strict=True))
        ],
        seconds=round(time.perf_counter() - started, 3),
        error=error,
    )
    return record


def _ask_record(question: BatchQuestion, rag: RAGSpine) -> Record:
    started = time.perf_counter()
    error: str | None = None
    answer = route = ""
    fallback: str | None = None
    sources: list[dict[str, object]] = []
    try:
        result = rag.ask(question.question)
        answer = result.answer_plain or result.answer
        route = result.route
        fallback = result.fallback
        sources = list(result.sources)
    except Exception as exc:  # noqa: BLE001 — 单题失败记为未命中，整批继续
        error = _error_text(exc)
    judged = judge_hits(question.model_copy(update={"expected": None}), sources)
    record = _base_record(question, MODE_ASK)
    record.update(
        answer=answer,
        route="error" if error else route,
        fallback=fallback,
        sources=[
            {
                "doc": str(source.get("doc") or ""),
                "locator": str(source.get("locator") or ""),
                "page": key[1] if (key := hit_page(source)) is not None else None,
                "hit": flag,
            }
            for source, flag in zip(sources, judged.hit_flags, strict=True)
        ],
        page_hit=(judged.rank is not None) if question.page_groups else None,
        content_hit=content_hit(answer, question.expected) if question.expected else None,
        seconds=round(time.perf_counter() - started, 3),
        error=error,
    )
    return record


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def read_records(path: Path, *, warn: bool = False) -> dict[str, Record]:
    """读 results.jsonl（同 id 取最后一条）；损坏的行跳过（续跑时该题会重做），``warn`` 时告警到 stderr。"""
    records: dict[str, Record] = {}
    if not path.is_file():
        return records
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = None
        if isinstance(record, dict) and "id" in record:
            records[str(record["id"])] = record
        elif warn:
            print(f"warning: 跳过 {path} 第 {number} 行（损坏或不完整）", file=sys.stderr)
    return records


def _ensure_trailing_newline(path: Path) -> None:
    """上次中断可能留下没有换行的半行：追加前补一个换行，免得续写的第一条和它粘在一起。"""
    if not path.is_file() or path.stat().st_size == 0:
        return
    with path.open("rb") as handle:
        handle.seek(-1, 2)
        last = handle.read(1)
    if last != b"\n":
        with path.open("ab") as handle:
            handle.write(b"\n")


def _run_retrieval(
    rag: RAGSpine,
    pending: Sequence[BatchQuestion],
    *,
    top_k: int,
    concurrency: int,
    emit: Callable[[Record], None],
) -> None:
    todo: queue.SimpleQueue[BatchQuestion] = queue.SimpleQueue()
    for question in pending:
        todo.put(question)

    def worker() -> None:
        # 检索器（含 sqlite 连接）在本线程打开、本线程关闭。
        with rag.open_retriever() as retriever:
            if retriever is None:
                raise BatchError("workspace 没有可用的叙事检索器")
            while True:
                try:
                    question = todo.get_nowait()
                except queue.Empty:
                    return
                emit(_retrieval_record(question, retriever, top_k=top_k))

    n_workers = max(1, min(concurrency, len(pending)))
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(worker) for _ in range(n_workers)]
    for future in futures:
        future.result()


def _run_ask(
    rag: RAGSpine,
    pending: Sequence[BatchQuestion],
    *,
    concurrency: int,
    emit: Callable[[Record], None],
) -> None:
    # 完成一题写一题（as_completed）：前面的题卡住时，后面已完成的题不会压在内存里不落盘。
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(_ask_record, question, rag) for question in pending]
        for future in as_completed(futures):
            emit(future.result())


def run(args: argparse.Namespace) -> int:
    """``ragspine batch`` 的实现；前置条件不满足返回 2。"""
    try:
        return _run(args)
    except BatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    if args.top_k < 1 or args.concurrency < 1 or (args.limit is not None and args.limit < 1):
        raise BatchError("--top-k / --concurrency / --limit 须为正整数")
    workspace = Path(args.workspace)
    _check_workspace(workspace, retrieval_only=args.retrieval_only)
    try:
        questions = load_questions(args.questions)
    except QuestionSetError as exc:
        raise BatchError(str(exc)) from exc
    if args.limit is not None:
        questions = questions[: args.limit]
    try:
        provider = _make_provider(args.provider)
    except Exception as exc:  # noqa: BLE001 — 缺 extra / key / 二进制：前置条件不满足
        raise BatchError(f"provider {args.provider!r} 不可用：{_error_text(exc)}") from exc
    rag = _open_rag(args, provider)
    vector_channel = _precheck(rag)
    out = _out_dir(args)
    _pin_settings(
        out,
        run_settings(
            args,
            contextual_index=rag.retrieval.contextual_index,
            query_translation=rag.retrieval.query_translation,
        ),
        resume=args.resume,
    )
    results_path = out / RESULTS_FILE

    done = {
        rid
        for rid, record in read_records(results_path, warn=True).items()
        if record.get("error") is None
    }
    _ensure_trailing_newline(results_path)
    pending = [q for q in questions if q.id not in done]
    lock = threading.Lock()
    completed = failed = 0

    with results_path.open("a", encoding="utf-8") as sink:

        def emit(record: Record) -> None:
            nonlocal completed, failed
            with lock:
                sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                sink.flush()
                completed += 1
                failed += 1 if record["error"] else 0
                status = "error" if record["error"] else "ok"
                print(f"  [{completed}/{len(pending)}] {record['id']} {status}", flush=True)

        with rag:
            if args.retrieval_only:
                try:
                    _run_retrieval(
                        rag, pending, top_k=args.top_k, concurrency=args.concurrency, emit=emit
                    )
                except BatchError:
                    raise
                except Exception as exc:  # noqa: BLE001 — 单题错误已在记录里；逃出来的是装配失败
                    raise BatchError(f"检索器装配失败：{_error_text(exc)}") from exc
            else:
                _run_ask(rag, pending, concurrency=args.concurrency, emit=emit)

    records = read_records(results_path)
    ordered = [records[q.id] for q in questions if q.id in records]
    settings = {
        "questions": str(args.questions),
        "workspace": str(workspace),
        "mode": MODE_RETRIEVAL if args.retrieval_only else MODE_ASK,
        "profile": args.profile,
        "retrieval_mode": rag.retrieval.retrieval_mode,
        "embedding": rag.retrieval.embedding,
        "reranker": rag.retrieval.reranker,
        "postprocessor": rag.retrieval.postprocessor,
        "vector_channel": vector_channel,
        "page_parent": rag.retrieval.page_parent,
        "contextual_index": rag.retrieval.contextual_index,
        "query_translation": rag.retrieval.query_translation,
        "provider": args.provider,
        "top_k": str(args.top_k) if args.retrieval_only else "(ask 固定 50)",
        "concurrency": str(args.concurrency),
        "questions_run": f"{len(ordered)}/{len(questions)}（本次新跑 {len(pending)}）",
    }
    summary = render_summary(ordered, settings, top_k=args.top_k)
    (out / SUMMARY_FILE).write_text(summary, encoding="utf-8")
    print(f"results: {results_path}\nsummary: {out / SUMMARY_FILE}")
    print(_headline(ordered, top_k=args.top_k))
    if pending and failed == len(pending):
        print(f"error: 本次 {failed} 题全部出错，见 {results_path}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# summary.md
# ---------------------------------------------------------------------------


def _mark(flag: object) -> str:
    return "✓" if flag else "✗"


def _retrieval_metrics(records: Sequence[Mapping[str, Any]], top_k: int) -> dict[str, Any]:
    judged = [r for r in records if r.get("basis") in ("page", "content")]
    ranks = [(r.get("rank"), r.get("page_rank")) for r in judged]
    metrics = retrieval_metrics(ranks, recall_ks(top_k))
    metrics["unjudged"] = len(records) - len(judged)
    metrics["errors"] = sum(1 for r in records if r.get("error"))
    return metrics


def _rate(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.4f} ({numerator}/{denominator})" if denominator else "—"


def _headline(records: Sequence[Mapping[str, Any]], *, top_k: int) -> str:
    if records and records[0].get("mode") == MODE_RETRIEVAL:
        m = _retrieval_metrics(records, top_k)
        recall = " ".join(f"page_recall{k}={v:.4f}" for k, v in m["page_recall"].items())
        return f"judged={m['judged']} {recall} mrr={m['mrr']:.4f} errors={m['errors']}"
    page = [r for r in records if r.get("page_hit") is not None]
    content = [r for r in records if r.get("content_hit") is not None]
    return (
        f"page_hit={_rate(sum(bool(r['page_hit']) for r in page), len(page))} "
        f"content_hit={_rate(sum(bool(r['content_hit']) for r in content), len(content))} "
        f"errors={sum(1 for r in records if r.get('error'))}"
    )


_JUDGING_NOTES = [
    "有页码（page group）时按页判定：命中 = 块的页 ∈ group；题目给了 doc 时还要求 doc_id（或去扩展名后）匹配。"
    "没页码的块（如 pptx 的 slide=）不参与页级排名，与 nl_gold 一致。",
    "没页码时回退到 expected 与命中块 text / prompt_text 比对：整串出现即命中，否则 expected 里的每个数字都须出现"
    "（规范化与匹配委托 nl_gold 的 normalize_answer / contains_normalized）。比对的是命中块文本而非整页；38.00 与 38 不视为相等。",
    "rank = 各 group 首次命中位置的最大值（所有 group 都进前 k 才算命中）。"
    "recall@k 的名次：按页判定时只数带页码的命中（没页码的块不占名次，与 nl_gold 一致），按 expected 判定时数全部检索条数；"
    "page_recall@k 按不同页计名次（同一页只算第一次出现）；MRR = mean(1/rank)，未命中记 0。"
    "逐题列表的序号是检索结果的原始位置，可能与按页判定的 rank 不同。",
    "按 expected 判定且题目没给 doc 时，page_rank 只按页码去重：多文档 workspace 里不同文档的同号页算同一页，"
    "page_rank 可能偏低、page_recall 偏高；需要精确时请在题目里给 doc。",
    "既无页码也无 expected 的题不计入分母；出错的题记为未命中。",
]


def render_summary(
    records: Sequence[Mapping[str, Any]], settings: Mapping[str, str], *, top_k: int
) -> str:
    """指标表 + 判定说明 + 逐题明细（命中项打 ✓）。"""
    mode = settings.get("mode", MODE_RETRIEVAL)
    lines = [f"# ragspine batch — {mode}", "", "## 配置", ""]
    lines += [f"- {key}: `{value}`" for key, value in settings.items()]
    lines += ["", "## 说明", ""]
    if mode == MODE_RETRIEVAL:
        lines.append(
            "- retrieval-only 不带 ask 的 entity/period 意图过滤（ask 会按问题解析出的实体/期间过滤检索），"
            "排序可能与 ask 不同。检索组装与 ask 相同（`RAGSpine.open_retriever()`），"
            "listwise 二审跟随 provider。"
        )
    else:
        lines.append(
            "- 端到端：每题调 `RAGSpine.ask`；页命中按 sources 的 locator 判定，内容命中比对答案。"
        )
    if settings.get("provider") == "mock" and settings.get("query_translation") == "auto":
        lines.append(f"- {_MOCK_TRANSLATION_NOTE}")
    if settings.get("provider") == "claude-cli":
        lines.append(f"- {_CLAUDE_CLI_NOTE}")
    lines += [f"- {note}" for note in _JUDGING_NOTES]
    lines += ["", "## 指标", ""]
    if mode == MODE_RETRIEVAL:
        metrics = _retrieval_metrics(records, top_k)
        ks = list(metrics["recall"])
        lines.append("| 口径 | " + " | ".join(ks) + " | MRR |")
        lines.append("|---" * (len(ks) + 2) + "|")
        for name, mrr_key in (("recall", "mrr"), ("page_recall", "page_mrr")):
            cells = " | ".join(f"{metrics[name][k]:.4f}" for k in ks)
            lines.append(f"| {name} | {cells} | {metrics[mrr_key]:.4f} |")
        lines += [
            "",
            f"已判定 {metrics['judged']} 题，无法判定 {metrics['unjudged']} 题，出错 {metrics['errors']} 题。",
        ]
    else:
        page = [r for r in records if r.get("page_hit") is not None]
        content = [r for r in records if r.get("content_hit") is not None]
        routes = Counter(
            f"{r.get('route')}→fallback" if r.get("fallback") else str(r.get("route"))
            for r in records
        )
        lines += [
            "| 指标 | 值 |",
            "|---|---|",
            f"| page_hit | {_rate(sum(bool(r['page_hit']) for r in page), len(page))} |",
            f"| content_hit | {_rate(sum(bool(r['content_hit']) for r in content), len(content))} |",
            f"| errors | {sum(1 for r in records if r.get('error'))} |",
            f"| routes | {', '.join(f'{k}={v}' for k, v in routes.most_common())} |",
        ]
    lines += ["", "## 逐题", ""]
    for record in records:
        lines += _question_lines(record)
    return "\n".join(lines).rstrip() + "\n"


def _question_lines(record: Mapping[str, Any]) -> list[str]:
    header = f"### {record['id']}"
    if record.get("mode") == MODE_RETRIEVAL:
        if record.get("basis") != "none":
            header += f" — rank={record.get('rank')} page_rank={record.get('page_rank')}"
        lines = [header, "", f"问：{record['question']}"]
        if record.get("error"):
            lines.append(f"错误：{record['error']}")
        lines += [
            f"{hit['rank']}. {_mark(hit['hit'])} `{hit['locator']}` {_clip(str(hit['text']), 120)}"
            for hit in record.get("hits", [])
        ]
    else:
        flags = [
            f"page_hit={_mark(record['page_hit'])}" if record.get("page_hit") is not None else "",
            f"content_hit={_mark(record['content_hit'])}"
            if record.get("content_hit") is not None
            else "",
        ]
        route = record.get("route")
        if record.get("fallback"):
            route = f"{route}→fallback({record['fallback']})"
        header += f" — route={route} " + " ".join(f for f in flags if f)
        lines = [header.rstrip(), "", f"问：{record['question']}"]
        if record.get("error"):
            lines.append(f"错误：{record['error']}")
        lines.append(f"答：{_clip(str(record.get('answer') or ''), 400)}")
        lines += [
            f"- {_mark(source['hit'])} `{source['doc']} · {source['locator']}`"
            for source in record.get("sources", [])
        ]
    if record.get("expected"):
        lines.append(f"期望：{record['expected']}")
    return [*lines, ""]

"""nl-answers-gold 集跑 ragspine 主链路（两路：真实 ask / 强制叙事），写基线报告。

判定与汇总逻辑在 ``ragspine.eval.nl_gold_ragspine``；本脚本只做接线：入库文档 → （可选）补齐块向量
→ 组装检索器（embedding / 精排可选真实 HTTP 模型）→ 逐路由逐 case 提问 → 落盘报告。

默认值指向 AIA 样本（gold + DI markdown），公司 / gold / 文档全部可由参数改；默认 provider 为 mock、
embedding / 精排为 none，即零模型零网络。真实模型基线（先 ``source data/local-models/local-models.env``
取 EMBEDDING_* / RERANK_*，并确认 SSH 隧道在）：

    .venv/bin/python scripts/run_nl_gold_ragspine.py --provider claude-cli \\
        --embedding local-http --reranker local-http --label baseline

真实模型连不上即退出（exit 2），绝不静默降级成 mock。报告写到
``data/validation/ragspine-nl-gold/<YYYY-MM-DD>-<label>/``（report.md / report.json / cases/）。
"""

import argparse
import hashlib
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

DEFAULT_GOLD = (
    ROOT_DIR / "data/benchmarks/enterprise-pdf-rag/aia-2026-interim/nl-answers-gold-v1.json"
)
DEFAULT_DOCUMENT = ROOT_DIR / "data/di-markdown/aia-group-2026-interim-results-presentation.md"
DEFAULT_OUT_ROOT = ROOT_DIR / "data/validation/ragspine-nl-gold"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="评测专用 workspace（默认 <out-root>/workspaces/<文档名>）",
    )
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument("--label", default="baseline")
    parser.add_argument(
        "--company-config",
        type=Path,
        default=None,
        help="公司 profile TOML（设 RAGSPINE_COMPANY_CONFIG）；缺省沿用当前环境",
    )
    parser.add_argument("--provider", choices=("mock", "claude-cli"), default="mock")
    parser.add_argument("--claude-model", default=None, help="claude-cli 的 --model（缺省不指定）")
    parser.add_argument("--embedding", choices=("none", "local-http"), default="none")
    parser.add_argument(
        "--reranker",
        choices=("none", "local-http", "llm"),
        default="none",
        help="精排：local-http=/v1/rerank；llm=provider listwise（每问多一次 LLM 调用）",
    )
    parser.add_argument("--routes", default="A-ask,B-narrative")
    parser.add_argument("--languages", default="en,zh")
    parser.add_argument("--cases", default="", help="只跑这些 case_id（逗号分隔）")
    parser.add_argument("--reference-date", default=None, help="ISO 日期，缺省今天")
    return parser.parse_args(argv)


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.company_config is not None:
        # 必须先于 ragspine.agent 的 import：profile 在模块导入期绑定。
        os.environ["RAGSPINE_COMPANY_CONFIG"] = str(args.company_config.resolve())

    from ragspine.eval.nl_gold_ragspine import (
        CaseRun,
        CountingProvider,
        index_chunk_vectors,
        load_nl_gold,
        run_route,
        summarize,
        write_report,
    )
    from ragspine.retrieval.link.narrative_link import build_narrative_retriever
    from ragspine.retrieval.vector.store import InProcessVectorStore
    from ragspine.session import RAGSpine
    from ragspine.storage.fact_store import SqliteFactStore

    started_all = time.perf_counter()
    routes = [r for r in args.routes.split(",") if r]
    languages = [lang for lang in args.languages.split(",") if lang]
    wanted = {c for c in args.cases.split(",") if c}
    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else None

    cases = load_nl_gold(args.gold)
    if wanted:
        cases = tuple(c for c in cases if c.case_id in wanted)

    document: Path = args.document
    workspace: Path = args.workspace or args.out_root / "workspaces" / document.stem
    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    ingest = RAGSpine.local(workspace).ingest(document)
    timings["ingest_s"] = round(time.perf_counter() - t0, 2)
    if ingest.failed:
        print(f"入库失败：{ingest.summary}", file=sys.stderr)
        return 1
    db = workspace / "knowledge.db"

    embedding_backend = None
    judge = None
    models: dict[str, str] = {}
    if args.embedding == "local-http" or args.reranker == "local-http":
        from ragspine.common.evidence.providers.local_models import (
            LocalEmbeddingAdapter,
            LocalRerankAdapter,
        )
        from ragspine.common.evidence.providers.providers import load_local_model_config
        from ragspine.retrieval.rerank.scored_judge import ScoredRerankJudge
        from ragspine.retrieval.vector.single_text_backend import SingleTextEmbeddingBackend

        try:
            if args.embedding == "local-http":
                config = load_local_model_config("embedding")
                embedding_backend = SingleTextEmbeddingBackend(LocalEmbeddingAdapter(config))
                probe = embedding_backend.embed_texts(["ping"])[0]
                models["embedding"] = f"local-http/{config.model} ({len(probe)}d)"
            if args.reranker == "local-http":
                config = load_local_model_config("rerank")
                judge = ScoredRerankJudge(LocalRerankAdapter(config))
                judge.judge("ping", ["ping", "pong"])
                models["reranker"] = f"local-http/{config.model}"
        except Exception as exc:  # noqa: BLE001 — 真实模型不可用即如实退出，绝不降级
            print(
                f"真实模型不可用（{type(exc).__name__}: {exc}）。先 source "
                "data/local-models/local-models.env 并确认 SSH 隧道在；不会降级成 mock。",
                file=sys.stderr,
            )
            return 2

    if args.provider == "claude-cli":
        from ragspine.agent.claude_cli_provider import ClaudeCliProvider

        provider = ClaudeCliProvider(model=args.claude_model)
    else:
        from ragspine.agent.llm_provider import MockProvider

        provider = MockProvider(reference_date=reference_date)

    vector_store = InProcessVectorStore() if embedding_backend is not None else None
    vectors = 0
    if embedding_backend is not None and vector_store is not None:
        t0 = time.perf_counter()
        vectors = index_chunk_vectors(db, embedding_backend, vector_store)
        timings["index_vectors_s"] = round(time.perf_counter() - t0, 2)

    rerank_counter = CountingProvider(provider) if args.reranker == "llm" else None
    retriever, chunk_store = build_narrative_retriever(
        db,
        provider=rerank_counter,
        embedding_backend=embedding_backend,
        vector_store=vector_store,
        reranker=judge,
    )
    fact_store = SqliteFactStore(db)
    fact_store.init_schema()

    chunk_count = sum(1 for _ in chunk_store.iter_chunks())

    def progress(run: CaseRun) -> None:
        status = "PASS" if run.judgement.passed else "FAIL"
        print(
            f"  {run.route} {run.case_id:<34} {run.language} {status} "
            f"route={run.route_label:<10} llm={run.llm_calls} {run.seconds:.1f}s",
            flush=True,
        )

    runs: dict[str, list[CaseRun]] = {}
    try:
        for route in routes:
            print(f"==> route {route}", flush=True)
            t0 = time.perf_counter()
            runs[route] = run_route(
                cases,
                route,
                store=fact_store,
                retriever=retriever,
                provider=provider,
                reference_date=reference_date,
                languages=languages,
                progress=progress,
            )
            timings[f"{route}_s"] = round(time.perf_counter() - t0, 1)
    finally:
        fact_store.close()
        chunk_store.close()

    timings["total_s"] = round(time.perf_counter() - started_all, 1)
    answer_calls = sum(r.llm_calls for rs in runs.values() for r in rs)
    meta = {
        "label": args.label,
        "date": date.today().isoformat(),
        "git_head": _git_head(),
        "gold": str(args.gold),
        "document": str(document),
        "document_sha256": hashlib.sha256(document.read_bytes()).hexdigest(),
        "workspace": str(workspace),
        "chunks": chunk_count,
        "vectors": vectors,
        "provider": args.provider + (f"/{args.claude_model}" if args.claude_model else ""),
        "embedding": models.get("embedding", args.embedding),
        "reranker": models.get("reranker", args.reranker),
        "reference_date": (reference_date or date.today()).isoformat(),
        "company_config": os.environ.get("RAGSPINE_COMPANY_CONFIG", "(default profile)"),
        "languages": ",".join(languages),
        "llm_calls_answer": answer_calls,
        "llm_calls_rerank": rerank_counter.calls if rerank_counter else 0,
        "timings": timings,
    }
    out = write_report(
        args.out_root / f"{date.today().isoformat()}-{args.label}",
        meta=meta,
        cases=cases,
        runs=runs,
    )
    for route, route_runs in runs.items():
        s = summarize(route_runs)
        print(
            f"{route}: main {s['main']['passed']}/{s['main']['total']} | "
            f"content {s['claims']['content_hit_rate']:.0%} page {s['claims']['page_hit_rate']:.0%}"
            f" | routes {s['route_distribution']} | llm {s['llm_calls']}"
        )
    print(f"report: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

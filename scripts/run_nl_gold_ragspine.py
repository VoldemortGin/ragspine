"""nl-answers-gold 集跑 ragspine 主链路（两路：真实 ask / 强制叙事），写基线报告。

判定与汇总逻辑在 ``ragspine.eval.nl_gold_ragspine``；本脚本只做接线：入库文档（开 embedding 时走正式的
入库即嵌入落盘路径 ``storage.persist_vectors``）→ 组装检索器（从持久化向量库读；embedding / 精排可选真实 HTTP
模型，经 ``make_embedding_backend`` / ``make_reranker`` 的 ``local-http``）→ 逐路由逐 case 提问 → 落盘报告。

默认值指向 AIA 样本（gold + DI markdown），公司 / gold / 文档全部可由参数改；默认 provider 为 mock、
embedding / 精排为 none，即零模型零网络。真实模型基线（先 ``source data/local-models/local-models.env``
取 EMBEDDING_* / RERANK_*，并确认 SSH 隧道在）：

    .venv/bin/python scripts/run_nl_gold_ragspine.py --provider claude-cli \\
        --embedding local-http --reranker local-http --repeat 3 --label baseline

主口径是整份文档（``--pages all``，默认）；``--pages gold`` 只入库 gold 冻结的页。``--repeat N`` 每题跑 N 次，
报告每题通过率、主分均值±标准差和不稳定题（不做多数票）。``--rejudge <旧报告目录>`` 不入库、不调模型，
只用当前判分器 + ``--gold`` 重判旧报告里记录的答案，看判分器 / gold 修正本身让分数变了多少。

图文混合上下文：``--source-pdf <原 PDF>`` 在入库时关联并渲染页图，``--page-images on``（需
``--page-parent dedup|page+child``）给检索结果的前 ``--page-images-top-n`` 页附页图，claude-cli 会读图。

真实模型连不上即退出（exit 2），绝不静默降级成 mock。报告写到
``data/validation/ragspine-nl-gold/<YYYY-MM-DD>-<label>/``（report.md / report.json / cases/）。
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

DEFAULT_GOLD = (
    ROOT_DIR / "data/benchmarks/enterprise-pdf-rag/aia-2026-interim/nl-answers-gold-v2.json"
)
DEFAULT_DOCUMENT = ROOT_DIR / "data/di-markdown/aia-group-2026-interim-results-presentation.md"
DEFAULT_OUT_ROOT = ROOT_DIR / "data/validation/ragspine-nl-gold"


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("须为 ≥1 的整数")
    return value


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
    parser.add_argument(
        "--pages",
        choices=("gold", "all"),
        default="all",
        help="all=整份文档（主口径，默认）；gold=只入库 gold 冻结的物理页"
        "（pinned.selected_physical_pages，其余页清空、页号不变）",
    )
    parser.add_argument(
        "--repeat",
        type=_positive_int,
        default=1,
        help="每题跑 N 次：报告每题通过率、主分均值±标准差、不稳定题（不做多数票）",
    )
    parser.add_argument(
        "--rejudge",
        type=Path,
        default=None,
        help="旧报告目录（含 report.json）：不重新生成，只用当前判分器 + --gold 重判其中记录的答案",
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
    parser.add_argument(
        "--page-parent",
        choices=("off", "dedup", "page+child"),
        default="page+child",
        help="页级父子（RAGSPINE_PAGE_PARENT）：dedup=按页去重 + 整页上下文；page+child=另加整页 BM25 一路",
    )
    parser.add_argument(
        "--contextual-index",
        choices=("off", "heading", "full"),
        default="off",
        help="标题进索引（RAGSPINE_CONTEXTUAL_INDEX）：heading=BM25/向量索引文本前拼标题路径；full=再加 title/entity/period",
    )
    parser.add_argument(
        "--source-pdf",
        type=Path,
        default=None,
        help="文档对应的原 PDF（页数须与 markdown 一致）；入库时渲染页图。缺省读 <stem>.meta.json 的 source_pdf",
    )
    parser.add_argument(
        "--page-images",
        choices=("off", "on"),
        default="off",
        help="图文混合上下文（RAGSPINE_PAGE_IMAGES）：on=前 N 页附原 PDF 页图（需 --page-parent 非 off）",
    )
    parser.add_argument("--page-images-top-n", type=int, default=3)
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
        gold_selected_pages,
        gold_version,
        load_nl_gold,
        rejudge_report,
        run_route,
        select_di_pages,
        summarize,
        write_report,
    )
    from ragspine.retrieval.link.narrative_link import build_narrative_retriever
    from ragspine.retrieval.page_images.attach import make_page_image_retriever
    from ragspine.retrieval.rerank.cross_encoder import make_reranker
    from ragspine.retrieval.vector.chunk_index import embedding_model_id
    from ragspine.retrieval.vector.embedding_backends import make_embedding_backend
    from ragspine.service.config import ServiceConfig, open_vector_channel
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
    gold_meta = {
        "gold": str(args.gold),
        "gold_version": gold_version(args.gold),
        "gold_sha256": hashlib.sha256(args.gold.read_bytes()).hexdigest(),
    }

    if args.rejudge is not None:
        old_report = args.rejudge / "report.json"
        old_meta = json.loads(old_report.read_text(encoding="utf-8"))["meta"]
        rejudged = rejudge_report(old_report, cases)
        meta = {
            **old_meta,
            **gold_meta,
            "label": args.label,
            "date": date.today().isoformat(),
            "git_head": _git_head(),
            "rejudged_from": str(args.rejudge),
            "rejudged_from_judge_version": old_meta.get("judge_version", "nl-gold-judge-v1"),
            "rejudged_from_gold": old_meta.get("gold", ""),
        }
        out = write_report(
            args.out_root / f"{date.today().isoformat()}-{args.label}",
            meta=meta,
            cases=cases,
            runs=rejudged,
        )
        _print_summary(rejudged, summarize)
        print(f"report: {out / 'report.md'}")
        return 0

    document: Path = args.document
    workspace: Path = args.workspace or args.out_root / "workspaces" / document.stem
    timings: dict[str, float] = {}

    source = document
    selected_pages: tuple[int, ...] = ()
    if args.pages == "gold":
        selected_pages = gold_selected_pages(args.gold)
        # 同名文件放进 workspace/source/，doc_id 与整份入库时一致。
        source = workspace / "source" / document.name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            select_di_pages(document.read_text(encoding="utf-8"), selected_pages),
            encoding="utf-8",
        )
    embedding_backend = None
    judge = None
    models: dict[str, str] = {}
    if args.embedding == "local-http" or args.reranker == "local-http":
        try:
            if args.embedding == "local-http":
                embedding_backend = make_embedding_backend("local-http")
                assert embedding_backend is not None
                probe = embedding_backend.embed_texts(["ping"])[0]
                models["embedding"] = f"{embedding_model_id(embedding_backend)} ({len(probe)}d)"
            if args.reranker == "local-http":
                judge = make_reranker("local-http")
                assert judge is not None
                judge.judge("ping", ["ping", "pong"])
                models["reranker"] = f"local-http/{os.environ.get('RERANK_MODEL', '')}"
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

    # 入库：开 embedding 时走正式路径——入库即按 embedding 配置嵌入、写进持久化向量库（幂等，已入库的
    # 块只补缺失 / 变更的向量）；不开则与原来一样只写块、纯 BM25。
    rag_config: dict[str, object] = {}
    preset = None
    if args.embedding != "none":
        preset = "balanced"
        rag_config = {
            "retrieval": {"embedding": args.embedding},
            "storage": {"persist_vectors": True},
        }
    t0 = time.perf_counter()
    if args.page_images == "on" and args.page_parent == "off":
        print(
            "警告：--page-images on 需要 --page-parent dedup|page+child，否则不附页图",
            file=sys.stderr,
        )
    rag = RAGSpine.local(workspace, preset=preset, config=rag_config)
    rag.retrieval = rag.retrieval.with_overrides(contextual_index=args.contextual_index)
    ingest = rag.ingest(source, source_pdf=args.source_pdf)
    timings["ingest_s"] = round(time.perf_counter() - t0, 2)
    if ingest.failed:
        print(f"入库失败：{ingest.summary}", file=sys.stderr)
        return 1
    db = workspace / "knowledge.db"
    vectors = ingest.vector_report.total if ingest.vector_report is not None else 0
    page_images_indexed = (
        sum(d.n_images for d in ingest.page_image_report.docs)
        if ingest.page_image_report is not None
        else 0
    )

    vector_index = None
    vector_store = None
    if embedding_backend is not None:
        embedding_backend, vector_index = open_vector_channel(
            ServiceConfig(
                db_path=str(db),
                chunk_db_path=str(db),
                persist_vectors=True,
                contextual_index=args.contextual_index,
            ),
            embedding_backend,
        )
        vector_store = vector_index.store if vector_index is not None else None

    rerank_counter = CountingProvider(provider) if args.reranker == "llm" else None
    retriever, chunk_store = build_narrative_retriever(
        db,
        provider=rerank_counter,
        embedding_backend=embedding_backend,
        vector_store=vector_store,
        reranker=judge,
        page_parent=args.page_parent,
        contextual_index=args.contextual_index,
    )
    retriever = make_page_image_retriever(
        retriever,
        args.page_images,
        chunk_db_path=db,
        top_n=args.page_images_top_n,
        page_parent=args.page_parent,
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
            runs[route] = []
            t0 = time.perf_counter()
            for repeat in range(args.repeat):
                print(f"==> route {route} run {repeat + 1}/{args.repeat}", flush=True)
                runs[route] += run_route(
                    cases,
                    route,
                    store=fact_store,
                    retriever=retriever,
                    provider=provider,
                    reference_date=reference_date,
                    languages=languages,
                    progress=progress,
                    repeat=repeat,
                )
            timings[f"{route}_s"] = round(time.perf_counter() - t0, 1)
    finally:
        fact_store.close()
        chunk_store.close()
        if vector_index is not None:
            vector_index.close()

    timings["total_s"] = round(time.perf_counter() - started_all, 1)
    answer_calls = sum(r.llm_calls for rs in runs.values() for r in rs)
    meta = {
        "label": args.label,
        "date": date.today().isoformat(),
        "git_head": _git_head(),
        **gold_meta,
        "repeat": args.repeat,
        "document": str(document),
        "document_sha256": hashlib.sha256(document.read_bytes()).hexdigest(),
        "workspace": str(workspace),
        "pages": (
            f"gold pinned {selected_pages[0]}-{selected_pages[-1]} ({len(selected_pages)} pages)"
            if selected_pages
            else "all"
        ),
        "chunks": chunk_count,
        "vectors": vectors,
        "provider": args.provider + (f"/{args.claude_model}" if args.claude_model else ""),
        "embedding": models.get("embedding", args.embedding),
        "reranker": models.get("reranker", args.reranker),
        "page_parent": args.page_parent,
        "contextual_index": args.contextual_index,
        "page_images": args.page_images,
        "page_images_top_n": args.page_images_top_n,
        "source_pdf": str(args.source_pdf) if args.source_pdf else "",
        "page_images_indexed": page_images_indexed,
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
    _print_summary(runs, summarize)
    print(f"report: {out / 'report.md'}")
    return 0


def _print_summary(
    runs: Mapping[str, Sequence[Any]], summarize: Callable[[Sequence[Any]], dict[str, Any]]
) -> None:
    for route, route_runs in runs.items():
        s = summarize(route_runs)
        rep = s["repeat"]
        print(
            f"{route}: main {s['main']['passed']}/{s['main']['total']} "
            f"(mean {rep['mean']:.1%} ± {rep['std']:.1%} over {rep['repeats']} run(s)) | "
            f"content {s['claims']['content_hit_rate']:.0%} page {s['claims']['page_hit_rate']:.0%}"
            f" fragment {s['claims']['fragment_hits']} | routes {s['route_distribution']}"
            f" | unstable {[c['case_id'] for c in rep['unstable']]} | llm {s['llm_calls']}"
        )


if __name__ == "__main__":
    raise SystemExit(main())

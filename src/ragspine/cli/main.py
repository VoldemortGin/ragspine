"""ragspine 控制台 CLI 的 argparse 入口与子命令实现（stdlib only，零新依赖）。

子命令均为包内 API 的薄封装（不 shell 调 scripts/，那些不进 wheel）：
- quickstart —— headline：全程离线、零 key。在临时目录建一个微型 KB，用 MockProvider
  跑两次 answer_question：①命中 → 打印数值 + 来源血缘；②查不到 → 打印确定性诚实拒答
  （绝不提供推测数字）。让首次用户一眼看到"会引用真实来源、且绝不臆造"。
- ask —— 镜像 scripts/ask.py：从 --db 建 FactStore、按 --provider 选 provider
  （默认 mock 离线确定性；anthropic 仅在装了 [llm] extra + 有 key 时延迟接入），
  跑 answer_question 并打印答案 + 来源。库路径不存在时诚实报错、非零退出。
- version —— 打印分发版本号。

# TODO: serve / worker / demo / topology 子命令暂不提供——它们需要 [service] extra
# 或随仓库分发的 scripts/，与"零-SDK 离线核心自包含"的设计相悖；待服务层入 wheel 后再补。
"""

import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory

from ragspine.agent.agent import AgentResult, answer_question
from ragspine.agent.llm_provider import MockProvider
from ragspine.common.core import DEFAULT_FACT_DB
from ragspine.storage.fact_store import Fact, FactStore

# 分发名（PEP 621 [project] name = "rag-spine"，import 名仍是 ragspine）。
_DIST_NAME = "rag-spine"

# quickstart 用的微型合成事实（虚构公司 ACME，确定性、可重建；与 qa_eval 的口径一致）。
# 命中演示：ACME_CN FY2024 REVENUE 1320 USD_M，带完整来源血缘。
_QUICKSTART_FACT = Fact(
    metric_code="REVENUE", entity="ACME_CN", geography="CN",
    channel="TOTAL", period_type="FY", period="2024",
    value=1320.0, unit="USD_M",
    source_doc_id="ACME_FY2024_Results.pptx",
    source_locator="slide=6,table=1,row=2,col=3",
)
# 命中问法（事实存在）与查不到问法（事实缺失，演示坦白拒答）。
_QUICKSTART_FOUND_Q = "中国内地FY2024的REVENUE是多少"
_QUICKSTART_MISSING_Q = "中国内地FY2030的REVENUE是多少"


def _print_result(result: AgentResult) -> None:
    """打印一次问答的答案与数据血缘（与 scripts/ask.py 输出格式一致）。"""
    print(result.answer)
    if result.sources:
        print("\n数据血缘：")
        for src in result.sources:
            print(f"  - {src['doc']} · {src['locator']}")


def _cmd_quickstart(args: argparse.Namespace) -> int:
    """headline 离线演示：建临时 KB → 命中（带血缘）+ 查不到（坦白拒答）→ 清理。"""
    print("RAGSpine quickstart —— 全程离线、零 API key，演示反幻觉 + 来源溯源。\n")
    with TemporaryDirectory(prefix="ragspine_quickstart_") as tmp:
        store = FactStore(Path(tmp) / "facts.db")
        store.init_schema()
        store.upsert_facts([_QUICKSTART_FACT])
        provider = MockProvider()
        try:
            print(f"① 命中（事实存在，回答必带来源）\n   问：{_QUICKSTART_FOUND_Q}")
            _print_result(answer_question(_QUICKSTART_FOUND_Q, store, provider))
            print(f"\n② 查不到（事实缺失，坦白拒答、绝不臆造）\n   问：{_QUICKSTART_MISSING_Q}")
            _print_result(answer_question(_QUICKSTART_MISSING_Q, store, provider))
        finally:
            store.close()
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    """单条提问：从 --db 建 FactStore、选 provider、跑 answer_question、打印答案 + 来源。"""
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"error: fact 库不存在：{db_path}\n"
            "请用 --db 指定有效的 fact_metric sqlite 路径，"
            "或先用 `ragspine quickstart` 体验离线演示。",
            file=sys.stderr,
        )
        return 2

    if args.provider == "anthropic":
        # 真实 Claude：SDK 延迟 import（缺 [llm] extra 或 key 时由 provider 自身报错）。
        from ragspine.agent.llm_provider import AnthropicProvider
        provider: object = AnthropicProvider()
    else:
        provider = MockProvider()

    store = FactStore(db_path)
    store.init_schema()
    try:
        result = answer_question(args.question, store, provider)  # type: ignore[arg-type]
    finally:
        store.close()

    _print_result(result)
    return 0


def _cmd_version(args: argparse.Namespace) -> int:
    """打印分发版本号（未安装为可分发包时回退到 unknown）。"""
    try:
        print(version(_DIST_NAME))
    except PackageNotFoundError:
        print("unknown")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ragspine",
        description="RAGSpine —— 无框架后端 RAG 引擎的控制台入口（离线优先）。",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True  # 无子命令时报错并非零退出（不静默成功）

    p_quick = sub.add_parser(
        "quickstart",
        help="离线演示：反幻觉 + 来源溯源（零 API key，秒级）",
    )
    p_quick.set_defaults(func=_cmd_quickstart)

    p_ask = sub.add_parser("ask", help="单条提问（默认离线 mock，确定性）")
    p_ask.add_argument("question", help="用户问题，如：中国内地FY2024的REVENUE是多少")
    p_ask.add_argument(
        "--db", default=str(DEFAULT_FACT_DB),
        help="fact_metric sqlite 路径（默认 data/fact_metric.db）",
    )
    p_ask.add_argument(
        "--provider", choices=["mock", "anthropic"], default="mock",
        help="mock=离线确定性（默认）；anthropic=真实 Claude（需装 [llm] + key）",
    )
    p_ask.set_defaults(func=_cmd_ask)

    p_ver = sub.add_parser("version", help="打印分发版本号")
    p_ver.set_defaults(func=_cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数并分发到子命令处理器，返回进程退出码。"""
    args = _build_parser().parse_args(argv)
    func = args.func
    result: int = func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())

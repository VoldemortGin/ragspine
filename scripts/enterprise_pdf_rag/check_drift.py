#!/usr/bin/env python3
"""文档/工件漂移守卫:扫描带 `covers:` 元数据的文档,确认其引用的代码路径仍存在。

引用失效即 CI 红——把"文档骗了 AI"这个最大隐性成本装上真实性保险丝。
最小实现做路径存在性;可扩展到符号级反射解析(改名/删除即红)。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COVERS = re.compile(r"^\s*covers:\s*(.+)$", re.MULTILINE)
# 只扫 enterprise_pdf_rag 自己的文档树:ragspine 的 md 用 frontmatter 块列表写 covers,
# 由 scripts/check_doc_drift.py 按它自己的规则(verified-against)守卫,不归本脚本判。
DOC_ROOTS = ("docs/enterprise-pdf-rag", "src/enterprise_pdf_rag")

# 只扫本仓库自己的文档。少一个目录就会假红:依赖树里随便哪个包的 README 带一行
# `covers:`,都会被当成本项目的漂移报出来,而你对它无能为力。
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        "build",
        "dist",
    }
)


def main() -> int:
    problems: list[str] = []
    for md in (md for doc_root in DOC_ROOTS for md in (ROOT / doc_root).rglob("*.md")):
        if any(part in SKIP_DIRS for part in md.parts):
            continue
        for m in COVERS.finditer(md.read_text(encoding="utf-8")):
            for raw in m.group(1).split(","):
                p = raw.strip().strip("[]\"' ")
                if p and not (ROOT / p).exists():
                    problems.append(f"{md.relative_to(ROOT)} -> covers 失效路径: {p}")
    if problems:
        print("✗ 文档漂移:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("✓ 无文档漂移。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

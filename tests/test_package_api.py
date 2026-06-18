"""顶层包 API —— 惰性领域访问（PEP 562）。

钉住三条不变量：
1. `import ragspine` 后，9 个领域子包可经属性直达（`ragspine.agent` …）。
2. `dir(ragspine)` / Tab 补全能发现这 9 个域。
3. **`import ragspine` 不急切加载任何域** —— 保住「核心零重依赖、离线可 import」
   这条用户硬约束（域仅在首次访问时才 import，故顶层 import 永不因缺可选 extra 而崩）。
"""

import subprocess
import sys
from types import ModuleType

import pytest

import ragspine

# 9 个顶层领域（与包 docstring 的 Submodules 清单一致）。
DOMAINS = (
    "agent",
    "common",
    "eval",
    "extraction",
    "ingestion",
    "pipeline",
    "retrieval",
    "service",
    "storage",
)


def test_all_domains_accessible_via_attribute():
    for name in DOMAINS:
        mod = getattr(ragspine, name)
        assert isinstance(mod, ModuleType)
        assert mod.__name__ == f"ragspine.{name}"


def test_dir_lists_all_domains():
    assert set(DOMAINS) <= set(dir(ragspine))


def test_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError):
        getattr(ragspine, "no_such_domain")


def test_top_import_does_not_eagerly_load_domains():
    # 干净子进程里 import ragspine：不得把任何域急切拉进 sys.modules（惰性 / 离线核心不变量）。
    probe = (
        "import sys, ragspine\n"
        f"eager = [d for d in {DOMAINS!r} if 'ragspine.' + d in sys.modules]\n"
        "assert not eager, eager\n"
        "print('LAZY-OK')\n"
    )
    res = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "LAZY-OK" in res.stdout


# 代表性深链（跨 1~3 层嵌套）：`import ragspine` 后能一路属性点到真正的类/函数。
DEEP_CHAINS = (
    ("ragspine.storage.fact_store", "FactStore"),
    ("ragspine.agent.agent", "answer_question"),
    ("ragspine.retrieval.chunking.chunk_store", "ChunkStore"),
    ("ragspine.ingestion.review.review_queue", "ReviewQueue"),
)


def test_deep_attribute_chains_resolve():
    import importlib

    for modpath, attr in DEEP_CHAINS:
        obj = ragspine
        for part in modpath.split(".")[1:]:  # 从 ragspine 顶层逐段属性下钻
            obj = getattr(obj, part)
        # obj 现在是链式访问到的叶子模块：必须与直接 import 的同一模块对象一致。
        assert obj is importlib.import_module(modpath)
        assert getattr(obj, attr).__name__ == attr


def test_leaf_modules_load_lazily_per_level():
    # 访问域包不得急切加载其叶子模块；碰到叶子那一刻才 import（逐层惰性）。
    probe = (
        "import sys, ragspine\n"
        "ragspine.storage\n"
        "assert 'ragspine.storage.fact_store' not in sys.modules\n"
        "_ = ragspine.storage.fact_store.FactStore\n"
        "assert 'ragspine.storage.fact_store' in sys.modules\n"
        "print('LAZY-DEPTH-OK')\n"
    )
    res = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "LAZY-DEPTH-OK" in res.stdout

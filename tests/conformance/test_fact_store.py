"""FactStore 缝 conformance（B2，🛡）：把【反编造 + provenance】不变量绑死在缝上。

结构化通路是反编造不变量的存储侧根基。落地 docs/prd-breadth-via-adapters.md「Structured store |
FactStore」行 + conformance 章节的 provenance / anti-fabrication 范式：对【每个注册 FactStore】
参数化断言——

  - found-determinism：upsert 一条 fact 后 query 命中返回【确定值】（跨调用逐位一致、等于入库值）；
    query 未命中返回【空】（绝不臆造一个值）——这是反编造的存储侧根基（agent 侧据此在无 found fact
    时把答案改写为「未找到」）。
  - provenance 不丢：found 结果带 source_doc_id + source_locator；lineage 经 upsert/query 存活。

两层互补（同 VectorStore / GraphStore / TraceSink 缝的范式）：
  - 机制层：corespine.ConformanceSuite 把【每个注册实现】×两项不变量绑成笛卡尔积，
    parametrize_kwargs() 两行消费（范围收敛在零依赖 sqlite 默认，见 conftest 注释）。
  - 领域层：fixture 形态参数化在 fact_store 夹具上，用【单一判定核】断言反编造 / provenance——任何
    实现只要登记进 conftest.FACT_STORE_IMPLS 就必须证明这两项不变量，不通过的 adapter 直接 CI 红。

诚实反证（同 GraphStore / TraceSink 的手法）：两个【故意】破不变量的 stub 喂进【同一判定核】必须
AssertionError——证明反编造 / provenance 断言不是空泛通过（有牙齿）：
  - _FabricatingFactStore：未命中却臆造一个值 -> 触 found-determinism 判定核 FAIL。
  - _LineageDroppingFactStore：命中结果照返，却把 source_doc_id / source_locator 抹空 -> 触 provenance 判定核 FAIL。
"""

import os
from dataclasses import replace

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.storage.fact_store import (
    FACT_STORE_ENTRY_POINT_GROUP,
    Fact,
    FactStore,
    SqliteFactStore,
    make_fact_store,
)
from tests.conformance.conftest import FACT_STORE_SUITE


def _fact(**over) -> Fact:
    """构造一条带齐血缘的 Fact（身份维 REVENUE×ACME_HK×FY2024×TOTAL，可逐项覆盖）。"""
    fields = dict(
        metric_code="REVENUE",
        entity="ACME_HK",
        geography="HK",
        channel="TOTAL",
        period_type="FY",
        period="2024",
        value=4500.0,
        unit="HKD_MN",
        source_doc_id="HK_FIN.pptx",
        source_locator="HK_FIN.pptx!slide3#para2",
    )
    fields.update(over)
    return Fact(**fields)


# ===========================================================================
# 领域层判定核：反编造（found-determinism）/ provenance（参数化用例与反证 stub 共用同一核）
# ===========================================================================

def _assert_found_determinism(store) -> None:
    """判定核：命中→确定值（跨调用逐位一致 + 等于入库值）；未命中→空（绝不臆造）。

    这是反编造的存储侧根基：agent 侧「无 found fact 就把答案改写为『未找到』」的前提，就是这条
    「命中即确定值、未命中即空」的读路径——dim_key 唯一保证命中 0-或-1 行。
    """
    store.upsert_facts([_fact(value=4500.0)])
    first = store.query("REVENUE", "ACME_HK", "FY", "2024")
    second = store.query("REVENUE", "ACME_HK", "FY", "2024")
    assert first and second, "命中查询应确定返回一条 Fact（found 语义）"
    assert (
        first[0].value == second[0].value == 4500.0
    ), "命中值必须确定且等于入库值（反编造根：不臆造 / 不漂移）"
    miss = store.query("REVENUE", "ACME_HK", "FY", "2099")
    assert miss == [], "未命中必须返回空（绝不臆造一个值）——反编造的存储侧根基"


def _assert_provenance_survives(store) -> None:
    """判定核：found 结果带非空 source_doc_id + source_locator，lineage 经 upsert/query 存活。"""
    store.upsert_facts(
        [_fact(source_doc_id="HK_FIN.pptx", source_locator="HK_FIN.pptx!slide3#para2")]
    )
    hits = store.query("REVENUE", "ACME_HK", "FY", "2024")
    assert hits, "provenance 判定核未命中（应至少回传入库的一条 Fact）"
    for f in hits:
        assert f.source_doc_id == "HK_FIN.pptx", f"found 结果丢了 source_doc_id（血缘根）：{f!r}"
        assert (
            f.source_locator == "HK_FIN.pptx!slide3#para2"
        ), f"found 结果丢了 source_locator（citation 回指）：{f!r}"


# ===========================================================================
# 机制层：corespine 套件消费者（实现 × 反编造/provenance 笛卡尔积；范式同 test_graph_store）
# ===========================================================================

@pytest.mark.parametrize(**FACT_STORE_SUITE.parametrize_kwargs())
def test_fact_store_conformance(case):
    """每个 (实现 × 不变量) 格子：调用 thunk，满足静默、违反原样抛。"""
    case()


# ===========================================================================
# 领域层：在每个注册实现（sqlite …）上各跑一遍
# ===========================================================================

def test_found_determinism_holds(fact_store):
    """每个注册 FactStore：命中→确定值、未命中→空（反编造存储侧根基）。"""
    _assert_found_determinism(fact_store)


def test_provenance_survives(fact_store):
    """每个注册 FactStore：found 结果带齐 source_doc_id + source_locator，lineage 存活。"""
    _assert_provenance_survives(fact_store)


def test_registered_fact_store_is_runtime_checkable(fact_store):
    """每个注册 FactStore 都结构匹配 @runtime_checkable FactStore Protocol。"""
    assert isinstance(fact_store, FactStore)


# ===========================================================================
# 诚实反证：故意破不变量的 stub 喂进同一判定核必须 FAIL（证明断言非空泛、有牙齿）
# ===========================================================================

class _FabricatingFactStore(SqliteFactStore):
    """反证 stub：查询【未命中】时不返回空，反而臆造一个值——必须触 found-determinism 判定核 FAIL。"""

    def query(self, *args, **kwargs):  # type: ignore[override]
        hits = super().query(*args, **kwargs)
        if not hits:
            return [_fact(value=999999.0, source_doc_id="FABRICATED", source_locator="FABRICATED")]
        return hits


class _LineageDroppingFactStore(SqliteFactStore):
    """反证 stub：命中结果照返（反编造仍守），却把 source_doc_id / source_locator 抹空——必须触 provenance 判定核 FAIL。"""

    def query(self, *args, **kwargs):  # type: ignore[override]
        return [
            replace(f, source_doc_id="", source_locator="")
            for f in super().query(*args, **kwargs)
        ]


def test_fabricating_store_fails_found_determinism():
    """未命中却臆造值的 stub 喂进同一反编造判定核必须 AssertionError——证明反编造断言有牙齿。"""
    store = _FabricatingFactStore(":memory:")
    store.init_schema()
    with pytest.raises(AssertionError):
        _assert_found_determinism(store)


def test_lineage_dropping_store_fails_provenance():
    """丢血缘的 stub 喂进同一 provenance 判定核必须 AssertionError——证明 provenance 断言有牙齿。"""
    store = _LineageDroppingFactStore(":memory:")
    store.init_schema()
    with pytest.raises(AssertionError):
        _assert_provenance_survives(store)


# ===========================================================================
# 注册表：make_fact_store 把「用哪个结构化后端」从改代码降为一个 spec/env（范式同 make_graph_store）
# ===========================================================================

def test_make_fact_store_default_is_sqlite():
    """缺省 spec -> sqlite 默认实现（默认结构化通路字节不变），且满足 FactStore Protocol。"""
    store = make_fact_store(db_path=":memory:")
    assert isinstance(store, SqliteFactStore)
    assert isinstance(store, FactStore)


def test_make_fact_store_sqlite_aliases():
    """内置 sqlite 别名（大小写 / 留白 / sqlite3 / default）都解析到 SqliteFactStore 默认实现。"""
    for spec in ("sqlite", "sqlite3", "default", "  SQLITE  "):
        store = make_fact_store(spec, db_path=":memory:")
        assert isinstance(store, SqliteFactStore)


def test_make_fact_store_none_is_none():
    """显式 'none' -> None（不注入具体 store；供调用方自建）。"""
    assert make_fact_store("none") is None
    assert make_fact_store("  NONE  ") is None


def test_make_fact_store_env_selected(monkeypatch):
    """缺省 spec 时从 RAGSPINE_FACT_STORE 环境变量读取（范式同 RAGSPINE_VECTOR_STORE）。"""
    monkeypatch.setenv("RAGSPINE_FACT_STORE", "sqlite")
    assert isinstance(make_fact_store(db_path=":memory:"), SqliteFactStore)


def test_make_fact_store_unknown_raises():
    """既非内置也非已注册 entry point 的名字 -> ValueError（列出可选名字，不让人猜）。"""
    with pytest.raises(ValueError, match="fact store"):
        make_fact_store("no_such_backend", db_path=":memory:")


def test_make_fact_store_kwargs_passthrough(tmp_path):
    """**kwargs（db_path）原样透传给 SqliteFactStore 构造函数（落一个真实 db 文件即证透传）。"""
    db = tmp_path / "facts.db"
    store = make_fact_store("sqlite", db_path=str(db))
    assert isinstance(store, SqliteFactStore)
    store.init_schema()
    assert db.exists()
    store.close()


# ---------------------------------------------------------------------------
# entry-point 自动发现：第三方装包即可按名字注册一个 FactStore 后端（DuckDB / Postgres 等），无需核心 PR。
# ---------------------------------------------------------------------------
class _DummyFactStore:
    """测试用最小 FactStore（结构匹配 @runtime_checkable FactStore Protocol；记 kwargs 以验证透传）。"""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def init_schema(self) -> None:
        pass

    def upsert_facts(self, facts, ingested_at=None) -> int:
        return len(list(facts))

    def query(self, metric_code, entity, period_type, period, channel="TOTAL", review_statuses=None) -> list:
        return []

    def count(self) -> int:
        return 0

    def execute_read(self, sql, params=()) -> list:
        return []

    def delete_by_source_doc(self, source_doc_id) -> int:
        return 0

    def set_review_status(self, dim_key, status) -> int:
        return 0

    @staticmethod
    def dim_key_for(fact) -> str:
        return ""

    def get_by_dim_key(self, dim_key):
        return None

    def close(self) -> None:
        pass


class _FakeEntryPoint:
    """importlib.metadata.EntryPoint 的最小替身（只用到 .name + .load()）。"""

    def __init__(self, name: str, target: object) -> None:
        self.name = name
        self._target = target

    def load(self) -> object:
        return self._target


def _patch_entry_points(monkeypatch, eps: list[_FakeEntryPoint]) -> None:
    """把 importlib.metadata.entry_points 替换成只对 ragspine.fact_stores group 返回 eps。"""
    import importlib.metadata

    def _fake(*, group=None):
        return list(eps) if group == FACT_STORE_ENTRY_POINT_GROUP else []

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake)


def test_entry_point_fact_store_selectable_by_name(monkeypatch):
    """注册一个名为 duckdb 的 entry point 后，make_fact_store('duckdb') 返回其实例（第三方装包即扩展）。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("duckdb", _DummyFactStore)])
    store = make_fact_store("duckdb")
    assert isinstance(store, _DummyFactStore)
    assert isinstance(store, FactStore)  # 满足 runtime_checkable 结构协议


def test_entry_point_kwargs_passthrough(monkeypatch):
    """选用 entry-point 后端时 **kwargs 原样透传给其构造函数。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("duckdb", _DummyFactStore)])
    store = make_fact_store("duckdb", db_path="/tmp/x.duckdb", threads=4)
    assert store.kwargs == {"db_path": "/tmp/x.duckdb", "threads": 4}


def test_builtin_name_wins_over_entry_point(monkeypatch):
    """内置名字优先于同名 entry point（第三方不能劫持内置 sqlite 默认语义）。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("sqlite", _DummyFactStore)])
    store = make_fact_store("sqlite", db_path=":memory:")
    assert isinstance(store, SqliteFactStore)
    assert not isinstance(store, _DummyFactStore)


def test_unknown_name_lists_discovered(monkeypatch):
    """未知名字的 ValueError 同时列出内置 + 已发现的 entry-point 名字（不臆造、不让人猜）。"""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("duckdb", _DummyFactStore)])
    with pytest.raises(ValueError) as excinfo:
        make_fact_store("nope_no_such_backend", db_path=":memory:")
    msg = str(excinfo.value)
    assert "sqlite" in msg  # 内置名字被列出
    assert "duckdb" in msg  # 已发现的 entry-point 名字也被列出

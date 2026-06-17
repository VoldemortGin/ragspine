"""DimensionSpec 规格（ADR 0004 DP-1）：冻结 + 集合字段不跨实例串味。

镜像 company_profile 既有的"返回副本"契约——每个 DimensionSpec 实例持有自己的
dict，绝不共享模块级默认 dict（否则一个 profile 改词表会污染另一个）。
"""

import os
from dataclasses import FrozenInstanceError

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common.company_profile import (
    CompanyProfile,
    DimensionSpec,
    DomainProfile,
    _DEFAULT_METRIC_SYNONYMS,
    load_company_profile,
)


def test_dimension_spec_constructs_with_safe_defaults():
    d = DimensionSpec("metric", "指标")
    assert (d.name, d.label, d.kind, d.clarify) == ("metric", "指标", "categorical", "assume")
    assert d.synonyms == {} and d.units == {} and d.labels == {} and d.derivation == {}
    assert d.identity is True and d.expand is True and d.required is False
    assert d.default is None and d.derived_from is None
    assert d.whitelist_in_fabrication_check is False


def test_dimension_spec_is_frozen():
    d = DimensionSpec("metric", "指标")
    with pytest.raises(FrozenInstanceError):
        d.name = "x"


def test_dimension_spec_collections_not_shared_across_instances():
    a = DimensionSpec("a", "A")
    b = DimensionSpec("b", "B")
    assert a.synonyms is not b.synonyms
    assert a.units is not b.units
    assert a.labels is not b.labels
    assert a.derivation is not b.derivation


# --- DP-2: DomainProfile 重命名 + dimensions 字段 --------------------------------


def test_company_profile_alias_is_same_object():
    """历史别名指向同一个类对象（非子类）：构造与 isinstance 契约不破。"""
    assert CompanyProfile is DomainProfile


def test_default_profile_carries_five_named_dimensions():
    """缺失回退 / 文件加载都继承默认金融 5 维（dimensions 按声明顺序）。"""
    prof = load_company_profile()
    assert isinstance(prof, DomainProfile)
    dims = prof.dimensions
    assert len(dims) == 5
    assert [d.name for d in dims] == [
        "metric", "entity", "period", "channel", "geography"
    ]
    by_name = {d.name: d for d in dims}
    assert by_name["period"].kind == "temporal"
    assert by_name["period"].whitelist_in_fabrication_check is True
    assert by_name["metric"].clarify == "ask_first" and by_name["metric"].required is True
    assert by_name["channel"].default == "TOTAL" and by_name["channel"].required is False
    assert by_name["geography"].identity is False
    assert by_name["geography"].derived_from == "entity"


def test_metric_dimension_vocab_byte_equal_to_glossary():
    """字节级证明 lift 等价：metric 维 synonyms/units == glossary 现行常量。"""
    from ragspine.common.glossary import METRIC_SYNONYMS, METRIC_UNITS

    metric = next(d for d in load_company_profile().dimensions if d.name == "metric")
    assert metric.synonyms == METRIC_SYNONYMS
    assert metric.units == METRIC_UNITS


def test_metric_dimension_synonyms_are_copies_not_frozen_constant():
    """每个 profile 持有自己的词表副本，绝不共享模块级冻结常量（防被改穿）。"""
    metric = next(d for d in load_company_profile().dimensions if d.name == "metric")
    assert metric.synonyms == _DEFAULT_METRIC_SYNONYMS
    assert metric.synonyms is not _DEFAULT_METRIC_SYNONYMS

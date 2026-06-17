"""DimensionSpec 规格（ADR 0004 DP-1）：冻结 + 集合字段不跨实例串味。

镜像 company_profile 既有的"返回副本"契约——每个 DimensionSpec 实例持有自己的
dict，绝不共享模块级默认 dict（否则一个 profile 改词表会污染另一个）。
"""

import os
from dataclasses import FrozenInstanceError

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common.company_profile import DimensionSpec


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

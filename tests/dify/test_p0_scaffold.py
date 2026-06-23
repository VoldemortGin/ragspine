"""P0 脚手架验收：import-clean + 域异常齐备且挂在家族 CorespineError 下。"""

from __future__ import annotations

import importlib

import pytest


def test_import_dify_is_clean() -> None:
    """`import ragspine.dify` 不报错、不急切拉起任何重依赖（PyYAML 仍延迟）。"""
    mod = importlib.import_module("ragspine.dify")
    assert mod is not None


def test_subpackages_importable() -> None:
    """四段子包均可独立 import（惰性子模块访问可点到底）。"""
    import ragspine.dify as dify

    assert dify.parse is not None
    assert dify.ir is not None
    assert dify.codegen is not None
    assert dify.optimize is not None


def test_errors_subclass_corespine() -> None:
    """域异常继承家族 CorespineError，带稳定可 grep 的 code。"""
    from corespine import CorespineError

    from ragspine.dify import (
        CyclicGraph,
        DifyCompileError,
        UnsupportedAppMode,
        UnsupportedNodeType,
    )

    assert issubclass(DifyCompileError, CorespineError)
    for exc_cls in (UnsupportedAppMode, UnsupportedNodeType, CyclicGraph):
        assert issubclass(exc_cls, DifyCompileError)

    # code 稳定可判别，且能归一为可序列化 dict。
    err = UnsupportedNodeType("knowledge-retrieval 暂未支持")
    assert err.code == "dify.unsupported_node"
    assert err.to_dict()["code"] == "dify.unsupported_node"


def test_unknown_attr_raises_attributeerror() -> None:
    """拼错的子模块名 → AttributeError（PEP 562 约定），不静默吞。"""
    import ragspine.dify as dify

    with pytest.raises(AttributeError):
        _ = dify.does_not_exist

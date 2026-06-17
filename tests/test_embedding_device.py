"""设备自适应 + 通用 SentenceTransformer 后端测试（TDD 红色阶段，GAP-A 续）。

User story（中文）：
    作为接手 PoC production 化的 GIO 算法工程师，我同时在 **M1 Pro 笔记本（MPS）**
    和 **GPU 盒子（CUDA）** 上跑同一份代码。我希望真实 embedding 后端能**按运行机器
    自动选 cuda/mps/cpu**，无需为每台机器改代码；并希望有一个**通用
    SentenceTransformer 后端**（默认 Qwen3-Embedding-0.6B）可经工厂一个 flag/env 接上。
    在没装 sentence-transformers 的机器上，我仍要能 import 模块、构造后端对象（用于
    单测设备逻辑），模型只在首次 embed 时延迟加载。默认（纯 BM25 / 离线）行为零变更。

诚实边界（钉死在测试意图里）：
    真实 Qwen3 的加载与编码属【集成行为】，需在 GPU 盒子上联网下载模型后跑，**不在
    本批单测内**。这些测试只验证「设备选择逻辑（用注入的 cuda/mps 可用性，不依赖本机
    真实硬件）/ 构造不加载模型 / 工厂接线 / 协议一致 / 空输入不触发加载 / 既有工厂回归」。

覆盖：
    DV1 设备矩阵：注入 (cuda,mps) -> cuda/mps/cpu。
    DV2 override 优先 + 非法 override 抛 ValueError。
    DV3 env 优先于自动探测；显式 override 参数又优先于 env。
    DV4 ST 后端构造不加载模型（device 属性正确，未 import sentence_transformers）。
    DV5 工厂：'qwen3'/'sentence-transformers'/'st' -> ST 后端；RAGSPINE_EMBEDDING_MODEL 覆盖；
        未知 spec 仍 ValueError；'none'->None 不变。
    DV6 协议一致：满足 EmbeddingBackend 协议（有 embed_texts）；空输入返回 []（不加载模型）。
    DV7 回归：none/deterministic/openai 既有工厂行为不变。

红色预期：_select_torch_device / SentenceTransformerEmbeddingBackend 尚未实现，且工厂
    未识别 'qwen3'/'sentence-transformers'/'st'，import 或断言因此 FAIL。DV7 中既有工厂
    行为保护项可能先绿（已注明，作回归护栏）。
"""

import os
import sys

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.vector.embedding_backends import (
    DeterministicEmbeddingBackend,
    OpenAIEmbeddingBackend,
    SentenceTransformerEmbeddingBackend,
    _select_torch_device,
    make_embedding_backend,
)

# 默认 Qwen3 模型名（契约：缺省 Qwen3-Embedding-0.6B）。
QWEN3_MODEL = "Qwen/Qwen3-Embedding-0.6B"


# ===========================================================================
# DV1 设备矩阵（用注入的 cuda/mps 可用性，不依赖本机真实硬件）
# ===========================================================================

def test_select_device_cuda_when_cuda_available():
    """注入 cuda=True、mps=False -> 'cuda'（cuda 优先级最高）。"""
    assert _select_torch_device(cuda_available=True, mps_available=False) == "cuda"


def test_select_device_mps_when_only_mps():
    """注入 cuda=False、mps=True -> 'mps'（M1 Pro 笔记本场景）。"""
    assert _select_torch_device(cuda_available=False, mps_available=True) == "mps"


def test_select_device_cpu_when_neither():
    """注入 cuda=False、mps=False -> 'cpu'（无加速器兜底）。"""
    assert _select_torch_device(cuda_available=False, mps_available=False) == "cpu"


def test_select_device_cuda_wins_over_mps():
    """cuda 与 mps 同时可用 -> 'cuda'（探测顺序 cuda→mps→cpu）。"""
    assert _select_torch_device(cuda_available=True, mps_available=True) == "cuda"


# ===========================================================================
# DV2 override 参数优先 + 非法 override 校验
# ===========================================================================

def test_explicit_override_wins_over_autodetect():
    """显式 override 参数压过自动探测：override='mps' 即便 cuda 可用也返回 'mps'。"""
    assert _select_torch_device("mps", cuda_available=True, mps_available=True) == "mps"


def test_explicit_override_cpu_wins():
    """override='cpu' 强制 CPU，无视注入的 cuda 可用。"""
    assert _select_torch_device("cpu", cuda_available=True, mps_available=True) == "cpu"


def test_illegal_override_raises_value_error():
    """非法 override（如 'tpu'）抛 ValueError（只接受 cuda/mps/cpu）。"""
    with pytest.raises(ValueError):
        _select_torch_device("tpu", cuda_available=True, mps_available=False)


# ===========================================================================
# DV3 env 优先于自动探测；显式 override 参数又优先于 env
# ===========================================================================

def test_env_overrides_autodetect(monkeypatch):
    """设 RAGSPINE_EMBEDDING_DEVICE=cpu 时，即便注入 cuda=True 也返回 'cpu'。"""
    monkeypatch.setenv("RAGSPINE_EMBEDDING_DEVICE", "cpu")
    assert _select_torch_device(cuda_available=True, mps_available=False) == "cpu"


def test_explicit_override_wins_over_env(monkeypatch):
    """显式 override 参数优先于环境变量：env=cpu 但 override=mps -> 'mps'。"""
    monkeypatch.setenv("RAGSPINE_EMBEDDING_DEVICE", "cpu")
    assert _select_torch_device("mps", cuda_available=False, mps_available=True) == "mps"


def test_illegal_env_raises_value_error(monkeypatch):
    """非法环境变量值（如 'gpu'）抛 ValueError（取值校验同 override）。"""
    monkeypatch.setenv("RAGSPINE_EMBEDDING_DEVICE", "gpu")
    with pytest.raises(ValueError):
        _select_torch_device(cuda_available=True, mps_available=False)


# ===========================================================================
# DV4 ST 后端构造不加载模型（不依赖 sentence-transformers 是否安装）
# ===========================================================================

def test_st_backend_construct_does_not_load_model():
    """SentenceTransformerEmbeddingBackend(device='cpu') 可构造、device=='cpu'，
    且构造**未** import sentence_transformers（构造轻、可在没装 ST 的机器上做单测）。
    """
    sys.modules.pop("sentence_transformers", None)
    backend = SentenceTransformerEmbeddingBackend(device="cpu")
    assert backend.device == "cpu"
    # 构造阶段绝不触发 sentence_transformers 的 import / 模型加载。
    assert "sentence_transformers" not in sys.modules


def test_st_backend_default_model_name_is_qwen3():
    """缺省 model_name 为 Qwen3-Embedding-0.6B（契约默认值）。"""
    backend = SentenceTransformerEmbeddingBackend(device="cpu")
    assert backend.model_name == QWEN3_MODEL


def test_st_backend_device_resolved_via_selector(monkeypatch):
    """device=None 时经 _select_torch_device 解析：env=cpu 即得 'cpu'（不碰真实硬件结论）。"""
    monkeypatch.setenv("RAGSPINE_EMBEDDING_DEVICE", "cpu")
    backend = SentenceTransformerEmbeddingBackend()
    assert backend.device == "cpu"


# ===========================================================================
# DV5 工厂接线：qwen3 / sentence-transformers / st
# ===========================================================================

def test_factory_qwen3_returns_st_backend():
    """spec='qwen3' -> SentenceTransformerEmbeddingBackend 实例（构造不加载模型）。"""
    sys.modules.pop("sentence_transformers", None)
    backend = make_embedding_backend("qwen3", device="cpu")
    assert isinstance(backend, SentenceTransformerEmbeddingBackend)
    assert "sentence_transformers" not in sys.modules


def test_factory_sentence_transformers_spec_returns_st_backend():
    """spec='sentence-transformers' -> ST 后端实例。"""
    backend = make_embedding_backend("sentence-transformers", device="cpu")
    assert isinstance(backend, SentenceTransformerEmbeddingBackend)


def test_factory_st_alias_returns_st_backend():
    """spec='st'（别名）-> ST 后端实例。"""
    backend = make_embedding_backend("st", device="cpu")
    assert isinstance(backend, SentenceTransformerEmbeddingBackend)


def test_factory_qwen3_default_model_name():
    """工厂构造的 ST 后端缺省 model_name 为 Qwen3-Embedding-0.6B。"""
    backend = make_embedding_backend("qwen3", device="cpu")
    assert backend.model_name == QWEN3_MODEL


def test_factory_model_name_kwarg_override():
    """model_name 可经 kwargs 覆盖默认 Qwen3。"""
    backend = make_embedding_backend(
        "qwen3", device="cpu", model_name="BAAI/bge-small-zh-v1.5"
    )
    assert backend.model_name == "BAAI/bge-small-zh-v1.5"


def test_factory_model_name_env_override(monkeypatch):
    """RAGSPINE_EMBEDDING_MODEL 覆盖默认 model_name。"""
    monkeypatch.setenv("RAGSPINE_EMBEDDING_MODEL", "BAAI/bge-m3")
    backend = make_embedding_backend("qwen3", device="cpu")
    assert backend.model_name == "BAAI/bge-m3"


def test_factory_device_env_passthrough(monkeypatch):
    """RAGSPINE_EMBEDDING_DEVICE 透传到 ST 后端 device。"""
    monkeypatch.setenv("RAGSPINE_EMBEDDING_DEVICE", "cpu")
    backend = make_embedding_backend("qwen3")
    assert backend.device == "cpu"


def test_factory_unknown_spec_still_value_error():
    """未知 spec 仍抛 ValueError（新增 spec 不放宽校验）。"""
    with pytest.raises(ValueError):
        make_embedding_backend("qwen9000")


def test_factory_none_unchanged():
    """spec='none' / None -> None（纯 BM25 默认不变）。"""
    assert make_embedding_backend("none") is None
    assert make_embedding_backend(None) is None


# ===========================================================================
# DV6 协议一致 + 空输入不触发模型加载
# ===========================================================================

def test_st_backend_satisfies_embedding_protocol():
    """ST 后端满足 EmbeddingBackend 协议（有可调用 embed_texts）。"""
    from ragspine.retrieval.lexical.retrieval import EmbeddingBackend

    backend = SentenceTransformerEmbeddingBackend(device="cpu")
    assert isinstance(backend, EmbeddingBackend)
    assert callable(backend.embed_texts)


def test_st_backend_empty_input_returns_empty_without_loading():
    """空输入 embed_texts([]) 返回 []，且不触发 sentence_transformers import / 模型加载。"""
    sys.modules.pop("sentence_transformers", None)
    backend = SentenceTransformerEmbeddingBackend(device="cpu")
    assert backend.embed_texts([]) == []
    assert "sentence_transformers" not in sys.modules


def test_st_backend_normalize_attribute_default_true():
    """normalize 默认 True（契约：encode(normalize_embeddings=normalize)）。"""
    backend = SentenceTransformerEmbeddingBackend(device="cpu")
    assert backend.normalize is True


# ===========================================================================
# DV7 回归：none / deterministic / openai 既有工厂行为不变
# ===========================================================================

def test_regression_factory_deterministic_unchanged():
    """spec='deterministic' 仍返回 DeterministicEmbeddingBackend（既有行为护栏）。"""
    assert isinstance(make_embedding_backend("deterministic"), DeterministicEmbeddingBackend)


def test_regression_factory_openai_unchanged(monkeypatch):
    """spec='openai' + 注入 fake SDK 仍返回 OpenAIEmbeddingBackend（既有行为护栏）。"""
    import types
    from types import SimpleNamespace

    class _Embeddings:
        def create(self, **kwargs):
            return SimpleNamespace(data=[])

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            self.embeddings = _Embeddings()

    fake = types.ModuleType("openai")
    fake.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)
    backend = make_embedding_backend("openai", api_key="k")
    assert isinstance(backend, OpenAIEmbeddingBackend)


def test_regression_factory_default_none_unchanged(monkeypatch):
    """无 env、无 spec 时工厂默认返回 None（纯 BM25 默认不变）。"""
    monkeypatch.delenv("RAGSPINE_EMBEDDING_BACKEND", raising=False)
    assert make_embedding_backend() is None

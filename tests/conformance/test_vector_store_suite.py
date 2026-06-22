"""VectorStore conformance 套件（corespine harness 骨架驱动·机制层）。

用 corespine.ConformanceSuite 把【每个注册实现】× 脊柱不变量绑成笛卡尔积，再用
parametrize_kwargs() 把消费者 glue 收敛成两行：每格新建一个全新实例、跑一条不变量，
满足则静默、违反则原样抛（pytest 直接定位到 "impl-invariant" 那一格）。

定位：这是【机制层】的 conformance——证明「实现 × 不变量」的组织骨架真在用，且任何
新登记到 conftest.VECTOR_STORE_REGISTRY 的实现都自动继承这组脊柱不变量。更细粒度的
领域合约（cosine 排序 / k 上限 / 维度校验 / 再入幂等 / tie-break / 能力分支等）仍在
test_vector_store_contract.py 与 test_vector_store_invariants.py，按 fixture 形态参数化；
两层互补，领域断言不外迁。

红色预期：不变量工厂延迟 import store；VectorStore 落地前每格在调用 thunk 时报 ERROR
（逐格隔离），不中断整轮收集。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from tests.conformance.conftest import VECTOR_STORE_SUITE


@pytest.mark.parametrize(**VECTOR_STORE_SUITE.parametrize_kwargs())
def test_vector_store_conformance(case):
    """每个 (实现 × 不变量) 格子：调用 thunk，满足静默、违反原样抛。"""
    case()

"""review —— SME 人工复核队列状态机：承接校验分歧项，记录复核流转。

sqlite 持久化，与 fact_store 同库不同表。

Submodules:
    review_queue.py — 人工复核队列（sqlite，与 fact_store 同库不同表）。
    apply.py — 人审写回闭环：把已决议项应用回 FactStore（决议生效、按审计 seq 幂等）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)

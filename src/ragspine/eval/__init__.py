"""eval —— Q&A 与抽取评测 harness，含基线门禁。

golden 集存于 data/golden/（强制纳入版本控制）；回归必须使基线门禁失败，
而非悄悄下调基线。

Submodules:
    extraction_eval.py — 抽取评测：分通道指标 + 回归门禁。
    groundedness.py — 叙事侧 groundedness 度量：faithfulness + free-text answer-accuracy。
    qa_eval.py — Q&A 评测闭环 harness：四命门 + groundedness 指标 + 基线门禁。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)

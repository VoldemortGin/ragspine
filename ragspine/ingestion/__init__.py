"""ingestion —— IR/文本 → 各类存储：结构化事实入库、叙事块入库、人工复核队列。

结构化入库幂等：重跑同一批次不得重复写入，由 manifest 台账守护。

Submodules:
    narrative/ — 叙事文档抽取 + 批量切块入库。
    review/ — SME 人工复核队列状态机。
    structured/ — 结构化事实入库 + 幂等批量 manifest 台账。
"""

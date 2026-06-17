"""review —— SME 人工复核队列状态机：承接校验分歧项，记录复核流转。

sqlite 持久化，与 fact_store 同库不同表。

Submodules:
    review_queue.py — 人工复核队列（sqlite，与 fact_store 同库不同表）。
"""

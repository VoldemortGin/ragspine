"""faq —— SME 审核型 FAQ 短路缓存：命中即跳过完整链路直接回答。

位于反幻觉 guard 之前，故保守排除：结构化数值/竞品/实时/过期/停用/RESTRICTED
一律不短路。

Submodules:
    faq_cache.py — SME 审核型 FAQ 短路缓存。
"""

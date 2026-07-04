"""common —— 跨域基础原语：company profile、敏感度模型、glossary、可观测性、全局常量。

身份/指标/竞品均由 CompanyProfile 配置驱动，绝不硬编码公司；trace 仅记录
代码/计数/时序，从不记录答案、事实值或 chunk 文本。

Submodules:
    company_profile.py — 所属公司（home company）身份的配置化载入。
    core.py — 跨切面全局常量（数据根 + 各 sqlite 库默认路径）的单一出处。
    glossary.py — 维度同义词词典与归一化。
    observability/ — 隐私安全可观测性：trace 发射原语 + 可插拔 TraceSink 缝（含 OTel 适配器）。
    sensitivity.py — 叙事入库的确定性敏感度分级策略与纯函数分级器。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)

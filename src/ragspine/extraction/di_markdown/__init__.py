"""di_markdown —— Azure Document Intelligence 风格 markdown → 类型化中间表示（页 → 块）。

纯模块、只用 stdlib、不涉及任何公司。块类型：Heading（带标题路径）/ Paragraph / Table
（HTML 表展开成矩形网格并保留合并跨度）/ Figure。解析契约见 parse.py 的模块 docstring。

Submodules:
    models.py — 中间表示：DiDocument / DiPage / 四类块 / TableGrid / TableCell（frozen dataclass）。
    parse.py — parse_di_markdown：分页、页码、页元数据注释、标题栈、段落 / 表 / 图切块。
    html_table.py — parse_html_table：stdlib HTMLParser 解析 <table>，th/td + rowspan/colspan 展开。
    page_tags.py — 页标签（页图按需附图的触发依据）：逐页原始度量（表 / 图文字量 / 文字量）+ 按阈值得 has_table / has_figure / low_text。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)

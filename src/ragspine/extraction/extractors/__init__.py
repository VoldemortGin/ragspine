"""extractors —— 各文件格式的抽取器：xlsx / pptx / pdf / docx，确定性优先，样式与颜色感知。

每个抽取器把原始文档转为统一的 StyledGrid IR；确定性路径零 OCR、零 LLM，
扫描型 PDF 走 OCR/VLM 两层策略。

Submodules:
    docspine_extractor.py — DOCX 表格抽取器（docspine 纯 Rust 封装，强表 gridSpan/vMerge/嵌套，W3b）。
    pdf_digital_extractor.py — 数字型 PDF 表格抽取器（Docling 封装契约，可选兜底）。
    pdf_spine_extractor.py — 数字型 PDF 表格抽取器（pdfspine 封装，默认实现）。
    pdf_scanned_extractor.py — 扫描型 PDF OCR/VLM 抽取契约（两层策略）。
    pptx_extractor.py — PPTX 确定性抽取：原生表格与图表读数，零 OCR、零 LLM。
    pptx_styled_extractor.py — PPT 增强抽取契约，与旧 pptx_extractor 并存。
    xlsx_extractor.py — XLSX 确定性抽取：5-yr summary 表按 schema 映射，零幻觉。
    xlsx_styled_extractor.py — XLSX 样式感知抽取器：每 sheet 产出一张 StyledGrid。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)

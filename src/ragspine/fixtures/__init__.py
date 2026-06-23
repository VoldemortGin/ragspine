"""合成 fixture / ground-truth 生成器包（确定性、硬编码）。

把端到端验证所需的测试数据与逐格真值平移进库：数值全部硬编码、产物可复现，
作为 tests/ 与 scripts/ 共用的唯一真值来源。各生成器互不急切 import（保住
import-clean：reportlab 等 dev-only extra 仅在真正生成时才拉起）。

Submodules:
    excel.py — Excel 线 fixture（样式 xlsx + 逐格 ground truth）。
    pdf.py — PDF 线 fixture（数字/扫描/OCR/混合/PPT 导出 + ground truth）。
    pptx.py — PPT 增强 + 扫描线 fixture（样式 deck + note/OCR fake 向量）。
    synthetic_deck.py — 端到端合成数据（pptx 表格/图表 + xlsx + ground_truth.json）。
"""

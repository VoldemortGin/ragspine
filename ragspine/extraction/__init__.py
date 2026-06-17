"""extraction —— 文档 → 冻结的 StyledGrid IR：样式与颜色感知抽取、PDF 分诊、双通道校验。

ir.py 的 styled_grid 是全项目最稳定的接口；各类抽取器以它为统一产物，
校验失败的结果进入人工复核队列。

Submodules:
    color/ — 颜色语义层：同色聚类、图例识别、版本化映射注册表。
    extractors/ — xlsx / pptx / pdf 抽取器（样式与颜色感知，确定性优先）。
    routing/ — 逐页 PDF 分诊路由。
    verification/ — 双通道交叉校验（纯逻辑，不依赖 Docling）。
    ir.py — 样式感知网格中间表示（styled_grid IR），全项目最稳定的接口。
"""

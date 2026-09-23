"""extraction.evidence —— 证据链(enterprise-pdf-rag 产品线)的 PDF → 源观测值,零模型证明与 typed IR(ADR 0022)。

这里是纯目录(只用标准库 + Protocol,不做 I/O);pdfspine 绑定,资格化与模型推理等 I/O 在 adapters/(随 B4 迁入)。
与 ragspine 的 extraction/ir.py(StyledGrid),extraction/tables/ 是两套并排实现,故意不合并。

Submodules:
    document/ — 源观测模型(span,区域,资产),源身份校验与逐页文本层诊断。
    figures/ — 图表管线:同一 SVG 双分支,快照绑定,ADR 0016 窗口规则与 typed ChartQA。
    metadata/ — 逐字页元数据,期间规范化,文档折叠与目录树(ADR 0013/0019)。
    objects/ — 对象 typed payload 与表格/图示/公式的零模型证明(ADR 0014/0015)。
    page/ — 选页处理记录,覆盖检查与区域几何。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)

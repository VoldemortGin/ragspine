"""Table Transformer（TATR）视觉结构识别适配器——需 `[tsr]` extra，延迟 import。

TATR 是微软的 DETR 架构文档表格模型，同一团队还产出了 PubTables-1M 数据集与 GriTS 指标
（本仓 `conformance/gt/grits.py` 用的就是它）。它分两阶段：表格检测 + 结构识别（TSR）。

**本适配器只用它的 TSR 那一半。** 实测（FinTabNet.c 150 页）pdfspine `strategy="text"` 的
检出已达召回 79.6% / 精确率 100%，检测阶段无需替换；瓶颈在网格重建（GriTS_Top 0.233）。
只跑 TSR 因此既省掉一个模型的推理开销，也少一处误差来源。

**内容仍走文本层。** 本适配器只把像素翻译成行列坐标，一个字都不"读"——文字由调用方按格子
坐标去 PDF 文本层取。这是与 2026 年 SOTA（DELTA 等结构/内容分离）一致的做法。

**坐标契约**：模型在栅格图上给出像素坐标，本适配器负责换算回 PDF point 并 clamp 进
`TableRegion.bbox`，故对调用方而言输出坐标系与确定性默认实现完全一致。

**确定性**：推理固定 `eval()` + 无采样（DETR 是集合预测，非自回归），同输入同输出；
输出前按 (row, col) 升序排列，与默认实现同口径。

**许可证提示（待用户核实后再上默认路径）**：TATR 代码与 HuggingFace checkpoint 的许可需
对照家族 ADR 0009 的 ≤Apache-2.0 门确认。本适配器是 opt-in、默认不启用，故不影响默认安装
与许可面；启用前请自行确认所选 checkpoint 的许可。
"""

from typing import Any

from ragspine.extraction.tables.structure import (
    Bbox,
    CellBox,
    TableRegion,
    TableStructure,
)

__all__ = ["DEFAULT_MODEL_ID", "DEFAULT_RENDER_DPI", "TatrStructureRecognizer"]

# HuggingFace 上的 TSR checkpoint（v1.1-all 在 PubTables-1M + FinTabNet 上训练）。
DEFAULT_MODEL_ID = "microsoft/table-transformer-structure-recognition-v1.1-all"

# 栅格化 DPI：TATR 在 150dpi 量级训练，过高只增开销不增精度。
DEFAULT_RENDER_DPI = 150.0

# TATR 的结构类别里，我们只取行与列（表体/表头行合并处理，跨格由行列交叉推出）。
_ROW_LABELS = frozenset({"table row", "table projected row header"})
_COL_LABELS = frozenset({"table column"})

# 置信度下限：低于此值的行列框丢弃（防止把噪声当成一行）。
DEFAULT_SCORE_THRESHOLD = 0.5


class TatrStructureRecognizer:
    """把 `TableRegion` 栅格化后交给 TATR TSR 模型，换算回 PDF point 的行列网格。"""

    name = "tatr"

    def __init__(
        self,
        *,
        model_id: str = DEFAULT_MODEL_ID,
        dpi: float = DEFAULT_RENDER_DPI,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        **_ignored: Any,
    ) -> None:
        self.model_id = model_id
        self.dpi = dpi
        self.score_threshold = score_threshold
        self._model: Any = None
        self._processor: Any = None

    def _load(self) -> tuple[Any, Any]:
        """首次使用时才加载模型（延迟 import + 延迟下载权重）。"""
        if self._model is not None:
            return self._processor, self._model
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModelForObjectDetection
        except ImportError as exc:  # pragma: no cover - 取决于安装的 extra
            raise ImportError(
                "未安装表格结构识别依赖：pip install 'rag-spine[tsr]' "
                "或 pip install torch transformers pillow"
            ) from exc
        self._processor = AutoImageProcessor.from_pretrained(self.model_id)
        self._model = AutoModelForObjectDetection.from_pretrained(self.model_id)
        self._model.eval()
        self._torch = torch
        return self._processor, self._model

    def recognize(self, region: TableRegion) -> TableStructure | None:
        """返回网格；无法栅格化或模型无输出时诚实返回 `None`（＝没有意见）。"""
        if region.render is None:
            # 没有栅格化钩子就拿不到像素——诚实降级为「没有意见」，绝不猜。
            return None
        processor, model = self._load()

        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise ImportError("未安装 pillow：pip install 'rag-spine[tsr]'") from exc

        import io

        png = region.render(self.dpi)
        if not png:
            return None
        image = Image.open(io.BytesIO(png)).convert("RGB")

        inputs = processor(images=image, return_tensors="pt")
        with self._torch.no_grad():
            outputs = model(**inputs)
        processed = processor.post_process_object_detection(
            outputs, threshold=self.score_threshold, target_sizes=[image.size[::-1]]
        )[0]

        id2label = model.config.id2label
        rows: list[tuple[float, float]] = []
        cols: list[tuple[float, float]] = []
        for score, label, box in zip(
            processed["scores"].tolist(),
            processed["labels"].tolist(),
            processed["boxes"].tolist(),
            strict=False,
        ):
            if score < self.score_threshold:
                continue
            name = id2label.get(label, "")
            if name in _ROW_LABELS:
                rows.append((box[1], box[3]))
            elif name in _COL_LABELS:
                cols.append((box[0], box[2]))

        if not rows or not cols:
            return None

        rows.sort()
        cols.sort()

        # 像素 -> PDF point：区域宽高之比即缩放因子，再平移回区域原点。
        rx0, ry0, rx1, ry1 = region.bbox
        width_px, height_px = image.size
        sx = (rx1 - rx0) / width_px if width_px else 0.0
        sy = (ry1 - ry0) / height_px if height_px else 0.0

        def to_pdf(x0: float, y0: float, x1: float, y1: float) -> Bbox:
            return (
                min(max(rx0 + x0 * sx, rx0), rx1),
                min(max(ry0 + y0 * sy, ry0), ry1),
                min(max(rx0 + x1 * sx, rx0), rx1),
                min(max(ry0 + y1 * sy, ry0), ry1),
            )

        cells: list[CellBox] = []
        for row_index, (top, bottom) in enumerate(rows):
            for col_index, (left, right) in enumerate(cols):
                cells.append(
                    CellBox(
                        row=row_index,
                        col=col_index,
                        row_span=1,
                        col_span=1,
                        bbox=to_pdf(left, top, right, bottom),
                    )
                )

        return TableStructure(n_rows=len(rows), n_cols=len(cols), cells=tuple(cells))

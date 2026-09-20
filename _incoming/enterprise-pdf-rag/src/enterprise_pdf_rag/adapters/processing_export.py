"""Human-readable projections of immutable processing artifacts, never model outputs."""

import json
import re
import tempfile
from collections import defaultdict
from hashlib import sha256
from html import escape
from pathlib import Path

from pydantic import TypeAdapter

from enterprise_pdf_rag.adapters.bar_publication import parse_displayed_bar_receipt
from enterprise_pdf_rag.adapters.chart_member_validation import (
    uses_displayed_bar_policy,
)
from enterprise_pdf_rag.adapters.chart_publication import parse_chart_receipt
from enterprise_pdf_rag.adapters.chart_qa_evaluation import read_evaluation
from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation import read_bar_evaluation
from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.http.processing_schemas import ProcessingEnvelope
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.processing.models import (
    ObjectKind,
    ObjectProcessingRecord,
    ProcessingManifest,
    StageOutcome,
)

_STYLE = "body{max-width:1100px;margin:32px auto;font:16px system-ui;padding:0 20px;line-height:1.5}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:8px;text-align:left}code{overflow-wrap:anywhere}.source>svg{width:100%;height:auto;max-height:700px}section{border:1px solid #ddd;padding:16px;margin:24px 0}pre{white-space:pre-wrap}"


def _html(title: str, body: str) -> str:
    return f'<!doctype html><html lang="zh"><meta charset="utf-8"><title>{escape(title)}</title><style>{_STYLE}</style><body><h1>{escape(title)}</h1>{body}</body></html>'


def _stage_file(stage: StageOutcome) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", stage.stage) is None:
        raise ValueError("Unsafe processing stage name")
    suffix = (
        ".svg"
        if stage.artifact is not None and stage.artifact.media_type == "image/svg+xml"
        else ".png"
        if stage.artifact is not None and stage.artifact.media_type == "image/png"
        else ".json"
    )
    return stage.stage + suffix


def _write_stage(
    outputs: ProcessingStore,
    folder: Path,
    stage: StageOutcome,
    filename: str | None = None,
) -> str:
    artifact = stage.artifact
    if artifact is None:
        return escape(f"{stage.state}: {stage.diagnostic}")
    name = _stage_file(stage) if filename is None else filename
    (folder / name).write_bytes(outputs.assets.get(artifact))
    return f'<a href="{escape(name, quote=True)}">{escape(stage.stage)}</a> — {escape(stage.state)}'


def _object(
    outputs: ProcessingStore, folder: Path, record: ObjectProcessingRecord
) -> tuple[str, str]:
    directory = "object-" + sha256(record.object_id.encode()).hexdigest()[:20]
    target = folder / "objects" / directory
    target.mkdir(parents=True, exist_ok=True)
    (target / "status.json").write_bytes(
        TypeAdapter(ObjectProcessingRecord).dump_json(record, indent=2)
    )
    stages = "".join(
        f"<li>{_write_stage(outputs, target, stage)}</li>" for stage in record.stages
    )
    svg = next(
        (stage.artifact for stage in record.stages if stage.stage == "svg"), None
    )
    preview = (
        ""
        if svg is None
        else re.sub(r"^\s*<\?xml[^>]*\?>", "", outputs.assets.get(svg).decode())
    )
    body = f'<p><a href="../../review.html">返回本页</a></p><p>类型: {escape(record.kind.value)}; 这是来源/模型产物审阅, 保存成功不代表语义已验证。</p><div class="source">{preview}</div><ul>{stages}</ul><p><a href="status.json">状态与实际产物引用</a></p>'
    (target / "review.html").write_text(
        _html(f"对象 {record.kind.value}", body), encoding="utf-8"
    )
    return (
        directory,
        f'<li><a href="objects/{directory}/review.html">{escape(record.kind.value)} 对象</a>: {escape(", ".join(stage.stage + "=" + stage.state for stage in record.stages))}</li>',
    )


def _summary(manifest: ProcessingManifest) -> str:
    object_count = sum(len(page.objects) for page in manifest.pages)
    deferred = sum(
        stage.state == "deferred"
        for page in manifest.pages
        for item in page.objects
        for stage in item.stages
    )
    qualified = sum(
        item.qualified_claim_count for page in manifest.pages for item in page.objects
    )
    return f"<p>源文件已保存 {manifest.scope.source_page_count} 页。本次选择物理页 {escape(', '.join(map(str, manifest.scope.physical_pages)))}, 有 {object_count} 个布局对象、{qualified} 个已获资格的数值 claim。</p><p>语义尚未完成的阶段会明确列出; deferred 阶段数: {deferred}。逐字原文资格只证明转录, 不能证明图表关系或财务结论。</p>"


def _retrieval_records(run: Path) -> str:
    links: list[str] = []
    if (run / "retrieval-validation.json").is_file():
        links.append(
            '<li><a href="retrieval-validation.json">首次检索原始记录(保留历史)</a> · <a href="retrieval-example.json">同快照回填</a></li>'
        )
    for folder in sorted((run / "retrieval-evaluations").glob("*")):
        if (
            re.fullmatch(r"[0-9a-f]{64}", folder.name) is None
            or not (folder / "evaluation.json").is_file()
        ):
            continue
        base = "retrieval-evaluations/" + folder.name
        links.append(
            f'<li><a href="{base}/retrieval-validation.json">独立检索记录:模型配置、完整候选与两类分数</a> · <a href="{base}/retrieval-example.json">同快照回填</a></li>'
        )
    for controls in sorted((run / "retrieval-controls").glob("*/controls.json")):
        identity = controls.parent.name
        if re.fullmatch(r"[0-9a-f]{64}", identity) is None:
            continue
        if sha256(controls.read_bytes()).hexdigest() != identity:
            raise ValueError("Rerank service control evidence digest mismatch")
        links.append(
            f'<li><a href="retrieval-controls/{identity}/controls.json">服务正反例、文档换序与 adapter 一致性 smoke 记录</a> — 通过服务 smoke 或单个图表查询,仍不等于整体排序 benchmark 已验收。</li>'
        )
    return (
        ""
        if not links
        else '<h2 id="retrieval">检索运行记录</h2><p>保留每次实际结果,不覆盖失败记录。分数不等于可信度;排序质量需要正反对照,不能把回填成功当作排序已通过。</p><ul>'
        + "".join(links)
        + "</ul>"
    )


def _chart_qa_records(run: Path, manifest: ProcessingManifest) -> str:
    links = []
    for folder in sorted((run / "chart-qa-evaluations").glob("*")):
        if re.fullmatch(r"[0-9a-f]{64}", folder.name) is None:
            continue
        report = read_evaluation(folder)
        base = "chart-qa-evaluations/" + folder.name
        state = "通过" if report.passed else "未通过"
        links.append(
            f'<li>受限 ChartQA {state}: <a href="{base}/report.json">独立评测</a> · <a href="{base}/observations.json">实际 HTTP 回答与拒答</a> · <a href="{base}/gold.json">来源金标</a></li>'
        )
    for folder in sorted((run / "chart-qa-v2-evaluations").glob("*")):
        if re.fullmatch(r"[0-9a-f]{64}", folder.name) is None:
            continue
        if manifest.retrieval is None:
            raise ValueError(
                "Displayed lookup evaluation requires a pinned retrieval release"
            )
        report_v2 = read_bar_evaluation(
            folder, processing_id=run.name, snapshot_id=manifest.retrieval.snapshot_id
        )
        base = "chart-qa-v2-evaluations/" + folder.name
        state = "通过" if report_v2.passed else "未通过"
        links.append(
            f'<li>柱状图显示值查值 {state}: <a href="{base}/report.json">独立评测与分层拒答统计</a> · <a href="{base}/observations.json">实际 HTTP 回答与拒答</a> · <a href="{base}/gold.json">来源金标</a> · <a href="{base}/targets.json">独立验证的快照和证据</a></li>'
        )
    if not links:
        return ""
    return (
        '<h2 id="chart-qa">可追溯数值查询</h2><p>v1 仅验收已资格环形图的显式百分比查值与同口径百分点差; v2 柱状图仅显示值查值,拒绝跨期计算、箭头和高度估值。不代表完整 P5 或任意财务问答。</p><ul>'
        + "".join(links)
        + "</ul>"
    )


def _coverage(
    outputs: ProcessingStore, manifest: ProcessingManifest
) -> tuple[str, str]:
    groups: dict[ObjectKind, list[ObjectProcessingRecord]] = defaultdict(list)
    for page in manifest.pages:
        for item in page.objects:
            groups[item.kind].append(item)
    rows: list[dict[str, str | int]] = []
    for kind, items in sorted(groups.items()):
        row: dict[str, str | int] = {"kind": kind.value, "objects": len(items)}
        row.update(
            {
                name: 0
                for name in (
                    "ir_artifacts",
                    "description_artifacts",
                    "source_transcription_qualified",
                    "labels_only_qualified",
                    "numeric_qualified",
                    "displayed_lookup_qualified",
                    "qualification_unavailable",
                )
            }
        )
        for item in items:
            stages = {stage.stage: stage for stage in item.stages}
            for name in ("ir", "description"):
                if name in stages and stages[name].artifact is not None:
                    key = name + "_artifacts"
                    row[key] = int(row[key]) + 1
            qualification = stages.get("qualification")
            if qualification is None or qualification.artifact is None:
                key = "qualification_unavailable"
            elif kind is not ObjectKind.CHART:
                key = "source_transcription_qualified"
            else:
                payload = outputs.assets.get(qualification.artifact)
                if uses_displayed_bar_policy(payload):
                    parse_displayed_bar_receipt(payload)
                    key = "displayed_lookup_qualified"
                else:
                    receipt = parse_chart_receipt(payload)
                    key = (
                        "labels_only_qualified"
                        if receipt.qualification.semantic_scope
                        == "figure-source-labels-only-v1"
                        else "numeric_qualified"
                    )
            row[key] = int(row[key]) + 1
        rows.append(row)
    columns = (
        "kind",
        "objects",
        "ir_artifacts",
        "description_artifacts",
        "source_transcription_qualified",
        "labels_only_qualified",
        "numeric_qualified",
        "displayed_lookup_qualified",
        "qualification_unavailable",
    )
    header = (
        "类型",
        "对象",
        "已保存 IR",
        "已保存描述",
        "仅原文转录资格",
        "仅标签资格",
        "数值关系资格",
        "仅显示值查值资格",
        "未获资格",
    )
    table = "<table><tr>" + "".join(f"<th>{name}</th>" for name in header) + "</tr>"
    table += (
        "".join(
            "<tr>"
            + "".join(f"<td>{escape(str(row[key]))}</td>" for key in columns)
            + "</tr>"
            for row in rows
        )
        + "</table>"
    )
    table += "<p>IR/描述数量只统计真实保存的产物,其中字段可能仍 unknown/PENDING。仅标签资格不等于数值 QA 资格; 原始图表推断保留供审阅,检索投影屏蔽未验证数值关系。柱状图显示值查值资格只允许读取原文明确值,不支持跨期计算、箭头或柱高估值。</p>"
    return table, json.dumps(rows, ensure_ascii=False, indent=2)


def export_processing_review(
    sources: LocalDocumentStore,
    outputs: ProcessingStore,
    snapshot_id: str,
    *,
    update_current: bool = True,
    title: str = "前 20 页处理审阅",
) -> Path:
    manifest = outputs.load(snapshot_id)
    source = sources.load(manifest.scope.source_manifest_id)
    if source.manifest.source.sha256 != manifest.scope.source_sha256:
        raise ValueError("Processing review has a different source identity")
    run = outputs.root / "runs" / snapshot_id
    run.mkdir(parents=True, exist_ok=True)
    coverage, coverage_json = _coverage(outputs, manifest)
    (run / "coverage.json").write_text(coverage_json, encoding="utf-8")
    (run / "manifest.json").write_bytes(
        ProcessingEnvelope(manifest=manifest).model_dump_json(indent=2).encode()
    )
    links: list[str] = []
    for page in manifest.pages:
        name = f"page-{page.page_index + 1:03d}"
        folder = run / name
        folder.mkdir(parents=True, exist_ok=True)
        canonical = _write_stage(outputs, folder, page.canonical, "canonical.json")
        layout = _write_stage(outputs, folder, page.partition, "layout.json")
        if page.raw_partition is not None:
            layout += "; " + _write_stage(
                outputs, folder, page.raw_partition, "layout.raw.json"
            )
        object_links = "".join(
            _object(outputs, folder, item)[1] for item in page.objects
        )
        native = sources.get(source.manifest.pages[page.page_index].svg).decode()
        native = re.sub(r"^\s*<\?xml[^>]*\?>", "", native)
        body = f'<p><a href="../review.html">返回批次</a></p><p>来源: {escape(source.manifest.filename)}, 物理第 {page.page_index + 1} 页。</p><p>{canonical}; {layout}</p><div class="source">{native}</div><ul>{object_links}</ul><p>布局为带来源的推断。所有具体结果和未完成原因见对象状态, 不能将 source saved 视为语义完成。</p>'
        (folder / "review.html").write_text(
            _html(f"物理第 {page.page_index + 1} 页", body), encoding="utf-8"
        )
        links.append(
            f'<li><a href="{name}/review.html">物理第 {page.page_index + 1} 页</a> — layout {page.partition.state}; {len(page.objects)} 个对象</li>'
        )
    body = (
        _summary(manifest)
        + coverage
        + '<p><a href="manifest.json">不可变 processing manifest</a> · <a href="coverage.json">分类型覆盖与资格统计</a></p>'
        + _retrieval_records(run)
        + _chart_qa_records(run, manifest)
        + "<ol>"
        + "".join(links)
        + "</ol>"
    )
    rendered = _html(title, body)
    (run / "review.html").write_text(rendered, encoding="utf-8")
    if not update_current:
        return run / "review.html"
    current = _html(
        "当前处理批次",
        _summary(manifest)
        + coverage
        + f'<p><a href="runs/{snapshot_id}/review.html">打开本次所有页面、IR、描述与诊断</a></p>',
    )
    with tempfile.NamedTemporaryFile(
        dir=outputs.root, mode="w", encoding="utf-8", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(current)
    try:
        temporary.replace(outputs.root / "review.html")
    finally:
        temporary.unlink(missing_ok=True)
    return outputs.root / "review.html"

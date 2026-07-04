"""端到端演示：生成合成数据 → 抽取 → 入库 → 参数化查询 → 对真值断言。

证明"结构化数字通路"：财务数字从 PPT/Excel 原生结构确定性抽取入 fact_metric，
查询返回确定值 + 数据血缘，查不到就明确说查不到（绝不编造）。
任一断言失配 exit 1；全部通过打印 ALL CHECKS PASSED。
"""

import json
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.fixtures.synthetic_deck import (
    GT_PATH,
    PPTX_PATH,
    XLSX_PATH,
    main as make_synthetic,
)
from ragspine.extraction.extractors import pptx_extractor, xlsx_extractor
from ragspine.storage.fact_store import FactStore, SqliteFactStore
from ragspine.agent.query_tools import execute_query_metric
from ragspine.common.core import DEFAULT_FACT_DB


def _ensure_synthetic() -> None:
    if not (PPTX_PATH.exists() and XLSX_PATH.exists() and GT_PATH.exists()):
        make_synthetic()


def _ingest() -> tuple[FactStore, int, list[str]]:
    """抽取两个源文件并入库（先删旧库）。返回 (store, 入库条数, warnings)。"""
    if DEFAULT_FACT_DB.exists():
        DEFAULT_FACT_DB.unlink()
    store = SqliteFactStore(DEFAULT_FACT_DB)
    store.init_schema()

    warnings: list[str] = []
    xf, xw = xlsx_extractor.extract_facts(XLSX_PATH)
    pf, pw = pptx_extractor.extract_facts(PPTX_PATH)
    warnings.extend(xw)
    warnings.extend(pw)
    # xlsx 先入、pptx 后入：重叠的 HK 年度事实以 pptx 表格血缘为准（与演示标签一致）
    store.upsert_facts(xf + pf)
    return store, store.count(), warnings


def _gt_index() -> dict[tuple, dict]:
    """真值按 (metric,entity,period_type,period) 去重索引（重叠源数值一致）。"""
    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    index: dict[tuple, dict] = {}
    for row in gt:
        key = (row["metric"], row["entity"], row["period_type"], row["period"])
        if key in index and index[key]["value"] != row["value"]:
            raise SystemExit(f"真值自相矛盾: {key} {index[key]['value']} vs {row['value']}")
        index[key] = row
    return index


def _fmt_value(value: float, unit: str) -> str:
    return f"{value:,.1f}%" if unit == "PCT" else f"US${value:,.1f}m"


def _report_found(label: str, res: dict) -> None:
    print(f"  [{label}]")
    print(f"    -> {_fmt_value(res['value'], res['unit'])}  "
          f"({res['metric_code']} / {res['entity']} / {res['period']})")
    print(f"    血缘: {res['source']['doc']}  @  {res['source']['locator']}")


def main() -> int:
    _ensure_synthetic()
    store, n_facts, warnings = _ingest()
    gt = _gt_index()

    print("=" * 72)
    print(f"已抽取入库 {n_facts} 条事实（warnings: {len(warnings)}）")
    if warnings:
        for w in warnings:
            print("   ! ", w)
    print("=" * 72)

    failures: list[str] = []

    def check_found(label, metric, entity, period, exp_key, channel="TOTAL"):
        res = execute_query_metric(store, metric, entity, period, channel)
        if res["status"] != "found":
            failures.append(f"{label}: 期望 found，实际 {res}")
            print(f"  [{label}] FAIL -> {res}")
            return None
        _report_found(label, res)
        truth = gt.get(exp_key)
        if truth is None:
            failures.append(f"{label}: 真值缺失 {exp_key}")
        elif abs(res["value"] - truth["value"]) > 1e-6:
            failures.append(f"{label}: 值失配 期望 {truth['value']} 实际 {res['value']}")
        elif res["unit"] != truth["unit"]:
            failures.append(f"{label}: 单位失配 期望 {truth['unit']} 实际 {res['unit']}")
        return res

    # (1) 表格路径：REVENUE / ACME_HK / FY2024
    check_found("1. 表格路径 REVENUE/ACME_HK/FY2024",
                "REVENUE", "ACME Hong Kong", "FY2024",
                ("REVENUE", "ACME_HK", "FY", "2024"))

    # (2) 中文同义词：营收 / 香港 / 2023
    check_found("2. 中文同义词 营收/香港/2023",
                "营收", "香港", "2023",
                ("REVENUE", "ACME_HK", "FY", "2023"))

    # (3) 图表内嵌数据路径：PROFIT / ACME_GROUP / FY2024
    check_found("3. 图表内嵌数据 PROFIT/ACME_GROUP/FY2024",
                "PROFIT", "ACME Group", "FY2024",
                ("PROFIT", "ACME_GROUP", "FY", "2024"))

    # (4) xlsx + HY 期间：NEWSALES / ACME_HK / 2024H1
    check_found("4. xlsx HY期间 NEWSALES/ACME_HK/2024H1",
                "NEWSALES", "ACME_HK", "2024H1",
                ("NEWSALES", "ACME_HK", "HY", "2024H1"))

    # (5) 防编造：ROE / ACME_CN / FY2024 必须 not_found
    res5 = execute_query_metric(store, "ROE", "ACME China", "FY2024")
    print("  [5. 防编造 ROE/ACME_CN/FY2024]")
    if res5["status"] == "not_found":
        print(f"    -> not_found（正确：中国页无 ROE，拒绝编造）  归一: {res5['normalized']}")
    else:
        failures.append(f"5: 期望 not_found，实际 {res5}")
        print(f"    FAIL -> {res5}")

    # (6) YoY：REVENUE ACME_HK FY2024 vs FY2023
    print("  [6. YoY REVENUE/ACME_HK FY2024 vs FY2023]")
    cur = execute_query_metric(store, "REVENUE", "ACME_HK", "FY2024")
    prev = execute_query_metric(store, "REVENUE", "ACME_HK", "FY2023")
    if cur["status"] == "found" and prev["status"] == "found":
        yoy = (cur["value"] - prev["value"]) / prev["value"] * 100
        print(f"    FY2024={_fmt_value(cur['value'], cur['unit'])}  "
              f"FY2023={_fmt_value(prev['value'], prev['unit'])}  YoY={yoy:+.1f}%")
        exp_yoy = (2680.0 - 2350.0) / 2350.0 * 100
        if abs(yoy - exp_yoy) > 1e-6:
            failures.append(f"6: YoY 失配 期望 {exp_yoy:.4f} 实际 {yoy:.4f}")
    else:
        failures.append(f"6: YoY 查询未命中 cur={cur['status']} prev={prev['status']}")

    # 全量比对：对每条去重真值都应能查到且值一致
    print("-" * 72)
    print(f"全量比对真值（{len(gt)} 条去重事实）...")
    for key, truth in gt.items():
        metric, entity, ptype, period = key
        res = execute_query_metric(store, metric, entity, period if ptype != "FY" else f"FY{period}")
        if res["status"] != "found":
            failures.append(f"全量[{key}]: 期望 found，实际 {res['status']}")
        elif abs(res["value"] - truth["value"]) > 1e-6:
            failures.append(f"全量[{key}]: 值失配 期望 {truth['value']} 实际 {res['value']}")

    print("=" * 72)
    store.close()
    if failures:
        print(f"FAILED — {len(failures)} 处失配：")
        for f in failures:
            print("   x ", f)
        return 1
    print(f"ALL CHECKS PASSED ({n_facts} facts ingested)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

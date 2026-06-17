"""冻结金标：结构化通道的指标/渠道词表（ADR 0004 pin-before-touch）。

ADR 0004 会把这些词表从 glossary.py / intent.py 的内联字面量迁入 DomainProfile 配置
（金融 ACME 作为一个实例）。本测试按 EXT-11（test_external_entity_guard 钉 ENTITY_SYNONYMS）
的风格钉住当前值【逐字不变】，证明迁移后字节级等价；任何放宽/改动都会立即变红。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.intent import _CHANNEL_SYNONYMS, _SUPPORTED_METRICS
from ragspine.common.glossary import METRIC_SYNONYMS, METRIC_UNITS


def test_metric_synonyms_frozen():
    assert METRIC_SYNONYMS == {
        "revenue": "REVENUE", "营收": "REVENUE",
        "newsales": "NEWSALES", "new sales": "NEWSALES", "新签金额": "NEWSALES",
        "profit": "PROFIT", "operating profit": "PROFIT", "营运利润": "PROFIT",
        "roe": "ROE", "return on equity": "ROE", "净资产收益率": "ROE", "股本回报率": "ROE",
    }


def test_metric_units_frozen():
    assert METRIC_UNITS == {"REVENUE": "USD_M", "NEWSALES": "USD_M", "PROFIT": "USD_M", "ROE": "PCT"}


def test_supported_metrics_frozen():
    assert _SUPPORTED_METRICS == ("REVENUE", "NEWSALES", "PROFIT", "ROE")


def test_channel_synonyms_frozen():
    assert _CHANNEL_SYNONYMS == {
        "代理": "AGENCY", "代理人": "AGENCY", "agency": "AGENCY",
        "银保": "BANCA", "bancassurance": "BANCA", "banca": "BANCA",
    }

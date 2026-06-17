"""color_semantics 模块红色测试：同色聚类、图例识别、版本化映射注册表、映射应用。

覆盖 PRD user story #6（同色聚类报告）、#7（图例区→映射草案）、
#24（确认一次长期生效）、#26（修订生成新版本不覆盖）。

只断言外部行为（返回值 / grid.warnings / 注册表持久化状态），不触碰内部实现细节。
StyledGrid / StyledCell 直接用 dataclass 构造器从 ground truth 装配——这些构造器可用，
而被测的 color_semantics 函数 / 方法体在红色阶段一律 raise NotImplementedError，
因此本文件期望「收集成功、全部 FAIL」。
"""

import json
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.color.color_semantics import (
    ColorMapping,
    LegendEntry,
    MappingRegistry,
    apply_mapping,
    cluster_colors,
    detect_legend,
)
from ragspine.extraction.ir import StyledCell, StyledGrid


def _build_grid(ground_truth: dict, sheet: str) -> StyledGrid:
    """从 ground truth 的逐格真值装配一张某 sheet 的 StyledGrid（不依赖被测代码）。"""
    grid = StyledGrid(sheet=sheet, source_doc_id=ground_truth["file"])
    for c in ground_truth["cells"]:
        if c["sheet"] != sheet:
            continue
        span = c["merge_span"]
        grid.cells[c["cell_ref"]] = StyledCell(
            value=c["value"],
            cell_ref=c["cell_ref"],
            resolved_rgb=c["resolved_rgb"],
            number_format=c["number_format"],
            is_merged_origin=c["is_merged_origin"],
            merge_span=tuple(span) if span is not None else None,
            cf_affected=c["cf_affected"],
        )
    return grid


@pytest.fixture
def hk_grid(ground_truth) -> StyledGrid:
    """HK_Performance：含 RGB 黄/绿、theme accent1+tint→95B3D7、灰表头、右侧图例区。"""
    return _build_grid(ground_truth, "HK_Performance")


@pytest.fixture
def cf_grid(ground_truth) -> StyledGrid:
    """CondFormat：色阶 CF 区域 B2:B5，受影响格 cf_affected=True、resolved_rgb=None。"""
    return _build_grid(ground_truth, "CondFormat")


@pytest.fixture
def transposed_grid(ground_truth) -> StyledGrid:
    """Transposed：无任何填充色、无图例区的转置表（聚类/图例都应为空）。"""
    return _build_grid(ground_truth, "Transposed")


def _legend_to_mapping(ground_truth, sheet: str, scope: str, *, status: str = "draft") -> ColorMapping:
    """把某 sheet 的 legend_expect 真值组装成一份 ColorMapping（用于 apply / 注册表测试）。"""
    entries = [
        LegendEntry(
            rgb=e["rgb"],
            meaning=e["meaning"],
            tag_key=e["tag_key"],
            tag_value=e["tag_value"],
            source_ref=e["text_ref"],
        )
        for e in ground_truth["sheets"][sheet]["legend_expect"]
    ]
    return ColorMapping(scope=scope, entries=entries, status=status)


# ----------------------------------------------------------------------------
# story #6 —— 同色单元格聚类 / 颜色分组报告
# ----------------------------------------------------------------------------

def test_cluster_colors_groups_match_ground_truth(hk_grid, ground_truth):
    """story #6 同色聚类的分组（rgb→cell_refs）必须与 ground truth 颜色组逐一吻合。"""
    clusters = cluster_colors(hk_grid)
    got = {c.rgb: sorted(c.cell_refs) for c in clusters}
    expected = {
        grp["rgb"]: sorted(grp["cell_refs"])
        for grp in ground_truth["sheets"]["HK_Performance"]["color_clusters_expect"]
    }
    assert got == expected


def test_cluster_colors_count_equals_len(hk_grid):
    """story #6 每个簇的 count 必须等于其 cell_refs 长度（报告排序依据自洽）。"""
    clusters = cluster_colors(hk_grid)
    for c in clusters:
        assert c.count == len(c.cell_refs)


def test_cluster_colors_sorted_by_size_desc(hk_grid):
    """story #6 颜色分组报告按簇大小降序返回，便于快速看出主导颜色。"""
    clusters = cluster_colors(hk_grid)
    counts = [c.count for c in clusters]
    assert counts == sorted(counts, reverse=True)
    assert clusters[0].rgb == "92D050"  # 最大簇 7 个


def test_cluster_colors_resolves_theme_tint(hk_grid):
    """story #6 theme accent1+tint 解析后的 95B3D7 必须作为独立一簇出现（不丢色）。"""
    clusters = cluster_colors(hk_grid)
    by_rgb = {c.rgb: c for c in clusters}
    assert "95B3D7" in by_rgb
    assert sorted(by_rgb["95B3D7"].cell_refs) == ["B5", "C5", "D5"]


def test_cluster_colors_skips_cf_affected(cf_grid):
    """story #6 边界：CF 区域填充色来源不可靠（resolved_rgb=None），不得进任何颜色簇。"""
    clusters = cluster_colors(cf_grid)
    assert clusters == []


def test_cluster_colors_empty_when_no_fill(transposed_grid):
    """story #6 边界：无任何可靠填充色的表，聚类返回空列表（不报错、不臆造）。"""
    assert cluster_colors(transposed_grid) == []


# ----------------------------------------------------------------------------
# story #7 —— 图例区识别 → 颜色→含义映射草案
# ----------------------------------------------------------------------------

def test_detect_legend_finds_both_entries(hk_grid, ground_truth):
    """story #7 右侧图例区（色块格+文字格）应解出两条「颜色→含义」草案。"""
    entries = detect_legend(hk_grid)
    expected = ground_truth["sheets"]["HK_Performance"]["legend_expect"]
    assert len(entries) == len(expected) == 2


def test_detect_legend_maps_rgb_to_meaning(hk_grid, ground_truth):
    """story #7 草案须正确绑定 swatch 真实 RGB 与相邻文字含义（黄→新/绿→成熟）。"""
    entries = detect_legend(hk_grid)
    got = {e.rgb: e.meaning for e in entries}
    expected = {e["rgb"]: e["meaning"] for e in ground_truth["sheets"]["HK_Performance"]["legend_expect"]}
    assert got == expected


def test_detect_legend_source_ref_lineage(hk_grid):
    """story #7 每条草案须带血缘（source_ref 指向图例文字格 G2/G3），便于回溯。"""
    entries = detect_legend(hk_grid)
    refs = {e.source_ref for e in entries}
    assert refs == {"G2", "G3"}


def test_detect_legend_empty_without_legend(transposed_grid):
    """story #7 边界：无图例区的表识别不到，返回空列表而非乱猜。"""
    assert detect_legend(transposed_grid) == []


# ----------------------------------------------------------------------------
# story #7 / #24 / #26 —— MappingRegistry 草案登记、版本自增、确认/驳回留痕
# ----------------------------------------------------------------------------

def test_register_draft_returns_version(tmp_db_path, ground_truth):
    """story #7 登记草案返回版本号，状态为 draft，可被 get_active 之外的流程引用。"""
    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        mapping = _legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance")
        version = reg.register_draft(mapping)
        assert isinstance(version, int)
    finally:
        reg.close()


def test_register_draft_version_autoincrement(tmp_db_path, ground_truth):
    """story #26 同 scope 重复登记草案，version 单调自增（修订不覆盖、各自留版本）。"""
    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        m1 = _legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance")
        m2 = _legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance")
        v1 = reg.register_draft(m1)
        v2 = reg.register_draft(m2)
        assert v2 > v1
    finally:
        reg.close()


def test_register_draft_scopes_independent(tmp_db_path, ground_truth):
    """story #26 边界：版本自增按 scope 隔离，不同工作簿的版本号互不串号。"""
    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        va = reg.register_draft(_legend_to_mapping(ground_truth, "HK_Performance", scope="book_A"))
        vb = reg.register_draft(_legend_to_mapping(ground_truth, "HK_Performance", scope="book_B"))
        assert va == vb  # 每个 scope 从同一起点独立计版
    finally:
        reg.close()


def test_confirm_activates_and_records_actor(tmp_db_path, ground_truth):
    """story #24 SME 确认一次后该版本生效，get_active 返回它且留痕确认人/备注。"""
    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        mapping = _legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance")
        version = reg.register_draft(mapping)
        reg.confirm("HK_Performance", version, actor="sme_fin", note="核对图例无误")
        active = reg.get_active("HK_Performance")
        assert active is not None
        assert active.version == version
        assert active.status == "active"
        assert active.confirmed_by == "sme_fin"
        assert active.note == "核对图例无误"
        assert active.confirmed_at  # 确认时间已写入
    finally:
        reg.close()


def test_get_active_none_before_confirm(tmp_db_path, ground_truth):
    """story #24 仅登记草案、尚未确认时，get_active 返回 None（草案绝不自动生效）。"""
    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        reg.register_draft(_legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance"))
        assert reg.get_active("HK_Performance") is None
    finally:
        reg.close()


def test_get_active_unknown_scope_none(tmp_db_path):
    """story #24 边界：从未登记过的 scope，get_active 返回 None 而非报错。"""
    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        assert reg.get_active("never_seen") is None
    finally:
        reg.close()


def test_reject_keeps_inactive(tmp_db_path, ground_truth):
    """story #24 被驳回的版本不得生效（get_active 仍为 None），且驳回留痕。"""
    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        version = reg.register_draft(_legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance"))
        reg.reject("HK_Performance", version, actor="sme_fin", note="图例文字有歧义")
        assert reg.get_active("HK_Performance") is None
    finally:
        reg.close()


def test_revision_new_version_does_not_overwrite(tmp_db_path, ground_truth):
    """story #26 确认 v1 后再登记并确认 v2：生效指针前移到新版本而非覆盖旧版本。"""
    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        v1 = reg.register_draft(_legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance"))
        reg.confirm("HK_Performance", v1, actor="sme_fin")
        v2 = reg.register_draft(_legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance"))
        reg.confirm("HK_Performance", v2, actor="sme_hr")
        active = reg.get_active("HK_Performance")
        assert active.version == v2
        assert active.confirmed_by == "sme_hr"
    finally:
        reg.close()


def test_registry_persists_across_reopen(tmp_db_path, ground_truth):
    """story #26 注册表是独立持久化资产：重开连接后已确认的生效映射仍可取回（历史可追溯）。"""
    reg = MappingRegistry(tmp_db_path)
    reg.init_schema()
    try:
        version = reg.register_draft(_legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance"))
        reg.confirm("HK_Performance", version, actor="sme_fin", note="long-lived")
    finally:
        reg.close()

    reg2 = MappingRegistry(tmp_db_path)
    try:
        active = reg2.get_active("HK_Performance")
        assert active is not None
        assert active.version == version
        assert active.confirmed_by == "sme_fin"
        assert len(active.entries) == 2
    finally:
        reg2.close()


# ----------------------------------------------------------------------------
# story #7 / #24 —— apply_mapping：未确认置空+告警；确认后产出正确 tags
# ----------------------------------------------------------------------------

def test_apply_unconfirmed_returns_empty_and_warns(hk_grid, ground_truth):
    """story #7 未确认（draft）映射应用时返回空 tags 并向 grid.warnings 追加告警（不静默入库）。"""
    mapping = _legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance", status="draft")
    warnings_before = len(hk_grid.warnings)
    result = apply_mapping(hk_grid, mapping)
    assert result == {}
    assert len(hk_grid.warnings) > warnings_before


def test_apply_active_produces_correct_tags(hk_grid, ground_truth):
    """story #24 确认（active）映射应用后，每个着色格产出与 ground truth 一致的 tags。"""
    mapping = _legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance", status="active")
    result = apply_mapping(hk_grid, mapping)
    expected = {
        c["cell_ref"]: c["tags"]
        for c in ground_truth["cells"]
        if c["sheet"] == "HK_Performance" and c["tags"]
    }
    for cell_ref, tags in expected.items():
        assert result.get(cell_ref) == tags


def test_apply_active_yellow_means_new(hk_grid, ground_truth):
    """story #24 黄色格（B2/C2/D2）必须被翻译成 product_line=new。"""
    mapping = _legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance", status="active")
    result = apply_mapping(hk_grid, mapping)
    for ref in ("B2", "C2", "D2"):
        assert result.get(ref) == {"product_line": "new"}


def test_apply_active_unmapped_color_gets_no_tag(hk_grid, ground_truth):
    """story #24 边界：映射里没有的颜色（蓝 95B3D7 / 灰 D9D9D9）不得被打 tag。"""
    mapping = _legend_to_mapping(ground_truth, "HK_Performance", scope="HK_Performance", status="active")
    result = apply_mapping(hk_grid, mapping)
    for ref in ("B5", "C5", "D5", "B1", "C1", "D1"):
        assert not result.get(ref)


def test_apply_active_skips_cf_affected(cf_grid, ground_truth):
    """story #24 刁钻形态：CF 受影响格颜色来源不可靠，即便映射 active 也不打颜色 tag。"""
    mapping = _legend_to_mapping(ground_truth, "HK_Performance", scope="CondFormat", status="active")
    result = apply_mapping(cf_grid, mapping)
    for ref in ("B2", "B3", "B4", "B5"):
        assert not result.get(ref)

"""ADR 0004 收官证明（步骤 13/13）：第二个【非金融、无 temporal 维】领域，
组件级证明结构化通道的硬不变量在任意维度下仍成立。

本步是整个重构的【证明】步骤——不是端到端跑自然语言 lab 问句（glossary 的
normalize_metric/entity/period 在步骤 4 刻意冻结为金融词表、不进 monkeypatch 契约，
认不出"抗拉强度"），而是【组件级】逐不变量证明：

- 直接构造 lab DomainProfile（仿 test_company_generalization._acme_profile 的范式）；
- 把维度值映射到 Fact 的类型化身份列（dim→列：measurement→metric_code、site→entity、
  batch→period[period_type=BATCH]、specimen→channel、region→geography），直接验证
  存储 / schema / 反幻觉 / 安全门各层不变量。

证明的领域 lab_metrology（材料/QA 测试实验室）刻意：
  - 无任何 kind="temporal" 维（batch 是类目身份维、走 period 槽但不是时间轴）；
  - 无 fabrication_whitelist_regex —— 这是"period 白名单从未被放宽"的最强证明前提：
    无 temporal 维 → 反幻觉检查不剥离任何数字 → 拒答答案里每个数字（含 period 形的
    "2024"）都被标记，严格性是金融默认的超集。

所有 monkeypatch 只在用例内（pytest fixture 自动还原，零跨用例污染）；金融 golden
（qa_golden_set.jsonl / qa_baseline.json，n_cases=41）字节不动。本文件纯测试新增，
不改任何 src。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

import ragspine.agent.intent as intent_mod
import ragspine.agent.query_tools as query_tools_mod
import ragspine.eval.qa_eval as qa_eval_mod
from ragspine.agent.security_gate import (
    SECURITY_ALLOW,
    SECURITY_REFUSE_OUT_OF_SCOPE,
    SecurityGate,
)
from ragspine.common.company_profile import DimensionSpec, DomainProfile
from ragspine.storage.fact_store import Fact, SqliteFactStore, _compute_dim_key


# ---------------------------------------------------------------------------
# lab_metrology profile 构造（仿 _acme_profile：构造一个与金融默认完全不同的领域）
# ---------------------------------------------------------------------------

_HOME_ENTITY_SYNONYMS: dict[str, str] = {
    "上海实验室": "SH_LAB",
    "上海": "SH_LAB",
    "sh lab": "SH_LAB",
    "sh_lab": "SH_LAB",
    "北京实验室": "BJ_LAB",
    "bj_lab": "BJ_LAB",
}


def _lab_profile() -> DomainProfile:
    """构造 lab_metrology DomainProfile：非金融、无 temporal 维。

    dim→Fact 列映射（blocker #4 resolution a）：
        measurement → metric_code   (measure 维：抗拉强度/硬度)
        site        → entity        (scope-anchor，安全门在 masked_text 上匹配)
        batch       → period        (period_type='BATCH'，类目身份维、无时间轴)
        specimen    → channel        (channel-analogue，可选、不展开)
        region      → geography      (derived_from='site'，从 schema/必填中排除)
    """
    return DomainProfile(
        home_company_name="晟测材料实验室",
        home_entity_code="SH_LAB",
        home_entity_synonyms=dict(_HOME_ENTITY_SYNONYMS),
        entity_geography={"SH_LAB": "EAST", "BJ_LAB": "NORTH"},
        external_entities={
            "竞测": "竞测实验室(RivalMetro)",
            "竞测实验室": "竞测实验室(RivalMetro)",
            "rivalmetro": "竞测实验室(RivalMetro)",
        },
        home_entity_labels={"SH_LAB": "Shanghai Lab", "BJ_LAB": "Beijing Lab"},
        dimensions=(
            DimensionSpec(
                "measurement",
                label="测量项",
                kind="measure",
                required=True,
                clarify="ask_first",
                whitelist_in_fabrication_check=False,
                synonyms={
                    "抗拉强度": "TENSILE",
                    "tensile": "TENSILE",
                    "硬度": "HARDNESS",
                    "hardness": "HARDNESS",
                },
                units={"TENSILE": "MPA", "HARDNESS": "HRC"},
            ),
            DimensionSpec(
                "site",
                label="实验室",
                required=True,
                synonyms=dict(_HOME_ENTITY_SYNONYMS),
            ),
            # 类目身份维、无 temporal 轴 —— 经 period 槽进入 dim_key，证明 period
            # 不是被偏爱的时间维，任意类目身份维都能确定性辨识。
            DimensionSpec(
                "batch",
                label="批次",
                required=True,
                synonyms={
                    "批次a": "BATCH_A",
                    "批次b": "BATCH_B",
                    "batch_a": "BATCH_A",
                    "batch_b": "BATCH_B",
                },
            ),
            DimensionSpec(
                "specimen",
                label="试样",
                required=False,
                default="ALL",
                expand=False,
                synonyms={"全部": "ALL"},
            ),
            # 派生维：derived_from='site'，从 tool schema properties / 必填中排除
            # （与金融 geography ← entity 同一谓词）。
            DimensionSpec(
                "region",
                label="区域",
                identity=False,
                expand=False,
                default="UNKNOWN",
                derived_from="site",
                derivation={"SH_LAB": "EAST", "BJ_LAB": "NORTH"},
            ),
        ),
    )


def _lab_fact(
    *,
    measurement: str = "TENSILE",
    site: str = "SH_LAB",
    region: str = "EAST",
    specimen: str = "ALL",
    batch: str = "BATCH_A",
    value: float = 520.0,
    unit: str = "MPA",
    doc: str = "lab_report.pdf",
    locator: str = "p=3,row=2",
) -> Fact:
    """按 dim→列映射构造一条 lab Fact（measurement→metric_code 等，见 _lab_profile）。"""
    return Fact(
        metric_code=measurement,
        entity=site,
        geography=region,
        channel=specimen,
        period_type="BATCH",
        period=batch,
        value=value,
        unit=unit,
        source_doc_id=doc,
        source_locator=locator,
    )


# ===========================================================================
# 证明 1：profile 构造 + frozen + 维度旗标
# ===========================================================================

def test_lab_profile_constructs_and_is_frozen():
    """lab profile 可构造；DomainProfile / DimensionSpec frozen（setattr raises）；
    measurement 维 kind=='measure' 且 whitelist_in_fabrication_check is False
    （measure 值绝不进反幻觉白名单）。"""
    profile = _lab_profile()
    assert profile.home_company_name == "晟测材料实验室"
    assert profile.home_entity_code == "SH_LAB"
    assert tuple(d.name for d in profile.dimensions) == (
        "measurement", "site", "batch", "specimen", "region"
    )

    # DomainProfile frozen
    with pytest.raises(Exception):  # noqa: B017  (frozen dataclass -> FrozenInstanceError)
        profile.home_company_name = "x"  # type: ignore[misc]

    measurement = next(d for d in profile.dimensions if d.name == "measurement")
    assert measurement.kind == "measure"
    assert measurement.whitelist_in_fabrication_check is False
    # measure 维携带单位，无 fabrication_whitelist_regex（绝不剥离 measure 值）。
    assert measurement.units == {"TENSILE": "MPA", "HARDNESS": "HRC"}
    assert measurement.fabrication_whitelist_regex is None

    # DimensionSpec frozen
    with pytest.raises(Exception):  # noqa: B017
        measurement.kind = "categorical"  # type: ignore[misc]

    # 关键前提：lab profile 无任何 temporal 维、无任何 fabrication_whitelist_regex。
    assert all(d.kind != "temporal" for d in profile.dimensions)
    assert all(d.fabrication_whitelist_regex is None for d in profile.dimensions)


# ===========================================================================
# 证明 2：tool schema 由任意维名驱动；派生维被排除
# ===========================================================================

def test_lab_schema_generalizes_to_arbitrary_dims(monkeypatch):
    """换上 lab profile 后，query_metric tool schema 的 properties 键 ==
    ['measurement','site','batch','specimen']（region 因 derived_from 被跳过、不在内），
    required == ['measurement','site','batch']。证明 schema 由任意维名驱动、派生维排除。

    两条路径都验证：(a) 直接传 profile 参数给 build_query_metric_tool_anthropic；
    (b) monkeypatch 模块级 _PROFILE 后用缺省参数（运行期换 profile 契约）。"""
    profile = _lab_profile()

    # (a) 显式传 profile
    tool = query_tools_mod.build_query_metric_tool_anthropic(profile)
    props = tool["input_schema"]["properties"]
    assert list(props.keys()) == ["measurement", "site", "batch", "specimen"]
    assert "region" not in props  # 派生维（derived_from='site'）被排除
    assert tool["input_schema"]["required"] == ["measurement", "site", "batch"]

    # (b) monkeypatch 模块级 _PROFILE，缺省参数也应反映 lab 维
    monkeypatch.setattr(query_tools_mod, "_PROFILE", profile, raising=False)
    tool2 = query_tools_mod.build_query_metric_tool_anthropic()
    props2 = tool2["input_schema"]["properties"]
    assert list(props2.keys()) == ["measurement", "site", "batch", "specimen"]
    assert "region" not in props2
    assert tool2["input_schema"]["required"] == ["measurement", "site", "batch"]

    # OpenAI 格式同样泛化（参数袋在 function.parameters 下）。
    tool_oai = query_tools_mod.build_query_metric_tool_openai(profile)
    oai_params = tool_oai["function"]["parameters"]
    assert list(oai_params["properties"].keys()) == [
        "measurement", "site", "batch", "specimen"
    ]
    assert oai_params["required"] == ["measurement", "site", "batch"]


# ===========================================================================
# 证明 3：dim_key / 存储确定性读路径（blocker #4 resolution a）
# ===========================================================================

def test_lab_dim_key_distinguishes_non_temporal_identity_dim(tmp_path):
    """两条【仅 batch 不同】的 lab Fact（BATCH_A/BATCH_B）经 period 槽进入 dim_key，
    dim_key 不同（非 temporal 身份维 batch 不碰撞）——证明 dim_key 公式不是金融专用。"""
    fact_a = _lab_fact(batch="BATCH_A", value=520.0)
    fact_b = _lab_fact(batch="BATCH_B", value=545.0)

    key_a = _compute_dim_key(fact_a)
    key_b = _compute_dim_key(fact_b)
    # 仅 batch（→period）不同即得不同 dim_key（否则两批次会碰撞、>1 行候选 ->
    # 歧义数字到达模型，正是设计拒绝的 EAV 失败模式）。
    assert key_a != key_b
    assert "BATCHBATCH_A" in key_a  # period = period_type + period
    assert "BATCHBATCH_B" in key_b


def test_lab_storage_deterministic_read_and_provenance(tmp_path):
    """真实 FactStore：两条仅 batch 不同的 lab Fact upsert -> count==2；
    确定性 5 列 query 各自唯一命中、value 正确、带 source 血缘（provenance）；
    查不存在的 batch -> 空（not_found 语义）；同身份重复 upsert -> count 不变（幂等）。"""
    store = SqliteFactStore(tmp_path / "lab.db")
    store.init_schema()

    fact_a = _lab_fact(batch="BATCH_A", value=520.0, locator="p=3,row=2")
    fact_b = _lab_fact(batch="BATCH_B", value=545.0, locator="p=3,row=3")
    written = store.upsert_facts([fact_a, fact_b])
    assert written == 2
    assert store.count() == 2  # 两批次共存、不碰撞

    # 确定性读路径：query(metric_code, entity, period_type, period, channel)
    # （dim→列映射后即 measurement/site/BATCH/batch/specimen）。
    hits_a = store.query("TENSILE", "SH_LAB", "BATCH", "BATCH_A", "ALL")
    assert len(hits_a) == 1  # 0-或-1 行，确定性
    got = hits_a[0]
    assert got.value == 520.0
    assert got.unit == "MPA"
    assert got.metric_code == "TENSILE"
    assert got.entity == "SH_LAB"
    assert got.geography == "EAST"  # region（派生维）落在 geography 列
    # provenance：每条事实带 source_doc_id + locator，绝不丢血缘。
    assert got.source_doc_id == "lab_report.pdf"
    assert got.source_locator == "p=3,row=2"

    hits_b = store.query("TENSILE", "SH_LAB", "BATCH", "BATCH_B", "ALL")
    assert len(hits_b) == 1
    assert hits_b[0].value == 545.0  # batch 维确实辨识出不同事实

    # not_found 语义：查不存在的 batch -> 空列表（绝不编造）。
    assert store.query("TENSILE", "SH_LAB", "BATCH", "BATCH_C", "ALL") == []
    # measurement / specimen 维同样确定性辨识：不存在的 specimen -> 空。
    assert store.query("TENSILE", "SH_LAB", "BATCH", "BATCH_A", "SPECIMEN_X") == []

    # 幂等：同身份（同 dim_key）重复 upsert，count 不变（覆盖而非新增）。
    store.upsert_facts([_lab_fact(batch="BATCH_A", value=999.0)])
    assert store.count() == 2
    # 覆盖生效：值被更新（确定性 upsert 覆盖语义对任意维成立）。
    assert store.query("TENSILE", "SH_LAB", "BATCH", "BATCH_A", "ALL")[0].value == 999.0

    store.close()


def test_lab_fact_dimensions_bag_mirrors_identity_columns():
    """lab Fact 的 dimensions 袋（内存态）从身份列派生镜像，period 用 period_type
    前缀（与 dim_key 同口径）；dim_key 永不作为 Fact 属性回灌。"""
    fact = _lab_fact(batch="BATCH_A")
    assert fact.dimensions == {
        "metric": "TENSILE",
        "entity": "SH_LAB",
        "channel": "ALL",
        "period": "BATCHBATCH_A",
    }
    assert not hasattr(fact, "dim_key")  # storage-only，从不进 Fact


# ===========================================================================
# 证明 4：反幻觉（无 temporal 维 → 不剥离任何数字，最严格）
# ===========================================================================

def test_lab_fabrication_strips_nothing_without_temporal_dim(monkeypatch):
    """用 lab profile 重绑 qa_eval._PROFILE（_PROFILE_BOUND_MODULES 已含 qa_eval）后：
    - _fabrication_whitelist_re() is None（无 temporal 维 → 无白名单）；
    - detect_fabricated_numbers 对 period 形数字也不剥离 —— "批次 2024 测得 520 MPa"
      返回【同时含 '2024' 和 '520'】。

    这是"period 白名单从未被放宽"的最强证明：无 temporal 维时严格性 == 标记每个数字，
    是金融默认（仅剥离 year-anchored 期间）的超集。"""
    profile = _lab_profile()
    monkeypatch.setattr(qa_eval_mod, "_PROFILE", profile, raising=False)

    # 无 temporal 维 → 白名单正则为 None（不剥离任何数字）。
    assert qa_eval_mod._fabrication_whitelist_re() is None

    fabricated = qa_eval_mod.detect_fabricated_numbers("批次 2024 测得 520 MPa")
    # period 形的 "2024" 也被标记（金融默认会剥离它，lab 不剥离 —— 严格超集）。
    assert "2024" in fabricated
    assert "520" in fabricated


def test_lab_fabrication_contrast_with_finance_default():
    """对照：金融【默认】profile（未 monkeypatch，模块级 _PROFILE 不变）仍会剥离
    year-anchored 期间 'FY2024' 但标记真数字 '520' —— 证明 lab 的不剥离是【更严格】，
    且默认金融行为字节级未受 lab 用例影响（用例内 monkeypatch 已还原）。"""
    # 此处不 monkeypatch：读默认金融 profile 的白名单。
    assert qa_eval_mod._fabrication_whitelist_re() is not None
    fabricated = qa_eval_mod.detect_fabricated_numbers("FY2024 营收为 520 USD_M")
    assert "520" in fabricated  # 真数字被标记
    assert "2024" not in fabricated  # 期间被白名单剥离（金融默认）


# ===========================================================================
# 证明 5：安全门泛化（配置驱动拒答，零硬编码金融竞品）
# ===========================================================================

def test_lab_security_gate_refuses_rival_lab():
    """SecurityGate 由 lab profile 的 external_entities + home_company_name 构造：
    问竞品实验室 -> REFUSE_OUT_OF_SCOPE，命中 '竞测实验室(RivalMetro)'，拒答文案含
    home 名 '晟测材料实验室'。证明安全门对任意领域配置驱动、不含硬编码金融竞品。"""
    profile = _lab_profile()
    gate = SecurityGate(profile.external_entities, profile.home_company_name)

    verdict = gate.screen(
        raw_question="竞测实验室的抗拉强度是多少", metric=None
    )
    assert verdict.decision == SECURITY_REFUSE_OUT_OF_SCOPE
    assert verdict.external_entity == "竞测实验室(RivalMetro)"
    assert verdict.message is not None
    assert "晟测材料实验室" in verdict.message  # home 名进拒答文案
    assert "竞测实验室(RivalMetro)" in verdict.message
    # 收窄项提议改查 home（泛化、配置驱动）。
    assert any("晟测材料实验室" in opt for opt in verdict.narrowing_options)


def test_lab_security_gate_allows_home_site():
    """home site 问句（'上海实验室…'）-> ALLOW。证明安全门只拦外部清单命中，
    不误伤 home 实体；金融竞品词（竞安/竞平等）不在 lab 清单内、与本域无关。"""
    profile = _lab_profile()
    gate = SecurityGate(profile.external_entities, profile.home_company_name)

    verdict = gate.screen(
        raw_question="上海实验室 批次A 的抗拉强度是多少", metric=None
    )
    assert verdict.decision == SECURITY_ALLOW
    assert verdict.external_entity is None


# ===========================================================================
# 证明 6：金融默认未受影响（本测试只在自身 scope monkeypatch）
# ===========================================================================

def test_finance_default_profile_untouched_by_lab():
    """守护：lab 用例的 monkeypatch 只在各用例内（pytest fixture 自动还原），
    模块级 _PROFILE 仍是金融默认。金融 qa_baseline n_cases=41 由现有 test_qa_eval 守护，
    无需重复跑；此处仅断言模块级 profile 维结构仍是金融 5 维，证明零跨用例污染。"""
    # qa_eval 模块级 _PROFILE：金融默认 5 维（含 temporal period 维）。
    fin_dims = tuple(d.name for d in qa_eval_mod._PROFILE.dimensions)
    assert fin_dims == ("metric", "entity", "period", "channel", "geography")
    period = next(d for d in qa_eval_mod._PROFILE.dimensions if d.name == "period")
    assert period.kind == "temporal"
    assert period.whitelist_in_fabrication_check is True
    assert period.fabrication_whitelist_regex is not None

    # query_tools / intent 模块级 _PROFILE 同样仍是金融默认（lab monkeypatch 已还原）。
    assert query_tools_mod._PROFILE.home_entity_code == "ACME_GROUP"
    assert intent_mod._PROFILE.home_entity_code == "ACME_GROUP"

"""CompanyProfile 配置化载入单测（GAP-B 配套）。

home 公司身份/同义词/默认实体/外部实体清单全部来自 config/company.toml；
文件缺失静默回退内置默认（= 现有 ACME 值），保证 glossary import 期零副作用。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common.company_profile import CompanyProfile, load_company_profile


def test_default_config_loads_as_acme():
    """本部署 config/company.toml 存在且解析为 ACME profile。"""
    profile = load_company_profile()
    assert isinstance(profile, CompanyProfile)
    assert "ACME" in profile.home_company_name
    assert profile.home_entity_code == "ACME_GROUP"
    assert profile.external_entities.get("竞安") == "竞安(Jingan)"
    assert profile.external_entities.get("jingcheng") == "Jingcheng"


def test_missing_file_falls_back_to_builtin_default(tmp_path):
    """指向不存在路径时静默回退内置默认（不抛错），值 = 现有 ACME 值。"""
    profile = load_company_profile(tmp_path / "no_such.toml")
    assert profile.home_company_name == "ACME Group"
    assert profile.home_entity_code == "ACME_GROUP"
    assert profile.home_entity_synonyms["香港"] == "ACME_HK"
    assert profile.home_entity_synonyms["中国"] == "ACME_CN"
    assert profile.entity_geography == {
        "ACME_GROUP": "ASIA", "ACME_HK": "HK", "ACME_CN": "CN"
    }
    assert "竞安" in profile.external_entities


def test_default_matches_file_byte_for_byte():
    """内置默认（缺失回退）与 config/company.toml 解析结果在 home 同义词上字节级等价。"""
    file_profile = load_company_profile()
    fallback = load_company_profile("definitely-not-a-real-path.toml")
    assert file_profile.home_entity_synonyms == fallback.home_entity_synonyms
    assert file_profile.entity_geography == fallback.entity_geography


def test_profile_is_frozen():
    """CompanyProfile 不可变（frozen dataclass）。"""
    profile = load_company_profile()
    with pytest.raises(Exception):
        profile.home_entity_code = "MUTATED"  # type: ignore[misc]


def test_custom_profile_round_trips(tmp_path):
    """临时 toml（ACME/Globex）→ 解析无 ACME 硬编码，证明配置化生效。"""
    toml_text = (
        "[home]\n"
        'company_name = "ACME"\n'
        'entity_code = "ACME_GROUP"\n'
        "\n"
        "[home.synonyms]\n"
        'acme = "ACME_GROUP"\n'
        "\n"
        "[external_entities]\n"
        'globex = "Globex"\n'
    )
    path = tmp_path / "company.toml"
    path.write_text(toml_text, encoding="utf-8")

    profile = load_company_profile(path)
    assert profile.home_company_name == "ACME"
    assert profile.home_entity_code == "ACME_GROUP"
    assert profile.home_entity_synonyms == {"acme": "ACME_GROUP"}
    assert profile.external_entities == {"globex": "Globex"}

"""A missing or incomplete trusted parser profile must never authorize paint."""

from hashlib import sha256
from types import MappingProxyType, SimpleNamespace

import pytest

from enterprise_pdf_rag.adapters.source_profile import read_source_profile


def test_missing_sdk_profile_is_explicitly_unavailable() -> None:
    with pytest.raises(ValueError, match="trusted_paint_profile_unavailable"):
        read_source_profile(object(), source_sha256="a" * 64, page_index=0)


class ProfilePage:
    def __init__(self, **changes: object) -> None:
        self.changes = changes

    def get_paint_profile(self) -> SimpleNamespace:
        profile = SimpleNamespace(
            version="strict-paint-profile-v1",
            complete=True,
            resource_scopes=(
                MappingProxyType(
                    {
                        "scope_id": "page:4",
                        "kind": "page",
                        "object_ref": (4, 0),
                        "parent_scope_id": None,
                        "origin": "direct",
                        "resource_sha256": "b" * 64,
                        "entries": (),
                        "group_keys": (),
                        "group": None,
                    }
                ),
            ),
            operators=(),
            inline_images=(),
            diagnostics=(),
        )
        for name, value in self.changes.items():
            setattr(profile, name, value)
        return profile


def test_complete_profile_is_bound_to_source_page_and_exact_profile_bytes() -> None:
    receipt = read_source_profile(ProfilePage(), source_sha256="a" * 64, page_index=2)

    assert receipt.source_sha256 == "a" * 64
    assert receipt.page_index == 2
    assert receipt.profile_digest == sha256(receipt.profile_json).hexdigest()
    assert receipt == read_source_profile(
        ProfilePage(), source_sha256="a" * 64, page_index=2
    )


def test_page_group_is_typed_and_only_the_bounded_rgb_profile_is_admitted() -> None:
    scope = dict(ProfilePage().get_paint_profile().resource_scopes[0])
    group: dict[str, object] = {
        "object_ref": None,
        "type": "Group",
        "subtype": "Transparency",
        "color_space": "DeviceRGB",
        "isolated": None,
        "knockout": False,
        "keys": ("CS", "K", "S", "Type"),
        "unsupported_fields": (),
    }
    scope.update(group_keys=group["keys"], group=group)
    receipt = read_source_profile(
        ProfilePage(resource_scopes=(scope,)), source_sha256="a" * 64, page_index=0
    )
    assert b'"color_space":"DeviceRGB"' in receipt.profile_json
    for change in (
        {"color_space": "DeviceGray"},
        {"isolated": True},
        {"knockout": True},
    ):
        scope["group"] = {**group, **change}
        with pytest.raises(ValueError, match="unsupported_transparency_group"):
            read_source_profile(
                ProfilePage(resource_scopes=(scope,)),
                source_sha256="a" * 64,
                page_index=0,
            )
    scope.update(group=group, kind="form")
    with pytest.raises(ValueError, match="unsupported_transparency_group"):
        read_source_profile(
            ProfilePage(resource_scopes=(scope,)), source_sha256="a" * 64, page_index=0
        )


@pytest.mark.parametrize(
    "change",
    [
        {"complete": False},
        {
            "diagnostics": (
                {
                    "code": "unknown_operator",
                    "scope_id": "page:4",
                    "operator_ordinal": 0,
                },
            )
        },
        {
            "operators": (
                {
                    "scope_id": "page:4",
                    "ordinal": 0,
                    "mnemonic": "sh",
                    "disposition": "unsupported",
                    "resource_name": "Shade",
                    "numeric_operands": (),
                },
            )
        },
    ],
)
def test_complete_flag_cannot_override_specific_incomplete_accounting(
    change: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="incomplete_source_paint_profile"):
        read_source_profile(ProfilePage(**change), source_sha256="a" * 64, page_index=0)


@pytest.mark.parametrize(
    "state_change",
    [
        {"line_width": 9.0},
        {"blend_mode": "Multiply"},
        {"soft_mask": "dictionary"},
        {"unsupported_keys": ("TR",)},
        {"fill_alpha": 0.999},
        {"stroke_alpha": 0.001},
    ],
)
def test_selected_unapplied_width_or_nonordinary_graphics_state_is_rejected(
    state_change: dict[str, object],
) -> None:
    state: dict[str, object] = {
        "line_width": None,
        "line_cap": None,
        "line_join": None,
        "miter_limit": None,
        "stroke_alpha": None,
        "fill_alpha": None,
        "blend_mode": "Normal",
        "soft_mask": "absent",
        "unsupported_keys": (),
        **state_change,
    }
    scope = dict(ProfilePage().get_paint_profile().resource_scopes[0])
    scope["entries"] = (
        {
            "category": "ExtGState",
            "name": "GS",
            "object_ref": (8, 0),
            "value_kind": "dictionary",
            "selected": True,
            "ext_gstate": state,
        },
    )
    with pytest.raises(ValueError, match="unsupported_selected_graphics_state"):
        read_source_profile(
            ProfilePage(resource_scopes=(scope,)), source_sha256="a" * 64, page_index=0
        )

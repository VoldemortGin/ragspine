"""Strict paint-profile boundary; parser capability is a prerequisite to proof."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, cast

import pdfspine
from pydantic import BaseModel, ConfigDict, Field


class _Boundary(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class _ExtGState(_Boundary):
    line_width: float | None
    line_cap: int | None
    line_join: int | None
    miter_limit: float | None
    stroke_alpha: float | None
    fill_alpha: float | None
    blend_mode: str | None
    soft_mask: str
    unsupported_keys: tuple[str, ...]


class _ResourceEntry(_Boundary):
    category: str
    name: str
    object_ref: tuple[int, int] | None
    value_kind: str
    selected: bool
    ext_gstate: _ExtGState | None


class _TransparencyGroup(_Boundary):
    object_ref: tuple[int, int] | None
    type: str | None
    subtype: str | None
    color_space: str | None
    isolated: bool | None
    knockout: bool | None
    keys: tuple[str, ...]
    unsupported_fields: tuple[str, ...]


class _ResourceScope(_Boundary):
    scope_id: str
    kind: Literal["page", "form"]
    object_ref: tuple[int, int] | None
    parent_scope_id: str | None
    origin: Literal["direct", "inherited", "parent_fallback"]
    resource_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[_ResourceEntry, ...]
    group_keys: tuple[str, ...]
    group: _TransparencyGroup | None


class _Operator(_Boundary):
    scope_id: str
    ordinal: int = Field(ge=0)
    mnemonic: str
    disposition: Literal["supported", "unsupported", "malformed"]
    resource_name: str | None
    numeric_operands: tuple[float, ...]


class _InlineImage(_Boundary):
    scope_id: str
    ordinal: int = Field(ge=0)
    parameter_keys: tuple[str, ...]
    disposition: Literal["supported", "unsupported", "malformed"]


class _Diagnostic(_Boundary):
    code: str
    scope_id: str
    operator_ordinal: int | None


class _PaintProfile(_Boundary):
    version: Literal["strict-paint-profile-v1"]
    complete: bool
    resource_scopes: tuple[_ResourceScope, ...]
    operators: tuple[_Operator, ...]
    inline_images: tuple[_InlineImage, ...]
    diagnostics: tuple[_Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class SourceProfileReceipt:
    source_sha256: str
    page_index: int
    sdk_version: str
    profile_json: bytes
    resource_digest: str
    rule_version: str = "source-paint-profile-admission-v1"

    @property
    def profile_digest(self) -> str:
        return sha256(self.profile_json).hexdigest()

    @property
    def artifact_id(self) -> str:
        return "source-profile-receipt-v1:" + sha256(repr(self).encode()).hexdigest()


def _copy_sdk(value: object) -> object:
    if isinstance(value, Mapping):
        entries = cast(Mapping[object, object], value)
        if any(not isinstance(key, str) for key in entries):
            raise ValueError("invalid_paint_profile_mapping_key")
        return {key: _copy_sdk(member) for key, member in entries.items()}
    if isinstance(value, tuple):
        return tuple(_copy_sdk(member) for member in value)
    return value


def read_source_profile(
    page: object, *, source_sha256: str, page_index: int
) -> SourceProfileReceipt:
    """Read the SDK boundary; callers must obtain the page from pinned PDF bytes."""
    method = getattr(page, "get_paint_profile", None)
    if not callable(method):
        raise ValueError("trusted_paint_profile_unavailable")
    raw = cast(Callable[[], object], method)()
    profile = _PaintProfile.model_validate(
        {name: _copy_sdk(getattr(raw, name)) for name in _PaintProfile.model_fields}
    )
    if (
        not profile.complete
        or profile.diagnostics
        or profile.inline_images
        or any(operator.disposition != "supported" for operator in profile.operators)
    ):
        raise ValueError("incomplete_source_paint_profile")
    for scope in profile.resource_scopes:
        group = scope.group
        if (group is None and scope.group_keys) or (
            group is not None
            and (
                scope.kind != "page"
                or group.type != "Group"
                or group.subtype != "Transparency"
                or group.color_space != "DeviceRGB"
                or group.isolated not in {None, False}
                or group.knockout not in {None, False}
                or group.unsupported_fields
                or set(group.keys) - {"Type", "S", "CS", "I", "K"}
                or group.keys != scope.group_keys
            )
        ):
            raise ValueError("unsupported_transparency_group")
        for entry in scope.entries:
            if entry.category != "ExtGState" or not entry.selected:
                continue
            state = entry.ext_gstate
            if (
                state is None
                or state.line_width is not None
                or state.blend_mode not in {None, "Normal"}
                or state.soft_mask not in {"absent", "None"}
                or state.unsupported_keys
                or state.fill_alpha not in {None, 0.0, 1.0}
                or state.stroke_alpha not in {None, 0.0, 1.0}
            ):
                raise ValueError("unsupported_selected_graphics_state")
    return SourceProfileReceipt(
        source_sha256,
        page_index,
        pdfspine.__version__,
        profile.model_dump_json().encode(),
        sha256(repr(profile.resource_scopes).encode()).hexdigest(),
    )

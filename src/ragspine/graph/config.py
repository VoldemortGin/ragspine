"""Lightweight public configuration for the opt-in workspace graph runtime."""

from typing import Literal

GraphMode = Literal["off", "auto"]


def normalize_graph_mode(value: str) -> GraphMode:
    """Validate the deliberately small high-level graph-mode surface."""
    normalized = value.strip().lower()
    if normalized not in {"off", "auto"}:
        raise ValueError("graph must be 'off' or 'auto'")
    return normalized  # type: ignore[return-value]  # narrowed by membership check


__all__ = ["GraphMode", "normalize_graph_mode"]

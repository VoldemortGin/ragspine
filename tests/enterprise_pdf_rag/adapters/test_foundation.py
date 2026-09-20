"""Runtime version, import hook and explicit mode are enforced contracts."""

from collections.abc import Callable
from typing import cast

import pytest
from beartype.roar import BeartypeCallHintParamViolation

from enterprise_pdf_rag.adapters.http.app import create_configured_app
from enterprise_pdf_rag.adapters.pdfspine_figure import PdfspineFigureParser
from enterprise_pdf_rag.core.settings import get_settings


def test_central_import_hook_checks_internal_calls() -> None:
    extract = cast(Callable[..., object], PdfspineFigureParser().extract)
    with pytest.raises(BeartypeCallHintParamViolation):
        extract("not bytes", page_index=0, bbox=(0.0, 0.0, 10.0, 10.0))


def test_app_has_no_default_demo_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_EXECUTION_MODE", "unconfigured")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="explicitly"):
            create_configured_app()
    finally:
        get_settings.cache_clear()

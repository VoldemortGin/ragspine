"""Chat over documents with verified metadata: readable names, routing, filters and page titles."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx2 import AsyncClient

from enterprise_pdf_rag.adapters.document_catalog import scan_catalog
from enterprise_pdf_rag.adapters.draft_publication import DraftPublication
from enterprise_pdf_rag.adapters.http.chat import model_id
from enterprise_pdf_rag.adapters.http.documents import create_documents_app
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from tests.enterprise_pdf_rag.adapters.page_metadata_helpers import publish_with_metadata
from tests.enterprise_pdf_rag.adapters.test_chat_http import _FRAGMENT, _URL, _body
from tests.enterprise_pdf_rag.adapters.test_documents_http import _run
from tests.enterprise_pdf_rag.answers.fake_llm import Script, answered, declined, scripted_client

_OFFLINE = OfflineDescriptionEmbedder()
Published = tuple[Path, DraftPublication, DraftPublication]


@pytest.fixture(scope="module")
def published(tmp_path_factory: pytest.TempPathFactory) -> Published:
    base = tmp_path_factory.mktemp("chat-metadata")
    root = base / "ingestion"
    with pytest.MonkeyPatch.context() as monkeypatch:
        meridian = publish_with_metadata(
            base,
            monkeypatch,
            filename="meridian.pdf",
            label="Meridian 1H26 Hong Kong",
            page_count=3,
            output_dir=root,
            embedder=_OFFLINE,
        )
        orion = publish_with_metadata(
            base,
            monkeypatch,
            filename="orion.pdf",
            label="Orion FY2024 Thailand",
            page_count=3,
            output_dir=root,
            embedder=_OFFLINE,
        )
    return root, meridian, orion


def _quote_page(page: str) -> Script:
    def script(prompt: str) -> ModelAnswer:
        block = next(block for block in prompt.split("| member ")[1:] if page in block)
        found = _FRAGMENT.search(block) if page == "page 2" else None
        if found is None:
            return declined()
        span_id, text = found.groups()
        return answered(
            f"It reads: {text}",
            ModelClaim(
                claim_id="q",
                member_id=block[:64],
                kind="quote",
                field_path=f"fragments.{span_id}",
                text=text,
            ),
        )

    return script


def _app(root: Path, llm_dir: Path, *, max_live_calls: int = 1) -> tuple[FastAPI, list[str]]:
    client, prompts = scripted_client(llm_dir, _quote_page("page 2"), max_live_calls=max_live_calls)
    return create_documents_app(scan_catalog(root), embedder=_OFFLINE, llm=client), prompts


def test_models_and_documents_show_the_verified_display_title(
    published: Published, tmp_path: Path
) -> None:
    root, meridian, orion = published
    app, _ = _app(root, tmp_path / "llm")

    async def scenario(client: AsyncClient) -> None:
        listing = await client.get("/v1/models")
        names = {item["id"]: item["name"] for item in listing.json()["data"]}
        assert names[model_id(meridian.source_sha256)] == (
            f"Meridian 1H26 Hong Kong page 1 ({meridian.source_sha256[:12]})"
        )
        assert names[model_id(orion.source_sha256)].startswith("Orion FY2024 Thailand page 1")
        documents = (await client.get("/v1/documents")).json()["documents"]
        by_id = {item["document_id"]: item for item in documents}
        assert by_id[orion.source_sha256]["display_title"] == "Orion FY2024 Thailand page 1"
        assert by_id[orion.source_sha256]["report_period"] == "FY2024"
        assert by_id[orion.source_sha256]["years"] == [2024]
        assert by_id[orion.source_sha256]["regions"] == ["Thailand"]
        assert by_id[meridian.source_sha256]["language"] == "en"

    _run(app, scenario)


def test_unnamed_document_is_routed_by_title_words_and_years(
    published: Published, tmp_path: Path
) -> None:
    root, meridian, orion = published
    app, prompts = _app(root, tmp_path / "llm", max_live_calls=3)

    async def scenario(client: AsyncClient) -> None:
        by_title = await client.post(_URL, json=_body("gpt-4", "What does Orion say on page 2?"))
        assert by_title.status_code == 200, by_title.text
        assert by_title.json()["enterprise_pdf_rag"]["document_sha256"] == orion.source_sha256
        by_year = await client.post(_URL, json=_body("gpt-4", "What did page 2 say in 2026?"))
        assert by_year.status_code == 200, by_year.text
        assert by_year.json()["enterprise_pdf_rag"]["document_sha256"] == meridian.source_sha256
        both = await client.post(_URL, json=_body("gpt-4", "Meridian FY2024 page 2"))
        assert both.status_code == 422, both.text  # title says Meridian, year says Orion
        detail = both.json()["detail"]
        assert (
            "Meridian 1H26 Hong Kong page 1" in detail and "Orion FY2024 Thailand page 1" in detail
        )
        none = await client.post(_URL, json=_body("gpt-4", "What does page 2 say?"))
        assert none.status_code == 422, none.text
        assert "names none" in none.json()["detail"]
        explicit = await client.post(
            _URL,
            json=_body("gpt-4", "What does Orion say on page 2?", document=meridian.source_sha256),
        )
        assert explicit.status_code == 200, explicit.text  # an explicit document always wins
        assert explicit.json()["enterprise_pdf_rag"]["document_sha256"] == meridian.source_sha256

    _run(app, scenario)
    assert len(prompts) == 3


def test_filters_are_derived_reported_and_relaxed_and_citations_name_the_page(
    published: Published, tmp_path: Path
) -> None:
    root, meridian, _ = published
    app, prompts = _app(root, tmp_path / "llm", max_live_calls=4)

    async def scenario(client: AsyncClient) -> None:
        derived = await client.post(
            _URL,
            json=_body("gpt-4", "Hong Kong page 2 in 1H26", document=meridian.source_sha256),
        )
        assert derived.status_code == 200, derived.text
        envelope = derived.json()["enterprise_pdf_rag"]
        assert envelope["filters_applied"] == {"periods": ["1H2026"], "regions": ["Hong Kong"]}
        assert envelope["filters_relaxed"] is True  # 3 pages minus the cover < top_k
        assert envelope["status"] == "answered"
        citation = envelope["claims"][0]["citations"][0]
        assert citation["page_title"] == "Meridian 1H26 Hong Kong page 2"
        explicit = await client.post(
            _URL,
            json=_body(
                "gpt-4",
                "What does page 2 say?",
                document=meridian.source_sha256,
                filters={"periods": ["2026"]},
            ),
        )
        assert explicit.status_code == 200, explicit.text
        assert explicit.json()["enterprise_pdf_rag"]["filters_applied"] == {
            "periods": ["2026"],
            "regions": [],
        }
        disabled = await client.post(
            _URL,
            json=_body(
                "gpt-4", "Hong Kong 1H26 page 2", document=meridian.source_sha256, filters={}
            ),
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["enterprise_pdf_rag"]["filters_applied"] is None
        assert disabled.json()["enterprise_pdf_rag"]["filters_relaxed"] is False
        too_many = await client.post(
            _URL,
            json=_body(
                "gpt-4",
                "page 2",
                document=meridian.source_sha256,
                filters={"periods": [str(year) for year in range(2010, 2019)]},
            ),
        )
        assert too_many.status_code == 422, too_many.text
        unknown_key = await client.post(
            _URL,
            json=_body(
                "gpt-4", "page 2", document=meridian.source_sha256, filters={"entities": ["x"]}
            ),
        )
        assert unknown_key.status_code == 422, unknown_key.text

    _run(app, scenario)
    assert len(prompts) == 3

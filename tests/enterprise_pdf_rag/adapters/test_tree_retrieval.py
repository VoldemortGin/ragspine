"""One bounded, cached call over a document's outline picks the few pages worth reading."""

import json
from pathlib import Path
from typing import Any, Never

import pytest
from pydantic import SecretStr

from enterprise_pdf_rag.adapters.tree_retrieval import (
    MAX_ROUTE_NODES,
    MAX_ROUTE_PAGES,
    MAX_TREE_CHARS,
    TREE_ROUTE_RULES,
    TREE_ROUTE_TASK,
    TreeRouteDTO,
    route_tree,
)
from enterprise_pdf_rag.answers.models import TreeRoute
from enterprise_pdf_rag.processing.document_tree import (
    DOCUMENT_TREE_SCHEMA,
    DocumentTree,
    TreeNode,
    render_tree,
)
from ragspine.common.evidence.providers.json_completion import JsonCompletionClient
from ragspine.common.evidence.providers.providers import LLMConfig

_QUESTION = "What was VONB by market in 1H26?"


def _tree() -> DocumentTree:
    """Eight pages (0-7, printed p1-p8) under three sections, each named by a node id."""
    return DocumentTree(
        DOCUMENT_TREE_SCHEMA,
        "a" * 64,
        "agenda",
        (
            TreeNode(
                "n0001",
                "Group overview",
                1,
                (0, 1, 2),
                (
                    TreeNode(
                        "n0002",
                        "Highlights",
                        2,
                        (0, 1),
                        (
                            TreeNode("n0003", "Highlights", 3, (0,)),
                            TreeNode("n0004", "Highlights continued", 3, (1,)),
                        ),
                    ),
                    TreeNode("n0005", "Outlook", 2, (2,)),
                ),
            ),
            TreeNode(
                "n0006",
                "Financial review",
                1,
                (3, 4, 5),
                (
                    TreeNode(
                        "n0007",
                        "VONB by market",
                        2,
                        (3, 4),
                        (
                            TreeNode("n0008", "VONB by market", 3, (3,)),
                            TreeNode("n0009", "VONB by market continued", 3, (4,)),
                        ),
                    ),
                    TreeNode("n0010", "Embedded value", 2, (5,)),
                ),
            ),
            TreeNode(
                "n0011",
                "Appendix",
                1,
                (6, 7),
                (
                    TreeNode("n0012", "Glossary", 2, (6,)),
                    TreeNode("n0013", "Disclaimer", 2, (7,)),
                ),
            ),
        ),
    )


def _config() -> LLMConfig:
    return LLMConfig(
        api_key=SecretStr("offline-secret"),
        base_url="https://provider.invalid",
        model="offline-test",
    )


def _client(
    cache_dir: Path, reply: str, *, max_live_calls: int = 2
) -> tuple[JsonCompletionClient, list[Any]]:
    seen: list[Any] = []

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        request = json.loads(payload)
        seen.append(request)
        return json.dumps(
            {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]}
        ).encode()

    client = JsonCompletionClient(
        _config(), cache_dir=cache_dir, max_live_calls=max_live_calls, sender=sender
    )
    return client, seen


def _reply(
    node_ids: list[str], pages: list[int], rationale: str = "The market table sits here."
) -> str:
    return json.dumps({"node_ids": node_ids, "pages": pages, "rationale": rationale})


def test_a_question_is_routed_to_pages_by_one_strict_bounded_call(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, _reply(["n0007"], [4, 5]))
    route = route_tree(_QUESTION, _tree(), client)
    # Printed p4/p5 are page indices 3/4, which is exactly what n0007 covers.
    assert route == TreeRoute(("n0007",), (3, 4), False, "The market table sits here.")
    assert client.live_call_count == 1

    (request,) = seen
    assert request["messages"][0] == {"role": "system", "content": TREE_ROUTE_RULES}
    prompt = str(request["messages"][1]["content"])
    # The question reaches the model as data, verbatim, beside the rendered outline.
    assert _QUESTION in prompt
    assert "n0007 p4-5 VONB by market" in prompt
    assert len(prompt) <= 24_000
    schema = request["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert sorted(schema["schema"]["properties"]) == ["node_ids", "pages", "rationale"]


def test_a_node_id_the_tree_does_not_print_is_ignored(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _reply(["n0007", "n9999", "n0010"], []))
    route = route_tree(_QUESTION, _tree(), client)
    assert route is not None
    assert route.node_ids == ("n0007", "n0010")
    assert route.pages == (3, 4, 5)


def test_a_page_outside_the_tree_or_repeated_is_dropped(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _reply([], [4, 4, 99, 0, 5]))
    route = route_tree(_QUESTION, _tree(), client)
    assert route is not None
    # p4 twice is one page; p99 is past the document and p0 is below its first printed page.
    assert route.node_ids == ()
    assert route.pages == (3, 4)


def test_a_named_node_contributes_every_page_it_covers(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _reply(["n0002"], []))
    route = route_tree(_QUESTION, _tree(), client)
    assert route is not None
    assert route.pages == (0, 1)


def test_only_the_first_six_named_nodes_are_kept(tmp_path: Path) -> None:
    named = ["n0001", "n0002", "n0003", "n0004", "n0005", "n0007", "n0010", "n0012"]
    client, _ = _client(tmp_path, _reply(named, []))
    route = route_tree(_QUESTION, _tree(), client)
    assert route is not None
    assert route.node_ids == tuple(named[:MAX_ROUTE_NODES])
    assert len(route.node_ids) == MAX_ROUTE_NODES
    # n0010's page 5 and n0012's page 6 never reach the pages, because their ids were cut.
    assert route.pages == (0, 1, 2, 3, 4)


def test_more_than_six_pages_are_cut_and_the_rest_returned_ascending(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _reply(["n0006", "n0001"], [8, 6, 3, 1]))
    route = route_tree(_QUESTION, _tree(), client)
    assert route is not None
    # The model's own pages come first (7, 5, 2, 0), then the nodes' (1, 3, 4 after the
    # duplicates); page 4 falls off the end of the cap, and what is left is sorted.
    assert route.pages == (0, 1, 2, 3, 5, 7)
    assert len(route.pages) == MAX_ROUTE_PAGES


def test_the_route_replays_from_the_cache_without_a_second_call(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, _reply(["n0007"], []))
    tree = _tree()
    first = route_tree(_QUESTION, tree, client)
    again = route_tree(_QUESTION, tree, client)
    assert first is not None and again is not None
    assert (first.cache_hit, again.cache_hit) == (False, True)
    assert again.pages == first.pages and again.node_ids == first.node_ids
    assert len(seen) == 1 and client.live_call_count == 1


def test_the_task_salt_keeps_routes_apart_from_other_bounded_calls(tmp_path: Path) -> None:
    assert TREE_ROUTE_TASK == "document-tree-route-v1"
    tree = _tree()
    client, _ = _client(tmp_path, _reply(["n0007"], []))
    route_tree(_QUESTION, tree, client)
    records = sorted(path.name for path in (tmp_path / "requests").glob("*.json"))
    assert len(records) == 1
    # The very same prompt under another task is a different cache entry, never this one
    # replayed: only the task salt differs between the two requests.
    outline = render_tree(tree, max_chars=MAX_TREE_CHARS)
    other = client.complete_text_json(
        task="query-translation-v1",
        prompt=f"Question:\n{_QUESTION}\n\nDocument outline:\n{outline}",
        response_model=TreeRouteDTO,
        system=TREE_ROUTE_RULES,
    )
    assert other.cache_hit is False
    assert other.request_fingerprint not in records[0]


def test_an_exhausted_budget_yields_no_route_instead_of_failing(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, _reply(["n0007"], []), max_live_calls=0)
    assert route_tree(_QUESTION, _tree(), client) is None
    assert seen == []


def test_a_transport_failure_yields_no_route(tmp_path: Path) -> None:
    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> Never:
        raise TimeoutError("no route")

    client = JsonCompletionClient(_config(), cache_dir=tmp_path, max_live_calls=2, sender=sender)
    assert route_tree(_QUESTION, _tree(), client) is None


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        json.dumps({"node_ids": ["n0007"]}),
        json.dumps({"node_ids": "n0007", "pages": [], "rationale": ""}),
        json.dumps({"node_ids": [], "pages": [], "rationale": "", "extra": 1}),
        _reply(["n9999"], [99]),
        _reply([], []),
    ],
)
def test_a_model_reply_that_names_no_usable_page_yields_no_route(
    tmp_path: Path, reply: str
) -> None:
    client, _ = _client(tmp_path, reply)
    assert route_tree(_QUESTION, _tree(), client) is None


def test_a_question_longer_than_the_budget_is_not_routed(tmp_path: Path) -> None:
    client, seen = _client(tmp_path, _reply(["n0007"], []))
    assert route_tree("v" * 2_001, _tree(), client) is None
    assert seen == []


def test_an_outline_that_renders_to_nothing_is_not_routed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "enterprise_pdf_rag.adapters.tree_retrieval.render_tree", lambda tree, **kwargs: "  "
    )
    client, seen = _client(tmp_path, _reply(["n0007"], []))
    assert route_tree(_QUESTION, _tree(), client) is None
    assert seen == []


def test_a_blank_question_is_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _reply(["n0007"], []))
    with pytest.raises(ValueError):
        route_tree("   ", _tree(), client)


def test_the_route_schema_bounds_what_the_model_may_return() -> None:
    schema = TreeRouteDTO.model_json_schema()
    assert schema["properties"]["node_ids"]["maxItems"] == 12
    assert schema["properties"]["pages"]["maxItems"] == 24
    assert schema["properties"]["rationale"]["maxLength"] == 400

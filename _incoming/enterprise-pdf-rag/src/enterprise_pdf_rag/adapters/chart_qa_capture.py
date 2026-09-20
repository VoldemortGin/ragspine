"""Capture ChartQA HTTP outcomes from explicitly named loopback services."""

import argparse
import http.client
import json
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, model_validator

from enterprise_pdf_rag.adapters.chart_qa_evaluation import (
    Observations,
    ObservedCase,
    ResponseDto,
    load_gold,
)

_MAX_RESPONSE_BYTES = 1_048_576


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class CaptureTarget(_StrictModel):
    endpoint: str
    processing_id: str
    snapshot_id: str
    member_id: str

    @model_validator(mode="after")
    def validate_target(self) -> "CaptureTarget":
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            raise ValueError(
                "ChartQA capture endpoint must be an explicit loopback port"
            )
        for value in (self.processing_id, self.snapshot_id, self.member_id):
            if len(value) != 64 or any(
                char not in "0123456789abcdef" for char in value
            ):
                raise ValueError("ChartQA capture pins require lowercase SHA-256 IDs")
        return self


class CaptureTargets(_StrictModel):
    schema_version: Literal["chart-qa-capture-targets-v1"]
    targets: dict[str, CaptureTarget]


class _ErrorDetail(_StrictModel):
    code: str
    message: str


class _ErrorBody(_StrictModel):
    error: _ErrorDetail


type HttpTransport = Callable[[CaptureTarget, bytes], tuple[int, bytes]]


def capture_chart_qa(
    gold_payload: bytes,
    targets_payload: bytes,
    *,
    transport: HttpTransport | None = None,
) -> Observations:
    """Capture every gold case without deriving any expected response from gold."""
    gold = load_gold(gold_payload)
    targets = CaptureTargets.model_validate_json(
        targets_payload, strict=True, extra="forbid"
    )
    required = {case.request.target for case in gold.cases}
    if set(targets.targets) != required:
        raise ValueError("Capture targets must exactly cover the gold target names")
    send = post_chart_query if transport is None else transport
    results: list[ObservedCase] = []
    for case in gold.cases:
        target = targets.targets[case.request.target]
        request_payload = json.dumps(
            {
                "kind": "chart",
                "processing_id": target.processing_id,
                "snapshot_id": target.snapshot_id,
                "member_id": target.member_id,
                "operation": case.request.operation,
                "series": case.request.series,
                "period": case.request.period,
                "unit": case.request.unit,
                "points": [point.model_dump() for point in case.request.points],
            },
            separators=(",", ":"),
        ).encode()
        status, response_payload = send(target, request_payload)
        if status == 200:
            body = ResponseDto.model_validate_json(
                response_payload, strict=True, extra="forbid"
            )
            results.append(
                ObservedCase(
                    case_id=case.case_id,
                    http_status=status,
                    body=body,
                    error_class=None,
                )
            )
        else:
            error_class = classify_query_error(status, response_payload)
            results.append(
                ObservedCase(
                    case_id=case.case_id,
                    http_status=status,
                    body=None,
                    error_class=error_class,
                )
            )
    return Observations(
        schema_version="chart-qa-observations-v1", results=tuple(results)
    )


def classify_query_error(status: int, payload: bytes) -> str:
    if status in {409, 503}:
        return _ErrorBody.model_validate_json(
            payload, strict=True, extra="forbid"
        ).error.code
    if status == 422:
        parsed = json.loads(payload)
        if not isinstance(parsed, dict) or "detail" not in parsed:
            raise ValueError("HTTP 422 response is not a validation error")
        return "invalid_request"
    return f"http_{status}"


def post_chart_query(target: CaptureTarget, payload: bytes) -> tuple[int, bytes]:
    """POST only to a validated loopback target, with no redirect/retry handling."""
    target = CaptureTarget.model_validate_json(target.model_dump_json(), strict=True)
    parsed = urlsplit(target.endpoint)
    assert parsed.hostname is not None and parsed.port is not None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
    try:
        connection.request(
            "POST",
            "/v1/queries",
            body=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        response = connection.getresponse()
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("ChartQA capture response exceeds the size limit")
        return response.status, body
    finally:
        connection.close()


def write_capture_output(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("Capture output already exists with different bytes")
        return
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError(
                    "Capture output already exists with different bytes"
                ) from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--targets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    observations = capture_chart_qa(
        arguments.gold.read_bytes(), arguments.targets.read_bytes()
    )
    write_capture_output(
        arguments.output,
        observations.model_dump_json(exclude_none=False).encode(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

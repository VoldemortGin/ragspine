"""Explicit loopback capture for v2 gold, preserving every HTTP response byte."""

import argparse
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field

from enterprise_pdf_rag.adapters.chart_qa_capture import (
    HttpTransport,
    classify_query_error,
    post_chart_query,
    write_capture_output,
)
from enterprise_pdf_rag.adapters.chart_qa_evaluation import QueryPoint
from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation import (
    query_payload,
    validate_bar_targets,
)
from enterprise_pdf_rag.adapters.chart_qa_v2_evaluation_models import (
    BarCaptureTargets,
    BarObservations,
    BarObservedCase,
    BarResponseDto,
    StrictModel,
    load_bar_gold,
)


class CaptureRequest(StrictModel):
    """Unsupported operations are deliberate HTTP-422 cases, never assertions."""

    schema_version: Literal["chart-qa-v2"]
    kind: Literal["chart"]
    processing_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    member_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: str = Field(min_length=1, max_length=128)
    series: str = Field(min_length=1, max_length=256)
    period: str = Field(min_length=1, max_length=128)
    unit: str = Field(min_length=1, max_length=32)
    points: tuple[QueryPoint, ...] = Field(min_length=1, max_length=1)


def capture_bar_chart_qa(
    gold_payload: bytes,
    targets_payload: bytes,
    *,
    transport: HttpTransport | None = None,
) -> BarObservations:
    gold = load_bar_gold(gold_payload)
    targets = BarCaptureTargets.model_validate_json(
        targets_payload, strict=True, extra="forbid"
    )
    validate_bar_targets(gold, targets)
    send = post_chart_query if transport is None else transport
    results = []
    for case in gold.cases:
        target = targets.targets[case.request.target]
        request = query_payload(case, target)
        CaptureRequest.model_validate_json(request, strict=True, extra="forbid")
        status, response = send(target.http, request)
        if len(response) > 1_048_576:
            raise ValueError("ChartQA capture response exceeds the size limit")
        if status == 200:
            BarResponseDto.model_validate_json(response, strict=True, extra="forbid")
        else:
            classify_query_error(status, response)
        results.append(
            BarObservedCase(
                case_id=case.case_id,
                http_status=status,
                request_json=request.decode(),
                response_json=response.decode(),
                response_sha256=sha256(response).hexdigest(),
            )
        )
    return BarObservations(
        schema_version="chart-qa-v2-observations-v1",
        targets_sha256=sha256(targets_payload).hexdigest(),
        results=tuple(results),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--targets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    observed = capture_bar_chart_qa(args.gold.read_bytes(), args.targets.read_bytes())
    write_capture_output(args.output, observed.model_dump_json().encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

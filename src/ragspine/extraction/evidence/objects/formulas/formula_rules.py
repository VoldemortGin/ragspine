"""Prove a formula from its own spans and paths: pure, deterministic, replayable, fail closed."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import hypot

from ragspine.extraction.evidence.document.models import Bounds
from ragspine.extraction.evidence.figures.models import SourceAnchor, Verification
from ragspine.extraction.evidence.objects.formulas.formula_models import (
    FormulaSourceObservation,
    FormulaStructure,
    FormulaToken,
    Matrix,
    ObservedPath,
    ObservedRun,
    PathEvidence,
    ProofLevel,
    ScriptEvidence,
    ScriptPosition,
    ScriptProof,
    StructureKind,
    TokenRole,
)
from ragspine.extraction.evidence.objects.typed_ir import FormulaIR

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
RISE_EPSILON = 1e-3
DERIVED_MAX_SIZE_RATIO = 0.8
DERIVED_MIN_SUPER_SHIFT = 0.15
DERIVED_MIN_SUB_SHIFT = 0.10
ADJACENCY_GAP = 0.5
RULE_MAX_SLOPE = 0.5
RULE_MAX_WIDTH = 2.0
X_OVERLAP_TOLERANCE = 1.0
VERTICAL_REACH = 1.5
BASELINE_CLUSTER = 0.3
PATH_INSIDE_TOLERANCE = 0.5

_OPERATORS = "+-−×÷*/·"  # noqa: RUF001 — typographic operator glyphs are source characters, never their ASCII lookalikes
_RELATIONS = "=<>≤≥≈≠≡→"
_BRACKETS = "()[]{}"
_UNITS = "%‰"
_RADICAL = "√"
_MERGEABLE = frozenset({TokenRole.OPERAND, TokenRole.NUMBER, TokenRole.GREEK})

_RELATION_WORDS = {
    "=": "等于",
    "<": "小于",
    ">": "大于",
    "≤": "小于等于",
    "≥": "大于等于",
    "≈": "约等于",
    "≠": "不等于",
    "≡": "恒等于",
    "→": "推出",
}
_OPERATOR_WORDS = {
    "+": "加",
    "-": "减",
    "−": "减",  # noqa: RUF001 — typographic operator glyphs are source characters, never their ASCII lookalikes
    "×": "乘以",  # noqa: RUF001 — typographic operator glyphs are source characters, never their ASCII lookalikes
    "*": "乘以",
    "·": "乘以",
    "÷": "除以",
    "/": "除以",
}

_IR_DIAGNOSTIC = (
    "Tokens quote span substrings verbatim; structures quote get_cdrawings paths; "
    "derived scripts are typographic inferences"
)
_LATEX_JOIN = "\\ "

# (path, y, x0, x1) for a horizontal rule taken straight from a source path.
type HorizontalRule = tuple[ObservedPath, float, float, float]


def role_of(char: str) -> TokenRole | None:
    """None means whitespace (a gap); everything printable gets exactly one role."""
    if char.isspace():
        return None
    if char in _OPERATORS:
        return TokenRole.OPERATOR
    if char in _RELATIONS:
        return TokenRole.RELATION
    if char in _BRACKETS:
        return TokenRole.BRACKET
    if char in _UNITS:
        return TokenRole.UNIT
    if char in _RADICAL:
        return TokenRole.RADICAL
    if char.isdigit() or char in ".,":
        return TokenRole.NUMBER
    if "Ͱ" <= char <= "Ͽ":
        return TokenRole.GREEK
    return TokenRole.OPERAND


def tile_run(text: str) -> tuple[tuple[int, int, TokenRole], ...]:
    """Maximal same-role runs (single-char for operator/relation/bracket/radical); whitespace is a gap."""
    pieces: list[tuple[int, int, TokenRole]] = []
    index = 0
    while index < len(text):
        role = role_of(text[index])
        if role is None:
            index += 1
            continue
        end = index + 1
        if role in _MERGEABLE:
            while end < len(text) and role_of(text[end]) is role:
                end += 1
        pieces.append((index, end, role))
        index = end
    return tuple(pieces)


def check_tiling(text: str, pieces: Sequence[tuple[int, int]]) -> str | None:
    """Closure rule: ordered, non-overlapping substrings whose concatenation is the span text
    with every whitespace run removed; nothing else is skipped. Returns a diagnostic or None."""
    last = 0
    for start, end in pieces:
        if start < last or start >= end or end > len(text):
            return "formula_span_pieces_overlap_or_disordered"
        if text[last:start].strip():
            return f"formula_span_not_tiled:{text[last:start]!r}"
        last = end
    if text[last:].strip():
        return f"formula_span_not_tiled:{text[last:]!r}"
    if "".join(text[start:end] for start, end in pieces) != "".join(text.split()):
        return "formula_span_tiling_mismatch"
    return None


def size_of(token: FormulaToken) -> float:
    """A token's typographic height, read from its own source character boxes."""
    return token.bbox[3] - token.bbox[1]


def rise_of(run: ObservedRun, page_height: float) -> float | None:
    if run.ctm != IDENTITY or run.direction != (1.0, 0.0):
        return None
    return (page_height - run.origin[1]) - run.text_matrix[5]


def script_of(
    run: ObservedRun,
    base: FormulaToken | None,
    base_run: ObservedRun | None,
    page_height: float,
) -> tuple[ScriptPosition, ScriptProof | None, ScriptEvidence | None, str | None]:
    """text_rise proof when the PDF really used Ts; otherwise size-ratio + baseline-shift → derived;
    otherwise base. The last element is a diagnostic that withholds the formula."""
    rise = rise_of(run, page_height)
    if rise is not None and abs(rise) < RISE_EPSILON:
        rise = 0.0
    if base is None or base_run is None:
        if rise:
            return ScriptPosition.BASE, None, None, f"formula_script_without_base:{run.span_id}"
        return ScriptPosition.BASE, None, None, None
    evidence = ScriptEvidence(
        rise,
        run.size / base_run.size,
        base_run.origin[1] - run.origin[1],
        bool(run.flags & 1),
    )
    if rise:
        position = ScriptPosition.SUPERSCRIPT if rise > 0 else ScriptPosition.SUBSCRIPT
        return position, "text_rise", evidence, None
    if evidence.size_ratio <= DERIVED_MAX_SIZE_RATIO:
        if evidence.baseline_offset >= DERIVED_MIN_SUPER_SHIFT * base_run.size:
            return ScriptPosition.SUPERSCRIPT, "derived", evidence, None
        if -evidence.baseline_offset >= DERIVED_MIN_SUB_SHIFT * base_run.size:
            return ScriptPosition.SUBSCRIPT, "derived", evidence, None
    return ScriptPosition.BASE, None, None, None


def base_for(
    run: ObservedRun,
    tokens: Sequence[FormulaToken],
    runs: Mapping[str, ObservedRun],
) -> FormulaToken | None:
    """The rightmost base token a script run sits next to, or None when nothing is adjacent."""
    best: FormulaToken | None = None
    for token in tokens:
        if token.script is not ScriptPosition.BASE:
            continue
        base_run = runs[token.source_span_id]
        gap = run.bbox[0] - token.bbox[2]
        if not -0.2 * run.size <= gap <= ADJACENCY_GAP * base_run.size:
            continue
        if abs(base_run.origin[1] - run.origin[1]) > 2 * base_run.size:
            continue
        if best is None or token.bbox[2] > best.bbox[2]:
            best = token
    return best


def horizontal_rules(paths: Sequence[ObservedPath]) -> tuple[HorizontalRule, ...]:
    """(path, y, x0, x1) for every stroked single 'l' with |dy| <= RULE_MAX_SLOPE and
    width <= RULE_MAX_WIDTH, plus a single 're' whose height <= RULE_MAX_WIDTH (y = mid)."""
    rules: list[HorizontalRule] = []
    for path in paths:
        if len(path.items) != 1:
            continue
        kind, points = path.items[0]
        if len(points) != 2:
            continue
        (x0, y0), (x1, y1) = points
        if kind == "l" and path.paint in {"s", "fs"} and path.width <= RULE_MAX_WIDTH:
            if abs(y0 - y1) <= RULE_MAX_SLOPE:
                rules.append((path, (y0 + y1) / 2, min(x0, x1), max(x0, x1)))
        elif kind == "re" and abs(y1 - y0) <= RULE_MAX_WIDTH:
            rules.append((path, (y0 + y1) / 2, min(x0, x1), max(x0, x1)))
    return tuple(rules)


def _over_x(token: FormulaToken, x0: float, x1: float) -> bool:
    center = (token.bbox[0] + token.bbox[2]) / 2
    return x0 - X_OVERLAP_TOLERANCE <= center <= x1 + X_OVERLAP_TOLERANCE


def _below_rule(tokens: Sequence[FormulaToken], y: float, x0: float, x1: float) -> tuple[int, ...]:
    return tuple(
        token.index
        for token in tokens
        if _over_x(token, x0, x1)
        and token.bbox[1] >= y - RULE_MAX_SLOPE
        and token.bbox[1] - y <= VERTICAL_REACH * size_of(token)
    )


def _rule_evidence(path: ObservedPath) -> PathEvidence:
    kind, points = path.items[0]
    return PathEvidence(path.path_index, "line" if kind == "l" else "rect", points, path.width)


def fraction_of(
    rule: HorizontalRule, tokens: Sequence[FormulaToken]
) -> tuple[FormulaStructure | None, str | None]:
    """A drawn horizontal rule with tokens on both sides is a fraction; anything else is withheld."""
    path, y, x0, x1 = rule
    over = [token for token in tokens if _over_x(token, x0, x1)]
    above = tuple(
        token.index
        for token in over
        if token.bbox[3] <= y + RULE_MAX_SLOPE
        and y - token.bbox[3] <= VERTICAL_REACH * size_of(token)
    )
    below = _below_rule(over, y, x0, x1)
    straddle = any(token.bbox[1] < y < token.bbox[3] for token in over)
    if straddle or not above or not below:
        return None, f"formula_fraction_line_unpaired:{path.path_index}"
    return FormulaStructure(StructureKind.FRACTION, _rule_evidence(path), above, below), None


def sqrt_from_glyph(
    radical: FormulaToken,
    rules: Sequence[HorizontalRule],
    tokens: Sequence[FormulaToken],
) -> tuple[FormulaStructure | None, str | None]:
    """A `√` glyph is a root only when a drawn overline starts at its top-right corner."""
    for path, y, x0, x1 in rules:
        if abs(x0 - radical.bbox[2]) > 2.0 or abs(y - radical.bbox[1]) > 2.0:
            continue
        radicand = tuple(
            index for index in _below_rule(tokens, y, x0, x1) if index != radical.index
        )
        if not radicand:
            break
        return (
            FormulaStructure(StructureKind.SQRT, _rule_evidence(path), radicand, (), radical.index),
            None,
        )
    return None, f"formula_radical_without_overline:{radical.source_span_id}"


def sqrt_from_path(
    path: ObservedPath, tokens: Sequence[FormulaToken]
) -> tuple[FormulaStructure | None, str | None]:
    """A stroked 3-4 segment polyline whose longest, last segment is horizontal is a drawn root."""
    if path.paint != "s" or not 3 <= len(path.items) <= 4:
        return None, None
    if any(kind != "l" or len(points) != 2 for kind, points in path.items):
        return None, None
    segments = [(points[0], points[1]) for _, points in path.items]
    lengths = [hypot(end[0] - start[0], end[1] - start[1]) for start, end in segments]
    (last_start, last_end) = segments[-1]
    if abs(last_start[1] - last_end[1]) > RULE_MAX_SLOPE or lengths[-1] < max(lengths):
        return None, None
    y = (last_start[1] + last_end[1]) / 2
    x0, x1 = min(last_start[0], last_end[0]), max(last_start[0], last_end[0])
    radicand = _below_rule(tokens, y, x0, x1)
    if not radicand:
        return None, None
    evidence = PathEvidence(
        path.path_index,
        "polyline",
        tuple(point for _, points in path.items for point in points),
        path.width,
    )
    return FormulaStructure(StructureKind.SQRT, evidence, radicand), None


def _consumed_indices(structures: Sequence[FormulaStructure]) -> set[int]:
    consumed: set[int] = set()
    for structure in structures:
        consumed.update(structure.first)
        consumed.update(structure.second)
        if structure.radical_token_index is not None:
            consumed.add(structure.radical_token_index)
    return consumed


def _scripts_by_base(
    tokens: Sequence[FormulaToken],
) -> dict[int, tuple[FormulaToken, ...]]:
    grouped: dict[int, list[FormulaToken]] = {}
    for token in tokens:
        if token.base_token_index is not None:
            grouped.setdefault(token.base_token_index, []).append(token)
    return {
        index: tuple(sorted(group, key=lambda token: token.bbox[0]))
        for index, group in grouped.items()
    }


def _base_members(
    indices: Sequence[int], tokens: Sequence[FormulaToken]
) -> tuple[FormulaToken, ...]:
    members = [tokens[index] for index in indices if tokens[index].script is ScriptPosition.BASE]
    return tuple(sorted(members, key=lambda token: token.bbox[0]))


def _structure_x(structure: FormulaStructure) -> float:
    return min(point[0] for point in structure.path.points)


def _latex_token(token: FormulaToken, scripts: Mapping[int, tuple[FormulaToken, ...]]) -> str:
    text = token.text
    attached = scripts.get(token.index, ())
    subs = [script for script in attached if script.script is ScriptPosition.SUBSCRIPT]
    sups = [script for script in attached if script.script is ScriptPosition.SUPERSCRIPT]
    if subs:
        text += "_{" + _LATEX_JOIN.join(script.text for script in subs) + "}"
    if sups:
        text += "^{" + _LATEX_JOIN.join(script.text for script in sups) + "}"
    return text


def _latex_group(
    indices: Sequence[int],
    tokens: Sequence[FormulaToken],
    scripts: Mapping[int, tuple[FormulaToken, ...]],
) -> str:
    return _LATEX_JOIN.join(
        _latex_token(token, scripts) for token in _base_members(indices, tokens)
    )


def linearize(tokens: Sequence[FormulaToken], structures: Sequence[FormulaStructure]) -> str:
    """LaTeX-style subset: top-level items in x order; scripts as ^{…} / _{…}; \\frac{…}{…}; \\sqrt{…}.
    Symbols stay verbatim Unicode (no \\alpha, no \\times); words inside one group join with '\\ '."""
    scripts = _scripts_by_base(tokens)
    consumed = _consumed_indices(structures)
    items: list[tuple[float, str]] = [
        (token.bbox[0], _latex_token(token, scripts))
        for token in tokens
        if token.index not in consumed and token.script is ScriptPosition.BASE
    ]
    for structure in structures:
        if structure.kind is StructureKind.FRACTION:
            body = (
                "\\frac{"
                + _latex_group(structure.first, tokens, scripts)
                + "}{"
                + _latex_group(structure.second, tokens, scripts)
                + "}"
            )
        else:
            body = "\\sqrt{" + _latex_group(structure.first, tokens, scripts) + "}"
        items.append((_structure_x(structure), body))
    items.sort(key=lambda item: item[0])
    return " ".join(text for _, text in items)


def _word_of(token: FormulaToken) -> str:
    if token.role is TokenRole.RELATION:
        return _RELATION_WORDS.get(token.text, token.text)
    if token.role is TokenRole.OPERATOR:
        return _OPERATOR_WORDS.get(token.text, token.text)
    return token.text


def _readable_token(token: FormulaToken, scripts: Mapping[int, tuple[FormulaToken, ...]]) -> str:
    text = _word_of(token)
    attached = scripts.get(token.index, ())
    subs = [script for script in attached if script.script is ScriptPosition.SUBSCRIPT]
    sups = [script for script in attached if script.script is ScriptPosition.SUPERSCRIPT]
    if subs:
        text = f"{text} 下标 " + " ".join(_word_of(script) for script in subs)
    if sups:
        body = " ".join(_word_of(script) for script in sups)
        if all(script.script_proof == "text_rise" for script in sups):
            text = f"{text} 的 {body} 次方"
        else:
            text = f"{text} 上标 {body}"
    return text


def _readable_group(
    indices: Sequence[int],
    tokens: Sequence[FormulaToken],
    scripts: Mapping[int, tuple[FormulaToken, ...]],
) -> str:
    members = _base_members(indices, tokens)
    text = " ".join(_readable_token(token, scripts) for token in members)
    if any(token.role in {TokenRole.OPERATOR, TokenRole.RELATION} for token in members):
        return f"({text})"
    return text


def readable_text(tokens: Sequence[FormulaToken], structures: Sequence[FormulaStructure]) -> str:
    """Same tree as linearize, spoken with a fixed connective table; symbols stay verbatim."""
    scripts = _scripts_by_base(tokens)
    consumed = _consumed_indices(structures)
    items: list[tuple[float, str]] = [
        (token.bbox[0], _readable_token(token, scripts))
        for token in tokens
        if token.index not in consumed and token.script is ScriptPosition.BASE
    ]
    for structure in structures:
        if structure.kind is StructureKind.FRACTION:
            body = (
                _readable_group(structure.first, tokens, scripts)
                + " 除以 "
                + _readable_group(structure.second, tokens, scripts)
            )
        else:
            body = _readable_group(structure.first, tokens, scripts) + " 的平方根"
        items.append((_structure_x(structure), body))
    items.sort(key=lambda item: item[0])
    return " ".join(text for _, text in items)


@dataclass(frozen=True, slots=True)
class FormulaCheck:
    ir: FormulaIR | None
    diagnostics: tuple[str, ...]


def _char_bounds(run: ObservedRun, start: int, end: int) -> Bounds:
    boxes = [char.bbox for char in run.chars[start:end]]
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _tokens_of(
    observation: FormulaSourceObservation, diagnostics: list[str]
) -> tuple[list[FormulaToken], dict[int, ObservedRun]]:
    runs_by_span = {run.span_id: run for run in observation.runs}
    tokens: list[FormulaToken] = []
    run_of_token: dict[int, ObservedRun] = {}
    for run in observation.runs:
        if run.direction != (1.0, 0.0):
            diagnostics.append(f"formula_span_transform_unsupported:{run.span_id}")
            continue
        if len(run.chars) != len(run.text):
            diagnostics.append(f"formula_char_count_mismatch:{run.span_id}")
            continue
        pieces = tile_run(run.text)
        failure = check_tiling(run.text, [(start, end) for start, end, _ in pieces])
        if failure is not None:
            diagnostics.append(failure)
            continue
        if not pieces:
            continue
        base = base_for(run, tokens, runs_by_span)
        base_run = runs_by_span[base.source_span_id] if base is not None else None
        position, proof, evidence, refusal = script_of(run, base, base_run, observation.page_height)
        if refusal is not None:
            diagnostics.append(refusal)
            continue
        is_base = position is ScriptPosition.BASE
        for start, end, role in pieces:
            token = FormulaToken(
                len(tokens),
                run.text[start:end],
                run.span_id,
                start,
                end,
                _char_bounds(run, start, end),
                role,
                position,
                None if is_base else proof,
                None if is_base or base is None else base.index,
                None if is_base else evidence,
            )
            run_of_token[token.index] = run
            tokens.append(token)
    return tokens, run_of_token


def _structures_of(
    observation: FormulaSourceObservation,
    tokens: Sequence[FormulaToken],
    diagnostics: list[str],
) -> list[FormulaStructure]:
    rules = horizontal_rules(observation.paths)
    ruled = {rule[0].path_index for rule in rules}
    structures: list[FormulaStructure] = []
    addressed: set[int] = set()
    for token in tokens:
        if token.role is not TokenRole.RADICAL or token.script is not ScriptPosition.BASE:
            continue
        available = [rule for rule in rules if rule[0].path_index not in addressed]
        structure, refusal = sqrt_from_glyph(token, available, tokens)
        if structure is None:
            if refusal is not None:
                diagnostics.append(refusal)
            continue
        structures.append(structure)
        addressed.add(structure.path.path_index)
    for path in observation.paths:
        if path.path_index in addressed or path.path_index in ruled:
            continue
        structure, _ = sqrt_from_path(path, tokens)
        if structure is not None:
            structures.append(structure)
            addressed.add(path.path_index)
    for rule in rules:
        if rule[0].path_index in addressed:
            continue
        structure, refusal = fraction_of(rule, tokens)
        addressed.add(rule[0].path_index)
        if structure is None:
            if refusal is not None:
                diagnostics.append(refusal)
            continue
        structures.append(structure)
    for path in observation.paths:
        if path.path_index not in addressed:
            diagnostics.append(f"formula_unexplained_path:{path.path_index}")
    structures.sort(key=lambda structure: structure.path.path_index)
    return structures


def check_formula(
    observation: FormulaSourceObservation, *, object_id: str, anchor: SourceAnchor
) -> FormulaCheck:
    """Pure, deterministic, replayable. Tokens ← runs (tiling + char bboxes); scripts ← rise / derived;
    structures ← rules; then every path must be consumed and every base token on one baseline."""
    diagnostics: list[str] = []
    tokens, run_of_token = _tokens_of(observation, diagnostics)
    if not tokens:
        diagnostics.append("formula_no_tokens")
        return FormulaCheck(None, tuple(diagnostics))
    structures = _structures_of(observation, tokens, diagnostics)
    reported: set[int] = set()
    seen: set[int] = set()
    for structure in structures:
        for index in (*structure.first, *structure.second):
            if index in seen and index not in reported:
                diagnostics.append(f"formula_token_in_two_structures:{index}")
                reported.add(index)
            seen.add(index)
    consumed = _consumed_indices(structures)
    loose = [
        token
        for token in tokens
        if token.index not in consumed and token.script is ScriptPosition.BASE
    ]
    if loose:
        origins = [run_of_token[token.index].origin[1] for token in loose]
        if max(origins) - min(origins) > BASELINE_CLUSTER * max(size_of(t) for t in loose):
            diagnostics.append("formula_multiline_unsupported")
    if diagnostics:
        return FormulaCheck(None, tuple(diagnostics))
    level: ProofLevel = (
        "literal" if any(token.script_proof == "derived" for token in tokens) else "full"
    )
    return FormulaCheck(
        FormulaIR(
            object_id,
            anchor,
            "\n".join(run.text for run in observation.runs),
            None,
            tuple(dict.fromkeys(token.source_span_id for token in tokens)),
            (_IR_DIAGNOSTIC, f"proof_level={level}"),
            Verification.VERIFIED if level == "full" else Verification.PENDING,
            tuple(tokens),
            tuple(structures),
            linearize(tokens, structures),
            readable_text(tokens, structures),
            level,
        ),
        (),
    )

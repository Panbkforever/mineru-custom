"""
Extract pin/package fields from MinerU middle_json tables.

The extractor does not copy full tables. It first classifies table columns by
pin/package semantics, keeps only relevant columns, and groups extracted rows by
package and table/group title.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from extract.table_association_rules import (
    PackageSnapshot,
    resolve_table_package_association,
)


TR_RE = re.compile(r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE)
CELL_RE = re.compile(
    r"(<t[dh][^>]*>)(.*?)(</t[dh]>)",
    re.DOTALL | re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
BALL_TOKEN_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}\b")
NUMERIC_PIN_RE = re.compile(r"^\d{1,4}$")
PACKAGE_HEADER_RE = re.compile(
    r"\b(?:\d{2,4}\s*)?(?:qfp|lqfp|tqfp|htqfp|vqfn|vssop|ssop|tssop|qfn|"
    r"bga|pbga|nfbga|fcbga|fc csp|soic|sop|pdip|pga|clcc|lccc|dfn|wqfn|"
    r"pm|pt|pn|pnp|pzp|pz|peu|rgc|rtd|rgz|rha|rhb|rsh|dgs\d*|da|pw|n|rsa|"
    r"zce\d*[a-z]*|nzn\d*[a-z]*|zwt|zjz|zhh|zay|alv|alx|am[a-z]|"
    r"abc|alw|alz|anf|anj|zqw|pwp|rgy|rsm|rge|rte)\b",
    re.IGNORECASE,
)
PACKAGE_FAMILY_RE = re.compile(
    r"\b(qfp|lqfp|tqfp|htqfp|vqfn|vssop|ssop|tssop|qfn|bga|pbga|nfbga|"
    r"fcbga|soic|sop|pdip|pga|clcc|lccc|dfn|wqfn)\b",
    re.IGNORECASE,
)
PACKAGE_CODE_RE = re.compile(
    r"\b(pm|pt|pn|pnp|pzp|pz|peu|rgc|rtd|rgz|rha|rhb|rsh|dgs\d*|da|pw|n|rsa|"
    r"zce\d*[a-z]*|nzn\d*[a-z]*|zwt|zjz|zhh|zay|alv|alx|am[a-z]|abc|alw|"
    r"alz|anf|anj|zqw|pwp|rgy|rsm|rge|rte)\b",
    re.IGNORECASE,
)
PIN_FIELD_ORDER = [
    "pin_no",
    "pin_name",
    "ball_no",
    "ball_name",
    "signal_name",
    "terminal_no",
    "terminal_name",
    "pad_name",
    "type",
    "io_type",
    "package",
]

IGNORE_HEADER_KEYWORDS = {
    "description",
    "test condition",
    "condition",
    "min",
    "typ",
    "nom",
    "max",
    "unit",
    "voltage",
    "reset",
    "pull",
    "power",
    "hys",
    "ret",
    "note",
    "comment",
    "address",
    "register",
}

ORDERING_TABLE_KEYWORDS = {
    "orderable device",
    "device",
    "status",
    "package type",
    "package drawing",
    "package qty",
    "eco plan",
    "lead finish",
    "msl peak temp",
    "samples",
}


@dataclass
class TableCandidate:
    html: str
    page_idx: int | None
    title: str = ""


@dataclass
class ColumnDecision:
    index: int
    raw_header: str
    field_name: str
    score: int


@dataclass
class ExtractedGroup:
    group: str
    pin_list: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PackageIdentity:
    display: str
    key: str
    pin_count: str = ""
    family: str = ""
    code: str = ""


def extract_pin_package_info_from_middle_json(
    middle_json: dict[str, Any],
    source_name: str = "",
    include_debug: bool = False,
    use_semantic_classifier: bool = False,
) -> list[dict[str, Any]]:
    """Extract package/group/pin records from one MinerU middle_json object."""
    packages: dict[str, dict[str, Any]] = {}

    for table in iter_table_candidates(middle_json):
        rows = parse_html_table(table.html)
        if len(rows) < 2:
            continue

        header_index, headers = choose_header_row(rows)
        if header_index < 0 and use_semantic_classifier:
            header_index, headers = choose_loose_header_row(rows, table.title)
        if header_index < 0:
            continue

        if is_ordering_table(headers):
            continue

        decisions = classify_columns(headers, rows[header_index + 1:], table.title)
        association = resolve_table_package_association(
            table.title,
            headers,
            rows[header_index + 1:],
            decisions,
            build_package_snapshots(packages),
        )
        if use_semantic_classifier:
            if not is_pin_package_table(decisions) and not is_semantic_candidate_table(
                table.title,
                headers,
                rows[header_index + 1:],
                decisions,
            ):
                continue
            semantic_decision = classify_semantic_table(
                table.title,
                headers,
                rows[header_index + 1:],
                decisions,
                include_debug=include_debug,
            )
            if not semantic_decision_allows_pin_creation(
                semantic_decision,
                decisions,
                table.title,
                has_associated_package=bool(association.package),
            ):
                continue
            decisions = merge_semantic_column_decisions(
                decisions,
                semantic_decision,
                headers,
            )
            decisions = keep_primary_type_decision(decisions, rows[header_index + 1:])
        elif not is_pin_package_table(decisions):
            continue

        if not is_pin_package_table(decisions):
            continue

        default_pkg = association.package or infer_package_name(table.title)
        group_name = infer_group_name(table.title) or "Pin/Package Table"
        current_group_name = group_name

        for row in rows[header_index + 1:]:
            if is_group_row(row):
                group_text = first_non_empty(row)
                if group_text:
                    current_group_name = group_text
                continue

            pin_records = extract_pin_records_from_row(row, decisions)
            for pin_record in pin_records:
                record_pkg = pin_record.pop("_pkg", default_pkg)
                raw_fields = pin_record.pop("_raw_fields", None)
                if include_debug and raw_fields:
                    pin_record["raw_fields"] = raw_fields
                package_identity = build_package_identity(record_pkg)
                package_bucket = get_package_bucket(
                    packages,
                    package_identity,
                )
                current_group = get_or_create_group(package_bucket, current_group_name)
                if include_debug and source_name:
                    pin_record.setdefault("source", source_name)
                if include_debug and table.page_idx is not None:
                    pin_record.setdefault("source_page", table.page_idx + 1)
                add_pin_record_to_group(current_group, pin_record)

    result = []
    for package_bucket in packages.values():
        groups = [
            {"group": group.group, "pin_list": group.pin_list}
            for group in package_bucket["_groups"].values()
            if group.pin_list
        ]
        if groups:
            package_result = {"pkg": package_bucket["pkg"], "group_list": groups}
            if include_debug:
                package_result["pkg_key"] = package_bucket["pkg_key"]
            result.append(package_result)
    return result


def extract_pin_package_info_from_middle_json_file(
    path: str | Path,
    use_semantic_classifier: bool = False,
    include_debug: bool = False,
) -> list[dict[str, Any]]:
    path = Path(path)
    middle_json = json.loads(path.read_text(encoding="utf-8"))
    return extract_pin_package_info_from_middle_json(
        middle_json,
        source_name=path.stem,
        use_semantic_classifier=use_semantic_classifier,
        include_debug=include_debug,
    )


def iter_table_candidates(middle_json: dict[str, Any]) -> list[TableCandidate]:
    """Return HTML tables with nearby text as weak title context."""
    candidates: list[TableCandidate] = []
    current_section_title = ""
    for page_info in middle_json.get("pdf_info", []):
        page_idx = page_info.get("page_idx")
        recent_texts: list[str] = []
        for span in iter_spans_in_reading_order(page_info):
            html = span.get("html")
            text = plain_text(span.get("content") or span.get("text") or "")
            if isinstance(html, str) and "<table" in html.lower():
                title = current_section_title or " ".join(dedupe_preserve_order(recent_texts[-5:])).strip()
                candidates.append(
                    TableCandidate(
                        html=html,
                        page_idx=page_idx if isinstance(page_idx, int) else None,
                        title=title,
                    )
                )
                continue
            if text:
                detected_group = infer_group_name(text)
                if detected_group:
                    current_section_title = detected_group
                recent_texts.append(text)
                recent_texts = recent_texts[-10:]
    return candidates


def dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped = []
    for value in values:
        value = value.strip()
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def iter_spans_in_reading_order(value: Any):
    if isinstance(value, dict):
        if "spans" in value and isinstance(value["spans"], list):
            for span in value["spans"]:
                if isinstance(span, dict):
                    yield span
        for key in ("para_blocks", "blocks", "lines", "spans"):
            child = value.get(key)
            if isinstance(child, list):
                for item in child:
                    yield from iter_spans_in_reading_order(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_spans_in_reading_order(item)


def parse_html_table(table_html: str) -> list[list[str]]:
    rows = []
    for tr_match in TR_RE.finditer(table_html):
        row = []
        for cell_match in CELL_RE.finditer(tr_match.group(2)):
            row.append(plain_text(cell_match.group(2)))
        if row:
            rows.append(row)
    return rows


def choose_header_row(rows: list[list[str]]) -> tuple[int, list[str]]:
    """Choose the row with the strongest pin/package header evidence."""
    best_index = -1
    best_score = 0
    best_headers: list[str] = []
    for index, row in enumerate(rows[:4]):
        headers = build_combined_headers(rows, index)
        score = sum(classify_header(cell)[1] for cell in headers)
        if is_ordering_table(headers):
            score = 0
        if score > best_score:
            best_index = index
            best_score = score
            best_headers = headers
    if best_score < 4:
        return -1, []
    return best_index, best_headers


def choose_loose_header_row(rows: list[list[str]], title: str = "") -> tuple[int, list[str]]:
    """Choose a possible header row for semantic classification."""
    best_index = -1
    best_score = 0
    best_headers: list[str] = []
    for index, _row in enumerate(rows[:4]):
        headers = build_combined_headers(rows, index)
        score = score_loose_table_evidence(title, headers, rows[index + 1 : index + 6])
        if is_ordering_table(headers):
            score -= 4
        if score > best_score:
            best_index = index
            best_score = score
            best_headers = headers
    if best_score < 3:
        return -1, []
    return best_index, best_headers


def build_combined_headers(rows: list[list[str]], header_index: int) -> list[str]:
    """Merge stacked header rows so Chinese multi-row package headers stay visible."""
    max_columns = max((len(row) for row in rows[: header_index + 1]), default=0)
    headers = []
    for column_index in range(max_columns):
        parts = []
        for row in rows[: header_index + 1]:
            if column_index < len(row) and row[column_index].strip():
                value = row[column_index].strip()
                if value not in parts:
                    parts.append(value)
        headers.append(" ".join(parts).strip())
    return headers


def classify_columns(
    headers: list[str],
    data_rows: list[list[str]],
    title_context: str = "",
) -> list[ColumnDecision]:
    decisions = []
    max_columns = max([len(headers), *(len(row) for row in data_rows[:20])] or [0])
    for index in range(max_columns):
        header = headers[index] if index < len(headers) else ""
        column_values = [row[index] for row in data_rows[:30] if index < len(row)]
        package_score = score_package_column(header, column_values, title_context)
        if package_score >= 6:
            decisions.append(
                ColumnDecision(
                    index=index,
                    raw_header=header,
                    field_name="package_pin_no",
                    score=package_score,
                )
            )
            continue
        field_name, header_score = classify_header(header)
        if is_value_inference_blocked_header(header):
            value_field, value_score = "", 0
        else:
            value_field, value_score = classify_values(column_values)
        if value_score > header_score and field_name == "":
            field_name = value_field
        score = header_score + value_score
        if field_name and score > 0:
            decisions.append(
                ColumnDecision(
                    index=index,
                    raw_header=header,
                    field_name=field_name,
                    score=score,
                )
            )
    return keep_primary_type_decision(decisions, data_rows)


def keep_primary_type_decision(
    decisions: list[ColumnDecision],
    data_rows: list[list[str]],
) -> list[ColumnDecision]:
    """Keep only the most relevant type-like column.

    Some tables contain both SIGNAL TYPE and BUFFER TYPE. The extractor should
    output one `type` field, so we choose the column most likely to describe the
    pin/signal itself by header meaning, value shape, and left-to-right order.
    """
    type_decisions = [
        decision
        for decision in decisions
        if normalize_field_name(decision.field_name) == "type"
    ]
    if len(type_decisions) <= 1:
        return decisions

    best_type = max(
        type_decisions,
        key=lambda decision: score_type_column_relevance(decision, data_rows),
    )
    return [
        decision
        for decision in decisions
        if normalize_field_name(decision.field_name) != "type" or decision.index == best_type.index
    ]


def score_type_column_relevance(
    decision: ColumnDecision,
    data_rows: list[list[str]],
) -> tuple[int, int]:
    normalized_header = normalize_header(decision.raw_header)
    score = decision.score

    if any(keyword in normalized_header for keyword in ("signal type", "pin type", "terminal type", "io type", "i o type", "i/o type")):
        score += 20
    if normalized_header in {"type", "i/o", "io", "i o"}:
        score += 10
    if any(keyword in normalized_header for keyword in ("buffer type", "buffer", "reset", "state", "power source", "supply", "source")):
        score -= 15

    values = [
        row[decision.index]
        for row in data_rows[:30]
        if decision.index < len(row) and row[decision.index].strip()
    ]
    score += score_type_column_values(values)
    # Earlier columns are preferred when semantic evidence is otherwise close.
    return score, -decision.index


def score_type_column_values(values: list[str]) -> int:
    if not values:
        return 0
    sample = [normalize_header(value).replace(" ", "") for value in values[:20]]
    signal_type_tokens = {"i", "o", "io", "ioz", "i/o", "i/o/z", "oz", "odz", "od", "p", "ipu", "ipd"}
    buffer_type_tokens = {"analog", "digital", "power", "ground", "gnd", "supply", "buffer"}
    signal_hits = sum(1 for value in sample if value in signal_type_tokens)
    buffer_hits = sum(1 for value in sample if value in buffer_type_tokens)
    return signal_hits * 3 - buffer_hits


def classify_header(header: str) -> tuple[str, int]:
    normalized = normalize_header(header)
    if not normalized:
        return "", 0

    if is_package_pin_header(normalized):
        return "package_pin_no", 6

    if any(keyword in normalized for keyword in ("引脚编号", "管脚编号", "端子编号", "pin 编号")):
        return "pin_no", 5
    if any(keyword in normalized for keyword in ("引脚名称", "管脚名称", "端子名称", "引脚名")):
        return "pin_name", 5
    if "信号名称" in normalized or normalized == "信号":
        return "pin_name", 5
    if any(keyword in normalized for keyword in ("引脚类型", "信号类型", "管脚类型", "端子类型", "io 结构", "i o 结构")):
        return "type", 4

    if has_ignored_header_keyword(normalized):
        if not any(keyword in normalized for keyword in ("signal type", "pin type", "io type")):
            return "", 0

    if "ball num" in normalized or "ball number" in normalized:
        return "pin_no", 5
    if normalized in {"no", "no.", "number"} or "pin no" in normalized:
        return "pin_no", 4
    if "terminal no" in normalized or "terminal number" in normalized:
        return "pin_no", 4
    if "pin number" in normalized:
        return "pin_no", 4

    if "signal name" in normalized:
        return "pin_name", 5
    if "ball name" in normalized:
        return "ball_name", 5
    if "pin name" in normalized:
        return "pin_name", 4
    if "terminal name" in normalized:
        return "pin_name", 4
    if normalized in {"signal", "name", "function name"}:
        return "pin_name", 3
    if "pad name" in normalized:
        return "pad_name", 3

    if "signal type" in normalized or "pin type" in normalized:
        return "type", 4
    if normalized == "type" or normalized.endswith(" type"):
        return "type", 3
    if "i/o" in normalized or normalized == "io" or normalized.startswith("i o"):
        return "io_type", 3

    if "package" in normalized and "pin" not in normalized:
        return "package", 3

    return "", 0


def is_value_inference_blocked_header(header: str) -> bool:
    normalized = normalize_header(header)
    if not normalized:
        return False
    if has_ignored_header_keyword(normalized):
        return not any(keyword in normalized for keyword in ("signal type", "pin type", "io type"))
    return False


def has_ignored_header_keyword(normalized_header: str) -> bool:
    """Avoid substring mistakes such as matching MIN inside TERMINAL."""
    for keyword in IGNORE_HEADER_KEYWORDS:
        if keyword in {"min", "typ", "nom", "max", "unit", "ret"}:
            if re.search(rf"\b{re.escape(keyword)}\b", normalized_header):
                return True
            continue
        if keyword in normalized_header:
            return True
    return False


def classify_values(values: list[str]) -> tuple[str, int]:
    values = [value for value in values if value]
    if not values:
        return "", 0

    sample = values[:20]
    pin_like = sum(looks_like_pin_list(value) for value in sample)
    signal_like = sum(looks_like_signal_name(value) for value in sample)
    type_like = sum(looks_like_type_value(value) for value in sample)

    threshold = max(2, len(sample) // 3)
    if pin_like >= threshold:
        return "pin_no", 3
    if type_like >= threshold:
        return "type", 2
    if signal_like >= threshold:
        return "pin_name", 1
    return "", 0


def is_pin_package_table(decisions: list[ColumnDecision]) -> bool:
    fields = {decision.field_name for decision in decisions}
    has_number = bool(fields & {"pin_no", "ball_no", "terminal_no", "package_pin_no"})
    has_name = bool(fields & {"pin_name", "ball_name", "signal_name", "terminal_name", "pad_name"})
    has_explicit_number_header = any(
        decision.field_name in {"pin_no", "ball_no", "terminal_no", "package_pin_no"}
        and classify_header(decision.raw_header)[1] >= 4
        for decision in decisions
    )
    return has_number and has_name and has_explicit_number_header


def classify_semantic_table(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
    decisions: list[ColumnDecision],
    include_debug: bool = False,
) -> dict[str, Any]:
    from extract.semantic_classifier import classify_table_semantics

    try:
        decision = classify_table_semantics(
            title=title,
            headers=headers,
            sample_rows=data_rows[:8],
            decisions=decisions,
        )
    except Exception as exc:
        print(f"语义分类失败，跳过当前表格: {exc}")
        return {}
    if include_debug:
        print(f"semantic decision: {decision}")
    return decision


def semantic_decision_allows_pin_creation(
    semantic_decision: dict[str, Any],
    decisions: list[ColumnDecision],
    title: str,
    has_associated_package: bool = False,
) -> bool:
    if not semantic_decision:
        return False
    if (
        not any(decision.field_name == "package_pin_no" for decision in decisions)
        and not infer_package_name(title)
        and not has_associated_package
        and not semantic_decision.get("package_columns")
    ):
        return False
    return bool(semantic_decision.get("should_create_pins")) and float(semantic_decision.get("confidence", 0)) >= 0.6


def is_semantic_candidate_table(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
    decisions: list[ColumnDecision],
) -> bool:
    """Loose recall before DeepSeek; final extraction still needs strict checks."""
    if is_ordering_table(headers):
        return False
    if is_pin_package_table(decisions):
        return True
    return score_loose_table_evidence(title, headers, data_rows[:5]) >= 3 and has_loose_pin_mapping_shape(title, headers)


def score_loose_table_evidence(
    title: str,
    headers: list[str],
    sample_rows: list[list[str]],
) -> int:
    title_text = normalize_header(title)
    header_text = normalize_header(" ".join(headers))
    text = " ".join([title_text, header_text]).strip()
    score = 0
    if any(keyword in header_text for keyword in ("pin", "ball", "terminal", "signal", "package", "引脚", "端子", "封装", "信号")):
        score += 2
    if any(keyword in title_text for keyword in ("pin attributes", "signal descriptions", "terminal functions", "connectivity requirements")):
        score += 1
    if any(keyword in header_text for keyword in ("pin attributes", "signal descriptions", "terminal functions", "connectivity requirements")):
        score += 2
    if "i/o" in header_text or "i o" in header_text or "type" in header_text:
        score += 1
    if PACKAGE_HEADER_RE.search(header_text):
        score += 1
    sample_text = " ".join(" ".join(row[:8]) for row in sample_rows)
    if BALL_TOKEN_RE.search(sample_text):
        score += 1
    if has_ignored_header_keyword(header_text) and not any(keyword in header_text for keyword in ("pin", "ball", "terminal", "signal", "package")):
        score -= 2
    return score


def has_loose_pin_mapping_shape(title: str, headers: list[str]) -> bool:
    """Require mapping-like columns before spending an LLM call."""
    title_text = normalize_header(title)
    header_text = normalize_header(" ".join(headers))
    has_number_column = any(
        keyword in header_text
        for keyword in (
            "pin no",
            "pin number",
            "ball number",
            "ball no",
            "terminal no",
            "terminal number",
            "引脚编号",
            "端子编号",
        )
    )
    has_name_column = any(
        keyword in header_text
        for keyword in (
            "pin name",
            "ball name",
            "terminal name",
            "signal name",
            "device signal",
            "引脚名称",
            "端子名称",
            "信号名称",
        )
    )
    if has_number_column and has_name_column:
        return True
    if "connectivity requirements" in title_text and has_number_column:
        return True
    if "package" in title_text and has_number_column and any(keyword in header_text for keyword in ("name", "signal")):
        return True
    return False


def merge_semantic_column_decisions(
    rule_decisions: list[ColumnDecision],
    semantic_decision: dict[str, Any],
    headers: list[str],
) -> list[ColumnDecision]:
    semantic_decisions = build_semantic_column_decisions(semantic_decision, headers)
    if not semantic_decisions:
        return rule_decisions

    merged_by_index: dict[int, ColumnDecision] = {decision.index: decision for decision in rule_decisions}
    for decision in semantic_decisions:
        existing = merged_by_index.get(decision.index)
        if not existing or semantic_field_priority(decision.field_name) >= semantic_field_priority(existing.field_name):
            merged_by_index[decision.index] = decision
    return sorted(merged_by_index.values(), key=lambda decision: decision.index)


def build_semantic_column_decisions(
    semantic_decision: dict[str, Any],
    headers: list[str],
) -> list[ColumnDecision]:
    decisions: list[ColumnDecision] = []
    for item in semantic_decision.get("package_columns") or []:
        index = parse_column_index(item.get("column_index"))
        pkg = str(item.get("pkg") or "").strip()
        if index is None or is_generic_package_label(pkg):
            continue
        decisions.append(
            ColumnDecision(
                index=index,
                raw_header=pkg or safe_header(headers, index),
                field_name="package_pin_no",
                score=10,
            )
        )

    for item in semantic_decision.get("pin_columns") or []:
        index = parse_column_index(item.get("column_index"))
        if index is None:
            continue
        decisions.append(
            ColumnDecision(
                index=index,
                raw_header=safe_header(headers, index),
                field_name="pin_no",
                score=8,
            )
        )

    for item in semantic_decision.get("name_columns") or []:
        index = parse_column_index(item.get("column_index"))
        if index is None:
            continue
        role = str(item.get("field") or "pin_name").strip()
        decisions.append(
            ColumnDecision(
                index=index,
                raw_header=safe_header(headers, index),
                field_name=semantic_role_to_field(role, default="pin_name"),
                score=8,
            )
        )

    for item in semantic_decision.get("type_columns") or []:
        index = parse_column_index(item.get("column_index"))
        if index is None:
            continue
        decisions.append(
            ColumnDecision(
                index=index,
                raw_header=safe_header(headers, index),
                field_name="type",
                score=8,
            )
        )
    return decisions


def parse_column_index(value: Any) -> int | None:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def safe_header(headers: list[str], index: int) -> str:
    return headers[index] if 0 <= index < len(headers) else f"column_{index + 1}"


def semantic_role_to_field(role: str, default: str) -> str:
    role = normalize_header(role)
    if role in {"ball name", "signal name", "terminal name", "pin name"}:
        return role.replace(" ", "_")
    if role in {"pin_no", "pin no", "pin number", "ball number", "terminal number"}:
        return "pin_no"
    return default


def semantic_field_priority(field_name: str) -> int:
    return {
        "package_pin_no": 5,
        "pin_no": 4,
        "ball_no": 4,
        "terminal_no": 4,
        "pin_name": 3,
        "ball_name": 3,
        "signal_name": 3,
        "terminal_name": 3,
        "type": 2,
        "io_type": 2,
    }.get(field_name, 1)


def is_generic_package_label(value: str) -> bool:
    normalized = normalize_header(value)
    return normalized in {
        "",
        "pin",
        "pins",
        "pin no",
        "pin number",
        "ball number",
        "terminal number",
        "package",
        "package name",
    }


def score_package_column(header: str, values: list[str], title_context: str = "") -> int:
    """Score whether a column is a package-specific pin-number column."""
    normalized_header = normalize_header(header)
    if not normalized_header:
        return 0
    if any(keyword in normalized_header for keyword in ("orderable", "package qty", "package type", "package drawing")):
        return 0

    score = 0
    has_package_name = bool(PACKAGE_HEADER_RE.search(normalized_header))
    has_pin_header = any(
        keyword in normalized_header
        for keyword in (
            "引脚编号",
            "管脚编号",
            "端子编号",
            "pin number",
            "pin no",
            "ball number",
            "ball no",
            "terminal number",
            "terminal no",
        )
    )
    if has_package_name:
        score += 3
    if has_pin_header:
        score += 2
    if extract_package_identity_parts(header)["pin_count"]:
        score += 1
    if extract_package_identity_parts(header)["code"]:
        score += 1

    sample = [value for value in values if value.strip() and value.strip() not in {"-", "—", "NA", "N/A"}][:20]
    if sample:
        pin_like = sum(looks_like_pin_list(value) for value in sample)
        if pin_like / len(sample) >= 0.65:
            score += 3
        elif pin_like / len(sample) >= 0.35:
            score += 1

    context = normalize_header(title_context)
    if any(keyword in context for keyword in ("pin attributes", "terminal functions", "pin assignment", "引脚属性", "引脚分配")):
        score += 1

    if not has_package_name:
        return 0
    return score


def extract_pin_records_from_row(
    row: list[str],
    decisions: list[ColumnDecision],
) -> list[dict[str, Any]]:
    raw_fields: dict[str, str] = {}
    fields: dict[str, str] = {}
    package_pin_values: list[tuple[str, str]] = []
    has_package_pin_columns = any(decision.field_name == "package_pin_no" for decision in decisions)
    for decision in decisions:
        value = row[decision.index].strip() if decision.index < len(row) else ""
        if not value:
            continue
        raw_key = decision.raw_header or f"column_{decision.index + 1}"
        raw_fields[raw_key] = value
        if decision.field_name == "package_pin_no":
            package_pin_values.append((raw_key, value))
            continue
        field_name = normalize_field_name(decision.field_name)
        fields[field_name] = merge_field_value(fields.get(field_name, ""), value)

    if not fields:
        return []

    if package_pin_values:
        records = []
        for package_header, pin_no_value in package_pin_values:
            pin_numbers = split_pin_numbers(pin_no_value)
            if not pin_numbers:
                continue
            package_name = clean_package_label(package_header)
            for pin_no in pin_numbers:
                record = {key: fields[key] for key in PIN_FIELD_ORDER if key in fields}
                for key, value in fields.items():
                    if key not in record:
                        record[key] = value
                record["pin_no"] = pin_no
                record["_pkg"] = package_name
                if raw_fields:
                    record["_raw_fields"] = raw_fields
                records.append(record)
        return records

    # If the table has explicit package-specific pin columns, rows without a
    # value in those columns do not have a package owner. Do not fall back to a
    # generic pin-number column, otherwise long signal-description tables create
    # an extra pkg="" bucket from unrelated helper rows.
    if has_package_pin_columns:
        return []

    pin_no_value = fields.get("pin_no", "")
    if not pin_no_value:
        return []

    pin_numbers = split_pin_numbers(pin_no_value)
    if not pin_numbers:
        return []

    records = []
    for pin_no in pin_numbers:
        record = {key: fields[key] for key in PIN_FIELD_ORDER if key in fields}
        for key, value in fields.items():
            if key not in record:
                record[key] = value
        record["pin_no"] = pin_no
        if raw_fields:
            record["_raw_fields"] = raw_fields
        records.append(record)
    return records


def get_package_bucket(
    packages: dict[str, dict[str, Any]],
    identity: PackageIdentity,
) -> dict[str, Any]:
    """Return the bucket for the same package identity and append new aliases."""
    bucket_key = find_compatible_package_key(packages, identity) or identity.key
    bucket = packages.get(bucket_key)

    if not bucket:
        bucket = {
            "pkg": identity.display,
            "pkg_key": bucket_key,
            "group_list": [],
            "_groups": {},
            "_identity": identity,
            "_aliases": [],
        }
        packages[bucket_key] = bucket
    append_package_alias(bucket, identity.display)
    return bucket


def build_package_snapshots(packages: dict[str, dict[str, Any]]) -> list[PackageSnapshot]:
    """Collect known package pin/name evidence for later table association."""
    snapshots = []
    for package in packages.values():
        pin_numbers: set[str] = set()
        pin_names: set[str] = set()
        for group in package.get("_groups", {}).values():
            for record in group.pin_list:
                pin_no = str(record.get("pin_no", "")).strip()
                pin_name = str(record.get("pin_name") or record.get("ball_name") or "").strip()
                if pin_no:
                    pin_numbers.add(pin_no)
                if pin_name:
                    pin_names.add(pin_name.upper())
        snapshots.append(
            PackageSnapshot(
                pkg=package.get("pkg", ""),
                pin_numbers=pin_numbers,
                pin_names=pin_names,
            )
        )
    return snapshots


def find_compatible_package_key(
    packages: dict[str, dict[str, Any]],
    identity: PackageIdentity,
) -> str:
    if identity.key in packages:
        return identity.key

    for bucket_key, bucket in packages.items():
        existing_identity = bucket.get("_identity")
        if isinstance(existing_identity, PackageIdentity) and package_identities_compatible(existing_identity, identity):
            return bucket_key
    return ""


def package_identities_compatible(left: PackageIdentity, right: PackageIdentity) -> bool:
    """Merge aliases such as ZCE and ZCE-64 when the stable package code matches."""
    if left.key == right.key:
        return True
    if left.code and right.code and left.code == right.code:
        return compatible_pin_count(left.pin_count, right.pin_count)
    if left.family and right.family and left.family == right.family:
        return compatible_pin_count(left.pin_count, right.pin_count)
    return False


def compatible_pin_count(left: str, right: str) -> bool:
    return not left or not right or left == right


def append_package_alias(package_bucket: dict[str, Any], alias: str) -> None:
    alias = alias.strip()
    if not alias:
        return
    aliases = package_bucket.setdefault("_aliases", [])
    if alias not in aliases:
        aliases.append(alias)
    package_bucket["pkg"] = " | ".join(aliases)


def get_or_create_group(package_bucket: dict[str, Any], group_name: str) -> ExtractedGroup:
    group_name = clean_group_name(group_name) or "Pin/Package Table"
    groups = package_bucket["_groups"]
    if group_name not in groups:
        groups[group_name] = ExtractedGroup(group=group_name)
        package_bucket["group_list"].append(groups[group_name])
    return groups[group_name]


def add_pin_record_to_group(group: ExtractedGroup, pin_record: dict[str, Any]) -> None:
    # 项目规则：引脚记录不按 pin_no 合并。同一个 pin_no 多次出现时，
    # 每条记录独立输出；这里只做输出前字段清洗。
    group.pin_list.append(normalize_pin_record(pin_record))


def normalize_pin_record(pin_record: dict[str, Any]) -> dict[str, Any]:
    record = dict(pin_record)
    record["pin_no"] = plain_text(str(record.get("pin_no", ""))).strip()
    record["pin_name"] = clean_pin_name(record.get("pin_name", ""))
    return record


def clean_pin_name(value: Any) -> str:
    value = plain_text(str(value or "")).strip()
    value = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", value)
    value = re.sub(r"\s*\(\s*continued\s*\)\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "Reserved"


def merge_pin_record(existing_record: dict[str, Any], new_record: dict[str, Any]) -> None:
    for key, value in new_record.items():
        if not value:
            continue
        if key not in existing_record or not existing_record[key]:
            existing_record[key] = value
            continue
        if existing_record[key] == value:
            continue
        if key == "pin_no":
            continue
        existing_record[key] = merge_field_value(str(existing_record[key]), str(value))


def infer_package_name(text: str) -> str:
    text = plain_text(text)
    patterns = [
        r"\(([A-Z0-9][A-Z0-9_\-/ ]{0,30}\s+Package)\)",
        r"\b([A-Z0-9][A-Z0-9_\-/]{1,20}\s+Package)\b",
        r"\bPackage\s+([A-Z0-9][A-Z0-9_\-/]{1,20})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def infer_group_name(text: str) -> str:
    text = plain_text(text)
    if not text:
        return ""
    group_keywords = (
        r"Pin Attributes|Terminal Functions|Pin Assignment|Terminal Assignment|"
        r"Package Pins?|Signal Descriptions|Connectivity Requirements|"
        r"Connection Requirements|引脚属性|引脚分配|封装引脚"
    )
    table_match = re.search(
        rf"((?:Table|表)\s+\S+\.?\s+[^。\n]{{0,140}}?(?:{group_keywords})[^。\n]{{0,80}})",
        text,
        re.IGNORECASE,
    )
    if table_match:
        return clean_group_name(table_match.group(1))
    section_match = re.search(
        rf"(\d+(?:\.\d+)+\s+[^。\n]{{0,120}}?(?:{group_keywords})[^。\n]{{0,80}})",
        text,
        re.IGNORECASE,
    )
    if section_match:
        return clean_group_name(section_match.group(1))
    return ""


def clean_group_name(value: str) -> str:
    value = re.sub(r"\s+", " ", plain_text(value)).strip()
    # 续表标题和原表标题应归到同一个 group。
    value = re.sub(r"\s*[\(（]\s*continued\s*[\)）]\s*", " ", value, flags=re.IGNORECASE)
    # 输出 group 只保留语义标题，不保留 Table/表格 编号前缀。
    value = re.sub(
        r"^(?:table|表格?|表)\s*[\w.\-一二三四五六七八九十百千万]+\.?\s*[:：.\-–—]?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    if len(value) > 180:
        value = value[:180].rsplit(" ", 1)[0].strip()
    return re.sub(r"\s+", " ", value).strip()


def is_group_row(row: list[str]) -> bool:
    non_empty = [cell for cell in row if cell.strip()]
    if not non_empty:
        return False
    if len(non_empty) == 1 and not looks_like_pin_list(non_empty[0]):
        return True
    if len(set(non_empty)) == 1 and not looks_like_pin_list(non_empty[0]):
        return True
    return False


def first_non_empty(row: list[str]) -> str:
    for cell in row:
        if cell.strip():
            return cell.strip()
    return ""


def split_pin_numbers(value: str) -> list[str]:
    tokens = BALL_TOKEN_RE.findall(value)
    if tokens:
        return tokens
    value = value.strip()
    if NUMERIC_PIN_RE.fullmatch(value):
        return [value]
    numeric_tokens = re.findall(r"\b\d{1,4}\b", value)
    return numeric_tokens


def is_ordering_table(headers: list[str]) -> bool:
    normalized_headers = [normalize_header(header) for header in headers if header.strip()]
    joined = " ".join(normalized_headers)
    hit_count = sum(1 for keyword in ORDERING_TABLE_KEYWORDS if keyword in joined)
    return "orderable device" in joined and hit_count >= 3


def is_package_pin_header(normalized_header: str) -> bool:
    if not PACKAGE_HEADER_RE.search(normalized_header):
        return False
    if any(keyword in normalized_header for keyword in ("orderable", "package qty", "package type", "package drawing")):
        return False
    if any(
        keyword in normalized_header
        for keyword in (
            "引脚编号",
            "管脚编号",
            "端子编号",
            "pin number",
            "pin no",
            "ball number",
            "ball no",
            "terminal number",
            "terminal no",
        )
    ):
        return True
    # In stacked package tables, the leaf header can be just "64 PM" or "RHB".
    return bool(re.fullmatch(r"(?:\d{2,4}\s*)?(?:[a-z0-9]+(?:\s+[a-z0-9]+){0,2})", normalized_header))


def clean_package_label(header: str) -> str:
    label = plain_text(header)
    label = re.sub(r"\[[^\]]+\]", " ", label)
    label = normalize_package_word_order(label)
    label = re.sub(r"\(\s*\d{1,2}\s*\)", " ", label)
    label = re.sub(
        r"(?:引脚编号|管脚编号|端子编号|pin\s*(?:number|no\.?)|pins?|ball\s*(?:number|no\.?)|terminal\s*(?:number|no\.?))",
        " ",
        label,
        flags=re.IGNORECASE,
    )
    label = normalize_package_word_order(label)
    label = re.sub(r"\s+", " ", label).strip(" -:/")
    return label


def build_package_identity(label: str) -> PackageIdentity:
    display = clean_package_label(label) if label else ""
    parts = extract_package_identity_parts(display)
    key_parts = []
    if parts["pin_count"]:
        key_parts.append(f"pins={parts['pin_count']}")
    if parts["family"]:
        key_parts.append(f"family={parts['family']}")
    if parts["code"]:
        key_parts.append(f"code={parts['code']}")
    if not key_parts:
        key_parts.append(f"label={normalize_package_key(display)}")
    return PackageIdentity(
        display=display,
        key="|".join(key_parts),
        pin_count=parts["pin_count"],
        family=parts["family"],
        code=parts["code"],
    )


def extract_package_identity_parts(label: str) -> dict[str, str]:
    normalized = normalize_package_word_order(plain_text(label))
    pin_count = ""
    family = ""
    code = ""

    count_match = re.search(r"\b(\d{2,4})\b", normalized)
    if count_match:
        pin_count = count_match.group(1)

    family_match = PACKAGE_FAMILY_RE.search(normalized)
    if family_match:
        family = family_match.group(1).upper().replace(" ", "")

    code_matches = [
        match.group(1).upper()
        for match in PACKAGE_CODE_RE.finditer(normalized)
        if match.group(1).upper() != family
    ]
    if code_matches:
        code = "_".join(dict.fromkeys(code_matches))

    return {"pin_count": pin_count, "family": family, "code": code}


def normalize_package_key(label: str) -> str:
    label = normalize_package_word_order(label)
    label = normalize_header(label)
    label = re.sub(r"[^a-z0-9]+", "_", label).strip("_")
    return label or "unknown"


def normalize_package_word_order(label: str) -> str:
    label = re.sub(r"\s+", " ", label).strip()
    match = re.fullmatch(r"([A-Za-z0-9]+)\s*\(\s*(\d{2,4})\s*\)", label)
    if match:
        return f"{match.group(2)} {match.group(1)}"
    match = re.fullmatch(r"([A-Za-z0-9]+)\s*[- ]\s*(\d{2,4})", label)
    if match:
        return f"{match.group(2)} {match.group(1)}"
    return label


def looks_like_pin_list(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if NUMERIC_PIN_RE.fullmatch(value):
        return True
    return bool(BALL_TOKEN_RE.search(value))


def looks_like_signal_name(value: str) -> bool:
    value = value.strip()
    if not value or len(value) > 80:
        return False
    if re.search(r"\s{2,}|[.!?;]", value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_/\-+().#]+(?:\s+[A-Za-z0-9_/\-+().#]+){0,3}", value))


def looks_like_type_value(value: str) -> bool:
    normalized = normalize_header(value).replace(" ", "")
    return normalized in {
        "i",
        "o",
        "io",
        "i/o",
        "oz",
        "odz",
        "od",
        "power",
        "ground",
        "gnd",
        "pwr",
        "input",
        "output",
        "ipu",
        "ipd",
        "analog",
        "supply",
    }


def normalize_field_name(field_name: str) -> str:
    if field_name in {"ball_no", "terminal_no"}:
        return "pin_no"
    if field_name in {"ball_name", "signal_name", "terminal_name"}:
        return "pin_name"
    if field_name == "io_type":
        return "type"
    return field_name


def merge_field_value(existing: str, value: str) -> str:
    if not existing:
        return value
    if existing == value:
        return existing
    return f"{existing} | {value}"


def normalize_header(value: str) -> str:
    value = plain_text(value).lower()
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = re.sub(r"[†‡*]+", " ", value)
    value = re.sub(r"[、，,]+", " ", value)
    value = re.sub(r"[_\-/]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def plain_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", " ", str(value), flags=re.IGNORECASE)
    value = TAG_RE.sub("", value)
    value = html_lib.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def write_extraction_json(result: list[dict[str, Any]], output_path: str | Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def strip_debug_fields(result: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove extraction metadata before writing the user-facing JSON."""
    stripped: list[dict[str, Any]] = []
    for package in result:
        new_package = {"pkg": package.get("pkg", ""), "group_list": []}
        for group in package.get("group_list", []):
            new_group = {"group": group.get("group", ""), "pin_list": []}
            for pin in group.get("pin_list", []):
                new_group["pin_list"].append(
                    {
                        key: value
                        for key, value in pin.items()
                        if key not in {"source", "source_page", "raw_fields"}
                    }
                )
            if new_group["pin_list"]:
                new_package["group_list"].append(new_group)
        if new_package["group_list"]:
            stripped.append(new_package)
    return stripped


def build_extraction_summary(
    result: list[dict[str, Any]],
    pdf_name: str = "",
) -> dict[str, Any]:
    """Build package/group counts and page spans for manual comparison."""
    packages = []
    total_pins = 0
    for package in result:
        groups = []
        package_pin_count = 0
        for group in package.get("group_list", []):
            pins = group.get("pin_list", [])
            pages = sorted(
                {
                    pin.get("source_page")
                    for pin in pins
                    if isinstance(pin.get("source_page"), int)
                }
            )
            pin_count = len(pins)
            package_pin_count += pin_count
            groups.append(
                {
                    "group": group.get("group", ""),
                    "pin_count": pin_count,
                    "page_start": pages[0] if pages else None,
                    "page_end": pages[-1] if pages else None,
                    "pages": pages,
                }
            )
        total_pins += package_pin_count
        packages.append(
            {
                "pkg": package.get("pkg", ""),
                "pin_count": package_pin_count,
                "table_count": len(groups),
                "group_list": groups,
            }
        )
    return {
        "pdf": pdf_name,
        "package_count": len(packages),
        "pin_count": total_pins,
        "packages": packages,
    }

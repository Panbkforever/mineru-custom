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


TR_RE = re.compile(r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE)
CELL_RE = re.compile(
    r"(<t[dh][^>]*>)(.*?)(</t[dh]>)",
    re.DOTALL | re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
BALL_TOKEN_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}\b")
NUMERIC_PIN_RE = re.compile(r"^\d{1,4}$")

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


def extract_pin_package_info_from_middle_json(
    middle_json: dict[str, Any],
    source_name: str = "",
) -> list[dict[str, Any]]:
    """Extract package/group/pin records from one MinerU middle_json object."""
    packages: dict[str, dict[str, Any]] = {}

    for table in iter_table_candidates(middle_json):
        rows = parse_html_table(table.html)
        if len(rows) < 2:
            continue

        header_index, headers = choose_header_row(rows)
        if header_index < 0:
            continue

        decisions = classify_columns(headers, rows[header_index + 1:])
        if not is_pin_package_table(decisions):
            continue

        pkg = infer_package_name(table.title)
        group_name = infer_group_name(table.title) or "Pin/Package Table"
        package_bucket = packages.setdefault(
            pkg,
            {"pkg": pkg, "group_list": [], "_groups": {}},
        )
        current_group = get_or_create_group(package_bucket, group_name)

        for row in rows[header_index + 1:]:
            if is_group_row(row):
                group_text = first_non_empty(row)
                if group_text:
                    current_group = get_or_create_group(package_bucket, group_text)
                continue

            pin_records = extract_pin_records_from_row(row, decisions)
            for pin_record in pin_records:
                if source_name:
                    pin_record.setdefault("source", source_name)
                if table.page_idx is not None:
                    pin_record.setdefault("source_page", table.page_idx + 1)
                current_group.pin_list.append(pin_record)

    result = []
    for package_bucket in packages.values():
        groups = [
            {"group": group.group, "pin_list": group.pin_list}
            for group in package_bucket["_groups"].values()
            if group.pin_list
        ]
        if groups:
            result.append({"pkg": package_bucket["pkg"], "group_list": groups})
    return result


def extract_pin_package_info_from_middle_json_file(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    middle_json = json.loads(path.read_text(encoding="utf-8"))
    return extract_pin_package_info_from_middle_json(middle_json, source_name=path.stem)


def iter_table_candidates(middle_json: dict[str, Any]) -> list[TableCandidate]:
    """Return HTML tables with nearby text as weak title context."""
    candidates: list[TableCandidate] = []
    for page_info in middle_json.get("pdf_info", []):
        page_idx = page_info.get("page_idx")
        recent_texts: list[str] = []
        for span in iter_spans_in_reading_order(page_info):
            html = span.get("html")
            text = plain_text(span.get("content") or span.get("text") or "")
            if isinstance(html, str) and "<table" in html.lower():
                candidates.append(
                    TableCandidate(
                        html=html,
                        page_idx=page_idx if isinstance(page_idx, int) else None,
                        title=" ".join(recent_texts[-3:]).strip(),
                    )
                )
                continue
            if text:
                recent_texts.append(text)
                recent_texts = recent_texts[-5:]
    return candidates


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
        score = sum(classify_header(cell)[1] for cell in row)
        if score > best_score:
            best_index = index
            best_score = score
            best_headers = row
    if best_score < 4:
        return -1, []
    return best_index, best_headers


def classify_columns(headers: list[str], data_rows: list[list[str]]) -> list[ColumnDecision]:
    decisions = []
    max_columns = max([len(headers), *(len(row) for row in data_rows[:20])] or [0])
    for index in range(max_columns):
        header = headers[index] if index < len(headers) else ""
        field_name, header_score = classify_header(header)
        value_field, value_score = classify_values(
            [row[index] for row in data_rows[:30] if index < len(row)]
        )
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
    return decisions


def classify_header(header: str) -> tuple[str, int]:
    normalized = normalize_header(header)
    if not normalized:
        return "", 0

    if any(keyword in normalized for keyword in IGNORE_HEADER_KEYWORDS):
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
    if "i/o" in normalized or normalized == "io":
        return "io_type", 3

    if "package" in normalized:
        return "package", 3

    return "", 0


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
    has_number = bool(fields & {"pin_no", "ball_no", "terminal_no"})
    has_name = bool(fields & {"pin_name", "ball_name", "signal_name", "terminal_name", "pad_name"})
    return has_number and has_name


def extract_pin_records_from_row(
    row: list[str],
    decisions: list[ColumnDecision],
) -> list[dict[str, Any]]:
    raw_fields: dict[str, str] = {}
    fields: dict[str, str] = {}
    for decision in decisions:
        value = row[decision.index].strip() if decision.index < len(row) else ""
        if not value:
            continue
        raw_key = decision.raw_header or f"column_{decision.index + 1}"
        raw_fields[raw_key] = value
        field_name = normalize_field_name(decision.field_name)
        fields[field_name] = merge_field_value(fields.get(field_name, ""), value)

    if not fields:
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
        record["raw_fields"] = raw_fields
        records.append(record)
    return records


def get_or_create_group(package_bucket: dict[str, Any], group_name: str) -> ExtractedGroup:
    group_name = group_name.strip() or "Pin/Package Table"
    groups = package_bucket["_groups"]
    if group_name not in groups:
        groups[group_name] = ExtractedGroup(group=group_name)
        package_bucket["group_list"].append(groups[group_name])
    return groups[group_name]


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
    table_match = re.search(
        r"(?:Table\s+\S+\.\s*)?([^。\\n]*?(?:Pin Attributes|Terminal Functions|Pin Assignment|Terminal Assignment|Package Pins?)[^。\\n]*)",
        text,
        re.IGNORECASE,
    )
    if table_match:
        return re.sub(r"\s+", " ", table_match.group(1)).strip()
    return ""


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
    if field_name in {"signal_name", "terminal_name"}:
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

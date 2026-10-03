"""STMicroelectronics-specific table recognition helpers."""

from __future__ import annotations

import re

from extract.special_table_handlers import (
    SpecialColumnMapping,
    SpecialTableMatch,
    normalize_header_text,
    normalize_text,
)


def classify_st_header(normalized_header: str) -> tuple[str, int]:
    """Map ST pad-table headers onto the shared pin fields."""

    if _ST_PACKAGE_LABEL_PATTERN.fullmatch(normalized_header):
        return "pin_no", 5
    if normalized_header in {
        "pad ref",
        "pad ref.",
        "pad reference",
        "pads ref",
        "pads ref.",
        "pads reference",
    }:
        return "pin_no", 5
    if normalized_header in {"pad name", "pads name"}:
        return "pin_name", 5
    return "", 0


def match_st_table(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    return (
        match_st_pad_description_table(title, headers, data_rows)
        or match_st_pin_assignment_table(title, headers, data_rows)
    )


def should_keep_st_record(record: dict[str, object]) -> bool:
    pin_no = re.sub(r"\s+", "", str(record.get("pin_no", "")))
    if pin_no and set(pin_no) == {"-"}:
        return False
    if pin_no.upper() in {"NA", "N/A", "N.A.", "NOTAPPLICABLE"}:
        return False
    return True


def repair_st_table(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    """Normalize ST pin definition table structure after PDF table parsing."""

    repaired_headers = _normalize_st_assignment_headers(title, headers)
    repaired_headers, repaired_rows = _repair_st_package_label_header_row(
        title,
        repaired_headers,
        data_rows,
    )
    repaired_headers, repaired_rows = _normalize_st_package_pin_headers(
        title,
        repaired_headers,
        repaired_rows,
    )
    repaired_headers, repaired_rows = _expand_combined_st_package_pin_columns(
        title,
        repaired_headers,
        repaired_rows,
    )
    repaired_headers, repaired_rows = _restore_missing_st_package_header_before_name(
        title,
        repaired_headers,
        repaired_rows,
    )
    if repaired_headers is headers and repaired_rows is data_rows:
        return headers, data_rows
    return repaired_headers, repaired_rows


def repair_st_table_rows(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> list[list[str]]:
    """Repair pin cells in ST physical tables; gate merged-row repair separately."""

    if not _is_st_pin_definition_context(title, headers):
        return data_rows

    roles = _st_pin_assignment_roles(headers)
    if not roles["pin_no"] or not roles["pin_name"] or not roles["type"]:
        return data_rows

    pin_indexes = roles["pin_no"]
    pin_name_index = roles["pin_name"][0]
    type_index = roles["type"][0]
    repair_merged_rows = _is_st_ball_definition_table(title, headers)
    repaired_rows: list[list[str]] = []
    changed = False

    for row_index, row in enumerate(data_rows):
        normalized_row = list(row)
        for index in pin_indexes:
            if index < len(normalized_row):
                if re.fullmatch(
                    r"(?:exposed|thermal)\s+pad",
                    str(normalized_row[index]).strip(),
                    flags=re.IGNORECASE,
                ):
                    # A named physical pad is one identifier, not a pin list.
                    normalized_row[index] = "EP"
                    changed = True
                fixed = _join_wrapped_pin_number(
                    normalized_row[index],
                    headers,
                    data_rows,
                    row_index,
                    index,
                )
                if fixed != normalized_row[index]:
                    normalized_row[index] = fixed
                    changed = True

        if pin_name_index < len(normalized_row):
            fixed_name = _normalize_st_pin_name_ocr(normalized_row[pin_name_index])
            if fixed_name != normalized_row[pin_name_index]:
                normalized_row[pin_name_index] = fixed_name
                changed = True

        split_rows = _split_merged_st_ball_row(
            normalized_row,
            pin_indexes,
            pin_name_index,
            type_index,
        ) if repair_merged_rows else None
        if split_rows is None:
            repaired_rows.append(normalized_row)
        else:
            repaired_rows.extend(split_rows)
            changed = True

    return repaired_rows if changed else data_rows


def _normalize_st_header_text(value: str) -> str:
    text = str(value or "").replace("$", "").replace("\\ ", " ")
    text = re.sub(r"\^\s*\{\s*(?:\(\s*\d+\s*\)|\d+|[,\s])+\s*\}", "", text)
    text = re.sub(r"\(\s*\d+(?:\s*[,;]\s*\d+)*\s*\)", "", text)
    return normalize_header_text(text)


def _normalize_st_assignment_headers(title: str, headers: list[str]) -> list[str]:
    if not re.search(r"\bpin\s+assignment\b", normalize_text(title)):
        return headers
    if not is_st_physical_table(title, headers):
        return headers
    repaired = []
    for header in headers:
        normalized = _normalize_st_header_text(header)
        if _is_st_package_label(normalized):
            repaired.append(f"Pin number {_canonicalize_st_package_label(normalized)}")
        elif normalized == "name":
            repaired.append("Pin name")
        elif normalized == "type":
            repaired.append("Pin type")
        else:
            repaired.append(header)
    return repaired if repaired != headers else headers


def _repair_st_package_label_header_row(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    if not data_rows or not _is_st_pin_definition_context(title, headers):
        return headers, data_rows

    first_row = data_rows[0]
    package_label_count = _leading_st_package_label_count(first_row)
    if package_label_count < 2:
        return headers, data_rows

    generic_pin_headers = sum(
        1
        for header in headers[:package_label_count]
        if _normalize_st_header_text(header) in {"pin", "pin number", "pin no", "pin no."}
    )
    if generic_pin_headers < 2:
        return headers, data_rows

    width = max([len(headers), len(first_row)] + [len(row) for row in data_rows[:20]])
    new_headers: list[str] = []
    for index in range(package_label_count):
        new_headers.append(f"Pin number {_canonicalize_st_package_label(first_row[index])}")

    tail_cells = [
        str(first_row[index] if index < len(first_row) else "").strip()
        for index in range(package_label_count, width)
    ]
    first_tail = normalize_header_text(tail_cells[0]) if tail_cells else ""
    if not first_tail.startswith("pin name"):
        pin_name_header = _first_header_matching(headers, "pin name") or (
            "Pin name(function after reset)"
        )
        tail_cells.insert(0, pin_name_header)

    for value in tail_cells[: max(0, width - len(new_headers))]:
        new_headers.append(value)
    while len(new_headers) < width:
        index = len(new_headers)
        fallback = headers[index] if index < len(headers) else ""
        new_headers.append(fallback)

    return new_headers, data_rows[1:]


def _expand_combined_st_package_pin_columns(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    if not _is_st_pin_definition_context(title, headers):
        return headers, data_rows

    new_headers: list[str] = []
    column_expansions: list[tuple[int, int]] = []
    changed = False
    for index, header in enumerate(headers):
        normalized = normalize_header_text(header)
        labels = (
            _split_combined_st_package_labels(header)
            if _is_st_package_pin_header(normalized)
            else []
        )
        if len(labels) < 2:
            new_headers.append(header)
            column_expansions.append((index, 1))
            continue
        for label in labels:
            new_headers.append(f"Pin number {label}")
        column_expansions.append((index, len(labels)))
        changed = True

    if not changed:
        return headers, data_rows

    new_rows = []
    for row in data_rows:
        new_row: list[str] = []
        for source_index, copies in column_expansions:
            value = row[source_index] if source_index < len(row) else ""
            new_row.extend([value] * copies)
        new_rows.append(new_row)
    return new_headers, new_rows


def _normalize_st_package_pin_headers(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    if not _is_st_pin_definition_context(title, headers):
        return headers, data_rows

    changed = False
    normalized_headers: list[str] = []
    for header in headers:
        normalized = normalize_header_text(header)
        if not _is_st_package_pin_header(normalized):
            normalized_headers.append(header)
            continue
        if len(_split_combined_st_package_labels(header)) >= 2:
            normalized_headers.append(header)
            continue
        label = _package_label_from_pin_header(header)
        if not label:
            normalized_headers.append(header)
            continue
        repaired = f"Pin number {label}"
        normalized_headers.append(repaired)
        changed = changed or repaired != header

    return (normalized_headers, data_rows) if changed else (headers, data_rows)


def _restore_missing_st_package_header_before_name(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    if not data_rows or not _is_st_pin_definition_context(title, headers):
        return headers, data_rows

    pin_name_index = next(
        (
            index
            for index, header in enumerate(headers)
            if normalize_header_text(header).startswith("pin name")
        ),
        None,
    )
    if pin_name_index is None or pin_name_index + 1 >= len(headers):
        return headers, data_rows

    current_values = [
        row[pin_name_index]
        for row in data_rows[:12]
        if pin_name_index < len(row) and str(row[pin_name_index]).strip()
    ]
    next_values = [
        row[pin_name_index + 1]
        for row in data_rows[:12]
        if pin_name_index + 1 < len(row) and str(row[pin_name_index + 1]).strip()
    ]
    if not current_values or not next_values:
        return headers, data_rows
    if sum(_looks_like_st_pin_value(value) for value in current_values) < 2:
        return headers, data_rows
    if sum(_looks_like_st_signal_name(value) for value in next_values) < 2:
        return headers, data_rows

    missing_label = _infer_missing_st_package_label(headers[:pin_name_index])
    if not missing_label:
        return headers, data_rows

    new_headers = list(headers)
    new_headers.insert(pin_name_index, f"Pin number {missing_label}")
    return new_headers, data_rows


def _infer_missing_st_package_label(headers: list[str]) -> str:
    labels = [
        _package_label_from_pin_header(header)
        for header in headers
        if _is_st_package_pin_header(normalize_header_text(header))
    ]
    labels = [label for label in labels if label]
    if not labels:
        return ""

    smps_bases = [
        re.sub(r"\s+SMPS$", "", label, flags=re.IGNORECASE)
        for label in labels
        if label.upper().endswith(" SMPS")
    ]
    plain_labels = {
        label
        for label in labels
        if not label.upper().endswith(" SMPS")
    }
    if not smps_bases or not plain_labels:
        return ""

    previous_plain = next(
        (
            label
            for label in reversed(labels)
            if not label.upper().endswith(" SMPS")
        ),
        "",
    )
    previous_count = _package_pin_count(previous_plain)
    candidates = [
        label
        for label in smps_bases
        if label not in plain_labels
        and (previous_count is None or _package_pin_count(label) == previous_count)
    ]
    return candidates[-1] if candidates else ""


def _package_label_from_pin_header(header: str) -> str:
    normalized = normalize_header_text(
        _normalize_st_package_ocr_text(_remove_st_pin_number_role(header))
    )
    labels = _split_combined_st_package_labels(normalized)
    if len(labels) == 1:
        return labels[0]
    if _is_st_package_label(normalized):
        return _canonicalize_st_package_label(normalized)
    return ""


def _package_pin_count(label: str) -> int | None:
    match = re.search(r"\d{1,3}", label)
    return int(match.group(0)) if match else None


def _looks_like_st_pin_value(value: str) -> bool:
    compact = re.sub(r"\s+", "", str(value or ""))
    return bool(re.fullmatch(r"(?:\d{1,3}|[A-Z]{1,2}\d{1,2})", compact))


def _looks_like_st_signal_name(value: str) -> bool:
    compact = re.sub(r"\s+", "", str(value or ""))
    return bool(
        re.fullmatch(
            r"(?:P[A-Z]\d{1,2}|VDD[A-Z0-9_]*|VSS[A-Z0-9_]*|VBAT|NRST|BOOT\d*)",
            compact,
        )
    )


def _is_st_pin_definition_context(title: str, headers: list[str]) -> bool:
    title_text = normalize_text(title)
    if "pin assignment" in title_text or "ball definitions" in title_text:
        return True
    normalized_headers = [_normalize_st_header_text(header) for header in headers]
    pin_header_count = sum(
        1
        for header in normalized_headers
        if header.startswith("pin number") or header in {"pin", "pin no", "pin no."}
    )
    has_name_or_type = any(
        header.startswith("pin name") or header == "pin type"
        for header in normalized_headers
    )
    return pin_header_count >= 2 and has_name_or_type


def _leading_st_package_label_count(row: list[str]) -> int:
    count = 0
    for value in row:
        if not _is_st_package_label(value):
            break
        count += 1
    return count


def _is_st_package_label(value: str) -> bool:
    normalized = normalize_header_text(_canonicalize_st_package_label(value))
    return bool(_ST_PACKAGE_LABEL_PATTERN.fullmatch(normalized)) or normalized in _ST_PACKAGE_TERMS


def _first_header_matching(headers: list[str], prefix: str) -> str:
    for header in headers:
        if normalize_header_text(header).startswith(prefix):
            return header
    return ""


def _split_combined_st_package_labels(header: str) -> list[str]:
    text = _remove_st_pin_number_role(header)
    normalized_text = normalize_header_text(_normalize_st_package_ocr_text(text))
    matches = list(_ST_PACKAGE_TOKEN_PATTERN.finditer(normalized_text))
    if len(matches) < 2:
        return []

    labels = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized_text)
        label = normalized_text[match.start():end].strip(" -:/,;&")
        label = _canonicalize_st_package_label(label)
        if label:
            labels.append(label)
    return labels if len(labels) >= 2 else []


def _remove_st_pin_number_role(value: str) -> str:
    return re.sub(
        r"\bpin\s*(?:number|no\.?|#)?\b",
        " ",
        _normalize_st_header_text(value),
        flags=re.IGNORECASE,
    )


def _canonicalize_st_package_label(value: str) -> str:
    text = _normalize_st_package_ocr_text(value)
    text = re.sub(r"\buqfpn\b", "UFQFPN", text, flags=re.IGNORECASE)
    return text.strip(" -:/,;&").upper()


def _normalize_st_package_ocr_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"\bW\s*L\s*C\s*S\s*P", "WLCSP", text, flags=re.IGNORECASE)
    text = re.sub(r"\bWLCS(?=\d)", "WLCSP", text, flags=re.IGNORECASE)
    text = re.sub(r"\bU\s*F\s*Q\s*F\s*P\s*N", "UFQFPN", text, flags=re.IGNORECASE)
    text = re.sub(r"\bU\s*F\s*B\s*G\s*A", "UFBGA", text, flags=re.IGNORECASE)
    text = re.sub(r"\bT\s*F\s*B\s*G\s*A", "TFBGA", text, flags=re.IGNORECASE)
    text = re.sub(r"\bL\s*Q\s*F\s*P", "LQFP", text, flags=re.IGNORECASE)
    text = re.sub(r"\bWLCSPT(?=\d)", "WLCSP7", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _is_st_ball_definition_table(title: str, headers: list[str]) -> bool:
    title_text = normalize_text(title)
    roles = _st_pin_assignment_roles(headers)
    if not (roles["pin_no"] and roles["pin_name"] and roles["type"]):
        return False
    if "ball definitions" in title_text:
        return True

    normalized_headers = [normalize_header_text(header) for header in headers]
    has_bga_package = any(
        header.startswith("pin number") and "bga" in header
        for header in normalized_headers
    )
    has_pin_name = any(header.startswith("pin name") for header in normalized_headers)
    has_pin_type = any(header == "pin type" for header in normalized_headers)
    return has_bga_package and has_pin_name and has_pin_type


def _join_wrapped_pin_number(
    value: str,
    headers: list[str],
    data_rows: list[list[str]],
    row_index: int,
    column_index: int,
) -> str:
    text = str(value or "").strip()
    if not text or "\n" not in text:
        return text
    name_indexes = _st_pin_assignment_roles(headers)["pin_name"]
    row = data_rows[row_index]
    pin_parts = [part.strip() for part in text.splitlines() if part.strip()]
    if name_indexes and name_indexes[0] < len(row):
        name_parts = [part.strip() for part in str(row[name_indexes[0]]).splitlines() if part.strip()]
        if (
            len(pin_parts) == len(name_parts) > 1
            and all(_looks_like_st_pin_value(part) for part in pin_parts)
            and all(_looks_like_st_signal_name(part) for part in name_parts)
        ):
            return text
    compact = re.sub(r"\s+", "", text)
    if re.fullmatch(r"[A-Z]{1,2}\d{1,2}", compact):
        return compact
    if _should_join_wrapped_numeric_pin(
        compact,
        headers,
        data_rows,
        row_index,
        column_index,
    ):
        return compact
    return text


def _should_join_wrapped_numeric_pin(
    compact: str,
    headers: list[str],
    data_rows: list[list[str]],
    row_index: int,
    column_index: int,
) -> bool:
    if not re.fullmatch(r"\d{2,3}", compact):
        return False
    header = headers[column_index] if column_index < len(headers) else ""
    package_limit = _numeric_package_pin_limit(header)
    if package_limit is None:
        return False
    value = int(compact)
    if value < 1 or value > package_limit:
        return False
    previous_value = _nearest_numeric_pin(data_rows, row_index, column_index, -1)
    next_value = _nearest_numeric_pin(data_rows, row_index, column_index, 1)
    if (previous_value is not None and previous_value + 1 == value) or (
        next_value is not None and next_value - 1 == value
    ):
        return True
    return _has_same_row_numeric_pin_evidence(
        value,
        package_limit,
        headers,
        data_rows,
        row_index,
        column_index,
    )


def _has_same_row_numeric_pin_evidence(
    value: int,
    package_limit: int,
    headers: list[str],
    data_rows: list[list[str]],
    row_index: int,
    column_index: int,
) -> bool:
    if row_index >= len(data_rows):
        return False
    row = data_rows[row_index]
    left = _nearest_same_row_numeric_package_pin(headers, row, column_index, -1)
    right = _nearest_same_row_numeric_package_pin(headers, row, column_index, 1)
    if left is not None and right is not None:
        return left < value < right
    if left is not None:
        return left < value <= package_limit
    if right is not None:
        return 1 <= value < right
    return False


def _nearest_same_row_numeric_package_pin(
    headers: list[str],
    row: list[str],
    column_index: int,
    step: int,
) -> int | None:
    origin_mode = _st_package_mode_marker(headers[column_index])
    index = column_index + step
    while 0 <= index < len(headers):
        normalized = normalize_header_text(headers[index])
        if _is_st_package_pin_header(normalized) and (
            _st_package_mode_marker(headers[index]) != origin_mode
        ):
            return None
        if _numeric_package_pin_limit(headers[index]) is not None:
            value = row[index] if index < len(row) else ""
            number = _single_numeric_pin_value(value)
            if number is not None:
                return number
        index += step
    return None


def _st_package_mode_marker(header: str) -> str:
    return "smps" if "smps" in normalize_header_text(header) else "plain"


def _normalize_st_pin_name_ocr(value: str) -> str:
    text = str(value or "").strip()
    compact = re.sub(r"\s+", "", text)
    if re.fullmatch(r"V(?:DD|SS)[A-Z0-9_]*", compact):
        return compact
    return text


def _numeric_package_pin_limit(header: str) -> int | None:
    normalized = normalize_header_text(_normalize_st_package_ocr_text(header))
    numeric_package_terms = (
        "lqfp",
        "ufqfpn",
        "uqfpn",
        "ufqfn",
        "qfpn",
        "qfn",
        "tssop",
        "so",
        "sop",
    )
    if not any(term in normalized for term in numeric_package_terms):
        return None
    counts = [
        int(match.group(1))
        for match in re.finditer(
            r"(?:lqfp|ufqfpn|uqfpn|ufqfn|qfpn|qfn|tssop|so|sop)\s*(\d{2,3})",
            normalized,
        )
    ]
    if not counts:
        return None
    return max(counts)


def _nearest_numeric_pin(
    data_rows: list[list[str]],
    row_index: int,
    column_index: int,
    step: int,
) -> int | None:
    index = row_index + step
    while 0 <= index < len(data_rows):
        row = data_rows[index]
        value = row[column_index] if column_index < len(row) else ""
        number = _single_numeric_pin_value(value)
        if number is not None:
            return number
        if str(value or "").strip() not in {"", "-"}:
            return None
        index += step
    return None


def _single_numeric_pin_value(value: str) -> int | None:
    compact = re.sub(r"\s+", "", str(value or ""))
    if re.fullmatch(r"\d{1,3}", compact):
        return int(compact)
    return None


def _split_merged_st_ball_row(
    row: list[str],
    pin_indexes: list[int],
    pin_name_index: int,
    type_index: int,
) -> list[list[str]] | None:
    if pin_name_index >= len(row) or type_index >= len(row):
        return None

    pin_splits: dict[int, tuple[str, str]] = {}
    merged_pin_cells = 0
    for index in pin_indexes:
        value = row[index] if index < len(row) else ""
        split = _split_merged_pin_cell(value)
        if split is None:
            return None
        pin_splits[index] = split
        if _cell_contains_two_values(value, split):
            merged_pin_cells += 1

    if merged_pin_cells < 2:
        return None

    type_split = _split_merged_type_cell(row[type_index])
    if type_split is None:
        return None
    name_split = _split_merged_pin_name_cell(row[pin_name_index], type_split)
    if name_split is None:
        return None

    first = list(row)
    second = list(row)
    for index, split in pin_splits.items():
        first[index], second[index] = split
    first[pin_name_index], second[pin_name_index] = name_split
    first[type_index], second[type_index] = type_split

    for index in range(len(row)):
        if index in pin_splits or index in {pin_name_index, type_index}:
            continue
        first[index], second[index] = _split_optional_merged_cell(row[index])

    return [first, second]


def _split_merged_pin_cell(value: str) -> tuple[str, str] | None:
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return ("", "")
    ball = r"[A-Z]{1,2}\d{1,2}"
    if text == "-":
        return ("-", "-")
    match = re.fullmatch(rf"-({ball})", text)
    if match:
        return ("-", match.group(1))
    match = re.fullmatch(rf"({ball})-", text)
    if match:
        return (match.group(1), "-")
    match = re.fullmatch(rf"({ball})({ball})", text)
    if match:
        return (match.group(1), match.group(2))
    if re.fullmatch(ball, text):
        return (text, text)
    return None


def _cell_contains_two_values(original: str, split: tuple[str, str]) -> bool:
    compact = re.sub(r"\s+", "", str(original or ""))
    return compact not in {split[0], "-"} or split[0] != split[1]


def _split_merged_type_cell(value: str) -> tuple[str, str] | None:
    text = re.sub(r"\s+", "", str(value or ""))
    for first in ("I/O", "S", "O", "I", "A"):
        if text.startswith(first):
            second = text[len(first):]
            if second in {"I/O", "S", "O", "I", "A"}:
                return (first, second)
    return None


def _split_merged_pin_name_cell(
    value: str,
    type_split: tuple[str, str],
) -> tuple[str, str] | None:
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return None
    power_names = ("VDDQ_DDR", "VDDCORE", "VDDCPU", "VSS", "VDD")
    if type_split[0] == "S":
        for name in power_names:
            if text.startswith(name) and len(text) > len(name):
                return (name, text[len(name):])
    if type_split[1] == "S":
        for name in power_names:
            if text.endswith(name) and len(text) > len(name):
                return (text[:-len(name)], name)
    return None


def _split_optional_merged_cell(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return ("", "")
    if set(compact) == {"-"} and len(compact) >= 2:
        return ("-", "-")
    if compact.startswith("-") and len(compact) > 1:
        return ("-", compact[1:])
    if compact.endswith("-") and len(compact) > 1:
        return (compact[:-1], "-")
    return (text, text)


def is_st_physical_table(title: str, headers: list[str]) -> bool:
    title_text = normalize_text(title)
    if not re.search(r"\bpin\s+assignment\b", title_text):
        return False
    roles = _st_pin_assignment_roles(headers)
    return bool(roles["pin_no"] and roles["pin_name"])


def match_st_pad_description_table(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """Recognize ST physical pad description tables.

    ST small RF/filter datasheets often publish the physical pin list as
    ``Pad ref | Pad name | Description`` under titles such as
    ``Pad description top view (pads down)``. These are equivalent to the
    shared ``pin_no | pin_name | description`` schema, but TI-oriented rules
    do not treat ``Pad`` as a pin axis.
    """

    title_text = normalize_text(title)
    if "pad" not in title_text or "description" not in title_text:
        return None

    normalized_headers = [normalize_header_text(header) for header in headers]
    ref_index = _find_header_index(normalized_headers, {
        "pad ref",
        "pad ref.",
        "pad reference",
        "pads ref",
        "pads ref.",
        "pads reference",
    })
    name_index = _find_header_index(normalized_headers, {"pad name", "pads name"})
    description_index = _find_header_index(normalized_headers, {"description"})
    if ref_index is None or name_index is None:
        return None

    if not any(_has_data_cell(row, ref_index) for row in data_rows):
        return None

    mappings = [
        SpecialColumnMapping(ref_index, headers[ref_index], "pin_no"),
        SpecialColumnMapping(name_index, headers[name_index], "pin_name"),
    ]
    if description_index is not None:
        mappings.append(
            SpecialColumnMapping(
                description_index,
                headers[description_index],
                "description",
            )
        )

    return SpecialTableMatch(
        handler_name="st_pad_description_table_handler",
        columns=tuple(mappings),
        included_row_indexes=frozenset(range(len(data_rows))),
    )


def _find_header_index(headers: list[str], accepted: set[str]) -> int | None:
    for index, header in enumerate(headers):
        if header in accepted:
            return index
    return None


def _has_data_cell(row: list[str], index: int) -> bool:
    return index < len(row) and bool(str(row[index]).strip())


def match_st_pin_assignment_table(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
) -> SpecialTableMatch | None:
    """Recognize ST MCU pin assignment tables with package-specific pin columns."""

    if not is_st_physical_table(title, headers):
        return None
    roles = _st_pin_assignment_roles(headers)
    mappings: list[SpecialColumnMapping] = []
    for index in roles["pin_no"]:
        mappings.append(SpecialColumnMapping(index, headers[index], "pin_no"))
    mappings.append(SpecialColumnMapping(roles["pin_name"][0], headers[roles["pin_name"][0]], "pin_name"))
    if roles["type"]:
        mappings.append(SpecialColumnMapping(roles["type"][0], headers[roles["type"][0]], "type"))

    if not any(
        any(_has_data_cell(row, index) for index in roles["pin_no"])
        for row in data_rows
    ):
        return None

    return SpecialTableMatch(
        handler_name="st_pin_assignment_table_handler",
        columns=tuple(mappings),
        included_row_indexes=frozenset(range(len(data_rows))),
    )


def _st_pin_assignment_roles(headers: list[str]) -> dict[str, list[int]]:
    roles = {"pin_no": [], "pin_name": [], "type": []}
    for index, header in enumerate(headers):
        normalized = _normalize_st_header_text(header)
        if _is_st_package_pin_header(normalized) or _is_st_package_label(normalized):
            roles["pin_no"].append(index)
        elif normalized.startswith("pin name") or normalized == "name":
            roles["pin_name"].append(index)
        elif normalized in {"pin type", "type"}:
            roles["type"].append(index)
    return roles


def _is_st_package_pin_header(normalized: str) -> bool:
    normalized = _normalize_st_header_text(normalized)
    if not normalized.startswith("pin "):
        return False
    if normalized.startswith("pin name") or normalized == "pin type":
        return False
    normalized = normalize_header_text(_normalize_st_package_ocr_text(normalized))
    compact = re.sub(r"\s+", "", normalized)
    if compact.startswith("pinnumber") and "bga" in compact:
        return True
    package_terms = (
        "so",
        "sop",
        "tssop",
        "wlcsp",
        "ufqfpn",
        "uqfpn",
        "ufqfn",
        "qfpn",
        "lqfp",
        "ufbga",
        "bga",
        "qfn",
    )
    return any(term in compact for term in package_terms)


_ST_PACKAGE_TERMS = (
    "vfqfpn",
    "ufqfpn",
    "uqfpn",
    "ufqfn",
    "lfbga",
    "tfbga",
    "ufbga",
    "wlcsp",
    "lqfp",
    "tssop",
    "qfpn",
    "qfn",
    "sop",
    "so",
    "bga",
)
_ST_PACKAGE_TERMS_RE = "|".join(_ST_PACKAGE_TERMS)

_ST_PACKAGE_TOKEN_PATTERN = re.compile(
    rf"(?:{_ST_PACKAGE_TERMS_RE})\s*-?\s*\d{{1,3}}"
    rf"(?:\+\d{{1,3}})?[a-z0-9]*?(?=(?:{_ST_PACKAGE_TERMS_RE})|$|\s)"
)

_ST_PACKAGE_LABEL_PATTERN = re.compile(
    rf"(?:{_ST_PACKAGE_TERMS_RE})\s*-?\s*\d{{1,3}}(?:\+\d{{1,3}})?[a-z0-9]*"
    r"(?:\s+[a-z0-9_+-]+)*"
)

"""从 MinerU 的表格结果中提取器件引脚和封装信息。

本文件只做一件事：把表格转换为项目约定的 JSON 结构。

处理流程固定为四个阶段，阶段之间不互相调用：

1. 表格判断：判断当前表是不是“物理引脚/封装关系表”。
2. 字段判断：只判断每一列的语义，不读取行来生成输出记录。
3. 行提取：按照已经确定的字段映射，完整读取所有数据行。
4. 结果整理：拆分显式 pin_no 分隔符、清洗 pin_name、分组和合并封装别名。

特别重要的项目规则：

* 一列一旦被判定为需要字段，就不因为某一行为空而跳过该行。
* pin_no 只有在原文有空格、逗号、斜杠等显式分隔符时才拆分；A1-C3 不拆。
* pin_name 为空填 ``Reserved``；去掉末尾的 ``(数字)`` 和 ``(continued)``。
* 同一个 pin_no 出现多次时不合并记录；不同 type 也不合并。
* “Pin Configuration and Function” 这类坐标矩阵不是物理引脚表，表级直接排除。
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from extract.table_association_rules import (
    PackageSnapshot,
    resolve_table_package_association,
)


# ---------------------------------------------------------------------------
# 数据结构：判断阶段的结果和输出阶段的结果分开
# ---------------------------------------------------------------------------


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
    score: int = 0


@dataclass
class TableDecision:
    """表级判断结果，不包含任何已经提取出的 pin 记录。"""

    should_extract: bool
    table_role: str = ""
    pkg: str = ""
    group: str = ""
    reason: str = ""
    confidence: float = 0.0
    columns: list[ColumnDecision] = field(default_factory=list)


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


LAST_EXTRACTION_DEBUG: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# 对外入口：所有入口都使用同一条流水线
# ---------------------------------------------------------------------------


def extract_pin_package_info_from_middle_json(
    middle_json: dict[str, Any],
    source_name: str = "",
    include_debug: bool = False,
    use_semantic_classifier: bool = False,
) -> list[dict[str, Any]]:
    return extract_pin_package_info_from_table_candidates(
        iter_table_candidates(middle_json),
        source_name=source_name,
        include_debug=include_debug,
        use_semantic_classifier=use_semantic_classifier,
    )


def extract_pin_package_info_from_middle_json_file(
    path: str | Path,
    use_semantic_classifier: bool = False,
    include_debug: bool = False,
) -> list[dict[str, Any]]:
    path = Path(path)
    return extract_pin_package_info_from_middle_json(
        json.loads(path.read_text(encoding="utf-8")),
        source_name=path.stem,
        use_semantic_classifier=use_semantic_classifier,
        include_debug=include_debug,
    )


def extract_pin_package_info_from_markdown_json_file(
    path: str | Path,
    use_semantic_classifier: bool = False,
    include_debug: bool = False,
) -> list[dict[str, Any]]:
    """从最终的 ``<pdf>.json`` 中提取，确保使用后处理后的表格。"""

    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    markdown = payload.get("markdown", "") if isinstance(payload, dict) else ""
    if not isinstance(markdown, str) or "<table" not in markdown.lower():
        return []
    return extract_pin_package_info_from_table_candidates(
        iter_table_candidates_from_markdown(markdown),
        source_name=path.stem,
        include_debug=include_debug,
        use_semantic_classifier=use_semantic_classifier,
    )


def extract_pin_package_info_from_table_candidates(
    tables: Iterable[TableCandidate],
    source_name: str = "",
    include_debug: bool = False,
    use_semantic_classifier: bool = False,
) -> list[dict[str, Any]]:
    """按“先判断、后提取”的顺序处理表格。"""

    global LAST_EXTRACTION_DEBUG
    LAST_EXTRACTION_DEBUG = []
    candidates = list(tables)
    packages: dict[str, dict[str, Any]] = {}

    # 第一阶段：只准备候选表，不创建任何 pin 记录。
    prepared = []
    for table_id, table in enumerate(candidates):
        rows = parse_html_table(table.html)
        debug = {
            "table_id": table_id,
            "source": source_name,
            "page": table.page_idx + 1 if isinstance(table.page_idx, int) else None,
            "title": table.title,
            "row_count": len(rows),
            "status": "pending",
            "skip_reason": "",
        }
        LAST_EXTRACTION_DEBUG.append(debug)

        if len(rows) < 2:
            skip(debug, "too_few_rows")
            continue
        if is_pinout_matrix_table(rows, table.title):
            skip(debug, "pinout_matrix_table")
            continue

        header_index, headers = choose_header_row(rows, table.title, semantic=use_semantic_classifier)
        if header_index < 0:
            skip(debug, "no_candidate_header")
            continue
        data_rows = rows[header_index + 1 :]
        if is_ordering_table(headers):
            skip(debug, "ordering_table")
            continue
        if not is_loose_candidate(table.title, headers, data_rows):
            skip(debug, "not_pin_table_candidate")
            continue
        if is_non_physical_port_function_table(table.title, headers):
            skip(debug, "non_physical_port_function_table")
            continue

        rule_columns = classify_columns(headers, data_rows, table.title)
        prepared.append(
            {
                "table_id": table_id,
                "table": table,
                "rows": rows,
                "header_index": header_index,
                "headers": headers,
                "data_rows": data_rows,
                "rule_columns": rule_columns,
                "debug": debug,
            }
        )
        debug["headers"] = headers
        debug["rule_columns"] = decisions_to_debug(rule_columns)

    # 第二阶段：先完成所有表级和字段级判断，再进入任何行提取。
    decisions = decide_all_tables(prepared, use_semantic_classifier, include_debug)

    # 第三阶段：只有已经通过判断的表才允许进入行提取。
    for item in prepared:
        table_decision = decisions[item["table_id"]]
        debug = item["debug"]
        debug["decision"] = decision_to_debug(table_decision)
        if not table_decision.should_extract:
            skip(debug, table_decision.reason or "table_rejected")
            continue

        association = resolve_table_package_association(
            item["table"].title,
            item["headers"],
            item["data_rows"],
            table_decision.columns,
            build_package_snapshots(packages),
        )
        default_pkg = (
            table_decision.pkg
            or association.package
            or infer_package_name(item["table"].title)
        )
        group_name = clean_group_name(
            table_decision.group
            or infer_group_name(item["table"].title)
            or infer_group_name_from_headers(item["headers"], default_pkg)
            or "Pin/Package Table"
        )

        # 行提取函数只做列映射，不再决定表格是否有效。
        current_group_name = group_name
        extracted_count = 0
        for row in item["data_rows"]:
            if is_group_row(row):
                current_group_name = clean_group_name(first_non_empty(row)) or current_group_name
                continue
            for record in extract_records_from_row(row, table_decision.columns):
                record_pkg = record.pop("_pkg", default_pkg)
                record.pop("_raw_fields", None)
                if include_debug and source_name:
                    record["source"] = source_name
                if include_debug and item["table"].page_idx is not None:
                    record["source_page"] = item["table"].page_idx + 1
                identity = build_package_identity(record_pkg)
                bucket = get_package_bucket(packages, identity)
                group = get_or_create_group(bucket, current_group_name)
                add_pin_record_to_group(group, record)
                extracted_count += 1

        if extracted_count:
            debug.update({
                "status": "extracted",
                "pin_count": extracted_count,
                "pkg": default_pkg,
                "group": group_name,
            })
        else:
            skip(debug, "no_pin_records_after_mapping")

    return build_public_result(packages, include_debug)


def decide_all_tables(prepared: list[dict[str, Any]], use_semantic: bool, include_debug: bool) -> dict[int, TableDecision]:
    """完成全部表格判断；此函数绝不调用行提取函数。"""

    if not prepared:
        return {}
    if not use_semantic:
        return {
            item["table_id"]: decide_table_by_rules(item)
            for item in prepared
        }

    from extract.semantic_classifier import classify_table_schema

    workers = max(1, int(os.getenv("EXTRACT_SCHEMA_WORKERS", "2")))
    print(f"语义字段判断: 候选表 {len(prepared)} 张, 并发 {workers}", flush=True)
    results: dict[int, TableDecision] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                classify_table_schema,
                item["table_id"],
                item["table"].title,
                item["headers"],
                item["data_rows"][:12],
                item["rule_columns"],
            ): item
            for item in prepared
        }
        for index, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                schema = future.result()
                decision = decision_from_schema(schema, item)
            except Exception as exc:
                decision = TableDecision(False, reason=f"semantic_classification_failed:{exc}")
            results[item["table_id"]] = decision
            print(f"语义字段判断进度: {index}/{len(prepared)}", flush=True)
    return results


def decide_table_by_rules(item: dict[str, Any]) -> TableDecision:
    """无 LLM 时的确定性表级/字段级判断。"""

    columns = keep_primary_type_decision(item["rule_columns"], item["data_rows"])
    if not is_pin_package_table(columns):
        return TableDecision(False, reason="missing_pin_name_or_number_column", columns=columns)
    return TableDecision(
        True,
        table_role="rule_pin_table",
        pkg=infer_package_name(item["table"].title),
        group=infer_group_name(item["table"].title),
        confidence=1.0,
        columns=columns,
        reason="rule_mapping_shape_valid",
    )


def decision_from_schema(schema: dict[str, Any], item: dict[str, Any]) -> TableDecision:
    """把模型返回的 schema 转成内部决定，并做最小确定性校验。"""

    role = normalize_header(str(schema.get("table_role") or ""))
    if role in {"port function table", "pin mux table", "alternate function table", "default mapping table", "module signal connection", "irrelevant", "timing table", "electrical conditions", "ordering table", "boot mode table"}:
        return TableDecision(False, table_role=role, reason=f"semantic_role_rejected:{role}")
    if not bool(schema.get("should_extract")):
        return TableDecision(False, table_role=role, reason="semantic_rejected")

    columns = build_schema_column_decisions(schema, item["headers"])
    columns = keep_primary_type_decision(columns, item["data_rows"])
    if not has_required_columns(columns):
        return TableDecision(False, table_role=role, reason="semantic_schema_missing_required_columns", columns=columns)
    pkg = str(schema.get("pkg") or "").strip()
    if is_invalid_schema_package_name(pkg):
        pkg = ""
    group = clean_group_name(str(schema.get("group") or ""))
    return TableDecision(
        True,
        table_role=role,
        pkg=pkg,
        group=group,
        confidence=float(schema.get("confidence") or 0.0),
        reason=str(schema.get("reason") or "semantic_schema_valid"),
        columns=columns,
    )


# ---------------------------------------------------------------------------
# 表格判断：只判断“是不是目标表”，不生成输出记录
# ---------------------------------------------------------------------------


def is_pinout_matrix_table(rows: list[list[str]], title: str = "") -> bool:
    """排除坐标矩阵/引脚排列图对应的表格。

    这类表格常见形式是：表头包含 1、2、3...，第一列数据是 A、B、C...，
    单元格内容是坐标交叉关系。它不是“每行一个物理引脚”的表，不能把
    A/B/C 误当成 pin_no，也不能把整行矩阵内容当成 pin_name。
    """

    text = normalize_header(title)
    if any(word in text for word in ("pin configuration", "pinout", "pin map", "pin assignment")):
        title_signal = True
    else:
        title_signal = False
    if len(rows) < 3:
        return False
    header = [normalize_header(cell) for cell in rows[0]]
    explicit_pin_header = any(
        any(token in cell for token in ("pin no", "pin number", "ball number", "terminal no", "signal name"))
        for cell in header
    )
    if explicit_pin_header and not title_signal:
        return False

    numeric_header = sum(bool(re.fullmatch(r"\d{1,3}", cell)) for cell in header if cell)
    row_label_hits = 0
    for row in rows[1: min(len(rows), 12)]:
        first = normalize_header(row[0]) if row else ""
        if re.fullmatch(r"[a-z]", first):
            row_label_hits += 1
    return (title_signal and numeric_header >= 2 and row_label_hits >= 2) or (numeric_header >= 4 and row_label_hits >= 3 and not explicit_pin_header)


def is_loose_candidate(title: str, headers: list[str], rows: list[list[str]]) -> bool:
    """召回候选表；宽松，但只负责决定是否值得做字段判断。"""

    header_text = normalize_header(" ".join(headers))
    title_text = normalize_header(title)
    if is_ordering_table(headers):
        return False
    if any(word in header_text for word in ("register description", "offset address", "timing requirement")):
        return False
    if any(word in header_text for word in ("pin", "ball", "terminal", "signal", "引脚", "端子", "信号")):
        return True
    if any(word in title_text for word in ("pin attributes", "terminal functions", "signal descriptions", "connectivity requirements")):
        return True
    return bool(re.search(r"\b[A-Z]{1,2}\d{1,3}\b", " ".join(" ".join(row) for row in rows[:8])))


def is_non_physical_port_function_table(title: str, headers: list[str]) -> bool:
    """排除功能复用表，但不排除有明确物理 pin 编号的表。"""

    h = normalize_header(" ".join(headers))
    t = normalize_header(title)
    physical = any(word in h for word in ("pin no", "pin number", "ball number", "terminal no", "terminal number", "引脚编号", "端子编号"))
    if physical:
        return False
    return bool(re.search(r"\bport\s+p[a-z0-9.]*\b", t) or any(word in h + " " + t for word in ("pin mux", "pinmux", "alternate function", "default mapping")))


def is_ordering_table(headers: list[str]) -> bool:
    text = normalize_header(" ".join(headers))
    words = ("orderable device", "package qty", "package type", "package drawing", "eco plan", "lead finish", "msl peak temp")
    return "orderable device" in text and sum(word in text for word in words) >= 2


def has_required_columns(columns: list[ColumnDecision]) -> bool:
    fields = {normalize_field_name(c.field_name) for c in columns}
    return bool(fields & {"pin_no", "package_pin_no"}) and bool(fields & {"pin_name"})


def is_pin_package_table(columns: list[ColumnDecision]) -> bool:
    if not has_required_columns(columns):
        return False
    number = [c for c in columns if normalize_field_name(c.field_name) in {"pin_no", "package_pin_no"}]
    name = [c for c in columns if normalize_field_name(c.field_name) == "pin_name"]
    return any(classify_header(c.raw_header)[1] >= 3 for c in number) and any(classify_header(c.raw_header)[1] >= 3 for c in name)


# ---------------------------------------------------------------------------
# 字段判断：规则或 LLM 只返回 ColumnDecision
# ---------------------------------------------------------------------------


def classify_columns(headers: list[str], rows: list[list[str]], title: str = "") -> list[ColumnDecision]:
    decisions = []
    width = max([len(headers)] + [len(row) for row in rows[:30]] or [0])
    for index in range(width):
        header = headers[index] if index < len(headers) else ""
        values = [row[index] for row in rows[:30] if index < len(row)]
        field, header_score = classify_header(header)
        value_field, value_score = classify_values(values)
        if not field and value_field:
            field = value_field
        if field and header_score + value_score > 0:
            decisions.append(ColumnDecision(index, header, field, header_score + value_score))
    return keep_primary_type_decision(decisions, rows)


def build_schema_column_decisions(schema: dict[str, Any], headers: list[str]) -> list[ColumnDecision]:
    result = []
    for item in schema.get("columns") or []:
        index = parse_column_index(item.get("column_index"))
        field = str(item.get("field") or "ignore").strip()
        if index is None or field == "ignore":
            continue
        if field == "package_pin_no":
            raw = str(item.get("pkg") or "").strip() or safe_header(headers, index)
        else:
            raw = safe_header(headers, index)
        try:
            score = max(1, int(float(item.get("confidence", 0.8)) * 10))
        except (TypeError, ValueError):
            score = 8
        result.append(ColumnDecision(index, raw, field, score))
    return result


def keep_primary_type_decision(columns: list[ColumnDecision], rows: list[list[str]]) -> list[ColumnDecision]:
    """多个 type 列时只保留最接近 signal/pin 语义的那一列。"""

    types = [c for c in columns if normalize_field_name(c.field_name) == "type"]
    if len(types) <= 1:
        return columns
    best = max(types, key=lambda c: score_type_column(c, rows))
    return [c for c in columns if normalize_field_name(c.field_name) != "type" or c.index == best.index]


def score_type_column(column: ColumnDecision, rows: list[list[str]]) -> tuple[int, int]:
    header = normalize_header(column.raw_header)
    score = column.score
    if any(word in header for word in ("signal type", "pin type", "terminal type", "io type", "i o type")):
        score += 20
    if header in {"type", "i o", "io", "i/o"}:
        score += 10
    if any(word in header for word in ("buffer type", "buffer", "reset state", "power source")):
        score -= 15
    values = [row[column.index] for row in rows[:30] if column.index < len(row)]
    score += 3 * sum(looks_like_signal_type(value) for value in values[:20])
    return score, -column.index


def classify_header(header: str) -> tuple[str, int]:
    h = normalize_header(header)
    if not h:
        return "", 0
    if any(word in h for word in ("pin no", "pin number", "ball no", "ball number", "terminal no", "terminal number", "引脚编号", "端子编号")):
        return "pin_no", 5
    if any(word in h for word in ("pin name", "ball name", "signal name", "terminal name", "引脚名称", "信号名称", "引脚名")):
        return "pin_name", 5
    if any(word in h for word in ("signal type", "pin type", "terminal type", "io type", "i o type", "引脚类型", "信号类型")):
        return "type", 5
    if h in {"no", "no.", "number", "signal no", "signal no."}:
        return "pin_no", 4
    if h in {"signal", "name"}:
        return "pin_name", 3
    if h in {"type", "io", "i o", "i/o"} or h.endswith(" type"):
        return "type", 3
    if "package" in h and "pin" not in h:
        return "package", 2
    return "", 0


def classify_values(values: list[str]) -> tuple[str, int]:
    sample = [str(value).strip() for value in values if str(value).strip()][:20]
    if not sample:
        return "", 0
    if sum(looks_like_pin_list(v) for v in sample) >= max(2, len(sample) // 3):
        return "pin_no", 3
    if sum(looks_like_signal_type(v) for v in sample) >= max(2, len(sample) // 3):
        return "type", 2
    return "", 0


# ---------------------------------------------------------------------------
# 行提取和清洗：这里不再决定表格是否有效
# ---------------------------------------------------------------------------


def extract_records_from_row(row: list[str], columns: list[ColumnDecision]) -> list[dict[str, Any]]:
    """按已确定的列映射读取一整行；空值不触发表级过滤。"""

    fields: dict[str, str] = {}
    package_columns: list[tuple[str, str]] = []
    raw_fields: dict[str, str] = {}
    for column in columns:
        value = row[column.index].strip() if column.index < len(row) else ""
        raw_fields[column.raw_header or f"column_{column.index + 1}"] = value
        field_name = normalize_field_name(column.field_name)
        if column.field_name == "package_pin_no":
            package_columns.append((column.raw_header, value))
        elif field_name:
            # 同一个字段有多个列时保留原始信息，不在这里合并 pin 记录。
            fields[field_name] = merge_field_value(fields.get(field_name, ""), value)

    records = []
    if package_columns:
        for package_header, value in package_columns:
            for pin_no in split_pin_numbers(value):
                record = dict(fields)
                record["pin_no"] = pin_no
                record["_pkg"] = clean_package_label(package_header)
                record["_raw_fields"] = raw_fields
                records.append(record)
        if records:
            return records

    pin_value = fields.get("pin_no", "")
    for pin_no in split_pin_numbers(pin_value):
        record = dict(fields)
        record["pin_no"] = pin_no
        record["_raw_fields"] = raw_fields
        records.append(record)
    return records


def split_pin_numbers(value: str) -> list[str]:
    """只按原文显式分隔符拆 pin_no，保留 A1-C3 等无分隔范围文本。"""

    value = plain_text(str(value or "")).strip()
    if not value:
        return []
    parts = [part.strip() for part in re.split(r"[\s,，;/／、|]+", value) if part.strip()]
    return parts or [value]


def normalize_pin_record(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["pin_no"] = plain_text(str(result.get("pin_no", ""))).strip()
    result["pin_name"] = clean_pin_name(result.get("pin_name", ""))
    return result


def clean_pin_name(value: Any) -> str:
    value = plain_text(str(value or "")).strip()
    value = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", value)
    value = re.sub(r"\s*[（(]\s*continued\s*[）)]\s*$", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip() or "Reserved"


def add_pin_record_to_group(group: ExtractedGroup, record: dict[str, Any]) -> None:
    # 项目规则：每条行记录独立保留，绝不按 pin_no 去重或合并。
    group.pin_list.append(normalize_pin_record(record))


# ---------------------------------------------------------------------------
# 表格读取、标题、分组和封装归并
# ---------------------------------------------------------------------------


TR_RE = re.compile(r"(<tr[^>]*>)(.*?)(</tr>)", re.DOTALL | re.IGNORECASE)
CELL_RE = re.compile(r"(<t[dh][^>]*>)(.*?)(</t[dh]>)", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
BALL_TOKEN_RE = re.compile(r"\b[A-Z]{1,2}\d{1,3}\b")
PACKAGE_RE = re.compile(r"\b(?:\d{2,4}\s*)?(?:qfp|lqfp|tqfp|qfn|bga|soic|sop|pdip|dfn|wqfn|zce\d*[a-z]*|nzn\d*[a-z]*|zwt|zjz|zay|rgz|rha|rhb|pz|pm|pw)\b", re.IGNORECASE)
TYPE_VALUES = {"i", "o", "io", "i/o", "i/o/z", "oz", "od", "odz", "p", "ipu", "ipd", "analog", "digital", "power", "ground", "gnd", "supply", "input", "output"}


def parse_html_table(html: str) -> list[list[str]]:
    rows = []
    for tr in TR_RE.finditer(html):
        row = [plain_text(match.group(2)) for match in CELL_RE.finditer(tr.group(2))]
        if row:
            rows.append(row)
    return rows


def choose_header_row(rows: list[list[str]], title: str = "", semantic: bool = False) -> tuple[int, list[str]]:
    best = (-1, -1, [])
    for index in range(min(6, len(rows))):
        headers = build_combined_headers(rows, index)
        score = sum(classify_header(header)[1] for header in headers)
        if score > best[1]:
            best = (index, score, headers)
    if best[1] < (2 if semantic else 4):
        return -1, []
    return best[0], best[2]


def build_combined_headers(rows: list[list[str]], header_index: int) -> list[str]:
    width = max((len(row) for row in rows[: header_index + 1]), default=0)
    result = []
    for index in range(width):
        parts = []
        for row in rows[: header_index + 1]:
            if index < len(row) and row[index].strip() and row[index].strip() not in parts:
                parts.append(row[index].strip())
        result.append(" ".join(parts))
    return result


def is_group_row(row: list[str]) -> bool:
    values = [cell.strip() for cell in row if cell.strip()]
    return bool(values) and (len(values) == 1 or len(set(values)) == 1) and not looks_like_pin_list(values[0])


def first_non_empty(row: list[str]) -> str:
    return next((cell.strip() for cell in row if cell.strip()), "")


def infer_group_name(text: str) -> str:
    text = plain_text(text)
    if not text:
        return ""
    match = re.search(r"((?:Table|表格?|表)\s*[\w.\-一二三四五六七八九十百千万]*\s*[^\n]{0,160}(?:Pin|Terminal|Signal|Connectivity|Connection|引脚|端子|信号|封装)[^\n]{0,100})", text, re.IGNORECASE)
    return clean_group_name(match.group(1) if match else "")


def infer_group_name_from_headers(headers: list[str], package: str = "") -> str:
    h = normalize_header(" ".join(headers))
    suffix = f", {package}" if package else ""
    if "connection requirements" in h or "connectivity requirements" in h:
        return f"Connectivity Requirements{suffix}"
    if "description" in h and ("function" in h or "signal type" in h):
        return f"Signal Descriptions{suffix}"
    if "pin" in h or "ball" in h or "terminal" in h:
        return f"Pin Attributes{suffix}"
    return ""


def clean_group_name(value: str) -> str:
    value = re.sub(r"\s+", " ", plain_text(value)).strip()
    value = re.sub(r"\s*[（(]\s*continued\s*[）)]", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(?:table|表格?|表)\s*[\w.\-一二三四五六七八九十百千万]+\s*[:：.\-–—]?\s*", "", value, flags=re.IGNORECASE)
    return value or ""


def infer_package_name(text: str) -> str:
    text = plain_text(text)
    match = re.search(r"\b([A-Z0-9][A-Z0-9-]{1,20})\s+Package\b", text, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def build_package_identity(label: str) -> PackageIdentity:
    display = clean_package_label(label)
    if looks_like_pin_list(display):
        display = ""
    compact = normalize_header(display)
    code = ""
    match = PACKAGE_RE.search(display)
    if match:
        code = match.group(0).upper().replace(" ", "")
    key = f"code={code}" if code else f"label={compact or 'unknown'}"
    return PackageIdentity(display, key, code=code)


def get_package_bucket(packages: dict[str, dict[str, Any]], identity: PackageIdentity) -> dict[str, Any]:
    key = identity.key
    if key not in packages:
        packages[key] = {"pkg": "", "pkg_key": key, "_groups": {}, "_aliases": [], "_identity": identity}
    bucket = packages[key]
    if identity.display and identity.display not in bucket["_aliases"]:
        bucket["_aliases"].append(identity.display)
        bucket["pkg"] = " | ".join(bucket["_aliases"])
    return bucket


def build_package_snapshots(packages: dict[str, dict[str, Any]]) -> list[PackageSnapshot]:
    snapshots = []
    for bucket in packages.values():
        pins, names = set(), set()
        for group in bucket["_groups"].values():
            for pin in group.pin_list:
                pins.add(pin.get("pin_no", ""))
                names.add(pin.get("pin_name", "").upper())
        snapshots.append(PackageSnapshot(bucket.get("pkg", ""), pins, names))
    return snapshots


def get_or_create_group(bucket: dict[str, Any], name: str) -> ExtractedGroup:
    name = clean_group_name(name) or "Pin/Package Table"
    if name not in bucket["_groups"]:
        bucket["_groups"][name] = ExtractedGroup(name)
    return bucket["_groups"][name]


def build_public_result(packages: dict[str, dict[str, Any]], include_debug: bool) -> list[dict[str, Any]]:
    result = []
    for bucket in packages.values():
        groups = [{"group": group.group, "pin_list": group.pin_list} for group in bucket["_groups"].values() if group.pin_list]
        if groups:
            item = {"pkg": bucket["pkg"], "group_list": groups}
            if include_debug:
                item["pkg_key"] = bucket["pkg_key"]
            result.append(item)
    return result


def clean_package_label(value: str) -> str:
    value = plain_text(value)
    value = re.sub(r"\[[^]]+\]", "", value)
    value = re.sub(r"\bpackage\b", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip(" -:/")


# ---------------------------------------------------------------------------
# 候选表来源
# ---------------------------------------------------------------------------


def iter_table_candidates(middle_json: dict[str, Any]) -> list[TableCandidate]:
    result = []
    current_title = ""
    for page in middle_json.get("pdf_info", []):
        page_idx = page.get("page_idx") if isinstance(page, dict) else None
        recent = []
        for span in iter_spans_in_reading_order(page):
            html = span.get("html") if isinstance(span, dict) else None
            text = plain_text((span.get("content") or span.get("text") or "") if isinstance(span, dict) else "")
            if isinstance(html, str) and "<table" in html.lower():
                result.append(TableCandidate(html, page_idx if isinstance(page_idx, int) else None, current_title or " ".join(recent[-5:])))
            elif text:
                current_title = infer_group_name(text) or current_title
                recent.append(text)
                recent = recent[-10:]
    return result


def iter_table_candidates_from_markdown(markdown: str) -> list[TableCandidate]:
    result = []
    table_re = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)
    cursor = 0
    last_title = ""
    for match in table_re.finditer(markdown):
        before = markdown[cursor : match.start()]
        texts = [plain_text(line).strip() for line in before.splitlines() if plain_text(line).strip()]
        for text in texts:
            last_title = infer_group_name(text) or last_title
        result.append(TableCandidate(match.group(0), None, last_title or " ".join(texts[-5:])))
        cursor = match.end()
    return result


def iter_spans_in_reading_order(value: Any):
    if isinstance(value, dict):
        for key in ("spans", "para_blocks", "blocks", "lines"):
            child = value.get(key)
            if isinstance(child, list):
                for item in child:
                    yield from iter_spans_in_reading_order(item)
        if any(key in value for key in ("html", "content", "text")):
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_spans_in_reading_order(item)


# ---------------------------------------------------------------------------
# 小工具、输出和调试
# ---------------------------------------------------------------------------


def normalize_field_name(name: str) -> str:
    if name in {"ball_no", "terminal_no"}:
        return "pin_no"
    if name in {"ball_name", "signal_name", "terminal_name", "pad_name"}:
        return "pin_name"
    if name == "io_type":
        return "type"
    return name


def looks_like_pin_list(value: str) -> bool:
    value = str(value).strip()
    return bool(re.fullmatch(r"\d{1,4}", value) or BALL_TOKEN_RE.search(value))


def looks_like_signal_type(value: str) -> bool:
    return normalize_header(str(value)).replace(" ", "") in TYPE_VALUES


def merge_field_value(left: str, right: str) -> str:
    if not left:
        return right
    if not right or left == right:
        return left
    return f"{left} | {right}"


def normalize_header(value: str) -> str:
    value = plain_text(value).lower()
    value = re.sub(r"\[[^]]+\]", " ", value)
    value = re.sub(r"[$\\^†‡*]+", " ", value)
    value = re.sub(r"[_\-/]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def plain_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", " ", str(value), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", html_lib.unescape(TAG_RE.sub("", value))).strip()


def parse_column_index(value: Any) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def safe_header(headers: list[str], index: int) -> str:
    return headers[index] if 0 <= index < len(headers) else f"column_{index + 1}"


def is_invalid_schema_package_name(value: str) -> bool:
    normalized = normalize_header(value)
    if not normalized:
        return False
    if any(word in normalized for word in ("pin name", "default mapping", "function", "description")):
        return True
    # 例如模型误把“A1 B4 A4”当作封装名。
    tokens = re.findall(r"\b[A-Z]{1,2}\d{1,3}\b", value.upper())
    return len(tokens) >= 2 and len(tokens) == len(value.split())


def skip(debug: dict[str, Any], reason: str) -> None:
    debug["status"] = "skipped"
    debug["skip_reason"] = reason


def decisions_to_debug(columns: list[ColumnDecision]) -> list[dict[str, Any]]:
    return [{"index": c.index, "header": c.raw_header, "field": c.field_name, "score": c.score} for c in columns]


def decision_to_debug(decision: TableDecision) -> dict[str, Any]:
    return {
        "should_extract": decision.should_extract,
        "table_role": decision.table_role,
        "pkg": decision.pkg,
        "group": decision.group,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "columns": decisions_to_debug(decision.columns),
    }


def get_last_extraction_debug() -> list[dict[str, Any]]:
    return LAST_EXTRACTION_DEBUG


def strip_debug_fields(result: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean = []
    for package in result:
        item = {"pkg": package.get("pkg", ""), "group_list": []}
        for group in package.get("group_list", []):
            pins = []
            for pin in group.get("pin_list", []):
                pins.append({k: v for k, v in pin.items() if k not in {"source", "source_page", "raw_fields"}})
            if pins:
                item["group_list"].append({"group": group.get("group", ""), "pin_list": pins})
        if item["group_list"]:
            clean.append(item)
    return clean


def write_extraction_json(result: Any, output_path: str | Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def build_extraction_summary(result: list[dict[str, Any]], pdf_name: str = "") -> dict[str, Any]:
    packages = []
    total = 0
    for package in result:
        groups = []
        count = 0
        for group in package.get("group_list", []):
            pins = group.get("pin_list", [])
            pages = sorted({p.get("source_page") for p in pins if isinstance(p.get("source_page"), int)})
            count += len(pins)
            groups.append({"group": group.get("group", ""), "pin_count": len(pins), "page_start": pages[0] if pages else None, "page_end": pages[-1] if pages else None, "pages": pages})
        total += count
        packages.append({"pkg": package.get("pkg", ""), "pin_count": count, "table_count": len(groups), "group_list": groups})
    return {"pdf": pdf_name, "package_count": len(packages), "pin_count": total, "packages": packages}

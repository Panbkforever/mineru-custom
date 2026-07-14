"""
Optional LLM semantic classifier for table roles.

The classifier only decides whether a table can create pin/package records.
Final extraction is still performed by deterministic code in pin_package_extractor.py.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
PIN_TABLE_ROLES = {
    "pin_package_mapping",
    "pin_attributes",
    "terminal_functions",
    "package_connectivity_requirements",
    "signal_description",
    "pin_signal_description",
}


def classify_table_semantics(
    title: str,
    headers: list[str],
    sample_rows: list[list[str]],
    decisions: list[Any],
) -> dict[str, Any]:
    """Classify table semantics with DeepSeek and return a normalized decision."""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("启用语义分类需要先设置环境变量 DEEPSEEK_API_KEY")

    payload = build_prompt_payload(title, headers, sample_rows, decisions)
    response = call_deepseek_json(payload, api_key=api_key)
    return normalize_semantic_response(response)


def classify_table_schema(
    table_id: int,
    title: str,
    headers: list[str],
    sample_rows: list[list[str]],
    rule_decisions: list[Any] | None = None,
) -> dict[str, Any]:
    """Ask the model for table role and column-level field mapping."""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("启用语义字段判断需要先设置环境变量 DEEPSEEK_API_KEY")

    payload = build_schema_prompt_payload(
        table_id=table_id,
        title=title,
        headers=headers,
        sample_rows=sample_rows,
        rule_decisions=rule_decisions or [],
    )
    response = call_deepseek_json(payload, api_key=api_key)
    return normalize_schema_response(response, headers)


def build_schema_prompt_payload(
    table_id: int,
    title: str,
    headers: list[str],
    sample_rows: list[list[str]],
    rule_decisions: list[Any],
) -> dict[str, Any]:
    return {
        "task": (
            "Identify whether this semiconductor datasheet table contains package, "
            "pin, terminal, ball, or signal records, and map useful columns."
        ),
        "rules": [
            "Use semantic meaning, not exact header names only.",
            "A valid pin table may have package columns, or may directly map pin/ball/terminal numbers to signal names.",
            "A table about port functions, pin multiplexing, alternate function selection, or default mapping is not a physical pin extraction table unless it has explicit physical package/ball/terminal pin number columns.",
            "Headers like PIN NAME (P1.x), Port P1, Port P2, GPIO function, default mapping, or module function describe alternate functions, not package identities.",
            "Do not classify timing/electrical/ordering/boot-mode tables as extractable pin tables.",
            "For multiple type-like columns, choose the one most related to the signal/pin itself, usually SIGNAL TYPE before BUFFER TYPE.",
            "When a table has multiple name columns, preserve their semantic roles: use signal_name for SIGNAL NAME, pin_name for PIN NAME, ball_name for BALL NAME, terminal_name for TERMINAL NAME, and pad_name for PAD NAME. Do not merge these columns into one field.",
            "ABY BALL, BALL NUMBER, BALL NO., PIN NUMBER, PIN NO., TERMINAL NUMBER, and TERMINAL NO. are physical pin-number columns, not name columns. Use pin_no for them when their values look like coordinates such as A1, B10, or P23.",
            "Return ignore for description, condition, min/typ/max/unit, reset state, power source, notes, package quantity, and ordering columns unless they are the only useful name/type evidence.",
        ],
        "allowed_fields": [
            "pin_no",
            "pin_name",
            "signal_name",
            "ball_name",
            "terminal_name",
            "pad_name",
            "type",
            "ignore",
        ],
        "valid_table_roles": [
            "pin_package_mapping",
            "pin_attributes",
            "terminal_functions",
            "signal_description",
            "package_connectivity_requirements",
            "port_function_table",
            "pin_mux_table",
            "alternate_function_table",
            "default_mapping_table",
            "module_signal_connection",
            "irrelevant",
            "timing_table",
            "electrical_conditions",
            "ordering_table",
            "boot_mode_table",
        ],
        "table": {
            "table_id": table_id,
            "title": title,
            "headers": headers,
            "sample_rows": sample_rows[:12],
            "rule_column_decisions": [
                {
                    "index": getattr(decision, "index", None),
                    "header": getattr(decision, "raw_header", ""),
                    "field": getattr(decision, "field_name", ""),
                    "score": getattr(decision, "score", 0),
                }
                for decision in rule_decisions
            ],
        },
        "output_schema": {
            "table_id": table_id,
            "table_role": "one valid_table_roles value",
            "should_extract": "boolean",
            "columns": [
                {
                    "column_index": 0,
                    "field": "pin_no|pin_name|signal_name|ball_name|terminal_name|pad_name|type|ignore",
                    "confidence": 0.0,
                    "reason": "short reason",
                }
            ],
            "confidence": 0.0,
            "reason": "short reason",
        },
    }


def build_prompt_payload(
    title: str,
    headers: list[str],
    sample_rows: list[list[str]],
    decisions: list[Any],
) -> dict[str, Any]:
    return {
        "task": "Classify whether this table expresses semiconductor pin/terminal/ball records.",
        "target_relation": {
            "description": (
                "A valid table may either map package identities to physical pin/ball/terminal "
                "numbers, or directly map physical pin/ball/terminal numbers to signal names "
                "and optional pin/signal types. A package name is useful but not required for "
                "single-package signal description tables."
            ),
            "valid_examples": [
                "ZCE Ball Number | NZN Ball Number | Ball Name | Signal Name | Signal Type",
                "64 LQFP | 48 VQFN | Pin Name | Type",
                "TERMINAL NAME | NO. | I/O | DESCRIPTION",
                "SIGNAL NAME | SIGNAL NO. | TYPE | DESCRIPTION",
                "Connectivity Requirements - AM273x ZCE Package with BALL NUMBER and BALL NAME",
            ],
            "invalid_examples": [
                "Input Conditions / Output Conditions / Timing Requirements",
                "SOP or Boot mode tables where numbers are mode values",
                "Orderable device or package quantity tables",
                "Electrical condition tables where NO. is a row index",
            ],
        },
        "table": {
            "title": title,
            "headers": headers,
            "sample_rows": sample_rows[:8],
            "rule_column_decisions": [
                {
                    "index": getattr(decision, "index", None),
                    "header": getattr(decision, "raw_header", ""),
                    "field": getattr(decision, "field_name", ""),
                    "score": getattr(decision, "score", 0),
                }
                for decision in decisions
            ],
        },
        "output_schema": {
            "table_role": "pin_package_mapping | pin_attributes | terminal_functions | package_connectivity_requirements | supplemental_signal_description | electrical_conditions | timing_table | boot_mode_table | ordering_table | other",
            "should_create_pins": "boolean",
            "package_columns": [{"column_index": 0, "pkg": "package name", "field": "pin_no"}],
            "pin_columns": [{"column_index": 0, "field": "pin_no"}],
            "name_columns": [{"column_index": 0, "field": "pin_name|ball_name|signal_name"}],
            "type_columns": [{"column_index": 0, "field": "type"}],
            "confidence": "0.0-1.0",
            "reason": "short reason",
        },
    }


def call_deepseek_json(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)
    timeout = float(os.getenv("DEEPSEEK_TIMEOUT", "30"))
    url = f"{base_url}/chat/completions"

    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict table semantic classifier for semiconductor datasheets. "
                    "Return valid JSON only. Do not extract final pin records."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": int(os.getenv("DEEPSEEK_MAX_TOKENS", "3000")),
        "stream": False,
    }

    last_error: Exception | None = None
    max_retries = max(1, int(os.getenv("DEEPSEEK_MAX_RETRIES", "4")))
    retry_base = float(os.getenv("DEEPSEEK_RETRY_BASE_SECONDS", "2"))
    for attempt in range(max_retries):
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            last_error = RuntimeError(f"DeepSeek API 请求失败: HTTP {exc.code} {detail}")
            if exc.code == 429 and attempt < max_retries - 1:
                time.sleep(retry_base * (2**attempt))
                continue
            raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"DeepSeek API 请求失败: {exc}")
            if attempt < max_retries - 1:
                time.sleep(retry_base * (2**attempt))
                continue
            raise last_error from exc
        except TimeoutError as exc:
            last_error = RuntimeError(f"DeepSeek API 请求超时: {exc}")
            if attempt < max_retries - 1:
                time.sleep(retry_base * (2**attempt))
                continue
            raise last_error from exc

        completion = json.loads(raw)
        content = str(completion["choices"][0]["message"].get("content") or "")
        try:
            return parse_json_content(content)
        except ValueError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(retry_base)
                continue
            break
    raise last_error or ValueError("DeepSeek returned invalid JSON")


def parse_json_content(content: str) -> dict[str, Any]:
    """Parse JSON even when the model wraps it in markdown or extra text."""
    content = content.strip()
    if not content:
        raise ValueError("DeepSeek returned empty message content")

    candidates = [content]
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL | re.IGNORECASE)
    if fenced_match:
        candidates.insert(0, fenced_match.group(1))

    object_match = re.search(r"\{.*\}", content, re.DOTALL)
    if object_match:
        candidates.append(object_match.group(0))

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            return parsed

    preview = content[:300].replace("\n", "\\n")
    raise ValueError(f"DeepSeek returned non-JSON content: {preview}") from last_error


def normalize_semantic_response(response: dict[str, Any]) -> dict[str, Any]:
    role = str(response.get("table_role", "other")).strip()
    should_create = bool(response.get("should_create_pins", False))
    confidence = response.get("confidence", 0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "table_role": role,
        "should_create_pins": should_create and role in PIN_TABLE_ROLES,
        "package_columns": response.get("package_columns") or [],
        "pin_columns": response.get("pin_columns") or [],
        "name_columns": response.get("name_columns") or [],
        "type_columns": response.get("type_columns") or [],
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(response.get("reason", "")).strip(),
    }


def normalize_schema_response(response: dict[str, Any], headers: list[str]) -> dict[str, Any]:
    role = str(response.get("table_role", "irrelevant")).strip()
    confidence = response.get("confidence", 0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    # 保留模型返回的每个名称列的具体语义，后面才能执行
    # SIGNAL NAME > PIN NAME > TERMINAL NAME > BALL NAME > PAD NAME 的选择规则。
    columns = []
    seen: set[tuple[int, str]] = set()
    for item in response.get("columns") or []:
        try:
            index = int(item.get("column_index"))
        except (TypeError, ValueError, AttributeError):
            continue
        if index < 0 or index >= max(1, len(headers)):
            continue
        field = normalize_schema_field(str(item.get("field") or "ignore"))
        key = (index, field)
        if key in seen:
            continue
        seen.add(key)
        item_confidence = item.get("confidence", confidence)
        try:
            item_confidence = float(item_confidence)
        except (TypeError, ValueError):
            item_confidence = confidence
        columns.append(
            {
                "column_index": index,
                "header": headers[index] if index < len(headers) else f"column_{index + 1}",
                "field": field,
                "pkg": str(item.get("pkg") or "").strip(),
                "confidence": max(0.0, min(1.0, item_confidence)),
                "reason": str(item.get("reason") or "").strip(),
            }
        )

    return {
        "table_id": response.get("table_id"),
        "table_role": role,
        "should_extract": bool(response.get("should_extract")) and role in PIN_TABLE_ROLES,
        "pkg": str(response.get("pkg") or "").strip(),
        "group": str(response.get("group") or "").strip(),
        "columns": columns,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(response.get("reason") or "").strip(),
    }


def normalize_schema_field(field: str) -> str:
    """把模型字段名归一化，但保留不同名称列的语义来源。

    这里不能把 ``SIGNAL NAME`` 和 ``BALL NAME`` 都压成 ``pin_name``。
    后续提取阶段需要根据列语义优先选择 SIGNAL NAME，只有它为空时才回退
    到 PIN NAME、BALL NAME 等列；如果在这里提前合并，优先级信息就丢失了。
    """
    normalized = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
    aliases = {
        "ball_no": "pin_no",
        "ball_number": "pin_no",
        "terminal_no": "pin_no",
        "terminal_number": "pin_no",
        "pin_number": "pin_no",
        "signal_no": "pin_no",
        "signal_number": "pin_no",
        "pin_signal_name": "signal_name",
        "signal_type": "type",
        "pin_type": "type",
        "io_type": "type",
        "i_o": "type",
        "package_name": "package",
        "pkg": "package",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {
        "pin_no",
        "pin_name",
        "signal_name",
        "ball_name",
        "terminal_name",
        "pad_name",
        "type",
        "ignore",
    }:
        return "ignore"
    return normalized

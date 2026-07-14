"""用大模型判断候选表是否需要提取，以及需要提取哪些列。

模型层的职责严格限制为两项：

1. 接收初筛后的表格标题、表头和完整表格内容，判断该表是否需要提取。
2. 表格需要提取时，把目标列映射为 ``pin_no``、``pin_name`` 或 ``type``。

模型不判断封装名、分组名或表格角色，也不生成最终引脚记录。后续逐行提取、
字段清洗、封装归并和分组均由 ``pin_package_extractor.py`` 负责。
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


def classify_table_schema(
    title: str,
    headers: list[str],
    table_rows: list[list[str]],
) -> dict[str, Any]:
    """将一张完整候选表交给模型，并返回规范化的表/字段判断。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("启用语义字段判断需要先设置环境变量 DEEPSEEK_API_KEY")

    payload = build_schema_prompt_payload(
        title=title,
        headers=headers,
        table_rows=table_rows,
    )
    response = call_deepseek_json(payload, api_key=api_key)
    return normalize_schema_response(response, headers)


def build_schema_prompt_payload(
    title: str,
    headers: list[str],
    table_rows: list[list[str]],
) -> dict[str, Any]:
    """构造最小模型请求；``table_rows`` 不截断，必须包含完整表格。"""

    return {
        "task": (
            "Decide whether this semiconductor datasheet table should be used to extract "
            "physical pin/ball/terminal records. If it should, identify only the columns "
            "needed as pin_no, pin_name, or type."
        ),
        "rules": [
            "Use semantic meaning, not exact header names only.",
            "Return should_extract=true only when rows express physical pin, ball, or terminal identifiers and their names/signals.",
            "For multiple name-like columns, select only the column that is the actual physical pin/signal name.",
            "For multiple type-like columns, select only the type most directly describing the pin/signal, such as SIGNAL TYPE rather than BUFFER TYPE.",
            "Do not select description, conditions, min/typ/max, unit, reset state, power source, notes, ordering, or other auxiliary columns.",
            "Do not return package names, group names, table roles, confidence scores, reasons, or extracted row values.",
        ],
        "allowed_fields": ["pin_no", "pin_name", "type"],
        "table": {
            "title": title,
            "headers": headers,
            # 不使用 sample_rows，也不截断。模型看到的是初筛后的完整表格。
            "rows": table_rows,
        },
        "output_schema": {
            "should_extract": "boolean",
            "columns": [
                {
                    "column_index": 0,
                    "field": "pin_no|pin_name|type",
                }
            ],
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
                    "You only decide whether one complete semiconductor datasheet table should "
                    "be extracted and map its needed columns. Return valid JSON containing only "
                    "should_extract and columns. Do not return any other fields or final pin records."
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


def normalize_schema_response(response: dict[str, Any], headers: list[str]) -> dict[str, Any]:
    """只保留约定的两个返回字段，并过滤越界或非法的列映射。"""

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
        if field == "ignore":
            continue
        key = (index, field)
        if key in seen:
            continue
        seen.add(key)
        columns.append(
            {
                "column_index": index,
                "header": headers[index] if index < len(headers) else f"column_{index + 1}",
                "field": field,
            }
        )

    return {
        "should_extract": bool(response.get("should_extract")),
        "columns": columns,
    }


def normalize_schema_field(field: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
    aliases = {
        "ball_no": "pin_no",
        "ball_number": "pin_no",
        "terminal_no": "pin_no",
        "terminal_number": "pin_no",
        "pin_number": "pin_no",
        "signal_no": "pin_no",
        "signal_number": "pin_no",
        "ball_name": "pin_name",
        "signal_name": "pin_name",
        "terminal_name": "pin_name",
        "pin_signal_name": "pin_name",
        "signal_type": "type",
        "pin_type": "type",
        "io_type": "type",
        "i_o": "type",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"pin_no", "pin_name", "type"}:
        return "ignore"
    return normalized

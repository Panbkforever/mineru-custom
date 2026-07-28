"""用大模型判断候选表是否需要提取，以及需要提取哪些列。

模型层的职责严格限制为两项：

1. 接收初筛后的表格标题、表头和完整表格内容，判断该表是否需要提取。
2. 表格需要提取时，把目标列映射为 ``pin_no``、``pin_name`` 或 ``type``。

``classify_table_schema`` 不判断封装名、分组名或表格角色，也不生成最终引脚
记录。文档级封装总述表使用独立的 ``classify_package_catalog_table`` 请求和
返回协议，两种任务不能复用返回字段。
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


def classify_package_catalog_table(
    table: Any,
    source_name: str = "",
) -> dict[str, Any]:
    """判断完整候选表是否为封装总述表，并返回独立 pinout 的真实名称。

    ``table`` 使用鸭子类型，避免语义模块反向依赖封装目录的数据类。
    """

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("启用封装目录判断需要先设置环境变量 DEEPSEEK_API_KEY")

    payload = {
        "task": (
            "Decide whether this complete table is a document-level package/device summary. "
            "If it is, list the canonical real names of the independent physical pinout "
            "namespaces described by the document."
        ),
        "rules": [
            "Read the complete table, title and chapter context.",
            "A package entry means one independent physical pin/ball mapping namespace.",
            "Return model or device variants separately only when the document gives them independent physical pin mappings.",
            "When one table contains multiple package-specific physical pin-number columns, return each independent package column.",
            "Do not return every orderable SKU, temperature suffix, tape-and-reel variant, package quantity or package drawing row as a separate package.",
            "Do not return unrelated comparison devices merely because they appear in a comparison table.",
            "The name must be one short canonical string of at most 15 characters present in, or a continuous substring of, the supplied evidence.",
            "Return exactly one name per entry. Never join candidates with |.",
            "This task does not identify pin_no, pin_name, type, group names or row values.",
        ],
        "document": {"source_name": source_name},
        "table": {
            "table_id": getattr(table, "table_id", None),
            "page": (
                getattr(table, "page_idx", None) + 1
                if isinstance(getattr(table, "page_idx", None), int)
                else None
            ),
            "title": getattr(table, "title", ""),
            "group_context": getattr(table, "group_context", ""),
            "current_chapter_titles": list(
                getattr(table, "current_chapter_titles", ()) or ()
            ),
            "headers": list(getattr(table, "headers", ()) or ()),
            "rows": [
                list(row)
                for row in (getattr(table, "rows", ()) or ())
            ],
        },
        "output_schema": {
            "is_package_summary": "boolean",
            "packages": [
                {
                    "name": "one canonical real package/device pinout name",
                    "aliases": ["explicit alias only"],
                    "package_type": "optional package family/type",
                    "pin_count": "optional integer",
                }
            ],
        },
    }
    response = call_model_json(
        payload,
        api_key=api_key,
        system_prompt=(
            "You identify a document's independent semiconductor package/pinout namespaces "
            "from one complete candidate summary table. Return valid JSON containing only "
            "is_package_summary and packages. Do not map pin fields or generate pin records."
        ),
        max_tokens=int(os.getenv("EXTRACT_PACKAGE_MAX_TOKENS", "4000")),
        timeout=float(os.getenv("EXTRACT_PACKAGE_TIMEOUT", "60")),
    )
    return normalize_package_catalog_response(response)


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
            "Return should_extract=true only when the table has at least one column containing physical pin, ball, or terminal identifiers.",
            "If there is no physical pin_no, ball-number, or terminal-number column, return should_extract=false even when the table contains logical Pin Name, Signal Name, Type, Mode, or Function columns.",
            "A pin_name column is optional. A physical-number-only table may still be extracted because missing names are filled as Reserved by deterministic code.",
            "If different packages have separate physical pin/ball number columns, return every one of those columns as pin_no; do not select only one package.",
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
    """调用唯一的字段判断模型并返回 JSON。"""

    return call_model_json(
        payload,
        api_key=api_key,
        system_prompt=(
            "You only decide whether one complete semiconductor datasheet table should "
            "be extracted and map its needed columns. Return valid JSON containing only "
            "should_extract and columns. Do not return any other fields or final pin records."
        ),
        max_tokens=int(os.getenv("DEEPSEEK_MAX_TOKENS", "3000")),
    )


def call_model_json(
    payload: dict[str, Any],
    *,
    api_key: str,
    system_prompt: str,
    max_tokens: int,
    timeout: float | None = None,
) -> dict[str, Any]:
    """调用 OpenAI 兼容 JSON 接口；业务协议由调用方的 prompt 决定。"""

    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)
    request_timeout = (
        float(timeout)
        if timeout is not None
        else float(os.getenv("DEEPSEEK_TIMEOUT", "30"))
    )
    url = f"{base_url}/chat/completions"

    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": max_tokens,
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
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
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


def normalize_package_catalog_response(response: dict[str, Any]) -> dict[str, Any]:
    """只保留封装总述判断协议中的字段，禁止混入字段映射或最终记录。"""

    packages = []
    for item in response.get("packages") or []:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()
        if not name or "|" in name:
            continue
        aliases = []
        for alias in item.get("aliases") or []:
            alias = re.sub(r"\s+", " ", str(alias or "")).strip()
            if alias and "|" not in alias and alias != name and alias not in aliases:
                aliases.append(alias)
        packages.append(
            {
                "name": name,
                "aliases": aliases,
                "package_type": re.sub(
                    r"\s+",
                    " ",
                    str(item.get("package_type") or ""),
                ).strip(),
                "pin_count": item.get("pin_count"),
            }
        )
    return {
        "is_package_summary": bool(response.get("is_package_summary")),
        "packages": packages,
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

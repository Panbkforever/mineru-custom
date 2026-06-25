"""
Association rules for assigning supplemental pin tables to packages.

These rules handle tables that do not have package-specific columns such as
"ZCE Ball Number", but whose title/content clearly ties them to a package. The
extractor still decides which columns are pin fields; this module only decides
whether the current table should inherit or bind to a package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


PIN_TOKEN_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}\b")
PACKAGE_TITLE_RE = re.compile(
    r"(?:^|[-–—:])\s*(?:[A-Z]{2,}\d+[A-Z0-9-]*\s+)?([A-Z]{2,5}\d*[A-Z]?)\s+Package\b",
    re.IGNORECASE,
)
PACKAGE_WORD_RE = re.compile(r"\b([A-Z]{2,5}\d*[A-Z]?)\s+Package\b", re.IGNORECASE)
DEVICE_PREFIX_RE = re.compile(r"^(?:AM|TMS|TPS|SN|LM|MSP|CC)\d", re.IGNORECASE)


@dataclass(frozen=True)
class PackageSnapshot:
    """Known package evidence collected from previously extracted tables."""

    pkg: str
    pin_numbers: set[str] = field(default_factory=set)
    pin_names: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class TableAssociationDecision:
    """Package association decision for the current table."""

    package: str = ""
    confidence: float = 0.0
    reason: str = ""


def resolve_table_package_association(
    title: str,
    headers: list[str],
    data_rows: list[list[str]],
    decisions: list[Any],
    known_packages: list[PackageSnapshot],
) -> TableAssociationDecision:
    """Return the best package association for a table, if confidence is high."""
    title_package = find_package_in_title(title, known_packages)
    if title_package and has_pin_name_table_shape(headers, decisions):
        return TableAssociationDecision(
            package=title_package,
            confidence=0.95,
            reason="title contains package name and table has pin/name columns",
        )

    overlap_decision = associate_by_existing_pin_overlap(data_rows, decisions, known_packages)
    if overlap_decision.confidence >= 0.75:
        return overlap_decision

    if title_package and has_connectivity_context(title, headers):
        return TableAssociationDecision(
            package=title_package,
            confidence=0.85,
            reason="title contains package name and table has connectivity context",
        )

    return TableAssociationDecision()


def find_package_in_title(title: str, known_packages: list[PackageSnapshot]) -> str:
    """Find a package alias/code in the table title."""
    normalized_title = normalize_text(title)
    for snapshot in known_packages:
        for alias in split_package_aliases(snapshot.pkg):
            if alias and re.search(rf"\b{re.escape(alias.lower())}\b", normalized_title):
                return alias

    # Prefer the package code after a separator, e.g. "... - AM273x ZCE Package".
    match = PACKAGE_TITLE_RE.search(title)
    if match and not is_device_prefix(match.group(1)):
        return match.group(1).upper()

    candidates = [
        match.group(1).upper()
        for match in PACKAGE_WORD_RE.finditer(title)
        if not is_device_prefix(match.group(1))
    ]
    return candidates[-1] if candidates else ""


def has_pin_name_table_shape(headers: list[str], decisions: list[Any]) -> bool:
    fields = {str(getattr(decision, "field_name", "")) for decision in decisions}
    if fields & {"package_pin_no"}:
        return True
    has_number = bool(fields & {"pin_no", "ball_no", "terminal_no"})
    has_name = bool(fields & {"pin_name", "ball_name", "signal_name", "terminal_name", "pad_name"})
    return has_number and has_name and has_connectivity_context("", headers)


def has_connectivity_context(title: str, headers: list[str]) -> bool:
    text = normalize_text(" ".join([title, *headers]))
    return any(
        keyword in text
        for keyword in (
            "connectivity requirement",
            "connection requirement",
            "ball number",
            "ball name",
            "terminal number",
            "terminal name",
            "package",
        )
    )


def associate_by_existing_pin_overlap(
    data_rows: list[list[str]],
    decisions: list[Any],
    known_packages: list[PackageSnapshot],
) -> TableAssociationDecision:
    """Use current table pin_no/pin_name overlap with known packages as fallback."""
    if not known_packages:
        return TableAssociationDecision()

    current_pins, current_names = collect_current_pin_evidence(data_rows, decisions)
    if not current_pins and not current_names:
        return TableAssociationDecision()

    scored: list[tuple[float, PackageSnapshot, str]] = []
    for snapshot in known_packages:
        pin_score = overlap_ratio(current_pins, snapshot.pin_numbers)
        name_score = overlap_ratio(current_names, snapshot.pin_names)
        score = max(pin_score, name_score * 0.8)
        reason = f"pin overlap={pin_score:.2f}, name overlap={name_score:.2f}"
        scored.append((score, snapshot, reason))

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_snapshot, best_reason = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= 0.75 and best_score - second_score >= 0.15:
        return TableAssociationDecision(
            package=best_snapshot.pkg,
            confidence=best_score,
            reason=best_reason,
        )
    return TableAssociationDecision()


def collect_current_pin_evidence(
    data_rows: list[list[str]],
    decisions: list[Any],
) -> tuple[set[str], set[str]]:
    pin_indexes = {
        int(getattr(decision, "index"))
        for decision in decisions
        if getattr(decision, "field_name", "") in {"pin_no", "ball_no", "terminal_no"}
    }
    name_indexes = {
        int(getattr(decision, "index"))
        for decision in decisions
        if getattr(decision, "field_name", "") in {"pin_name", "ball_name", "signal_name", "terminal_name", "pad_name"}
    }
    pins: set[str] = set()
    names: set[str] = set()
    for row in data_rows[:80]:
        for index in pin_indexes:
            if index < len(row):
                pins.update(PIN_TOKEN_RE.findall(row[index]))
        for index in name_indexes:
            if index < len(row):
                value = normalize_pin_name(row[index])
                if value:
                    names.add(value)
    return pins, names


def overlap_ratio(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left))


def split_package_aliases(pkg: str) -> list[str]:
    aliases = []
    for part in re.split(r"\s*\|\s*", pkg):
        part = part.strip()
        if not part:
            continue
        aliases.append(part)
        code_match = re.search(r"\b([A-Z]{2,5}\d*[A-Z]?)\b", part, re.IGNORECASE)
        if code_match and not is_device_prefix(code_match.group(1)):
            aliases.append(code_match.group(1).upper())
    return list(dict.fromkeys(aliases))


def normalize_pin_name(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value)).strip().upper()
    if not value or len(value) > 120:
        return ""
    return value


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).lower()).strip()


def is_device_prefix(value: str) -> bool:
    return bool(DEVICE_PREFIX_RE.search(str(value).strip()))

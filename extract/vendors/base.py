"""Common vendor profile contract.

Keep this object intentionally small for now. The TI pipeline is already
stable, so vendor-specific hooks should be added only when a real shared
difference is identified across a manufacturer's PDFs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


HeaderClassifier = Callable[[str], tuple[str, int]]
TableMatcher = Callable[[str, list[str], list[list[str]]], Any]
PhysicalTableGuard = Callable[[str, list[str]], bool]
TableRepairer = Callable[
    [str, list[str], list[list[str]]],
    tuple[list[str], list[list[str]]],
]
RowRepairer = Callable[[str, list[str], list[list[str]]], list[list[str]]]
RecordFilterer = Callable[[dict[str, Any]], bool]
PackageScopeResolver = Callable[..., Any]


@dataclass(frozen=True)
class VendorProfile:
    """Configuration for one manufacturer's extraction behavior."""

    name: str
    display_name: str
    description: str = ""
    header_classifier: HeaderClassifier | None = None
    table_matcher: TableMatcher | None = None
    physical_table_guard: PhysicalTableGuard | None = None
    table_repairer: TableRepairer | None = None
    row_repairer: RowRepairer | None = None
    record_filterer: RecordFilterer | None = None
    package_scope_resolver: PackageScopeResolver | None = None

    @property
    def canonical_name(self) -> str:
        return self.name.upper()

    def classify_header(self, normalized_header: str) -> tuple[str, int]:
        if self.header_classifier is None:
            return "", 0
        return self.header_classifier(normalized_header)

    def match_table(self, title: str, headers: list[str], data_rows: list[list[str]]) -> Any:
        if self.table_matcher is None:
            return None
        return self.table_matcher(title, headers, data_rows)

    def is_physical_table(self, title: str, headers: list[str]) -> bool:
        if self.physical_table_guard is None:
            return False
        return self.physical_table_guard(title, headers)

    def repair_table(
        self,
        title: str,
        headers: list[str],
        rows: list[list[str]],
    ) -> tuple[list[str], list[list[str]]]:
        if self.table_repairer is None:
            return headers, rows
        return self.table_repairer(title, headers, rows)

    def repair_rows(self, title: str, headers: list[str], rows: list[list[str]]) -> list[list[str]]:
        if self.row_repairer is None:
            return rows
        return self.row_repairer(title, headers, rows)

    def should_keep_record(self, record: dict[str, Any]) -> bool:
        if self.record_filterer is None:
            return True
        return self.record_filterer(record)

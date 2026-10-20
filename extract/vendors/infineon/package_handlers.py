"""按明确的输出配置范围绑定槽位，而非按引脚数或表格顺序猜测。"""

import re

from extract.vendors.infineon.table_handlers import physical_fields


def output_scope(title: str) -> str:
    title = re.sub(r"^\s*Table\s+\d+[.:]?\s*", "", title, flags=re.I)
    match = re.fullmatch(r"\s*(Single|Dual)\s+Output(?:\s*\((?:cont['’]?d|continued)\))?\s*", title, re.I)
    return match[1].lower() + " output" if match else ""


def resolve_infineon_scopes(target_tables, multi_package_plans):
    axes = []
    for table in target_tables:
        if physical_fields(list(table.headers)) is None:
            return None
        plan = multi_package_plans.get(table.table_id)
        if plan is None or plan.is_multi_package:
            return None
        axes.append((table, output_scope(table.title)))
    if not axes or not all(scope for _, scope in axes):
        return None
    from extract.package_catalog_resolver import (
        PackageCatalogEntry, PackageCatalogResolution, assignment_from_entry, freeze_package_slots,
    )

    scopes = list(dict.fromkeys(scope for _, scope in axes))
    entries = [PackageCatalogEntry(package_key="", identity_name=scope,
                                   evidence_table_ids=[t.table_id for t, s in axes if s == scope]) for scope in scopes]
    freeze_package_slots(entries)
    assignments = {}
    diagnostics = []
    for table, scope in axes:
        entry = entries[scopes.index(scope)]
        assignment = assignment_from_entry(entry, entries, reason="infineon_explicit_output_scope")
        assignments[(table.table_id, 0)] = assignment
        diagnostics.append({"stage": "package_binding", "table_id": table.table_id, "scope": scope,
                            "status": "resolved", "package_key": entry.package_key, "reason": assignment.reason})
    return PackageCatalogResolution(entries, assignments, diagnostics)

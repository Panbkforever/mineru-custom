"""仅按 Pinning 表中明确的范围标题绑定独立槽位。"""

import re

from extract.vendors.nexperia.table_handlers import is_pinning, roles


def resolve_nexperia_scopes(target_tables, multi_package_plans):
    axes = []
    for table in target_tables:
        plan = multi_package_plans.get(table.table_id)
        if not is_pinning(table.title) or roles(list(table.headers)) is None or plan is None or plan.is_multi_package:
            return None
        _, separator, scope = table.title.partition(" | ")
        if not separator and len(target_tables) != 1:
            # 多张无范围的表不根据编号数量或顺序强行绑定。
            return None
        axes.append((table, scope.strip() if separator else ""))
    if not axes:
        return None
    from extract.package_catalog_resolver import (
        PackageCatalogEntry, PackageCatalogResolution, assignment_from_entry, freeze_package_slots,
    )

    labels = dict.fromkeys(scope for _, scope in axes)
    entries = [PackageCatalogEntry(package_key="", identity_name=scope,
                                   package_type=scope if re.fullmatch(r"SOT\d+[A-Za-z]*", scope, re.I) else "",
                                   evidence_table_ids=[t.table_id for t, s in axes if s == scope]) for scope in labels]
    freeze_package_slots(entries)
    by_scope = dict(zip(labels, entries))
    assignments = {}
    diagnostics = []
    for table, scope in axes:
        entry = by_scope[scope]
        assignment = assignment_from_entry(entry, entries, reason="nexperia_explicit_pinning_scope")
        assignments[(table.table_id, 0)] = assignment
        diagnostics.append({"stage": "package_binding", "table_id": table.table_id, "scope": scope,
                            "status": "resolved", "package_key": entry.package_key, "reason": assignment.reason})
    return PackageCatalogResolution(entries, assignments, diagnostics)

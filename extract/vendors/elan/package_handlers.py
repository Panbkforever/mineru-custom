"""Keep explicit Elan pin-description subsections in separate mapping slots."""

from extract.vendors.elan.native_tables import scope_from_title
from extract.vendors.elan.table_handlers import roles


def resolve_elan_scopes(target_tables, multi_package_plans):
    axes = [(table, scope_from_title(table.title)) for table in target_tables]
    if not axes or any(not scope or len(roles(list(table.headers))["pin_no"]) != 1 for table, scope in axes):
        return None
    scopes = list(dict.fromkeys(scope for _, scope in axes))
    if len(scopes) < 2:
        return None
    from extract.package_catalog_resolver import PackageCatalogEntry, PackageCatalogResolution, assignment_from_entry, freeze_package_slots

    entries = [PackageCatalogEntry(package_key="", identity_name=scope,
                                   evidence_table_ids=[table.table_id for table, key in axes if key == scope]) for scope in scopes]
    freeze_package_slots(entries)
    assignments = {}
    diagnostics = []
    for table, scope in axes:
        entry = entries[scopes.index(scope)]
        assignment = assignment_from_entry(entry, entries, reason="elan_explicit_pin_subsection")
        assignments[(table.table_id, 0)] = assignment
        diagnostics.append({"stage": "package_binding", "table_id": table.table_id, "scope": scope,
                            "status": "resolved", "package_key": entry.package_key, "reason": assignment.reason})
    return PackageCatalogResolution(entries, assignments, diagnostics)

"""Separate explicit ST model groups that publish independent pin mappings."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from extract.vendors.st.table_handlers import (
    _ST_PACKAGE_TERMS_RE,
    _package_label_from_pin_header,
    _st_pin_assignment_roles,
)


def _assignment_model_scope(title: str) -> tuple[str, ...]:
    if not re.search(r"\bpin\s+assignment\b", title, flags=re.IGNORECASE):
        return ()
    package = re.search(
        rf"\b(?:{_ST_PACKAGE_TERMS_RE})(?:\s*\d{{1,3}})?\b",
        title,
        flags=re.IGNORECASE,
    )
    if package is None:
        return ()
    prefix = re.sub(r"^\s*Table\s+\d+\s*[.:]?", "", title[:package.start()], flags=re.IGNORECASE)
    models = re.findall(
        r"\b[A-Z][A-Z0-9]*\d[A-Z0-9]*(?:-[A-Z0-9]+)?\b",
        prefix,
        flags=re.IGNORECASE,
    )
    return tuple(sorted({model.upper() for model in models}))


def resolve_st_package_scopes(
    target_tables: Sequence[Any],
    multi_package_plans: Mapping[int, Any],
) -> Any | None:
    """Resolve only disjoint, explicit model groups with proven package columns.

    The title supplies the model scope; a confirmed pin column supplies the
    package axis. Neither filenames nor pin rows participate in this binding.
    """

    table_axes = []
    scopes: set[tuple[str, ...]] = set()
    for table in target_tables:
        scope = _assignment_model_scope(table.title)
        roles = _st_pin_assignment_roles(list(table.headers))
        if not scope or not roles["pin_no"] or len(roles["pin_name"]) != 1:
            return None
        plan = multi_package_plans.get(table.table_id)
        if plan is None or plan.mode not in {"single_package", "package_columns"}:
            return None
        if plan.is_multi_package:
            labels = [binding.package for binding in plan.bindings]
            if len(labels) != len(roles["pin_no"]):
                return None
        elif len(roles["pin_no"]) == 1:
            labels = [_package_label_from_pin_header(table.headers[roles["pin_no"][0]])]
        else:
            return None
        if not labels or any(not _package_label_from_pin_header(label) for label in labels):
            return None
        labels = [_package_label_from_pin_header(label) for label in labels]
        if len(set(labels)) != len(labels):
            return None
        scopes.add(scope)
        table_axes.append((table, scope, labels))

    if len(scopes) < 2:
        return None
    ordered_scopes = list(dict.fromkeys(scope for _, scope, _ in table_axes))
    for index, scope in enumerate(ordered_scopes):
        if any(set(scope) & set(other) for other in ordered_scopes[index + 1:]):
            return None

    # Identical package names across disjoint model groups are the ambiguity
    # this hook resolves. Other documents retain the shared catalog flow.
    label_scopes: dict[str, set[tuple[str, ...]]] = {}
    evidence: dict[tuple[tuple[str, ...], str], list[int]] = {}
    for table, scope, labels in table_axes:
        for label in labels:
            label_scopes.setdefault(label, set()).add(scope)
            evidence.setdefault((scope, label), []).append(table.table_id)
    if not any(len(groups) > 1 for groups in label_scopes.values()):
        return None

    from extract.package_catalog_resolver import (
        PackageCatalogEntry,
        PackageCatalogResolution,
        assignment_from_entry,
        clean_public_package_name,
        freeze_package_slots,
    )

    label_order = list(label_scopes)
    keys = sorted(evidence, key=lambda key: (label_order.index(key[1]), ordered_scopes.index(key[0])))
    entries = [
        PackageCatalogEntry(
            package_key="",
            identity_name="/".join(scope),
            identity_aliases=list(scope),
            package_type=clean_public_package_name(label),
            evidence_table_ids=list(dict.fromkeys(evidence[(scope, label)])),
        )
        for scope, label in keys
    ]
    freeze_package_slots(entries)
    by_axis = dict(zip(keys, entries))
    assignments = {}
    diagnostics = []
    for table, scope, labels in table_axes:
        for local_slot, label in enumerate(labels):
            entry = by_axis[(scope, label)]
            assignment = assignment_from_entry(entry, entries, reason="st_explicit_model_package_scope")
            assignments[(table.table_id, local_slot)] = assignment
            diagnostics.append({
                "stage": "package_binding",
                "status": "resolved",
                "table_id": table.table_id,
                "local_slot": local_slot,
                "local_label": label,
                "model_scope": list(scope),
                "package_key": entry.package_key,
                "pkg": assignment.pkg,
                "reason": assignment.reason,
            })
    return PackageCatalogResolution(entries, assignments, diagnostics)

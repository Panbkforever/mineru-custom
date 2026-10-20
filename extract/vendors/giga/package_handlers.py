"""以明确的封装表题或列头冻结槽位，不借用弱订购目录覆盖物理映射。"""

from __future__ import annotations

import re

from extract.vendors.giga.table_handlers import _NUMBER_HEADER, field_indexes, normalized


_PACKAGE_START = re.compile(
    r"^(?:WLCSP|TFBGA|FBGA|CSP|XDFN|DFN|SOT|SOP|VSOP|USON|WSON|SOIC|DIP|QFN|BGA)(?=\d|\s|\(|$)", re.I,
)


def package_label(value: str) -> str:
    label = re.sub(r"\\([*()])", r"\1", str(value)).strip()
    label = label.translate(str.maketrans({"\u2010": "-", "\u2011": "-", "\u2013": "-", "\u2212": "-"}))
    label = re.sub(r"\s+", " ", label)
    return label if _PACKAGE_START.match(label) else ""


def title_package(title: str) -> str:
    # 兼容 Ball in Description 等排版差异，但必须同时存在物理字段表题和封装名。
    if not re.search(r"\b(?:pin|ball|pad)\b", title, re.I):
        return ""
    match = re.search(r"\bdescription\s+for\s+(.+?)\s*package\b", title, re.I)
    return package_label(match[1]) if match else ""


def header_package(header: str) -> str:
    header = re.sub(r"\s+", " ", header).strip()
    match = _NUMBER_HEADER.match(header)
    return package_label(header[match.end():]) if match else ""


def resolve_giga_scopes(target_tables, multi_package_plans):
    axes = []
    for table in target_tables:
        fields = field_indexes(list(table.headers))
        if not fields["pin_no"] or len(fields["pin_name"]) != 1:
            return None
        plan = multi_package_plans.get(table.table_id)
        if plan is None:
            return None
        if plan.is_multi_package and plan.mode == "package_columns":
            labels = [header_package(table.headers[i]) for i in fields["pin_no"]]
            if len(labels) != len(plan.bindings) or not all(labels):
                return None
            if len(set(normalized(label) for label in labels)) != len(labels):
                return None
        elif not plan.is_multi_package and len(fields["pin_no"]) == 1:
            label = header_package(table.headers[fields["pin_no"][0]]) or title_package(table.title)
            if not label and len(target_tables) != 1:
                # 多张无明确范围的表不能凭表号、顺序或引脚内容强行拆封装。
                return None
            labels = [label]
        else:
            return None
        axes.append((table, labels))
    if not axes:
        return None

    from extract.package_catalog_resolver import (
        PackageCatalogEntry, PackageCatalogResolution, assignment_from_entry,
        clean_public_package_name, freeze_package_slots,
    )

    evidence = {}
    display = {}
    for table, labels in axes:
        for label in labels:
            key = normalized(label) if label else "anonymous:single_physical_table"
            evidence.setdefault(key, []).append(table.table_id)
            display.setdefault(key, label)
    entries = [PackageCatalogEntry(
        package_key="", package_type=clean_public_package_name(display[key]),
        evidence_table_ids=list(dict.fromkeys(ids)),
    ) for key, ids in evidence.items()]
    freeze_package_slots(entries)
    by_scope = dict(zip(evidence, entries))
    assignments = {}
    diagnostics = []
    for table, labels in axes:
        for local_slot, label in enumerate(labels):
            key = normalized(label) if label else "anonymous:single_physical_table"
            entry = by_scope[key]
            assignment = assignment_from_entry(entry, entries, reason="giga_explicit_package_scope")
            assignments[(table.table_id, local_slot)] = assignment
            diagnostics.append({"stage": "package_binding", "table_id": table.table_id,
                                "local_slot": local_slot, "status": "resolved", "source_label": label,
                                "package_key": entry.package_key, "reason": assignment.reason})
    return PackageCatalogResolution(entries, assignments, diagnostics)

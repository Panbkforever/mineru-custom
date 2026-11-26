"""TI 表头明确标注 drawing 编号列时，稳定对应跨页分支。"""

from __future__ import annotations

import re


def resolve_ti_package_scopes(target_tables, multi_package_plans):
    """依次尝试明确的 TI 跨页 drawing 列和封装标题证据。"""
    result = resolve_ti_slot_package_scopes(target_tables, multi_package_plans)
    if result is not None:
        return result
    result = resolve_ti_explicit_drawing_scopes(target_tables, multi_package_plans)
    if result is not None:
        return result
    return resolve_ti_title_drawing_scopes(target_tables, multi_package_plans)


def resolve_ti_slot_package_scopes(target_tables, multi_package_plans):
    """明确二维卡槽与 drawing 的列结构，将两卡槽绑定至相同物理封装。"""
    matrix_drawings = None
    for table in target_tables:
        plan = multi_package_plans.get(table.table_id)
        if plan is None or not plan.is_multi_package or plan.mode != 'package_columns':
            continue
        pairs = []
        for binding in plan.bindings:
            if binding.pin_no_column >= len(table.headers):
                continue
            header = table.headers[binding.pin_no_column]
            match = re.search(r'\b(?:slot|socket)\s*([AB])\b[^A-Za-z0-9]+([A-Z]{2,5})\b', header, re.I)
            if match:
                pairs.append((match.group(1).upper(), match.group(2).upper()))
        drawings = {drawing for _, drawing in pairs}
        if len(drawings) == 2 and set(pairs) == {(slot, drawing) for slot in ('A', 'B') for drawing in drawings}:
            matrix_drawings = drawings
            break
    if matrix_drawings is None:
        return None

    # 其他多列目标表必须仅包含已证明的封装标签，避免误接管其他结构。
    for table in target_tables:
        plan = multi_package_plans.get(table.table_id)
        if plan is None:
            return None
        if not plan.is_multi_package:
            continue
        if plan.mode != 'package_columns':
            return None
        for binding in plan.bindings:
            parts = re.findall(r'[A-Za-z0-9]+', binding.package.upper())
            if not parts or parts[-1] not in matrix_drawings:
                return None

    from extract.package_catalog_resolver import (
        PackageCatalogEntry, PackageCatalogResolution, assignment_from_entry,
        freeze_package_slots,
    )
    labels = sorted(matrix_drawings)
    entries = [PackageCatalogEntry(package_key='', public_label=label,
                                   package_drawing=label) for label in labels]
    freeze_package_slots(entries)
    by_label = dict(zip(labels, entries))
    assignments = {}
    diagnostics = []
    for table in target_tables:
        plan = multi_package_plans[table.table_id]
        if not plan.is_multi_package:
            # 水平重复字段块存在多个封装，保留未解析状态，不猜单一归属。
            diagnostics.append({'stage': 'package_binding', 'status': 'unresolved',
                                'table_id': table.table_id, 'local_slot': 0,
                                'reason': 'ti_slot_single_table_unresolved'})
            continue
        for slot, binding in enumerate(plan.bindings):
            label = re.findall(r'[A-Za-z0-9]+', binding.package.upper())[-1]
            assignment = assignment_from_entry(by_label[label], entries, reason='ti_slot_physical_drawing')
            assignments[(table.table_id, slot)] = assignment
            diagnostics.append({'stage': 'package_binding', 'status': 'resolved',
                                'table_id': table.table_id, 'local_slot': slot,
                                'local_label': binding.package, 'package_key': assignment.package_key,
                                'pkg': assignment.pkg, 'reason': assignment.reason})
    return PackageCatalogResolution(entries, assignments, diagnostics)


def resolve_ti_title_drawing_scopes(target_tables, multi_package_plans):
    """表题 drawing 与编号表头独立吻合时，按 drawing 绑定多张表。"""
    if len(target_tables) < 2:
        return None
    axes = []
    for table in target_tables:
        plan = multi_package_plans.get(table.table_id)
        if plan is None or plan.is_multi_package:
            return None
        match = re.search(r"(?:表|table)\s*[\d\-–−]+\.\s*([A-Z0-9]{2,5})\b", table.title, re.I)
        if not match:
            return None
        label = match.group(1).upper()
        matches = [re.search(r"\b(?:pin|ball|terminal)\s+(\d{2,4})\s+([A-Z0-9]{2,5})\b", h, re.I)
                   for h in table.headers]
        found = [(m.group(1), m.group(2).upper()) for m in matches if m]
        if len(found) != 1 or found[0][1] != label:
            return None
        axes.append((table, label, found[0][0]))
    labels = list(dict.fromkeys(label for _, label, _ in axes))
    if len(labels) < 2 or any(len({count for _, key, count in axes if key == label}) != 1 for label in labels):
        return None

    from extract.package_catalog_resolver import (
        PackageCatalogEntry, PackageCatalogResolution, assignment_from_entry,
        freeze_package_slots,
    )
    entries = [PackageCatalogEntry(package_key="", public_label=label, package_drawing=label,
                pin_count=next(count for _, key, count in axes if key == label),
                evidence_table_ids=[table.table_id for table, key, _ in axes if key == label])
               for label in labels]
    freeze_package_slots(entries)
    by_label = dict(zip(labels, entries))
    assignments = {}
    diagnostics = []
    for table, label, _ in axes:
        assignment = assignment_from_entry(by_label[label], entries, reason="ti_title_and_header_drawing")
        assignments[(table.table_id, 0)] = assignment
        diagnostics.append({"stage": "package_binding", "status": "resolved",
                            "table_id": table.table_id, "local_slot": 0,
                            "local_label": label, "package_key": assignment.package_key,
                            "pkg": assignment.pkg, "reason": assignment.reason})
    return PackageCatalogResolution(entries, assignments, diagnostics)


def _drawing_axis(value: str) -> str:
    """仅接受带 PIN/BALL 后缀的明确短标签，不从数据行推测。"""
    compact = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    match = re.fullmatch(r"([A-Z]{3,12})(?:PIN|BALL)", compact)
    if not match:
        return ""
    label = match.group(1)
    if label.startswith(("IOSET", "INPUT", "OUTPUT")):
        return ""
    return label


def resolve_ti_explicit_drawing_scopes(target_tables, multi_package_plans):
    """仅在所有目标页均具有相同 drawing 列时接管目录与绑定。"""
    if len(target_tables) < 2:
        return None
    axes = []
    for table in target_tables:
        plan = multi_package_plans.get(table.table_id)
        if plan is None or not plan.is_multi_package or plan.mode != "package_columns":
            return None
        labels = []
        for binding in plan.bindings:
            label = _drawing_axis(binding.package)
            if not label and binding.pin_no_column < len(table.headers):
                # TERMINAL NO. RGC 等表头必须与分支标签完全一致。
                header = table.headers[binding.pin_no_column]
                match = re.fullmatch(r'\s*terminal\s+(?:no\.?|number)\s+([a-z0-9]{2,6})\s*', header, re.I)
                if match and match.group(1).upper() == binding.package.strip().upper():
                    label = match.group(1).upper()
            labels.append(label)
        if len(labels) < 2 or any(not label for label in labels) or len(set(labels)) != len(labels):
            return None
        axes.append((table, labels))
    reference = axes[0][1]
    if len({re.sub(r"\\s+", " ", table.title.strip().lower()) for table, _ in axes}) != 1:
        return None
    if any(set(labels) != set(reference) for _, labels in axes[1:]):
        return None

    from extract.package_catalog_resolver import (
        PackageCatalogEntry, PackageCatalogResolution, assignment_from_entry,
        freeze_package_slots,
    )

    # 表头已证明每个编号列的 drawing；标签保持与所有续页一致。
    entries = [
        PackageCatalogEntry(package_key="", public_label=label,
                            identity_aliases=[label],
                            evidence_table_ids=[table.table_id for table, _ in axes])
        for label in reference
    ]
    freeze_package_slots(entries)
    by_label = dict(zip(reference, entries))
    assignments = {}
    diagnostics = []
    for table, labels in axes:
        for slot, label in enumerate(labels):
            entry = by_label[label]
            assignment = assignment_from_entry(entry, entries, reason="ti_explicit_drawing_header")
            assignments[(table.table_id, slot)] = assignment
            diagnostics.append({
                "stage": "package_binding", "status": "resolved",
                "table_id": table.table_id, "local_slot": slot,
                "local_label": label, "package_key": entry.package_key,
                "pkg": assignment.pkg, "reason": assignment.reason,
            })
    return PackageCatalogResolution(entries, assignments, diagnostics)

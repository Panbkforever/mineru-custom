"""目标表 local slots 调试信息回归测试。"""

from __future__ import annotations

from extract.multi_package_extractor import MultiPackagePlan, PackageBinding
from extract.pin_package_extractor import (
    TableCandidate,
    build_target_local_slots_debug,
)


def table_candidate(
    *,
    title: str,
    group_context: str = "",
    figure_context_title: str = "",
) -> TableCandidate:
    return TableCandidate(
        html="<table></table>",
        page_idx=0,
        title=title,
        group_context=group_context or title,
        current_chapter_titles=("6 Pin Configuration and Functions",),
        figure_context_title=figure_context_title,
    )


def test_local_slots_debug_reports_hs_variants_from_package_columns():
    plan = MultiPackagePlan(
        is_multi_package=True,
        mode="package_columns",
        bindings=(
            PackageBinding("DRV8320H", 0, 2, 3),
            PackageBinding("DRV8320S", 1, 2, 3),
        ),
        evidence=("package-specific pin columns",),
    )
    table = table_candidate(
        title="Pin Functions—32-Pin DRV8320 Devices",
        group_context=(
            "DRV8320H RTV Package 32-Pin WQFN With Exposed Thermal Pad\n"
            "DRV8320S RTV Package 32-Pin WQFN With Exposed Thermal Pad\n"
            "Pin Functions—32-Pin DRV8320 Devices"
        ),
    )

    slots = build_target_local_slots_debug(
        table=table,
        headers=["NO. DRV8320H", "NO. DRV8320S", "NAME", "TYPE"],
        plan=plan,
    )

    assert slots == [
        {
            "local_slot": 0,
            "local_label": "DRV8320H",
            "mode": "package_columns",
            "identity_name": "DRV8320H",
            "package_type": "WQFN",
            "package_drawing": "RTV",
            "pin_count": "32",
            "sources": ["identity_text", "multi_package_plan", "table_context"],
        },
        {
            "local_slot": 1,
            "local_label": "DRV8320S",
            "mode": "package_columns",
            "identity_name": "DRV8320S",
            "package_type": "WQFN",
            "package_drawing": "RTV",
            "pin_count": "32",
            "sources": ["identity_text", "multi_package_plan", "table_context"],
        },
    ]


def test_local_slots_debug_reports_cbp_cbc_cus_header_slots():
    plan = MultiPackagePlan(
        is_multi_package=True,
        mode="package_columns",
        bindings=(
            PackageBinding("CBP", 0, 5, None),
            PackageBinding("CBC", 2, 5, None),
            PackageBinding("CUS", 4, 5, None),
        ),
    )
    table = table_candidate(
        title="Table 2-4. Multiplexing Characteristics",
        group_context=(
            "Table 2-4 provides a description of the multiplexing on the "
            "CBP, CBC, and CUS packages respectively.\n"
            "Table 2-4. Multiplexing Characteristics"
        ),
    )

    slots = build_target_local_slots_debug(
        table=table,
        headers=["CBP Bottom", "CBP Top", "CBC Bottom", "CBC Top", "CUS", "MODE 0"],
        plan=plan,
    )

    assert [slot["local_label"] for slot in slots] == ["CBP", "CBC", "CUS"]
    assert [slot["mode"] for slot in slots] == [
        "package_columns",
        "package_columns",
        "package_columns",
    ]
    assert all("multi_package_plan" in slot["sources"] for slot in slots)

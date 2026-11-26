"""Texas Instruments extraction profile.

TI 厂家特定的表格识别、数据修复与封装绑定接入点。
"""

from __future__ import annotations

from extract.vendors.base import VendorProfile
from extract.vendors.ti.package_handlers import resolve_ti_package_scopes
from extract.vendors.ti.table_handlers import match_ti_table, repair_ti_rows, ti_binding_group
from extract.vendors.ti.source_tables import prepare_ti_tables


PROFILE = VendorProfile(
    name="TI",
    display_name="Texas Instruments",
    description="Default profile for the existing TI datasheet extraction flow.",
    package_scope_resolver=resolve_ti_package_scopes,
    table_matcher=match_ti_table,
    source_table_repairer=prepare_ti_tables,
    row_repairer=repair_ti_rows,
    binding_group_resolver=ti_binding_group,
)

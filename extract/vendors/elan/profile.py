"""Elan native source evidence, physical pin fields and mapping scopes."""

from extract.vendors.base import VendorProfile
from extract.vendors.elan.native_tables import recover_elan_tables
from extract.vendors.elan.package_handlers import resolve_elan_scopes
from extract.vendors.elan.table_handlers import classify_elan_header, match_elan_table, repair_elan_rows, repair_elan_table


PROFILE = VendorProfile(
    name="ELAN",
    display_name="Elan Microelectronics",
    header_classifier=classify_elan_header,
    table_matcher=match_elan_table,
    table_repairer=repair_elan_table,
    row_repairer=repair_elan_rows,
    source_table_repairer=recover_elan_tables,
    package_scope_resolver=resolve_elan_scopes,
)

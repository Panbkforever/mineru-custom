"""ABOV physical pin tables, package variants and display-driver ranges."""

from extract.vendors.base import VendorProfile
from extract.vendors.abov.table_handlers import (
    classify_abov_header,
    match_abov_table,
    repair_abov_table,
    repair_abov_rows,
    should_keep_abov_record,
)


PROFILE = VendorProfile(
    name="ABOV",
    display_name="ABOV Semiconductor",
    description="Physical package columns and explicit display-driver pin ranges.",
    header_classifier=classify_abov_header,
    table_matcher=match_abov_table,
    table_repairer=repair_abov_table,
    row_repairer=repair_abov_rows,
    record_filterer=should_keep_abov_record,
)

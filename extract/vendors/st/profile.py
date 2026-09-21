"""STMicroelectronics extraction profile.

This profile initially reuses the shared extraction flow. ST-specific hooks
should be added here after we identify common ST document patterns.
"""

from __future__ import annotations

from extract.vendors.base import VendorProfile
from extract.vendors.st.table_handlers import (
    classify_st_header,
    is_st_physical_table,
    match_st_table,
)


PROFILE = VendorProfile(
    name="ST",
    display_name="STMicroelectronics",
    description="Profile placeholder for ST datasheet-specific extraction rules.",
    header_classifier=classify_st_header,
    table_matcher=match_st_table,
    physical_table_guard=is_st_physical_table,
)

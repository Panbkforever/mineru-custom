"""STMicroelectronics extraction profile.

This profile initially reuses the shared extraction flow. ST-specific hooks
should be added here after we identify common ST document patterns.
"""

from __future__ import annotations

from extract.vendors.base import VendorProfile


PROFILE = VendorProfile(
    name="ST",
    display_name="STMicroelectronics",
    description="Profile placeholder for ST datasheet-specific extraction rules.",
)

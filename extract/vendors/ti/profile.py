"""Texas Instruments extraction profile.

TI remains the default behavior and currently uses the existing shared
pipeline without overriding any processing stage.
"""

from __future__ import annotations

from extract.vendors.base import VendorProfile


PROFILE = VendorProfile(
    name="TI",
    display_name="Texas Instruments",
    description="Default profile for the existing TI datasheet extraction flow.",
)

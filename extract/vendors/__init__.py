"""Vendor-specific extraction profiles.

The public extraction pipeline stays shared. Vendor profiles provide a
controlled place for manufacturer-specific rules as they are discovered.
"""

from extract.vendors.base import VendorProfile
from extract.vendors.registry import get_vendor_profile, supported_vendors

__all__ = ["VendorProfile", "get_vendor_profile", "supported_vendors"]

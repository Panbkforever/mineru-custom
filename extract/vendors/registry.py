"""Resolve user-facing vendor names to extraction profiles."""

from __future__ import annotations

import os

from extract.vendors.base import VendorProfile
from extract.vendors.st.profile import PROFILE as ST_PROFILE
from extract.vendors.ti.profile import PROFILE as TI_PROFILE


_PROFILES: dict[str, VendorProfile] = {
    TI_PROFILE.canonical_name: TI_PROFILE,
    ST_PROFILE.canonical_name: ST_PROFILE,
}


def normalize_vendor_name(value: str | None) -> str:
    """Normalize CLI/API vendor values to the registry key format."""

    normalized = (value or "TI").strip().upper()
    if not normalized:
        return "TI"
    aliases = {
        "TEXAS_INSTRUMENTS": "TI",
        "TEXAS-INSTRUMENTS": "TI",
        "TEXAS INSTRUMENTS": "TI",
        "STMICRO": "ST",
        "STMICROELECTRONICS": "ST",
        "ST MICRO": "ST",
        "ST MICROELECTRONICS": "ST",
    }
    return aliases.get(normalized, normalized)


def get_vendor_profile(value: str | None) -> VendorProfile:
    """Return a supported vendor profile or raise a clear ValueError."""

    key = normalize_vendor_name(value)
    try:
        return _PROFILES[key]
    except KeyError as exc:
        supported = ", ".join(supported_vendors())
        raise ValueError(
            f"Unsupported vendor {value!r}. Supported vendors: {supported}"
        ) from exc


def get_current_vendor_profile() -> VendorProfile:
    return get_vendor_profile(os.environ.get("EXTRACT_VENDOR", "TI"))


def supported_vendors() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))

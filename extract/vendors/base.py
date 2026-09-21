"""Common vendor profile contract.

Keep this object intentionally small for now. The TI pipeline is already
stable, so vendor-specific hooks should be added only when a real shared
difference is identified across a manufacturer's PDFs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VendorProfile:
    """Configuration for one manufacturer's extraction behavior."""

    name: str
    display_name: str
    description: str = ""

    @property
    def canonical_name(self) -> str:
        return self.name.upper()

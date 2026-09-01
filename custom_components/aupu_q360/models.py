"""Immutable data models for the AUPU Q360 integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """Validated identifiers needed to address one AUPU device."""

    did: str
    tag: str

    def __post_init__(self) -> None:
        """Reject values that cannot be serialized into the confirmed payload."""
        if not isinstance(self.did, str) or not self.did or not all(
            "0" <= character <= "9" for character in self.did
        ):
            raise ValueError("Device identifier must be decimal digits")
        if not isinstance(self.tag, str) or not self.tag.strip():
            raise ValueError("Device tag must be a non-empty string")

    @property
    def topic_name(self) -> str:
        """Return the AWS IoT shadow update topic for this device."""
        return f"$aws/things/{self.did}/shadow/update"


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """Validated fields from a successful AUPU business response."""

    status: int
    result: Any
    timestamp: int

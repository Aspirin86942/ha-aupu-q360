"""Offline implementation of the dynamic App-Authorization header signer."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REQUIRED_FIELDS = (
    "app_key",
    "key_prefix",
    "package_name",
    "key_suffix",
    "sdk_version",
    "message_prefix",
    "sdk_label",
    "type_timestamp_label",
    "header_prefix",
    "header_sep_1",
    "header_sep_2",
    "signature_label",
)


@dataclass(frozen=True, repr=False)
class SignerSecrets:
    """Private constants required to build the authorization header."""

    app_key: str
    key_prefix: str
    package_name: str
    key_suffix: str
    sdk_version: str
    message_prefix: str
    sdk_label: str
    type_timestamp_label: str
    header_prefix: str
    header_sep_1: str
    header_sep_2: str
    signature_label: str

    def __post_init__(self) -> None:
        """Reject unusable direct construction without echoing field values."""
        invalid = [
            field
            for field in _REQUIRED_FIELDS
            if not isinstance(getattr(self, field), str) or not getattr(self, field)
        ]
        if invalid:
            raise ValueError(
                f"Signer secret fields must be non-empty strings: {', '.join(invalid)}"
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SignerSecrets:
        """Validate a complete, exact secret mapping before using it."""
        missing = [field for field in _REQUIRED_FIELDS if field not in value]
        if missing:
            raise ValueError(f"Signer secrets missing fields: {', '.join(missing)}")
        non_strings = [field for field in _REQUIRED_FIELDS if not isinstance(value[field], str)]
        if non_strings:
            raise TypeError(f"Signer secret fields must be strings: {', '.join(non_strings)}")
        unexpected = sorted(set(value) - set(_REQUIRED_FIELDS))
        if unexpected:
            raise ValueError(f"Signer secrets contain unexpected fields: {', '.join(unexpected)}")
        return cls(**{field: value[field] for field in _REQUIRED_FIELDS})

    @classmethod
    def load(cls, path: str | Path) -> SignerSecrets:
        """Load a JSON object without echoing its contents on parsing failures."""
        try:
            parsed = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Signer secrets file is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise TypeError("Signer secrets file must contain one JSON object")
        return cls.from_mapping(parsed)

    def __repr__(self) -> str:
        """Expose field names and lengths only, never private values."""
        field_lengths = ", ".join(
            f"{field}=<len={len(getattr(self, field))}>" for field in _REQUIRED_FIELDS
        )
        return f"{type(self).__name__}({field_lengths})"


class AppAuthorizationSigner:
    """Generate and inspect an App-Authorization header without network I/O."""

    def __init__(self, secrets: SignerSecrets) -> None:
        self._secrets = secrets

    @classmethod
    def from_file(cls, path: str | Path) -> AppAuthorizationSigner:
        """Create a signer from validated local secret material."""
        return cls(SignerSecrets.load(path))

    def sign(self, timestamp: int | None = None) -> str:
        """Return a header for a non-negative Unix timestamp."""
        unix_seconds = int(time.time()) if timestamp is None else int(timestamp)
        if unix_seconds < 0:
            raise ValueError("timestamp must be a non-negative Unix timestamp")

        value = self._secrets
        key_material = value.key_prefix + value.package_name + value.key_suffix
        message = (
            value.message_prefix
            + value.app_key
            + value.sdk_label
            + value.sdk_version
            + value.type_timestamp_label
            + str(unix_seconds)
        )
        digest_hex = hmac.new(
            key_material.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        signature = base64.b64encode(digest_hex.encode("utf-8")).decode("ascii")
        return (
            value.header_prefix
            + value.app_key
            + value.header_sep_1
            + value.sdk_version
            + value.header_sep_2
            + str(unix_seconds)
            + value.signature_label
            + signature
        )

    def timestamp_from_header(self, header: str) -> int:
        """Extract the timestamp only from a header produced for these secrets."""
        value = self._secrets
        fixed_prefix = (
            value.header_prefix
            + value.app_key
            + value.header_sep_1
            + value.sdk_version
            + value.header_sep_2
        )
        if not header.startswith(fixed_prefix):
            raise ValueError("App-Authorization has an unexpected prefix")
        timestamp_text, separator, signature = header[len(fixed_prefix) :].partition(
            value.signature_label
        )
        if not separator or not timestamp_text.isdigit() or not signature:
            raise ValueError("App-Authorization has an unexpected structure")
        return int(timestamp_text)

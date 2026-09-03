"""Immutable data models for the AUPU Q360 integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from .auth import AuthState, BearerCredential
from .signer import AppAuthorizationSigner, SignerSecrets

if TYPE_CHECKING:
    from .api import AupuApiClient
    from .coordinator import AupuCoordinator
    from .probe import PanelStateProbe


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """Validated identifiers needed to address one AUPU device."""

    did: str
    tag: str

    def __post_init__(self) -> None:
        """Reject values that cannot be serialized into the confirmed payload."""
        if (
            not isinstance(self.did, str)
            or not self.did
            or not all("0" <= character <= "9" for character in self.did)
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


class AsyncStopper(Protocol):
    """A runtime-owned object that can stop its asynchronous work."""

    async def async_stop(self) -> None:
        """Stop background work without retaining integration secrets."""


@dataclass(frozen=True, slots=True, repr=False)
class AupuConfigEntryData:
    """Validated, JSON-serializable data persisted by one config entry."""

    signer: dict[str, str]
    token: str
    did: str
    tag: str
    use_wss: bool
    user_uuid: str | None = None
    phone: str | None = None

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        require_user_uuid: bool = True,
        require_unexpired_token: bool = True,
    ) -> AupuConfigEntryData:
        """Validate and normalize the complete persisted-data candidate."""
        signer_value = value.get("signer")
        if not isinstance(signer_value, Mapping) or not all(
            isinstance(key, str) for key in signer_value
        ):
            raise TypeError("Signer data must be an object")
        signer_mapping = cast(Mapping[str, Any], signer_value)
        SignerSecrets.from_mapping(signer_mapping)

        token_value = value.get("token")
        if not isinstance(token_value, str):
            raise TypeError("Token must be a string")
        credential = BearerCredential.parse(token_value)
        if require_unexpired_token and credential.state() is AuthState.EXPIRED:
            raise ValueError("Credential is expired")

        did_value = value.get("did")
        tag_value = value.get("tag")
        if not isinstance(did_value, str) or not isinstance(tag_value, str):
            raise TypeError("Device data must contain strings")
        device = DeviceConfig(did=did_value, tag=tag_value)
        use_wss = value.get("use_wss", False)
        if not isinstance(use_wss, bool):
            raise TypeError("WSS choice must be boolean")

        user_uuid = _optional_non_empty_string(value.get("user_uuid"), "User UUID")
        if use_wss and require_user_uuid and user_uuid is None:
            raise ValueError("User UUID is required when WSS is enabled")

        phone = _optional_non_empty_string(value.get("phone"), "Phone")
        return cls(
            signer={key: cast(str, signer_mapping[key]) for key in signer_mapping},
            token=credential.authorization_header.removeprefix("Bearer "),
            did=device.did,
            tag=device.tag,
            use_wss=use_wss,
            user_uuid=user_uuid,
            phone=phone,
        )

    @property
    def secrets(self) -> SignerSecrets:
        """Reconstruct validated signer material for the runtime only."""
        return SignerSecrets.from_mapping(self.signer)

    @property
    def credential(self) -> BearerCredential:
        """Reconstruct the locally validated bearer credential."""
        return BearerCredential.parse(self.token)

    @property
    def device(self) -> DeviceConfig:
        """Reconstruct validated device addressing data."""
        return DeviceConfig(did=self.did, tag=self.tag)

    def as_mapping(self) -> dict[str, Any]:
        """Return only JSON-compatible values allowed in Config Entry data."""
        result: dict[str, Any] = {
            "signer": dict(self.signer),
            "token": self.token,
            "did": self.did,
            "tag": self.tag,
            "use_wss": self.use_wss,
        }
        if self.user_uuid is not None:
            result["user_uuid"] = self.user_uuid
        if self.phone is not None:
            result["phone"] = self.phone
        return result


@dataclass(slots=True)
class AupuRuntimeData:
    """Non-serializable objects owned by one loaded config entry."""

    signer: AppAuthorizationSigner
    credential: BearerCredential
    device: DeviceConfig
    api: AupuApiClient
    use_wss: bool = False
    user_uuid: str | None = field(default=None, repr=False)
    coordinator: AupuCoordinator = field(init=False)
    probe: PanelStateProbe = field(init=False, repr=False)
    stoppers: list[AsyncStopper] = field(default_factory=list)


def _optional_non_empty_string(value: object, field_name: str) -> str | None:
    """Normalize an absent/blank optional string without exposing its value."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    return normalized or None

"""Tests for the offline App-Authorization signer."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.aupu_q360.signer import AppAuthorizationSigner, SignerSecrets


@pytest.fixture
def synthetic_secrets() -> SignerSecrets:
    """Provide fixture-only values that cannot authenticate against any service."""
    fixture_path = Path(__file__).parent / "fixtures" / "synthetic_signer.json"
    return SignerSecrets.from_mapping(json.loads(fixture_path.read_text(encoding="utf-8")))


def test_signer_is_deterministic_for_fixed_timestamp(synthetic_secrets: SignerSecrets) -> None:
    """The same Unix second must produce the same complete authorization header."""
    signer = AppAuthorizationSigner(synthetic_secrets)

    first = signer.sign(1_700_000_000)
    second = signer.sign(1_700_000_000)

    assert first == second
    assert len(first) > 100


def test_signer_uses_the_expected_fixed_time_header(synthetic_secrets: SignerSecrets) -> None:
    """A changed signing algorithm or header order must change this independent vector."""
    signer = AppAuthorizationSigner(synthetic_secrets)

    assert signer.sign(1_700_000_000) == (
        "Synthetic: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,Sdk=1.2.3,Timestamp=1700000000,Signature="
        "NTEwOWI4YmI5MGYyNzFiYjJkNjE0YTZlNzcxYTYwMjE2NTljNzA3Mjc0NGIxMjdjNjAyMGZjMDVhZTRkZjUzZA=="
    )


def test_signer_changes_signature_for_an_adjacent_second(synthetic_secrets: SignerSecrets) -> None:
    """Timestamp must be part of both the header and the signed message."""
    signer = AppAuthorizationSigner(synthetic_secrets)

    assert signer.sign(1_700_000_000) != signer.sign(1_700_000_001)


def test_signer_rejects_negative_timestamps(synthetic_secrets: SignerSecrets) -> None:
    """Negative Unix seconds are outside the authorization protocol."""
    with pytest.raises(ValueError, match="non-negative"):
        AppAuthorizationSigner(synthetic_secrets).sign(-1)


def test_secrets_reject_missing_required_field() -> None:
    """Incomplete secret material must fail closed before signing."""
    with pytest.raises(ValueError, match="app_key"):
        SignerSecrets.from_mapping({})


def test_secrets_reject_unknown_field(synthetic_secrets: SignerSecrets) -> None:
    """Unexpected JSON keys must not silently alter signer input."""
    values = dict(synthetic_secrets.__dict__)
    values["unexpected"] = "value"

    with pytest.raises(ValueError, match="unexpected"):
        SignerSecrets.from_mapping(values)


def test_secret_values_are_not_shown_by_repr(synthetic_secrets: SignerSecrets) -> None:
    """Debug output must never expose a private field value."""
    rendered = repr(synthetic_secrets)

    assert "app_key" in rendered
    assert str(len(synthetic_secrets.app_key)) in rendered
    for secret in synthetic_secrets.__dict__.values():
        assert secret not in rendered


def test_private_verification_skips_when_local_material_is_absent() -> None:
    """A fresh checkout must not fail or reveal data when private files are unavailable."""
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "verify_private_signer.py"

    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        check=False,
        cwd=script_path.parent.parent,
        text=True,
    )

    assert result.returncode == 0
    assert "SKIP" in result.stdout
    assert "AAAAAAAA" not in result.stdout

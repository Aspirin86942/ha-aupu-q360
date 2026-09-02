"""Permission, durability, and failure tests for the private raw discovery archive."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.aupu_q360.discovery_models import (
    DiscoveryExperiment,
    DiscoveryPhase,
)
from custom_components.aupu_q360.errors import (
    DiscoveryRawArchiveFailedError,
    DiscoveryRawArchiveLimitError,
    DiscoveryRawArchiveUnavailableError,
)
from custom_components.aupu_q360.raw_discovery_archive import (
    RAW_ARCHIVE_ROOT,
    ArchiveContext,
    RawDiscoveryArchive,
)
from custom_components.aupu_q360.shadow import RawShadowEvent

_SESSION_ID = "rd-" + "a" * 32
_TOPIC = "$aws/things/123456789/shadow/update/accepted"
_CONTEXT = ArchiveContext(
    experiment=DiscoveryExperiment.NIGHT_LIGHT,
    round=1,
    phase=DiscoveryPhase.MODE_ON,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "private-root"
    root.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    return root


def _event(payload: bytes = b"synthetic-shadow") -> RawShadowEvent:
    return RawShadowEvent(direction="incoming", topic=_TOPIC, payload=payload)


def _times(*values: datetime) -> Callable[[], datetime]:
    iterator: Iterator[datetime] = iter(values)
    return lambda: next(iterator)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("missing", "symlink", "file", "mode"))
async def test_open_rejects_an_unavailable_or_permission_unsafe_root(
    tmp_path: Path,
    kind: str,
) -> None:
    """Catch archive creation escaping into a missing, linked, non-directory, or broad root."""
    root = tmp_path / "unsafe-root"
    if kind == "symlink":
        target = _root(tmp_path)
        root.symlink_to(target, target_is_directory=True)
    elif kind == "file":
        root.write_text("not a directory", encoding="utf-8")
    elif kind == "mode":
        root.mkdir(mode=0o755)
        root.chmod(0o755)

    failures: list[str] = []
    with pytest.raises(DiscoveryRawArchiveUnavailableError) as raised:
        await RawDiscoveryArchive.async_open(failures.append, root=root)

    assert str(raised.value) == "discovery_raw_archive_unavailable"
    assert failures == []
    assert not any(path.name.startswith("rd-") for path in tmp_path.rglob("*"))


@pytest.mark.asyncio
async def test_open_uses_fixed_production_root_and_unpredictable_private_names(
    tmp_path: Path,
) -> None:
    """Catch device IDs, timestamps, or permissive modes entering archive paths."""
    assert RAW_ARCHIVE_ROOT == Path("/var/lib/aupu-q360-private-discovery")
    root = _root(tmp_path)

    archive = await RawDiscoveryArchive.async_open(lambda _: None, root=root)
    session_id = archive.metadata.session_id

    assert session_id is not None
    assert re.fullmatch(r"rd-[0-9a-f]{32}", session_id)
    assert "123456789" not in session_id
    session = root / session_id
    partial = session / "events.jsonl.partial"
    assert session.stat().st_mode & 0o777 == 0o700
    assert partial.stat().st_mode & 0o777 == 0o600

    metadata = await archive.async_abort()
    assert metadata.status == "incomplete"
    assert (session / "manifest.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_open_rejects_existing_session_or_partial_file_without_following_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch exclusive session or file creation being weakened into overwrite semantics."""
    module = __import__(
        "custom_components.aupu_q360.raw_discovery_archive",
        fromlist=["raw_discovery_archive"],
    )
    root = _root(tmp_path)
    monkeypatch.setattr(module.secrets, "token_hex", lambda _: "a" * 32)
    (root / _SESSION_ID).mkdir(mode=0o700)

    with pytest.raises(DiscoveryRawArchiveUnavailableError):
        await RawDiscoveryArchive.async_open(lambda _: None, root=root)

    (root / _SESSION_ID).rmdir()
    real_mkdir = module.os.mkdir

    def mkdir_with_link(path: str, mode: int, *, dir_fd: int | None = None) -> None:
        real_mkdir(path, mode, dir_fd=dir_fd)
        (root / _SESSION_ID / "events.jsonl.partial").symlink_to(tmp_path / "outside")

    monkeypatch.setattr(module.os, "mkdir", mkdir_with_link)
    with pytest.raises(DiscoveryRawArchiveUnavailableError):
        await RawDiscoveryArchive.async_open(lambda _: None, root=root)

    assert not (tmp_path / "outside").exists()


@pytest.mark.asyncio
async def test_archive_round_trips_exact_bytes_in_queue_order_and_completes_atomically(
    tmp_path: Path,
) -> None:
    """Catch raw bytes, event order, timestamps, counts, or completion hash drifting."""
    root = _root(tmp_path)
    archive = await RawDiscoveryArchive.async_open(
        lambda _: None,
        root=root,
        now=_times(
            datetime(2026, 9, 3, 0, 0, 1, tzinfo=UTC),
            datetime(2026, 9, 3, 0, 0, 2, 123456, tzinfo=UTC),
        ),
    )
    payloads = (b"\x00\xffsynthetic", b'{"state":{"reported":{}}}')

    archive.enqueue(_event(payloads[0]), _CONTEXT)
    archive.enqueue(
        RawShadowEvent(
            direction="outgoing",
            topic="$aws/things/123456789/shadow/get",
            payload=payloads[1],
        ),
        ArchiveContext(
            experiment=DiscoveryExperiment.GLOBAL_FAN_LEVEL,
            round=2,
            phase=DiscoveryPhase.PARAMETER_CHANGE,
        ),
    )
    metadata = await archive.async_complete()

    assert metadata.enabled is True
    assert metadata.status == "complete"
    assert metadata.session_id is not None
    session = root / metadata.session_id
    final = session / "events.jsonl"
    assert final.exists()
    assert not (session / "events.jsonl.partial").exists()
    encoded = final.read_bytes()
    lines = encoded.splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert rows[0] == {
        "sequence": 1,
        "recorded_at_utc": "2026-09-03T00:00:01.000000Z",
        "experiment": "night_light",
        "round": 1,
        "phase": "mode_on",
        "direction": "incoming",
        "topic": _TOPIC,
        "payload_base64": base64.b64encode(payloads[0]).decode("ascii"),
    }
    assert rows[1]["sequence"] == 2
    assert rows[1]["recorded_at_utc"] == "2026-09-03T00:00:02.123456Z"
    assert rows[1]["experiment"] == "global_fan_level"
    assert rows[1]["round"] == 2
    assert rows[1]["phase"] == "parameter_change"
    assert rows[1]["direction"] == "outgoing"
    assert base64.b64decode(rows[1]["payload_base64"], validate=True) == payloads[1]
    assert metadata.event_count == 2
    assert metadata.file_bytes == len(encoded)
    assert metadata.sha256 == hashlib.sha256(encoded).hexdigest()
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "status": "complete",
        "session_id": metadata.session_id,
        "event_count": 2,
        "file_bytes": len(encoded),
        "sha256": metadata.sha256,
    }
    assert final.stat().st_mode & 0o777 == 0o600
    assert (session / "manifest.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_queue_and_encoded_byte_limits_fail_closed_once(
    tmp_path: Path,
) -> None:
    """Catch pending events escaping either the bounded queue or encoded archive limit."""
    queue_failures: list[str] = []
    queue_archive = await RawDiscoveryArchive.async_open(
        queue_failures.append,
        root=_root(tmp_path / "queue"),
        queue_limit=1,
    )
    queue_archive.enqueue(_event(b"first"), _CONTEXT)
    with pytest.raises(DiscoveryRawArchiveFailedError):
        queue_archive.enqueue(_event(b"second"), _CONTEXT)
    with pytest.raises(DiscoveryRawArchiveFailedError):
        queue_archive.enqueue(_event(b"third"), _CONTEXT)
    await queue_archive.async_abort()
    assert queue_failures == ["discovery_raw_archive_failed"]

    limit_failures: list[str] = []
    limit_archive = await RawDiscoveryArchive.async_open(
        limit_failures.append,
        root=_root(tmp_path / "limit"),
        max_bytes=1,
    )
    with pytest.raises(DiscoveryRawArchiveLimitError):
        limit_archive.enqueue(_event(b"too-large"), _CONTEXT)
    with pytest.raises(DiscoveryRawArchiveFailedError):
        limit_archive.enqueue(_event(b"again"), _CONTEXT)
    await limit_archive.async_abort()
    assert limit_failures == ["discovery_raw_archive_limit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ("write", "fsync", "hash", "rename"))
async def test_filesystem_completion_failures_preserve_partial_and_fixed_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_point: str,
) -> None:
    """Catch any durability failure being mislabeled complete or leaking raw content."""
    module = __import__(
        "custom_components.aupu_q360.raw_discovery_archive",
        fromlist=["raw_discovery_archive"],
    )
    root = _root(tmp_path)
    failures: list[str] = []
    archive = await RawDiscoveryArchive.async_open(failures.append, root=root)
    marker = b"synthetic-private-raw-marker"
    archive.enqueue(_event(marker), _CONTEXT)

    if failure_point == "write":
        original = module._write_all
        failed = False

        def fail_once(fd: int, data: bytes) -> None:
            nonlocal failed
            if not failed and b"payload_base64" in data:
                failed = True
                raise OSError("private write detail")
            original(fd, data)

        monkeypatch.setattr(module, "_write_all", fail_once)
    elif failure_point == "fsync":
        original = module._fsync
        failed = False

        def fail_once(fd: int) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("private fsync detail")
            original(fd)

        monkeypatch.setattr(module, "_fsync", fail_once)
    elif failure_point == "hash":
        original = module._sha256_file
        failed = False

        def fail_once(directory_fd: int, name: str) -> str:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("private hash detail")
            return original(directory_fd, name)

        monkeypatch.setattr(module, "_sha256_file", fail_once)
    else:
        original = module._rename
        failed = False

        def fail_once(directory_fd: int, source: str, target: str) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("private rename detail")
            original(directory_fd, source, target)

        monkeypatch.setattr(module, "_rename", fail_once)

    with pytest.raises(DiscoveryRawArchiveFailedError):
        await archive.async_complete()

    assert failures == ["discovery_raw_archive_failed"]
    session_id = archive.metadata.session_id
    assert session_id is not None
    session = root / session_id
    assert (session / "events.jsonl.partial").exists()
    assert not (session / "events.jsonl").exists()
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"
    assert "sha256" not in manifest
    assert archive.metadata.sha256 is None
    assert marker.decode() not in caplog.text
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_existing_final_or_manifest_symlink_is_never_overwritten(
    tmp_path: Path,
) -> None:
    """Catch completion following or replacing attacker-controlled session file paths."""
    root = _root(tmp_path)
    archive = await RawDiscoveryArchive.async_open(lambda _: None, root=root)
    session_id = archive.metadata.session_id
    assert session_id is not None
    session = root / session_id
    outside = tmp_path / "outside"
    outside.write_text("sentinel", encoding="utf-8")
    (session / "events.jsonl").symlink_to(outside)
    archive.enqueue(_event(), _CONTEXT)

    with pytest.raises(DiscoveryRawArchiveFailedError):
        await archive.async_complete()

    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert (session / "events.jsonl.partial").exists()
    assert (session / "events.jsonl").is_symlink()

    other_root = _root(tmp_path / "manifest")
    other = await RawDiscoveryArchive.async_open(lambda _: None, root=other_root)
    other_session_id = other.metadata.session_id
    assert other_session_id is not None
    other_session = other_root / other_session_id
    (other_session / "manifest.json").symlink_to(outside)
    await other.async_abort()
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert (other_session / "manifest.json").is_symlink()


@pytest.mark.asyncio
async def test_cancelled_completion_and_stop_leave_incomplete_archives_without_task_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch cancellation or HA stop losing accepted events or claiming a complete hash."""
    module = __import__(
        "custom_components.aupu_q360.raw_discovery_archive",
        fromlist=["raw_discovery_archive"],
    )
    root = _root(tmp_path / "cancel")
    failures: list[str] = []
    archive = await RawDiscoveryArchive.async_open(failures.append, root=root)
    archive.enqueue(_event(b"cancelled-event"), _CONTEXT)
    original = module._write_all
    started = threading.Event()
    release = threading.Event()

    def block_event_write(fd: int, data: bytes) -> None:
        if b"payload_base64" in data:
            started.set()
            release.wait(timeout=5)
        original(fd, data)

    monkeypatch.setattr(module, "_write_all", block_event_write)
    completion = asyncio.create_task(archive.async_complete())
    assert await asyncio.to_thread(started.wait, 1)
    completion.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await completion

    session_id = archive.metadata.session_id
    assert session_id is not None
    session = root / session_id
    assert (session / "events.jsonl.partial").exists()
    assert not (session / "events.jsonl").exists()
    assert archive.metadata.status == "incomplete"
    assert archive.metadata.sha256 is None
    assert failures == ["discovery_raw_archive_failed"]

    stop_root = _root(tmp_path / "stop")
    stop_failures: list[str] = []
    stopped = await RawDiscoveryArchive.async_open(stop_failures.append, root=stop_root)
    stopped.enqueue(_event(b"stopped-event"), _CONTEXT)
    await stopped.async_stop()
    await stopped.async_stop()
    stopped_session_id = stopped.metadata.session_id
    assert stopped_session_id is not None
    stopped_session = stop_root / stopped_session_id
    assert (stopped_session / "events.jsonl.partial").exists()
    assert (
        json.loads((stopped_session / "manifest.json").read_text(encoding="utf-8"))["status"]
        == "incomplete"
    )
    assert stop_failures == ["discovery_raw_archive_failed"]


def test_raw_event_and_metadata_repr_never_expose_content() -> None:
    """Catch raw topic, payload, or hashes entering routine object representations."""
    event = _event(b"synthetic-private-raw-marker")
    assert repr(event) == "RawShadowEvent(<redacted>)"
    assert _TOPIC not in repr(event)
    assert "synthetic-private-raw-marker" not in repr(event)

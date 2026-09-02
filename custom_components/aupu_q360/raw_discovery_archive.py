"""Permission-safe, bounded raw Shadow archive outside Home Assistant storage."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import json
import logging
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .discovery_models import (
    DiscoveryExperiment,
    DiscoveryPhase,
    DiscoveryRound,
    JsonObject,
)
from .errors import (
    DiscoveryRawArchiveFailedError,
    DiscoveryRawArchiveLimitError,
    DiscoveryRawArchiveUnavailableError,
)
from .shadow import RawShadowEvent

RAW_ARCHIVE_ROOT = Path("/var/lib/aupu-q360-private-discovery")
_DEFAULT_QUEUE_LIMIT = 256
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_SENTINEL = object()
_LOGGER = logging.getLogger(__name__)

type ArchiveStatus = Literal["not_requested", "open", "complete", "incomplete"]
type FailureCallback = Callable[[str], None]
type ArchiveQueueItem = bytes | object


def _utc_now() -> datetime:
    """Return the current aware UTC time for one archive event."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ArchiveContext:
    """Controlled experiment labels attached to one raw archive event."""

    experiment: DiscoveryExperiment
    round: DiscoveryRound
    phase: DiscoveryPhase

    def __post_init__(self) -> None:
        """Reject free-form labels before they can enter the private archive."""
        if (
            not isinstance(self.experiment, DiscoveryExperiment)
            or type(self.round) is not int
            or self.round not in (1, 2)
            or not isinstance(self.phase, DiscoveryPhase)
        ):
            raise DiscoveryRawArchiveFailedError


@dataclass(frozen=True, slots=True)
class RawArchiveMetadata:
    """Secret-free archive status suitable for a sanitized report."""

    enabled: bool
    status: ArchiveStatus
    session_id: str | None = None
    event_count: int | None = None
    file_bytes: int | None = None
    sha256: str | None = None

    @classmethod
    def not_requested(cls) -> RawArchiveMetadata:
        """Return the exact disabled metadata variant."""
        return cls(enabled=False, status="not_requested")

    def to_public(self) -> JsonObject:
        """Serialize metadata without any filesystem path or raw event content."""
        if not self.enabled:
            return {"enabled": False, "status": "not_requested"}
        public: JsonObject = {
            "enabled": True,
            "status": self.status,
        }
        if self.session_id is not None:
            public["session_id"] = self.session_id
        if self.event_count is not None:
            public["event_count"] = self.event_count
        if self.file_bytes is not None:
            public["file_bytes"] = self.file_bytes
        if self.sha256 is not None:
            public["sha256"] = self.sha256
        return public


@dataclass(frozen=True, slots=True)
class _OpenedArchive:
    root_fd: int
    session_fd: int
    file_fd: int
    session_id: str


class RawDiscoveryArchive:
    """Own one bounded JSONL writer and its permission-restricted directory handles."""

    def __init__(
        self,
        *,
        opened: _OpenedArchive,
        on_failure: FailureCallback,
        queue_limit: int,
        max_bytes: int,
        now: Callable[[], datetime],
    ) -> None:
        self._root_fd: int | None = opened.root_fd
        self._session_fd: int | None = opened.session_fd
        self._file_fd: int | None = opened.file_fd
        self._session_id = opened.session_id
        self._on_failure = on_failure
        self._queue: asyncio.Queue[ArchiveQueueItem] = asyncio.Queue(maxsize=queue_limit)
        self._max_bytes = max_bytes
        self._now = now
        self._reserved_bytes = 0
        self._reserved_events = 0
        self._durable_bytes = 0
        self._durable_events = 0
        self._failure_code: str | None = None
        self._failure_notified = False
        self._writer_disabled = False
        self._closing = False
        self._sentinel_enqueued = False
        self._status: ArchiveStatus = "open"
        self._sha256: str | None = None
        self._renamed_final = False
        self._lifecycle_lock = asyncio.Lock()
        self._writer_task = asyncio.create_task(
            self._async_writer(),
            name="aupu_q360_raw_discovery_archive",
        )

    @classmethod
    async def async_open(
        cls,
        on_failure: FailureCallback,
        *,
        root: Path = RAW_ARCHIVE_ROOT,
        queue_limit: int = _DEFAULT_QUEUE_LIMIT,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        now: Callable[[], datetime] = _utc_now,
    ) -> RawDiscoveryArchive:
        """Open the pre-created fixed root and exclusively create one private session."""
        if (
            not isinstance(root, Path)
            or type(queue_limit) is not int
            or queue_limit <= 0
            or type(max_bytes) is not int
            or max_bytes <= 0
        ):
            raise DiscoveryRawArchiveUnavailableError
        try:
            opened = await asyncio.to_thread(_open_archive, root)
        except (OSError, ValueError):
            raise DiscoveryRawArchiveUnavailableError from None
        return cls(
            opened=opened,
            on_failure=on_failure,
            queue_limit=queue_limit,
            max_bytes=max_bytes,
            now=now,
        )

    @property
    def metadata(self) -> RawArchiveMetadata:
        """Return a point-in-time secret-free metadata snapshot."""
        return RawArchiveMetadata(
            enabled=True,
            status=self._status,
            session_id=self._session_id,
            event_count=self._durable_events,
            file_bytes=self._durable_bytes,
            sha256=self._sha256,
        )

    def enqueue(self, event: RawShadowEvent, context: ArchiveContext) -> None:
        """Reserve and queue one exact event without blocking the WSS receive path."""
        if self._failure_code is not None or self._closing:
            raise DiscoveryRawArchiveFailedError
        try:
            line = self._encode_line(event, context, self._reserved_events + 1)
        except DiscoveryRawArchiveFailedError:
            self._fail(DiscoveryRawArchiveFailedError.error_code)
            raise
        if self._reserved_bytes + len(line) > self._max_bytes:
            self._fail(DiscoveryRawArchiveLimitError.error_code)
            raise DiscoveryRawArchiveLimitError
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            self._fail(DiscoveryRawArchiveFailedError.error_code)
            raise DiscoveryRawArchiveFailedError from None
        self._reserved_bytes += len(line)
        self._reserved_events += 1

    async def async_complete(self) -> RawArchiveMetadata:
        """Drain, verify, hash, atomically complete, and manifest the archive."""
        async with self._lifecycle_lock:
            if self._status == "complete":
                return self.metadata
            if self._status == "incomplete":
                raise DiscoveryRawArchiveFailedError
            if self._closing:
                raise DiscoveryRawArchiveFailedError
            self._closing = True
            try:
                await self._async_stop_writer()
                if self._failure_code is not None:
                    raise self._failure_exception()
                if (
                    self._durable_bytes != self._reserved_bytes
                    or self._durable_events != self._reserved_events
                ):
                    raise DiscoveryRawArchiveFailedError
                digest = await asyncio.to_thread(self._complete_sync)
            except asyncio.CancelledError:
                self._fail(DiscoveryRawArchiveFailedError.error_code)
                abort_task = asyncio.create_task(self._async_incomplete())
                await asyncio.shield(abort_task)
                raise
            except (DiscoveryRawArchiveFailedError, DiscoveryRawArchiveLimitError) as exc:
                self._fail(exc.error_code)
                await self._async_incomplete()
                raise
            except (OSError, ValueError):
                self._fail(DiscoveryRawArchiveFailedError.error_code)
                await self._async_incomplete()
                raise DiscoveryRawArchiveFailedError from None
            self._sha256 = digest
            self._status = "complete"
            await asyncio.to_thread(self._close_directories_sync)
            return self.metadata

    async def async_abort(self) -> RawArchiveMetadata:
        """Drain accepted events and retain a plainly incomplete partial archive."""
        async with self._lifecycle_lock:
            if self._status in {"complete", "incomplete"}:
                return self.metadata
            self._closing = True
            self._fail(DiscoveryRawArchiveFailedError.error_code)
            await self._async_incomplete()
            return self.metadata

    async def async_stop(self) -> None:
        """Idempotently stop the writer while preserving incomplete local evidence."""
        if self._status == "open":
            await self.async_abort()

    def _encode_line(
        self,
        event: RawShadowEvent,
        context: ArchiveContext,
        sequence: int,
    ) -> bytes:
        if not isinstance(event, RawShadowEvent) or not isinstance(context, ArchiveContext):
            raise DiscoveryRawArchiveFailedError
        if (
            event.direction not in {"incoming", "outgoing"}
            or not isinstance(event.topic, str)
            or type(event.payload) is not bytes
        ):
            raise DiscoveryRawArchiveFailedError
        recorded_at = self._now()
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise DiscoveryRawArchiveFailedError
        timestamp = (
            recorded_at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        row = {
            "sequence": sequence,
            "recorded_at_utc": timestamp,
            "experiment": context.experiment.value,
            "round": context.round,
            "phase": context.phase.value,
            "direction": event.direction,
            "topic": event.topic,
            "payload_base64": base64.b64encode(event.payload).decode("ascii"),
        }
        try:
            return (
                json.dumps(
                    row,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError, UnicodeEncodeError):
            raise DiscoveryRawArchiveFailedError from None

    async def _async_writer(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _SENTINEL:
                    return
                if not isinstance(item, bytes):
                    self._writer_disabled = True
                    self._fail(DiscoveryRawArchiveFailedError.error_code)
                    continue
                if self._writer_disabled:
                    continue
                file_fd = self._file_fd
                if file_fd is None:
                    self._writer_disabled = True
                    self._fail(DiscoveryRawArchiveFailedError.error_code)
                    continue
                try:
                    await asyncio.to_thread(_write_all, file_fd, item)
                except OSError:
                    self._writer_disabled = True
                    self._fail(DiscoveryRawArchiveFailedError.error_code)
                    continue
                self._durable_bytes += len(item)
                self._durable_events += 1
            finally:
                self._queue.task_done()

    async def _async_stop_writer(self) -> None:
        if not self._sentinel_enqueued:
            self._sentinel_enqueued = True
            await self._queue.put(_SENTINEL)
        await asyncio.shield(self._writer_task)

    async def _async_incomplete(self) -> None:
        try:
            await self._async_stop_writer()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - preserve partial data behind fixed status
            self._fail(DiscoveryRawArchiveFailedError.error_code)
        await asyncio.to_thread(self._incomplete_sync)
        self._sha256 = None
        self._status = "incomplete"

    def _complete_sync(self) -> str:
        session_fd = self._required_session_fd()
        self._sync_and_close_file()
        digest = _sha256_file(session_fd, "events.jsonl.partial")
        if _path_exists(session_fd, "events.jsonl"):
            raise OSError
        _rename(session_fd, "events.jsonl.partial", "events.jsonl")
        self._renamed_final = True
        try:
            _fsync(session_fd)
            _write_manifest(
                session_fd,
                {
                    "status": "complete",
                    "session_id": self._session_id,
                    "event_count": self._durable_events,
                    "file_bytes": self._durable_bytes,
                    "sha256": digest,
                },
            )
            _fsync(session_fd)
        except (OSError, ValueError):
            self._restore_partial_sync()
            raise
        return digest

    def _incomplete_sync(self) -> None:
        session_fd = self._session_fd
        if session_fd is None:
            return
        try:
            self._sync_and_close_file()
        except OSError:
            pass
        self._restore_partial_sync()
        try:
            _write_manifest(
                session_fd,
                {
                    "status": "incomplete",
                    "session_id": self._session_id,
                    "event_count": self._durable_events,
                    "file_bytes": self._durable_bytes,
                },
            )
            _fsync(session_fd)
        except (OSError, ValueError):
            pass
        self._close_directories_sync()

    def _sync_and_close_file(self) -> None:
        file_fd = self._file_fd
        if file_fd is None:
            return
        self._file_fd = None
        try:
            _fsync(file_fd)
        finally:
            os.close(file_fd)

    def _restore_partial_sync(self) -> None:
        if not self._renamed_final:
            return
        session_fd = self._required_session_fd()
        try:
            if not _path_exists(session_fd, "events.jsonl.partial"):
                _rename(session_fd, "events.jsonl", "events.jsonl.partial")
        finally:
            self._renamed_final = False

    def _required_session_fd(self) -> int:
        if self._session_fd is None:
            raise OSError
        return self._session_fd

    def _close_directories_sync(self) -> None:
        for attribute in ("_session_fd", "_root_fd"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, attribute, None)

    def _fail(self, error_code: str) -> None:
        if self._failure_code is None:
            self._failure_code = error_code
        if self._failure_notified:
            return
        self._failure_notified = True
        try:
            self._on_failure(self._failure_code)
        except Exception:  # noqa: BLE001 - failure callback is isolated from raw data
            _LOGGER.error("AUPU raw discovery archive failure callback failed")

    def _failure_exception(
        self,
    ) -> DiscoveryRawArchiveFailedError | DiscoveryRawArchiveLimitError:
        if self._failure_code == DiscoveryRawArchiveLimitError.error_code:
            return DiscoveryRawArchiveLimitError()
        return DiscoveryRawArchiveFailedError()


def _open_archive(root: Path) -> _OpenedArchive:
    """Open and validate directory descriptors without following any symlink."""
    root_fd: int | None = None
    session_fd: int | None = None
    file_fd: int | None = None
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_IMODE(root_stat.st_mode) != 0o700:
            raise OSError
        session_id = f"rd-{secrets.token_hex(16)}"
        os.mkdir(session_id, 0o700, dir_fd=root_fd)
        session_fd = os.open(
            session_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        os.fchmod(session_fd, 0o700)
        session_stat = os.fstat(session_fd)
        if not stat.S_ISDIR(session_stat.st_mode) or stat.S_IMODE(session_stat.st_mode) != 0o700:
            raise OSError
        file_fd = os.open(
            "events.jsonl.partial",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=session_fd,
        )
        os.fchmod(file_fd, 0o600)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise OSError
        return _OpenedArchive(root_fd, session_fd, file_fd, session_id)
    except BaseException:
        for descriptor in (file_fd, session_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)
        raise


def _write_all(fd: int, data: bytes) -> None:
    """Write the complete encoded record or fail without exposing its bytes."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError
        view = view[written:]


def _fsync(fd: int) -> None:
    """Flush one owned file or directory descriptor."""
    os.fsync(fd)


def _sha256_file(directory_fd: int, name: str) -> str:
    """Hash one regular no-follow file relative to the private session descriptor."""
    fd = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory_fd,
    )
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _rename(directory_fd: int, source: str, target: str) -> None:
    """Atomically rename one session-local path without replacing a collision."""
    renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(target),
        1,  # RENAME_NOREPLACE
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _path_exists(directory_fd: int, name: str) -> bool:
    """Check a session-local name without following a possible symlink."""
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _write_manifest(directory_fd: int, manifest: dict[str, object]) -> None:
    """Atomically write one exact-mode manifest without replacing an existing path."""
    if _path_exists(directory_fd, "manifest.json"):
        raise OSError
    temp_name = f"manifest.json.tmp-{secrets.token_hex(16)}"
    fd: int | None = None
    try:
        fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(fd, 0o600)
        payload = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        _write_all(fd, payload)
        _fsync(fd)
        os.close(fd)
        fd = None
        _rename(directory_fd, temp_name, "manifest.json")
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except OSError:
            pass
        raise

"""Private, bounded staging for agent-streamed tool assets.

The MCP shim opens an agent-visible file and streams its bytes over the tools
Unix socket. This module stores those bytes under the tools service's private
admin-volume directory and exposes only opaque, tool-scoped ids to tool packages.
No agent-controlled pathname crosses the service boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import threading
import time
from typing import BinaryIO, Iterator, Literal

from host.tools.host_api import AssetMetadata

DEFAULT_ASSET_ROOT = Path("/mnt/trustyclaw-admin/tools-state/assets")
MAX_VIDEO_BYTES = 200_000_000
MIN_VIDEO_BYTES = 512
MAX_IMAGE_BYTES = 200_000_000
MIN_IMAGE_BYTES = 512
MAX_STAGED_ASSETS = 20
MAX_TOTAL_BYTES = 1_000_000_000
# Assets expire 26h after staging. This gives promptly-created 24h approvals a
# buffer for the hourly approval sweep; an approval created near asset expiry
# can outlive the bytes and then fails closed when decided.
ASSET_TTL_SECONDS = 26 * 3600
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
ALLOWED_VIDEO_TYPES = {
    "video/mp4": frozenset({".mp4"}),
    "video/quicktime": frozenset({".mov"}),
}
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": frozenset({".jpg", ".jpeg"}),
    "image/jpg": frozenset({".jpg", ".jpeg"}),
    "image/png": frozenset({".png"}),
    "image/webp": frozenset({".webp"}),
}
AssetKind = Literal["video", "image"]


class AssetError(ValueError):
    """An asset staging or lookup failure safe to return to the agent."""


@dataclass(frozen=True)
class _AssetRecord:
    tool_id: str
    metadata: AssetMetadata
    path: Path
    ready: bool


class ToolAssetStore:
    """A process-local index over tools-owned private spool files.

    The index intentionally does not survive a tools-service restart. Pending
    work then fails closed and the agent stages the source again from scratch.
    """

    def __init__(self, root: Path, *, clean_start: bool = False) -> None:
        self._root = root
        self._records: dict[str, _AssetRecord] = {}
        self._lock = threading.Lock()
        if clean_start:
            self._clean_start()

    def _clean_start(self) -> None:
        """Discard every prior-process file without following a replaced root."""
        try:
            root_mode = self._root.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(root_mode):
            self._root.unlink()
            self._ensure_root()
            return
        if not stat.S_ISDIR(root_mode):
            raise AssetError("Staged asset storage path is not a directory.")
        for child in self._root.iterdir():
            if child.is_symlink() or not child.is_dir():
                child.unlink(missing_ok=True)
            else:
                shutil.rmtree(child)

    def _ensure_root(self) -> None:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._root.chmod(0o700)

    def _remove_locked(self, asset_id: str) -> None:
        record = self._records.pop(asset_id, None)
        if record is not None:
            record.path.unlink(missing_ok=True)

    def _cleanup_locked(self, now: int) -> None:
        for asset_id, record in tuple(self._records.items()):
            if record.metadata.expires_at <= now:
                self._remove_locked(asset_id)

    def cleanup_expired(self, now: int | None = None) -> None:
        """Remove expired records and files for the service's recurring sweep."""
        with self._lock:
            self._cleanup_locked(int(time.time()) if now is None else now)

    def stage(
        self,
        *,
        kind: AssetKind,
        tool_id: str,
        filename: str,
        media_type: str,
        size_bytes: int,
        source: BinaryIO,
    ) -> AssetMetadata:
        allowed_types = ALLOWED_VIDEO_TYPES if kind == "video" else ALLOWED_IMAGE_TYPES
        allowed_suffixes = allowed_types.get(media_type)
        if allowed_suffixes is None:
            supported = "an MP4 or MOV file" if kind == "video" else "a JPEG, PNG, or WebP file"
            raise AssetError(f"{kind.title()} must be {supported}.")
        safe_filename = Path(filename).name
        if not safe_filename or Path(safe_filename).suffix.lower() not in allowed_suffixes:
            raise AssetError(f"{kind.title()} filename extension does not match its media type.")
        if len(safe_filename.encode("utf-8")) > 255 or any(
            ord(character) < 32 or ord(character) == 127 for character in safe_filename
        ):
            raise AssetError(f"{kind.title()} filename is invalid or too long.")
        minimum = MIN_VIDEO_BYTES if kind == "video" else MIN_IMAGE_BYTES
        maximum = MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES
        if not minimum <= size_bytes <= maximum:
            raise AssetError(
                f"{kind.title()} size must be between {minimum} bytes and {maximum} bytes."
            )
        now = int(time.time())
        # Under the lock: check quota and reserve a slot (a placeholder record
        # carrying the declared size) so concurrent stages see the reservation.
        # The disk write itself is agent-paced, so it runs OUTSIDE the lock —
        # otherwise a trickling upload would block describe/open/delete (and an
        # in-flight approval that needs to read a staged asset) for its whole
        # duration.
        with self._lock:
            self._cleanup_locked(now)
            if len(self._records) >= MAX_STAGED_ASSETS:
                raise AssetError("Too many assets are staged. Use or wait for an existing asset to expire.")
            total = sum(record.metadata.size_bytes for record in self._records.values())
            if total + size_bytes > MAX_TOTAL_BYTES:
                raise AssetError("Staged asset storage limit reached. Use or wait for an existing asset to expire.")
            self._ensure_root()
            asset_id = secrets.token_urlsafe(32)
            destination = self._root / asset_id
            reservation = AssetMetadata(
                asset_id=asset_id,
                filename=safe_filename,
                media_type=media_type,
                size_bytes=size_bytes,
                sha256="",
                expires_at=now + ASSET_TTL_SECONDS,
            )
            self._records[asset_id] = _AssetRecord(tool_id, reservation, destination, False)
        hasher = hashlib.sha256()
        remaining = size_bytes
        try:
            with destination.open("xb") as output:
                os.chmod(destination, 0o600)
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise AssetError(f"{kind.title()} upload ended before Content-Length bytes were received.")
                    output.write(chunk)
                    hasher.update(chunk)
                    remaining -= len(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            with self._lock:
                self._records.pop(asset_id, None)
            raise
        metadata = AssetMetadata(
            asset_id=asset_id,
            filename=safe_filename,
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=hasher.hexdigest(),
            expires_at=now + ASSET_TTL_SECONDS,
        )
        with self._lock:
            # Finalize only if the reservation survived (a concurrent cleanup or
            # delete could have removed it); otherwise discard the written file.
            if asset_id not in self._records:
                destination.unlink(missing_ok=True)
                raise AssetError(f"{kind.title()} staging was cancelled before it completed.")
            self._records[asset_id] = _AssetRecord(tool_id, metadata, destination, True)
        return metadata

    def describe(self, tool_id: str, asset_id: str) -> AssetMetadata:
        if not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id):
            raise AssetError("Asset id is invalid or expired. Upload the file again.")
        with self._lock:
            self._cleanup_locked(int(time.time()))
            record = self._records.get(asset_id)
            if record is None or record.tool_id != tool_id or not record.ready:
                raise AssetError("Asset id is invalid or expired. Upload the file again.")
            return record.metadata

    @contextmanager
    def open(self, tool_id: str, asset_id: str) -> Iterator[BinaryIO]:
        self.describe(tool_id, asset_id)
        with self._lock:
            record = self._records.get(asset_id)
            if record is None or record.tool_id != tool_id or not record.ready:
                raise AssetError("Asset id is invalid or expired. Upload the file again.")
            path = record.path
        try:
            with path.open("rb") as source:
                yield source
        except FileNotFoundError as exc:
            raise AssetError("Asset id is invalid or expired. Upload the file again.") from exc

    def delete(self, tool_id: str, asset_id: str) -> None:
        with self._lock:
            record = self._records.get(asset_id)
            if record is not None and record.tool_id == tool_id:
                self._remove_locked(asset_id)

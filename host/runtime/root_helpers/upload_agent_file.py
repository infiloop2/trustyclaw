"""Receive one bounded operator upload on stdin and publish it atomically.

The fixed ``upload-agent-file`` sudo helper demotes to ``trustyclaw-agent``
before invoking this module. The upload therefore has no filesystem authority
beyond the agent itself. Files land under ``user-files`` in the durable agent
home, with a UTC timestamp prefix that makes a lexical name sort chronological.
"""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import stat
import sys
import uuid
from typing import NoReturn


AGENT_HOME = Path("/mnt/trustyclaw-agent/agent-home")
UPLOAD_DIRECTORY = "user-files"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FILENAME_BYTES = 200
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def fail(message: str) -> NoReturn:
    print(json.dumps({"error": {"message": message}}, sort_keys=True))
    raise SystemExit(2)


def validate_filename(value: str) -> str:
    if not value or value in {".", ".."}:
        fail("filename must be non-empty")
    if any(character in value for character in ("/", "\\", "\0")):
        fail("filename must not contain path separators or a NUL byte")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        fail("filename must not contain control characters")
    if len(value.encode("utf-8")) > MAX_FILENAME_BYTES:
        fail(f"filename must be at most {MAX_FILENAME_BYTES} UTF-8 bytes")
    return value


def parse_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        fail("size must be an integer")
    if not 0 <= size <= MAX_UPLOAD_BYTES:
        fail(f"size must be between 0 and {MAX_UPLOAD_BYTES} bytes")
    return size


def open_upload_directory() -> int:
    home_fd = os.open(AGENT_HOME.resolve(strict=True), os.O_RDONLY | DIRECTORY | NOFOLLOW)
    try:
        try:
            os.mkdir(UPLOAD_DIRECTORY, mode=0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        try:
            directory_fd = os.open(
                UPLOAD_DIRECTORY,
                os.O_RDONLY | DIRECTORY | NOFOLLOW,
                dir_fd=home_fd,
            )
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                fail("user-files must be a real directory, not a symlink")
            raise
    finally:
        os.close(home_fd)
    info = os.fstat(directory_fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(directory_fd)
        fail("user-files must be a directory")
    return directory_fd


def write_all(file_fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(file_fd, view)
        if written <= 0:
            raise OSError("upload write made no progress")
        view = view[written:]


def upload(filename: str, size: int) -> dict[str, object]:
    directory_fd = open_upload_directory()
    temporary_name = f".uploading-{uuid.uuid4().hex}"
    file_fd = -1
    try:
        file_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        remaining = size
        while remaining:
            chunk = sys.stdin.buffer.read(min(1024 * 1024, remaining))
            if not chunk:
                fail("upload ended before the declared size")
            write_all(file_fd, chunk)
            remaining -= len(chunk)
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = -1

        uploaded_at = datetime.now(timezone.utc)
        timestamp = uploaded_at.strftime("%Y%m%dT%H%M%S.%fZ")
        base_name = f"{timestamp}_{filename}"
        stored_name = base_name
        for collision in range(1, 1000):
            try:
                os.link(
                    temporary_name,
                    stored_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                break
            except FileExistsError:
                stored_name = f"{base_name}.{collision + 1}"
        else:
            fail("could not allocate a unique upload filename")
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return {
            "path": f"{UPLOAD_DIRECTORY}/{stored_name}",
            "name": stored_name,
            "original_name": filename,
            "size_bytes": size,
            "uploaded_at": uploaded_at.isoformat().replace("+00:00", "Z"),
        }
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def main() -> None:
    if len(sys.argv) != 3:
        fail("usage: upload-agent-file <filename> <size>")
    filename = validate_filename(sys.argv[1])
    size = parse_size(sys.argv[2])
    print(json.dumps(upload(filename, size), sort_keys=True))


if __name__ == "__main__":
    main()

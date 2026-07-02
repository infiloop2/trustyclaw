#!/usr/bin/env bash
set -euo pipefail
exec /usr/sbin/runuser -u trustyclaw-agent -- env HOME=/mnt/trustyclaw-agent/agent-home /usr/bin/python3 - "$@" <<'PY'
from __future__ import annotations

from datetime import datetime, timezone
import errno
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath


AGENT_HOME = Path("/mnt/trustyclaw-agent/agent-home").resolve(strict=True)
MAX_LIST_ENTRIES = 1000
MAX_READ_BYTES = 1024 * 1024
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def fail(code: int, message: str) -> None:
    print(json.dumps({"error": {"message": message}}, sort_keys=True))
    raise SystemExit(code)


def public_path_for(parts: list[str]) -> str:
    if not parts:
        return "/"
    return "/" + "/".join(parts)


def parse_path(raw_path: str) -> list[str]:
    if "\0" in raw_path:
        fail(4, "path contains a NUL byte")
    if raw_path in {"", "/"}:
        return []
    parts: list[str] = []
    for part in PurePosixPath(raw_path.lstrip("/")).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            fail(4, "path escapes the agent home")
        parts.append(part)
    return parts


def open_agent_dir(parts: list[str]) -> int:
    dir_fd = os.open(AGENT_HOME, os.O_RDONLY | DIRECTORY | NOFOLLOW)
    try:
        for part in parts:
            try:
                part_info = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                fail(2, "path not found")
            if stat.S_ISLNK(part_info.st_mode):
                fail(3, "symlinks are not supported")
            try:
                next_fd = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=dir_fd)
            except FileNotFoundError:
                fail(2, "path not found")
            except NotADirectoryError:
                fail(3, "path is not a directory")
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    fail(3, "symlinks are not supported")
                raise
            os.close(dir_fd)
            dir_fd = next_fd
        return dir_fd
    except BaseException:
        os.close(dir_fd)
        raise


def kind_from_mode(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def entry_for(parent_parts: list[str], entry: os.DirEntry[str]) -> dict[str, object]:
    info = entry.stat(follow_symlinks=False)
    mode = info.st_mode
    entry: dict[str, object] = {
        "name": entry.name,
        "path": public_path_for([*parent_parts, entry.name]),
        "type": kind_from_mode(mode),
        "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if stat.S_ISREG(mode):
        entry["size_bytes"] = info.st_size
    return entry


def entry_sort_key(entry: dict[str, object]) -> tuple[bool, str, str]:
    name = str(entry["name"])
    return (entry["type"] != "directory", name.lower(), name)


def list_entries(parent_parts: list[str], dir_fd: int) -> tuple[list[dict[str, object]], bool]:
    entries: list[dict[str, object]] = []
    truncated = False
    with os.scandir(dir_fd) as scanner:
        for scanned, entry in enumerate(scanner, start=1):
            if scanned > MAX_LIST_ENTRIES:
                truncated = True
                break
            try:
                if entry.is_symlink():
                    continue
                entries.append(entry_for(parent_parts, entry))
            except FileNotFoundError:
                continue
    entries.sort(key=entry_sort_key)
    return entries, truncated


def list_path(raw_path: str) -> None:
    parts = parse_path(raw_path)
    dir_fd = open_agent_dir(parts)
    try:
        entries, truncated = list_entries(parts, dir_fd)
    finally:
        os.close(dir_fd)
    print(json.dumps({
        "path": public_path_for(parts),
        "entries": entries,
        "truncated": truncated,
    }, sort_keys=True))


def read_path(raw_path: str) -> None:
    parts = parse_path(raw_path)
    if not parts:
        fail(3, "path is not a regular file")
    parent_fd = open_agent_dir(parts[:-1])
    try:
        try:
            file_fd = os.open(parts[-1], os.O_RDONLY | NOFOLLOW | NONBLOCK, dir_fd=parent_fd)
        except FileNotFoundError:
            fail(2, "path not found")
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                fail(3, "symlinks are not supported")
            raise
    finally:
        os.close(parent_fd)
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            fail(3, "path is not a regular file")
        with os.fdopen(file_fd, "rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)
            file_fd = -1
    finally:
        if file_fd >= 0:
            os.close(file_fd)
    truncated = len(data) > MAX_READ_BYTES
    if truncated:
        data = data[:MAX_READ_BYTES]
    print(json.dumps({
        "path": public_path_for(parts),
        "size_bytes": info.st_size,
        "truncated": truncated,
        "encoding": "utf-8-replacement",
        "content": data.decode("utf-8", errors="replace"),
    }, sort_keys=True))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        fail(3, "usage: read-agent-file <list|read> <path>")
    action, raw_path = argv[1], argv[2]
    if action == "list":
        list_path(raw_path)
        return 0
    if action == "read":
        read_path(raw_path)
        return 0
    fail(3, "operation must be list or read")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
PY

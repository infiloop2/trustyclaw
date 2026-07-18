"""Minimal PostgreSQL wire-protocol client, Python standard library only.

TrustyClaw keeps host runtime code free of third-party dependencies, so the
admin service speaks the frontend/backend protocol (v3) itself instead of
using a driver. The scope is deliberately tiny — exactly what the admin-state
database needs and nothing more:

- Local Unix-socket connections only. No TCP, no TLS.
- ``peer``/``trust`` authentication only (no credential exchange on the wire);
  any password-based authentication request is refused.
- Text format for every parameter and result value.
- The simple query protocol for parameterless SQL, which may contain multiple
  statements (migration files); the extended protocol (Parse/Bind/Execute)
  for parameterized SQL, using ``%s`` placeholders like the DB-API.
- Result decoding for the handful of types the schema uses: bool, integers,
  floats, json/jsonb, and text (everything else comes back as ``str``).

Transactions are the caller's job (``host.runtime.core.db`` sends explicit
BEGIN/COMMIT/ROLLBACK); a parameterless statement outside a transaction simply
autocommits, which is also what lets ``CREATE DATABASE`` run.

Protocol reference: https://www.postgresql.org/docs/14/protocol-message-formats.html
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import struct
from typing import Any, Sequence

PROTOCOL_VERSION = 196608  # protocol 3.0
AUTHENTICATION_OK = 0
DEFAULT_SOCKET_DIR = "/var/run/postgresql"
DEFAULT_PORT = 5432
# Result-column type OIDs (pg_type.oid) the runtime schema can produce.
_BOOL_OID = 16
_INT_OIDS = frozenset({20, 21, 23, 26, 28})  # int8, int2, int4, oid, xid
_FLOAT_OIDS = frozenset({700, 701})
_JSON_OIDS = frozenset({114, 3802})


class Error(Exception):
    """A database error. ``fields`` holds the server's ErrorResponse fields;
    ``sqlstate`` is the five-character SQLSTATE code when the server sent one."""

    def __init__(self, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.fields = fields or {}
        self.sqlstate = self.fields.get("C")


class ProtocolError(Error):
    """The server sent something outside this client's documented scope."""


class Jsonb:
    """Marks a parameter to be sent as its JSON text (for json/jsonb columns),
    with deterministic key order."""

    def __init__(self, value: Any) -> None:
        self.value = value

    def dumps(self) -> str:
        return json.dumps(self.value, sort_keys=True)


class Result:
    def __init__(self, columns: list[tuple[str, int]], rows: list[tuple[Any, ...]]) -> None:
        self.columns = columns
        self.rows = rows


def _encode_parameter(value: Any) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, Jsonb):
        return value.dumps().encode()
    if isinstance(value, bool):
        return b"t" if value else b"f"
    if isinstance(value, int):
        return str(value).encode()
    if isinstance(value, float):
        return repr(value).encode()  # repr round-trips float64 exactly
    if isinstance(value, str):
        return value.encode()
    raise TypeError(f"unsupported parameter type {type(value).__name__!r}")


def _decode_value(type_oid: int, data: bytes | None) -> Any:
    if data is None:
        return None
    text = data.decode()
    if type_oid == _BOOL_OID:
        return text == "t"
    if type_oid in _INT_OIDS:
        return int(text)
    if type_oid in _FLOAT_OIDS:
        return float(text)
    if type_oid in _JSON_OIDS:
        return json.loads(text)
    return text


def _number_placeholders(sql: str, parameter_count: int) -> str:
    """Translate DB-API ``%s`` placeholders to the protocol's ``$1..$n``."""
    parts = sql.split("%s")
    if len(parts) - 1 != parameter_count:
        raise Error(
            f"query has {len(parts) - 1} placeholders but {parameter_count} parameters were given"
        )
    numbered = [parts[0]]
    for index, part in enumerate(parts[1:], start=1):
        numbered.append(f"${index}")
        numbered.append(part)
    return "".join(numbered)


class Connection:
    """One protocol session over the local Unix socket. Not thread-safe; the
    pool in ``host.runtime.core.db`` hands a connection to one caller at a time."""

    def __init__(
        self,
        *,
        socket_dir: str = DEFAULT_SOCKET_DIR,
        port: int = DEFAULT_PORT,
        dbname: str,
        user: str | None = None,
    ) -> None:
        self._sock: socket.socket | None = None
        if user is None:
            # peer auth maps the OS user to the role of the same name, so the
            # default role is simply who we are.
            user = getpass.getuser()
        path = os.path.join(socket_dir, f".s.PGSQL.{port}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            self._sock = sock
            self._startup(user=user, dbname=dbname)
        except BaseException:
            sock.close()
            self._sock = None
            raise

    # -- connection lifecycle ------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._sock is None

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.sendall(b"X" + struct.pack("!i", 4))  # Terminate
            except OSError:
                pass
            sock.close()

    def _startup(self, *, user: str, dbname: str) -> None:
        payload = struct.pack("!i", PROTOCOL_VERSION)
        for key, value in (("user", user), ("database", dbname), ("client_encoding", "UTF8")):
            payload += key.encode() + b"\x00" + value.encode() + b"\x00"
        payload += b"\x00"
        self._sendall(struct.pack("!i", len(payload) + 4) + payload)
        while True:
            message_type, payload = self._read_message()
            if message_type == b"R":
                (method,) = struct.unpack_from("!i", payload)
                if method != AUTHENTICATION_OK:
                    raise ProtocolError(
                        f"server requested authentication method {method}; only"
                        " peer/trust (no credential exchange) is supported"
                    )
            elif message_type in (b"S", b"K", b"N"):
                continue  # ParameterStatus / BackendKeyData / NoticeResponse
            elif message_type == b"Z":
                return
            elif message_type == b"E":
                raise self._error(payload)
            else:
                raise ProtocolError(f"unexpected startup message type {message_type!r}")

    # -- queries ---------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Result:
        """Run ``sql`` and return its (last) result set. With ``params`` the
        extended protocol binds them by ``%s`` position (one statement only);
        without, the simple protocol runs the string, which may hold several
        statements."""
        if self._sock is None:
            raise Error("connection is closed")
        try:
            if params is None:
                return self._simple_query(sql)
            return self._extended_query(sql, params)
        except (OSError, ProtocolError):
            # The stream state is unknown after a transport/framing failure;
            # this connection cannot be reused.
            self.close()
            raise

    def _simple_query(self, sql: str) -> Result:
        self._send_message(b"Q", sql.encode() + b"\x00")
        return self._collect_results()

    def _extended_query(self, sql: str, params: Sequence[Any]) -> Result:
        numbered = _number_placeholders(sql, len(params))
        parse = b"\x00" + numbered.encode() + b"\x00" + struct.pack("!h", 0)
        bind = b"\x00\x00"  # unnamed portal, unnamed statement
        bind += struct.pack("!hh", 1, 0)  # all parameters in text format
        bind += struct.pack("!h", len(params))
        for value in params:
            encoded = _encode_parameter(value)
            if encoded is None:
                bind += struct.pack("!i", -1)
            else:
                bind += struct.pack("!i", len(encoded)) + encoded
        bind += struct.pack("!hh", 1, 0)  # all results in text format
        self._send_message(b"P", parse)
        self._send_message(b"B", bind)
        self._send_message(b"D", b"P\x00")  # Describe the unnamed portal
        self._send_message(b"E", b"\x00" + struct.pack("!i", 0))  # no row limit
        self._send_message(b"S", b"")  # Sync
        return self._collect_results()

    def _collect_results(self) -> Result:
        """Consume messages until ReadyForQuery, keeping the last result set.
        On ErrorResponse, keep consuming to ReadyForQuery (the server discards
        the rest itself) and then raise, so the connection stays usable."""
        columns: list[tuple[str, int]] = []
        rows: list[tuple[Any, ...]] = []
        error: Error | None = None
        while True:
            message_type, payload = self._read_message()
            if message_type == b"Z":
                if error is not None:
                    raise error
                return Result(columns, rows)
            if message_type == b"E":
                error = error or self._error(payload)
            elif message_type == b"T":
                columns = self._row_description(payload)
                rows = []
            elif message_type == b"D":
                rows.append(self._data_row(payload, columns))
            elif message_type in (b"1", b"2", b"3", b"C", b"I", b"n", b"S", b"N", b"A"):
                # ParseComplete, BindComplete, CloseComplete, CommandComplete,
                # EmptyQueryResponse, NoData, ParameterStatus, Notice,
                # NotificationResponse: nothing to record.
                continue
            else:
                raise ProtocolError(f"unexpected message type {message_type!r}")

    # -- message decoding --------------------------------------------------------

    def _row_description(self, payload: bytes) -> list[tuple[str, int]]:
        (count,) = struct.unpack_from("!h", payload)
        offset = 2
        columns: list[tuple[str, int]] = []
        for _ in range(count):
            end = payload.index(b"\x00", offset)
            name = payload[offset:end].decode()
            offset = end + 1
            _table_oid, _column, type_oid, _size, _modifier, _format = struct.unpack_from(
                "!ihihih", payload, offset
            )
            offset += 18
            columns.append((name, type_oid))
        return columns

    def _data_row(self, payload: bytes, columns: list[tuple[str, int]]) -> tuple[Any, ...]:
        (count,) = struct.unpack_from("!h", payload)
        offset = 2
        values: list[Any] = []
        for index in range(count):
            (length,) = struct.unpack_from("!i", payload, offset)
            offset += 4
            if length == -1:
                data = None
            else:
                data = payload[offset : offset + length]
                offset += length
            type_oid = columns[index][1] if index < len(columns) else 25
            values.append(_decode_value(type_oid, data))
        return tuple(values)

    def _error(self, payload: bytes) -> Error:
        fields: dict[str, str] = {}
        offset = 0
        while offset < len(payload) and payload[offset : offset + 1] != b"\x00":
            code = payload[offset : offset + 1].decode()
            end = payload.index(b"\x00", offset + 1)
            fields[code] = payload[offset + 1 : end].decode()
            offset = end + 1
        message = fields.get("M", "database error")
        severity = fields.get("S", "ERROR")
        return Error(f"{severity}: {message}", fields)

    # -- transport ------------------------------------------------------------

    def _send_message(self, message_type: bytes, payload: bytes) -> None:
        self._sendall(message_type + struct.pack("!i", len(payload) + 4) + payload)

    def _sendall(self, data: bytes) -> None:
        if self._sock is None:
            raise Error("connection is closed")
        self._sock.sendall(data)

    def _read_message(self) -> tuple[bytes, bytes]:
        header = self._read_exactly(5)
        message_type = header[:1]
        (length,) = struct.unpack("!i", header[1:5])
        if length < 4:
            raise ProtocolError(f"invalid message length {length}")
        return message_type, self._read_exactly(length - 4)

    def _read_exactly(self, count: int) -> bytes:
        if self._sock is None:
            raise Error("connection is closed")
        chunks: list[bytes] = []
        remaining = count
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise ProtocolError("server closed the connection mid-message")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


class Cursor:
    """DB-API-shaped adapter over ``Connection.execute`` — just enough for the
    runtime's call sites (execute/fetchone/fetchall as a context manager)."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._result = Result([], [])

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        self._result = self._connection.execute(sql, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result.rows[0] if self._result.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result.rows)


def connect(
    *,
    socket_dir: str = DEFAULT_SOCKET_DIR,
    port: int = DEFAULT_PORT,
    dbname: str,
    user: str | None = None,
) -> Connection:
    return Connection(socket_dir=socket_dir, port=port, dbname=dbname, user=user)

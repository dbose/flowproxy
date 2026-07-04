"""PostgreSQL v3 wire-protocol codecs.

Pure functions that build backend messages (server -> client) and parse
frontend messages (client -> server). No I/O policy lives here — the session
state machine in ``network/server.py`` decides *when* to send what.

Reference: https://www.postgresql.org/docs/current/protocol-message-formats.html
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Sequence

from asyncio import StreamReader

from engine.exceptions import ProtocolViolationError

# --------------------------------------------------------------------------- #
# Special startup-phase request codes (sent length-prefixed, without a tag byte)
# --------------------------------------------------------------------------- #
PROTOCOL_V3: int = 196608          # 0x00030000
SSL_REQUEST_CODE: int = 80877103   # 0x04D2162F
GSSENC_REQUEST_CODE: int = 80877104
CANCEL_REQUEST_CODE: int = 80877102

MAX_STARTUP_PACKET: int = 10_000
MAX_MESSAGE_LENGTH: int = 16 * 1024 * 1024

# --------------------------------------------------------------------------- #
# PostgreSQL type OIDs (pg_type.oid) used for RowDescription
# --------------------------------------------------------------------------- #
OID_BOOL: int = 16
OID_INT8: int = 20
OID_INT4: int = 23
OID_TEXT: int = 25
OID_FLOAT8: int = 701
OID_VARCHAR: int = 1043
OID_DATE: int = 1082
OID_TIMESTAMP: int = 1114
OID_NUMERIC: int = 1700

_TYPE_SIZES: dict[int, int] = {
    OID_BOOL: 1,
    OID_INT4: 4,
    OID_INT8: 8,
    OID_FLOAT8: 8,
    OID_DATE: 4,
    OID_TIMESTAMP: 8,
    OID_TEXT: -1,
    OID_VARCHAR: -1,
    OID_NUMERIC: -1,
}


class StartupKind(Enum):
    SSL_REQUEST = "ssl"
    GSSENC_REQUEST = "gssenc"
    CANCEL_REQUEST = "cancel"
    STARTUP = "startup"


@dataclass(frozen=True)
class StartupMessage:
    kind: StartupKind
    parameters: dict[str, str]


@dataclass(frozen=True)
class PGColumn:
    """One field entry in a RowDescription ('T') packet."""

    name: str
    type_oid: int = OID_TEXT
    table_oid: int = 0
    column_attr: int = 0
    type_mod: int = -1
    format_code: int = 0  # 0 = text format

    @property
    def type_size(self) -> int:
        return _TYPE_SIZES.get(self.type_oid, -1)


# --------------------------------------------------------------------------- #
# Low-level framing
# --------------------------------------------------------------------------- #
def _cstr(value: str) -> bytes:
    return value.encode("utf-8") + b"\x00"


def message(tag: bytes, payload: bytes = b"") -> bytes:
    """Frame one backend message: tag byte + int32 length (self-inclusive) + payload."""
    return tag + struct.pack("!I", len(payload) + 4) + payload


# --------------------------------------------------------------------------- #
# Backend message builders (server -> client)
# --------------------------------------------------------------------------- #
def ssl_denied() -> bytes:
    """Single-byte 'N' answer to an SSLRequest — NOT a framed message."""
    return b"N"


def authentication_ok() -> bytes:
    return message(b"R", struct.pack("!I", 0))


def authentication_cleartext_password() -> bytes:
    return message(b"R", struct.pack("!I", 3))


def parameter_status(name: str, value: str) -> bytes:
    return message(b"S", _cstr(name) + _cstr(value))


def backend_key_data(pid: int, secret_key: int) -> bytes:
    return message(b"K", struct.pack("!II", pid, secret_key))


def ready_for_query(txn_status: bytes = b"I") -> bytes:
    """'Z' — I=idle, T=in transaction, E=failed transaction."""
    return message(b"Z", txn_status)


def row_description(columns: Sequence[PGColumn]) -> bytes:
    body = struct.pack("!H", len(columns))
    for col in columns:
        body += _cstr(col.name)
        body += struct.pack(
            "!IhIhih",
            col.table_oid,
            col.column_attr,
            col.type_oid,
            col.type_size,
            col.type_mod,
            col.format_code,
        )
    return message(b"T", body)


def data_row(values: Sequence[Any]) -> bytes:
    body = struct.pack("!H", len(values))
    for value in values:
        if value is None:
            body += struct.pack("!i", -1)  # SQL NULL
        else:
            encoded = encode_text_value(value)
            body += struct.pack("!I", len(encoded)) + encoded
    return message(b"D", body)


def command_complete(tag: str) -> bytes:
    return message(b"C", _cstr(tag))


def empty_query_response() -> bytes:
    return message(b"I")


def parse_complete() -> bytes:
    return message(b"1")


def bind_complete() -> bytes:
    return message(b"2")


def close_complete() -> bytes:
    return message(b"3")


def no_data() -> bytes:
    return message(b"n")


def error_response(
    msg: str,
    *,
    sqlstate: str = "XX000",
    severity: str = "ERROR",
    detail: str | None = None,
) -> bytes:
    body = (
        b"S" + _cstr(severity)
        + b"V" + _cstr(severity)
        + b"C" + _cstr(sqlstate)
        + b"M" + _cstr(msg)
    )
    if detail:
        body += b"D" + _cstr(detail)
    body += b"\x00"
    return message(b"E", body)


# --------------------------------------------------------------------------- #
# Value encoding (text format, format_code=0)
# --------------------------------------------------------------------------- #
def encode_text_value(value: Any) -> bytes:
    if isinstance(value, bool):
        return b"t" if value else b"f"
    if isinstance(value, float):
        return repr(value).encode("ascii")
    if isinstance(value, (int, Decimal)):
        return str(value).encode("ascii")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S.%f").encode("ascii")
    if isinstance(value, date):
        return value.isoformat().encode("ascii")
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def infer_column(name: str, sample: Any) -> PGColumn:
    """Choose a pg_type OID from a Python sample value so drivers coerce correctly."""
    if isinstance(sample, bool):
        oid = OID_BOOL
    elif isinstance(sample, int):
        oid = OID_INT8
    elif isinstance(sample, float):
        oid = OID_FLOAT8
    elif isinstance(sample, Decimal):
        oid = OID_NUMERIC
    elif isinstance(sample, datetime):
        oid = OID_TIMESTAMP
    elif isinstance(sample, date):
        oid = OID_DATE
    else:
        oid = OID_VARCHAR
    return PGColumn(name=name, type_oid=oid)


# --------------------------------------------------------------------------- #
# Frontend message parsing (client -> server)
# --------------------------------------------------------------------------- #
async def read_startup_message(reader: StreamReader) -> StartupMessage:
    """Read one untagged startup-phase packet (StartupMessage / SSLRequest / ...)."""
    header = await reader.readexactly(4)
    length = int.from_bytes(header, "big")
    if not 8 <= length <= MAX_STARTUP_PACKET:
        raise ProtocolViolationError(f"invalid startup packet length {length}")

    payload = await reader.readexactly(length - 4)
    code = int.from_bytes(payload[:4], "big")

    if code == SSL_REQUEST_CODE:
        return StartupMessage(StartupKind.SSL_REQUEST, {})
    if code == GSSENC_REQUEST_CODE:
        return StartupMessage(StartupKind.GSSENC_REQUEST, {})
    if code == CANCEL_REQUEST_CODE:
        return StartupMessage(StartupKind.CANCEL_REQUEST, {})
    if code != PROTOCOL_V3:
        raise ProtocolViolationError(
            f"unsupported protocol version 0x{code:08X}; only protocol 3.0 is implemented"
        )

    parameters: dict[str, str] = {}
    tokens = payload[4:].split(b"\x00")
    for key, value in zip(tokens[::2], tokens[1::2]):
        if key:
            parameters[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return StartupMessage(StartupKind.STARTUP, parameters)


async def read_message(reader: StreamReader) -> tuple[bytes, bytes] | None:
    """Read one tagged frontend message. Returns ``None`` on clean EOF."""
    tag = await reader.read(1)
    if not tag:
        return None
    length = int.from_bytes(await reader.readexactly(4), "big")
    if not 4 <= length <= MAX_MESSAGE_LENGTH:
        raise ProtocolViolationError(f"invalid message length {length} for tag {tag!r}")
    payload = await reader.readexactly(length - 4) if length > 4 else b""
    return tag, payload


class PayloadReader:
    """Cursor over a frontend message payload (Parse/Bind/Describe bodies)."""

    def __init__(self, payload: bytes) -> None:
        self._buf: bytes = payload
        self._pos: int = 0

    def cstring(self) -> str:
        end = self._buf.index(b"\x00", self._pos)
        value = self._buf[self._pos:end].decode("utf-8", "replace")
        self._pos = end + 1
        return value

    def int16(self) -> int:
        value = struct.unpack_from("!h", self._buf, self._pos)[0]
        self._pos += 2
        return value

    def int32(self) -> int:
        value = struct.unpack_from("!i", self._buf, self._pos)[0]
        self._pos += 4
        return value

    def read(self, n: int) -> bytes:
        value = self._buf[self._pos:self._pos + n]
        self._pos += n
        return value

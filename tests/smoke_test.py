"""End-to-end wire-path smoke test.

Boots the full stack in-process (dry-run compiler + mock executor) and drives
it with a hand-rolled PostgreSQL v3 client over a real TCP socket — the same
byte sequences QuickSight's JDBC driver emits: SSLRequest, StartupMessage,
driver handshake noise, then a cube query.

Run:  python -m tests.smoke_test
"""

from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.compiler import SemanticCompiler
from engine.parser import SQLExtractor
from executor.warehouse import MockWarehouseExecutor
from network.server import PostgresProxyServer

FIXTURE = Path(__file__).parent / "fixtures" / "semantic_manifest.json"
HOST, PORT = "127.0.0.1", 55432


def _startup_packet() -> bytes:
    body = struct.pack("!I", 196608)
    for k, v in (("user", "quicksight"), ("database", "flowproxy")):
        body += k.encode() + b"\x00" + v.encode() + b"\x00"
    body += b"\x00"
    return struct.pack("!I", len(body) + 4) + body


def _query_packet(sql: str) -> bytes:
    payload = sql.encode() + b"\x00"
    return b"Q" + struct.pack("!I", len(payload) + 4) + payload


async def _read_backend_messages(reader: asyncio.StreamReader) -> list[tuple[bytes, bytes]]:
    """Read framed backend messages until ReadyForQuery ('Z')."""
    messages: list[tuple[bytes, bytes]] = []
    while True:
        tag = await reader.readexactly(1)
        length = int.from_bytes(await reader.readexactly(4), "big")
        payload = await reader.readexactly(length - 4) if length > 4 else b""
        messages.append((tag, payload))
        if tag == b"Z":
            return messages


async def run() -> None:
    compiler = SemanticCompiler(FIXTURE, dry_run=True)
    server = PostgresProxyServer(
        compiler,
        SQLExtractor(compiler.registry),
        MockWarehouseExecutor(row_count=3),
        host=HOST,
        port=PORT,
    )
    server_task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.3)

    reader, writer = await asyncio.open_connection(HOST, PORT)

    # 1. SSLRequest -> expect single-byte 'N'
    writer.write(struct.pack("!II", 8, 80877103))
    await writer.drain()
    assert await reader.readexactly(1) == b"N", "SSL denial failed"
    print("PASS  SSLRequest answered with 'N'")

    # 2. StartupMessage -> AuthenticationOk ... ReadyForQuery
    writer.write(_startup_packet())
    await writer.drain()
    msgs = await _read_backend_messages(reader)
    tags = [t for t, _ in msgs]
    assert tags[0] == b"R" and b"K" in tags and tags[-1] == b"Z", f"bad handshake: {tags}"
    assert any(t == b"S" for t in tags), "missing ParameterStatus"
    print(f"PASS  handshake: {b''.join(tags).decode()}")

    # 3. Driver noise: SET must yield CommandComplete
    writer.write(_query_packet("SET extra_float_digits = 3"))
    await writer.drain()
    msgs = await _read_backend_messages(reader)
    assert [t for t, _ in msgs] == [b"C", b"Z"], f"SET mishandled: {msgs}"
    print("PASS  SET answered with CommandComplete")

    # 4. The QuickSight-style cube query
    sql = 'SELECT "turnover_rate", "department", "hired_at" FROM "turnover_cube" GROUP BY 2, 3'
    writer.write(_query_packet(sql))
    await writer.drain()
    msgs = await _read_backend_messages(reader)
    tags = [t for t, _ in msgs]
    assert tags == [b"T", b"D", b"D", b"D", b"C", b"Z"], f"unexpected sequence: {tags}"
    n_cols = struct.unpack("!H", msgs[0][1][:2])[0]
    assert n_cols == 3, f"expected 3 columns, got {n_cols}"
    assert msgs[-2][1].rstrip(b"\x00") == b"SELECT 3"
    print(f"PASS  data query -> T + 3xD + C ({n_cols} cols): {msgs[1][1]!r}")

    # 5. Unknown column -> ErrorResponse with SQLSTATE 42703, connection survives
    writer.write(_query_packet('SELECT "nonexistent_metric" FROM "cube"'))
    await writer.drain()
    msgs = await _read_backend_messages(reader)
    assert [t for t, _ in msgs] == [b"E", b"Z"], f"error path broken: {msgs}"
    assert b"42703" in msgs[0][1], "wrong SQLSTATE"
    print("PASS  unknown column -> ErrorResponse(42703), session intact")

    # 6. Terminate cleanly
    writer.write(b"X" + struct.pack("!I", 4))
    await writer.drain()
    writer.close()
    server_task.cancel()
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run())

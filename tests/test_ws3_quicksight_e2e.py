"""WS3 — end-to-end QuickSight exploration over the real stack.

Boots the full PostgresProxyServer against DuckDB (real MetricFlow, real
catalog) and drives it over a TCP socket exactly as QuickSight's PostgreSQL
JDBC driver would: handshake → discover cubes via information_schema → run a
slice/dice query on a cube → assert the semi-additive golden numbers come back
through the wire.

This is the success-criterion test: "a real dbt project with sem_*.yml,
queried by an analyst from a BI tool, guardrails enforced."
"""

from __future__ import annotations

import asyncio
import hashlib
import struct

import pytest

from engine.catalog import CatalogBuilder
from engine.parser import SQLExtractor
from executor.warehouse import MockWarehouseExecutor
from network.catalog import CatalogResponder
from network.server import PostgresProxyServer

HOST, PORT = "127.0.0.1", 55433


# --------------------------------------------------------------------------- #
# Minimal PostgreSQL v3 client (same bytes QuickSight's JDBC driver sends)
# --------------------------------------------------------------------------- #
def _startup() -> bytes:
    body = struct.pack("!I", 196608)
    for k, v in (("user", "analyst"), ("database", "flowproxy")):
        body += k.encode() + b"\x00" + v.encode() + b"\x00"
    body += b"\x00"
    return struct.pack("!I", len(body) + 4) + body


def _query(sql: str) -> bytes:
    payload = sql.encode() + b"\x00"
    return b"Q" + struct.pack("!I", len(payload) + 4) + payload


async def _read_until_ready(reader: asyncio.StreamReader) -> list[tuple[bytes, bytes]]:
    msgs: list[tuple[bytes, bytes]] = []
    while True:
        tag = await reader.readexactly(1)
        length = int.from_bytes(await reader.readexactly(4), "big")
        payload = await reader.readexactly(length - 4) if length > 4 else b""
        msgs.append((tag, payload))
        if tag == b"Z":
            return msgs


def _rows_from(msgs: list[tuple[bytes, bytes]]) -> tuple[list[str], list[list[str]]]:
    """Decode RowDescription 'T' column names + DataRow 'D' text values."""
    cols: list[str] = []
    rows: list[list[str]] = []
    for tag, payload in msgs:
        if tag == b"T":
            n = struct.unpack_from("!H", payload, 0)[0]
            pos = 2
            for _ in range(n):
                end = payload.index(b"\x00", pos)
                cols.append(payload[pos:end].decode())
                pos = end + 1 + 18  # skip the 18-byte field descriptor
        elif tag == b"D":
            n = struct.unpack_from("!H", payload, 0)[0]
            pos = 2
            vals: list[str] = []
            for _ in range(n):
                ln = struct.unpack_from("!i", payload, pos)[0]
                pos += 4
                if ln == -1:
                    vals.append(None)  # type: ignore[arg-type]
                else:
                    vals.append(payload[pos:pos + ln].decode())
                    pos += ln
            rows.append(vals)
    return cols, rows


@pytest.fixture
async def server(compiler):
    from tests.conftest import MANIFEST_PATH

    sha = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()[:12]
    catalog = CatalogBuilder(compiler, sha).build()
    srv = PostgresProxyServer(
        compiler,
        SQLExtractor(compiler.registry),
        MockWarehouseExecutor(),  # unused on the real path
        catalog_responder=CatalogResponder(catalog),
        host=HOST,
        port=PORT,
    )
    task = asyncio.create_task(srv.serve_forever())
    await asyncio.sleep(0.3)
    yield srv
    task.cancel()


async def _connect():
    reader, writer = await asyncio.open_connection(HOST, PORT)
    # SSLRequest → 'N'
    writer.write(struct.pack("!II", 8, 80877103))
    await writer.drain()
    assert await reader.readexactly(1) == b"N"
    # Startup → ReadyForQuery
    writer.write(_startup())
    await writer.drain()
    await _read_until_ready(reader)
    return reader, writer


@pytest.mark.asyncio
async def test_quicksight_discovers_cubes(server):
    reader, writer = await _connect()
    writer.write(_query(
        "SELECT table_schema, table_name, table_type "
        "FROM information_schema.tables WHERE table_schema = 'semantic_layer'"
    ))
    await writer.drain()
    cols, rows = _rows_from(await _read_until_ready(reader))
    ti = cols.index("table_name")
    names = {r[ti] for r in rows}
    assert "daily_balances" in names
    assert "all_metrics" in names
    writer.close()


@pytest.mark.asyncio
async def test_quicksight_discovers_columns(server):
    reader, writer = await _connect()
    writer.write(_query(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'daily_balances'"
    ))
    await writer.drain()
    cols, rows = _rows_from(await _read_until_ready(reader))
    ci = cols.index("column_name")
    colnames = {r[ci] for r in rows}
    assert "account_balance" in colnames
    assert "account__region" in colnames
    # Pre-scoping: transaction dims are NOT on the balances cube.
    assert "transaction__transaction_type" not in colnames
    writer.close()


@pytest.mark.asyncio
async def test_quicksight_slices_semiadditive_metric(server):
    """The headline: analyst drags account_balance + region onto a visual.

    QuickSight emits GROUP BY SQL; the wire returns end-of-period balances
    (semi-additive guardrail enforced end-to-end, over the socket)."""
    reader, writer = await _connect()
    writer.write(_query(
        'SELECT "account__region", "account_balance" FROM "daily_balances" '
        'GROUP BY 1 ORDER BY 1'
    ))
    await writer.drain()
    cols, rows = _rows_from(await _read_until_ready(reader))

    # Aggregated across all months → last-value per region summed over accounts.
    # Region totals of end-of-period balances: this is semi-additive over time.
    by_region = {r[cols.index("account__region")]: float(r[cols.index("account_balance")]) for r in rows}
    assert set(by_region) == {"EMEA", "AMER"}
    # EMEA end-of-period across the window = 2000 (Mar), AMER = 3000 (Mar).
    # (Grouping only by region collapses time to the max window per account.)
    assert by_region["EMEA"] == 2000.0
    assert by_region["AMER"] == 3000.0
    writer.close()


@pytest.mark.asyncio
async def test_quicksight_filtered_slice(server):
    """Analyst adds a region filter in QuickSight → WHERE pushed to MetricFlow."""
    reader, writer = await _connect()
    # QuickSight emits a month-grain time column when the analyst sets a
    # monthly date hierarchy; our parser resolves the __month grain suffix.
    writer.write(_query(
        'SELECT "metric_time__month", "account_balance" FROM "daily_balances" '
        "WHERE region = 'EMEA' GROUP BY 1 ORDER BY 1"
    ))
    await writer.drain()
    cols, rows = _rows_from(await _read_until_ready(reader))
    bi = cols.index("account_balance")
    balances = [float(r[bi]) for r in rows]
    # EMEA monthly end-of-period balances only.
    assert balances == [1500.0, 1200.0, 2000.0]
    writer.close()

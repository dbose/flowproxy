"""Module 3 — Async PostgreSQL wire-protocol proxy server.

The asyncio TCP front door QuickSight connects to with its stock PostgreSQL
connector. Per connection this runs the v3 protocol state machine:

    startup:  SSLRequest -> 'N'  ·  StartupMessage -> auth -> params -> 'Z'
    steady:   'Q' simple queries, plus the extended protocol ('P','B','D',
              'E','S','C','H') that JDBC/ODBC drivers use under the hood.

Data-bearing queries flow:  raw SQL -> SQLExtractor (sqlglot) ->
SemanticCompiler (MetricFlow plan) -> WarehouseExecutor -> RowDescription
'T' + DataRow 'D'* + CommandComplete 'C' back over the socket.

Driver handshake noise (SET/SHOW/BEGIN/pg_catalog probes) is answered
locally with protocol-correct canned responses so QuickSight's connection
validation succeeds without ever reaching MetricFlow.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

from engine.compiler import SemanticCompiler
from engine.exceptions import FlowProxyError, ProtocolViolationError, WarehouseExecutionError
from engine.parser import ExtractedQuery, QueryKind, SQLExtractor
from executor.warehouse import QueryResult, WarehouseExecutor
from network import protocol
from network.catalog import CatalogResponder
from network.protocol import PayloadReader, PGColumn, StartupKind

logger = logging.getLogger("flowproxy.network.server")

_session_counter = itertools.count(1)

# GUCs the JVM/libpq drivers expect to see echoed at session start.
_STARTUP_PARAMETERS: tuple[tuple[str, str], ...] = (
    ("server_version", "15.4"),
    ("server_encoding", "UTF8"),
    ("client_encoding", "UTF8"),
    ("application_name", "flowproxy"),
    ("DateStyle", "ISO, MDY"),
    ("TimeZone", "UTC"),
    ("integer_datetimes", "on"),
    ("standard_conforming_strings", "on"),
    ("is_superuser", "off"),
)

# SHOW <name> canned answers for driver probes.
_SHOW_VALUES: dict[str, str] = {name.lower(): value for name, value in _STARTUP_PARAMETERS} | {
    "transaction isolation level": "read committed",
    "max_identifier_length": "63",
}


@dataclass
class PreparedStatement:
    name: str
    sql: str
    param_oids: list[int] = field(default_factory=list)


@dataclass
class Portal:
    name: str
    statement: PreparedStatement
    result: QueryResult | None = None  # planned+executed lazily at Describe/Execute


@dataclass
class SessionState:
    session_id: int
    parameters: dict[str, str] = field(default_factory=dict)
    in_transaction: bool = False
    statements: dict[str, PreparedStatement] = field(default_factory=dict)
    portals: dict[str, Portal] = field(default_factory=dict)

    @property
    def txn_status(self) -> bytes:
        return b"T" if self.in_transaction else b"I"


class PostgresProxyServer:
    """Asyncio TCP server emulating a PostgreSQL backend for QuickSight."""

    def __init__(
        self,
        compiler: SemanticCompiler,
        extractor: SQLExtractor,
        executor: WarehouseExecutor,
        *,
        catalog_responder: "CatalogResponder | None" = None,
        layer_provider: "Callable[[], Any] | None" = None,
        host: str = "0.0.0.0",
        port: int = 5432,
        password: str | None = None,
    ) -> None:
        # Fixed instances (direct/dry-run mode). In store/hot-swap mode a
        # layer_provider returns the current LiveSemanticLayer per request, so
        # the proxy picks up manifest swaps without a restart (WS7c/ADR-0014).
        self._fixed_compiler = compiler
        self._fixed_extractor = extractor
        self._executor = executor
        self._fixed_catalog = catalog_responder  # None in dry-run/wire-only mode
        self._layer_provider = layer_provider
        self._host = host
        self._port = port
        self._password = password  # None => trust auth

    # ------------------------------------------------------------------ #
    # Active semantic layer — fixed instances, or the hot-swappable current one
    # ------------------------------------------------------------------ #
    @property
    def _compiler(self) -> SemanticCompiler:
        return self._layer_provider().compiler if self._layer_provider else self._fixed_compiler

    @property
    def _extractor(self) -> SQLExtractor:
        return self._layer_provider().extractor if self._layer_provider else self._fixed_extractor

    @property
    def _catalog(self) -> "CatalogResponder | None":
        return self._layer_provider().catalog_responder if self._layer_provider else self._fixed_catalog

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def serve_forever(self) -> None:
        server = await asyncio.start_server(self._handle_connection, self._host, self._port)
        sockets = ", ".join(str(s.getsockname()) for s in server.sockets)
        logger.info("flowproxy listening on %s (auth=%s)", sockets, "password" if self._password else "trust")
        async with server:
            await server.serve_forever()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = SessionState(session_id=next(_session_counter))
        peer = writer.get_extra_info("peername")
        logger.info("[s%d] connection accepted from %s", session.session_id, peer)
        try:
            if not await self._startup_phase(session, reader, writer):
                return
            await self._message_loop(session, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            logger.info("[s%d] client disconnected mid-stream", session.session_id)
        except ProtocolViolationError as exc:
            logger.warning("[s%d] protocol violation: %s", session.session_id, exc.message)
            self._safe_write(writer, protocol.error_response(exc.message, sqlstate=exc.sqlstate))
        except Exception:
            logger.exception("[s%d] unexpected session failure", session.session_id)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            logger.info("[s%d] connection closed", session.session_id)

    # ------------------------------------------------------------------ #
    # Phase 1: startup handshake
    # ------------------------------------------------------------------ #
    async def _startup_phase(
        self,
        session: SessionState,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        """Run SSL negotiation, StartupMessage, auth; end with ReadyForQuery."""
        while True:
            msg = await protocol.read_startup_message(reader)
            logger.debug("[s%d] startup packet: %s", session.session_id, msg.kind.value)

            if msg.kind in (StartupKind.SSL_REQUEST, StartupKind.GSSENC_REQUEST):
                # Decline encryption; the client retries in cleartext on the
                # same socket. (Terminate TLS at a fronting NLB/stunnel.)
                writer.write(protocol.ssl_denied())
                await writer.drain()
                continue
            if msg.kind is StartupKind.CANCEL_REQUEST:
                # Cancellation arrives on a NEW connection carrying the pid/
                # secret from BackendKeyData; acknowledge by closing silently.
                logger.info("[s%d] cancel request received; closing per protocol", session.session_id)
                return False
            break  # StartupMessage proper

        session.parameters = msg.parameters
        logger.info(
            "[s%d] startup: user=%r database=%r application=%r",
            session.session_id,
            msg.parameters.get("user"),
            msg.parameters.get("database"),
            msg.parameters.get("application_name"),
        )

        if not await self._authenticate(session, reader, writer):
            return False

        # AuthenticationOk + ParameterStatus* + BackendKeyData + ReadyForQuery.
        out = bytearray(protocol.authentication_ok())
        for name, value in _STARTUP_PARAMETERS:
            out += protocol.parameter_status(name, value)
        out += protocol.backend_key_data(pid=session.session_id, secret_key=secrets.randbits(32))
        out += protocol.ready_for_query(session.txn_status)
        writer.write(bytes(out))
        await writer.drain()
        logger.info("[s%d] handshake complete; ReadyForQuery sent", session.session_id)
        return True

    async def _authenticate(
        self,
        session: SessionState,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        if self._password is None:
            return True  # trust

        writer.write(protocol.authentication_cleartext_password())
        await writer.drain()

        frame = await protocol.read_message(reader)
        if frame is None or frame[0] != b"p":
            logger.warning("[s%d] expected PasswordMessage, got %r", session.session_id, frame and frame[0])
            return False
        supplied = frame[1].rstrip(b"\x00").decode("utf-8", "replace")
        if not secrets.compare_digest(supplied, self._password):
            self._safe_write(
                writer,
                protocol.error_response(
                    f"password authentication failed for user \"{session.parameters.get('user', '?')}\"",
                    sqlstate="28P01",
                    severity="FATAL",
                ),
            )
            logger.warning("[s%d] authentication failed", session.session_id)
            return False
        logger.info("[s%d] password authentication succeeded", session.session_id)
        return True

    # ------------------------------------------------------------------ #
    # Phase 2: steady-state message loop
    # ------------------------------------------------------------------ #
    async def _message_loop(
        self,
        session: SessionState,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            frame = await protocol.read_message(reader)
            if frame is None:
                return
            tag, payload = frame
            logger.debug("[s%d] frontend packet tag=%s len=%d", session.session_id, tag, len(payload))

            if tag == b"X":  # Terminate
                logger.info("[s%d] Terminate received", session.session_id)
                return
            if tag == b"Q":
                await self._handle_simple_query(session, writer, payload)
            elif tag == b"P":
                self._handle_parse(session, writer, payload)
            elif tag == b"B":
                self._handle_bind(session, writer, payload)
            elif tag == b"D":
                await self._handle_describe(session, writer, payload)
            elif tag == b"E":
                await self._handle_execute(session, writer, payload)
            elif tag == b"C":
                self._handle_close(session, writer, payload)
            elif tag == b"H":  # Flush
                pass
            elif tag == b"S":  # Sync — extended-protocol batch boundary
                writer.write(protocol.ready_for_query(session.txn_status))
            elif tag == b"p":  # stray PasswordMessage post-auth
                pass
            else:
                logger.warning("[s%d] unsupported frontend tag %r", session.session_id, tag)
                writer.write(
                    protocol.error_response(
                        f"unsupported frontend message {tag!r}", sqlstate="0A000"
                    )
                )
                writer.write(protocol.ready_for_query(session.txn_status))
            await writer.drain()

    # ------------------------------------------------------------------ #
    # Simple query protocol ('Q')
    # ------------------------------------------------------------------ #
    async def _handle_simple_query(
        self, session: SessionState, writer: asyncio.StreamWriter, payload: bytes
    ) -> None:
        raw = payload.rstrip(b"\x00").decode("utf-8", "replace")
        logger.info("[s%d] Q packet: %r", session.session_id, raw[:500])

        # A simple-query message may batch several ';'-separated statements.
        statements = [s for s in (part.strip() for part in raw.split(";")) if s] or [""]
        for statement in statements:
            try:
                await self._dispatch_statement(session, writer, statement)
            except FlowProxyError as exc:
                logger.warning("[s%d] query failed: %s", session.session_id, exc.message)
                writer.write(protocol.error_response(exc.message, sqlstate=exc.sqlstate, detail=exc.detail))
                break  # per protocol: abort remainder of the batch on error
            except Exception as exc:
                logger.exception("[s%d] internal error on %r", session.session_id, statement[:200])
                writer.write(protocol.error_response(f"internal error: {exc}", sqlstate="XX000"))
                break
        writer.write(protocol.ready_for_query(session.txn_status))
        await writer.drain()

    async def _dispatch_statement(
        self, session: SessionState, writer: asyncio.StreamWriter, sql: str
    ) -> None:
        kind = self._extractor.classify(sql)
        logger.debug("[s%d] classified %r as %s", session.session_id, sql[:120], kind.value)

        if kind is QueryKind.EMPTY:
            writer.write(protocol.empty_query_response())
        elif kind is QueryKind.SET:
            writer.write(protocol.command_complete("SET"))
        elif kind is QueryKind.TRANSACTION:
            verb = sql.split(None, 1)[0].upper()
            if verb in ("BEGIN", "START"):
                session.in_transaction = True
                tag = "BEGIN"
            else:
                session.in_transaction = False
                tag = "COMMIT" if verb in ("COMMIT", "END") else "ROLLBACK"
            writer.write(protocol.command_complete(tag))
        elif kind is QueryKind.SHOW:
            self._write_result(writer, self._answer_show(sql))
        elif kind is QueryKind.SCALAR:
            self._write_result(writer, self._answer_scalar(sql))
        elif kind is QueryKind.CATALOG:
            self._write_result(writer, self._answer_catalog(session, sql))
        else:
            result = await self._run_semantic_query(session, sql)
            self._write_result(writer, result)

    def _answer_catalog(self, session: SessionState, sql: str) -> QueryResult:
        """Answer an introspection probe from the virtual catalog (WS3)."""
        if self._catalog is None:
            logger.info("[s%d] catalog probe but no catalog bound; empty set", session.session_id)
            return QueryResult(columns=["name"], rows=[])
        answer = self._catalog.answer(sql)
        logger.info(
            "[s%d] catalog probe → %d rows (handled=%s)",
            session.session_id,
            answer.row_count,
            answer.handled,
        )
        return QueryResult(columns=answer.columns, rows=answer.rows)

    # ------------------------------------------------------------------ #
    # The core pipeline: raw SQL -> extraction -> MetricFlow -> warehouse
    # ------------------------------------------------------------------ #
    async def _run_semantic_query(self, session: SessionState, sql: str) -> QueryResult:
        extracted: ExtractedQuery = self._extractor.extract(sql)
        logger.info(
            "[s%d] pipeline stage 1/3 extracted: cube=%r metrics=%s dimensions=%s "
            "where=%s time=[%s,%s] order_by=%s limit=%s",
            session.session_id,
            extracted.cube,
            extracted.metrics,
            extracted.dimensions,
            extracted.where_constraints,
            extracted.time_constraint_start,
            extracted.time_constraint_end,
            extracted.order_by,
            extracted.limit,
        )

        loop = asyncio.get_running_loop()
        if self._compiler.is_dry_run:
            return await self._run_dry_run(session, extracted, loop)
        return await self._run_real(session, extracted, loop)

    async def _run_real(
        self, session: SessionState, extracted: ExtractedQuery, loop: asyncio.AbstractEventLoop
    ) -> QueryResult:
        """Real path: MetricFlow plans AND executes; re-project to client order."""
        try:
            compiled = await loop.run_in_executor(
                None,
                lambda: self._compiler.execute_request(
                    extracted.metrics,
                    extracted.dimensions,
                    where_constraints=extracted.where_constraints or None,
                    time_constraint_start=extracted.time_constraint_start,
                    time_constraint_end=extracted.time_constraint_end,
                    order_by=extracted.order_by or None,
                    limit=extracted.limit,
                ),
            )
        except FlowProxyError:
            raise
        except Exception as exc:
            raise WarehouseExecutionError(
                f"warehouse execution failed: {exc}",
                detail="Check warehouse connectivity and credentials.",
            ) from exc

        logger.info(
            "[s%d] pipeline stage 2+3/3 MetricFlow planned+executed: %d rows",
            session.session_id,
            compiled.row_count,
        )
        return self._reproject(compiled.columns, compiled.rows, extracted.projection)

    async def _run_dry_run(
        self, session: SessionState, extracted: ExtractedQuery, loop: asyncio.AbstractEventLoop
    ) -> QueryResult:
        """Dry-run path: stubbed SQL + mock executor (wire-path testing)."""
        warehouse_sql: str = await loop.run_in_executor(
            None, self._compiler.compile_request, extracted.metrics, extracted.dimensions
        )
        logger.info(
            "[s%d] pipeline stage 2/3 dry-run plan ready (%d chars)",
            session.session_id,
            len(warehouse_sql),
        )
        result = await self._executor.execute(
            warehouse_sql, extracted.projection, extracted.metrics
        )
        logger.info(
            "[s%d] pipeline stage 3/3 mock executor returned %d rows",
            session.session_id,
            result.row_count,
        )
        return result

    @staticmethod
    def _reproject(
        columns: list[str], rows: list[tuple[Any, ...]], desired: list[str]
    ) -> QueryResult:
        """Reorder MetricFlow's output columns to the client's SELECT order.

        MetricFlow emits columns as (dimensions, metrics) and may lower-case or
        grain-suffix names; map by case-insensitive suffix match to the client's
        requested projection, so QuickSight sees exactly the columns it asked
        for, in order. Unmatched desired columns are filled from position.
        """
        norm = {c.lower(): i for i, c in enumerate(columns)}

        def find(name: str) -> int | None:
            n = name.lower()
            if n in norm:
                return norm[n]
            # MetricFlow may return metric_time as metric_time__month etc.
            for col_lower, idx in norm.items():
                if col_lower == n or col_lower.startswith(n + "__") or col_lower.endswith("__" + n):
                    return idx
            return None

        order: list[int] = []
        used: set[int] = set()
        for want in desired:
            idx = find(want)
            if idx is not None and idx not in used:
                order.append(idx)
                used.add(idx)
        # Append any columns MetricFlow returned that weren't matched (safety).
        for i in range(len(columns)):
            if i not in used:
                order.append(i)

        out_cols = [columns[i] for i in order]
        out_rows = [tuple(row[i] for i in order) for row in rows]
        return QueryResult(columns=out_cols, rows=out_rows)

    # ------------------------------------------------------------------ #
    # Extended query protocol ('P','B','D','E','C') — used by JDBC/ODBC
    # ------------------------------------------------------------------ #
    def _handle_parse(self, session: SessionState, writer: asyncio.StreamWriter, payload: bytes) -> None:
        buf = PayloadReader(payload)
        name = buf.cstring()
        sql = buf.cstring()
        n_params = buf.int16()
        oids = [buf.int32() for _ in range(n_params)]
        session.statements[name] = PreparedStatement(name=name, sql=sql, param_oids=oids)
        logger.info("[s%d] Parse stmt=%r sql=%r", session.session_id, name or "<unnamed>", sql[:300])
        writer.write(protocol.parse_complete())

    def _handle_bind(self, session: SessionState, writer: asyncio.StreamWriter, payload: bytes) -> None:
        buf = PayloadReader(payload)
        portal_name = buf.cstring()
        stmt_name = buf.cstring()
        n_formats = buf.int16()
        for _ in range(n_formats):
            buf.int16()
        n_params = buf.int16()

        statement = session.statements.get(stmt_name)
        if statement is None:
            writer.write(
                protocol.error_response(f'prepared statement "{stmt_name}" does not exist', sqlstate="26000")
            )
            return
        if n_params > 0:
            # Semantic-layer queries are shaped by column *names*; bound
            # parameters (WHERE $1) have no MetricFlow mapping yet.
            writer.write(
                protocol.error_response(
                    "bound parameters are not supported by the semantic proxy",
                    sqlstate="0A000",
                    detail="Inline literal filters or query the cube without parameters.",
                )
            )
            return
        session.portals[portal_name] = Portal(name=portal_name, statement=statement)
        logger.debug("[s%d] Bind portal=%r -> stmt=%r", session.session_id, portal_name, stmt_name)
        writer.write(protocol.bind_complete())

    async def _handle_describe(self, session: SessionState, writer: asyncio.StreamWriter, payload: bytes) -> None:
        buf = PayloadReader(payload)
        target_kind = buf.read(1)
        name = buf.cstring()

        if target_kind == b"S":
            statement = session.statements.get(name)
            if statement is None:
                writer.write(protocol.error_response(f'prepared statement "{name}" does not exist', sqlstate="26000"))
                return
            # ParameterDescription (no params) + row shape.
            writer.write(protocol.message(b"t", (0).to_bytes(2, "big")))
            portal = Portal(name="", statement=statement)
        else:
            maybe_portal = session.portals.get(name)
            if maybe_portal is None:
                writer.write(protocol.error_response(f'portal "{name}" does not exist', sqlstate="34000"))
                return
            portal = maybe_portal

        # Row-returning kinds get a RowDescription; everything else NoData.
        # CATALOG is included: JDBC drivers (QuickSight) issue catalog probes
        # via the extended protocol and Describe them before Execute - omitting
        # CATALOG here made the driver see NoData and read zero rows.
        kind = self._extractor.classify(portal.statement.sql)
        if kind not in (QueryKind.DATA, QueryKind.SHOW, QueryKind.SCALAR, QueryKind.CATALOG):
            writer.write(protocol.no_data())
            return

        try:
            result = await self._materialize_portal(session, portal, kind)
        except FlowProxyError as exc:
            writer.write(protocol.error_response(exc.message, sqlstate=exc.sqlstate, detail=exc.detail))
            return
        writer.write(protocol.row_description(self._describe_columns(result)))

    async def _handle_execute(self, session: SessionState, writer: asyncio.StreamWriter, payload: bytes) -> None:
        buf = PayloadReader(payload)
        name = buf.cstring()
        _max_rows = buf.int32()  # 0 = unlimited; row paging not implemented

        portal = session.portals.get(name)
        if portal is None:
            writer.write(protocol.error_response(f'portal "{name}" does not exist', sqlstate="34000"))
            return

        kind = self._extractor.classify(portal.statement.sql)
        try:
            if kind in (QueryKind.DATA, QueryKind.SHOW, QueryKind.SCALAR, QueryKind.CATALOG):
                result = await self._materialize_portal(session, portal, kind)
                for row in result.rows:
                    writer.write(protocol.data_row(row))
                writer.write(protocol.command_complete(f"SELECT {result.row_count}"))
            elif kind is QueryKind.SET:
                writer.write(protocol.command_complete("SET"))
            elif kind is QueryKind.EMPTY:
                writer.write(protocol.empty_query_response())
            else:
                writer.write(protocol.command_complete("SELECT 0"))
        except FlowProxyError as exc:
            writer.write(protocol.error_response(exc.message, sqlstate=exc.sqlstate, detail=exc.detail))

    async def _materialize_portal(
        self, session: SessionState, portal: Portal, kind: QueryKind
    ) -> QueryResult:
        """Plan+execute once per portal; Describe and Execute share the result."""
        if portal.result is None:
            if kind is QueryKind.DATA:
                portal.result = await self._run_semantic_query(session, portal.statement.sql)
            elif kind is QueryKind.SHOW:
                portal.result = self._answer_show(portal.statement.sql)
            elif kind is QueryKind.CATALOG:
                portal.result = self._answer_catalog(session, portal.statement.sql)
            else:
                portal.result = self._answer_scalar(portal.statement.sql)
        return portal.result

    def _handle_close(self, session: SessionState, writer: asyncio.StreamWriter, payload: bytes) -> None:
        buf = PayloadReader(payload)
        target_kind = buf.read(1)
        name = buf.cstring()
        if target_kind == b"S":
            session.statements.pop(name, None)
        else:
            session.portals.pop(name, None)
        writer.write(protocol.close_complete())

    # ------------------------------------------------------------------ #
    # Canned responses for driver probes
    # ------------------------------------------------------------------ #
    @staticmethod
    def _answer_show(sql: str) -> QueryResult:
        target = sql.strip().rstrip(";").split(None, 1)[1].lower() if " " in sql else ""
        value = _SHOW_VALUES.get(target, "unset")
        return QueryResult(columns=[target.replace(" ", "_") or "setting"], rows=[(value,)])

    @staticmethod
    def _answer_scalar(sql: str) -> QueryResult:
        lowered = sql.lower()
        if "version()" in lowered:
            return QueryResult(
                columns=["version"],
                rows=[("PostgreSQL 15.4 (FlowProxy semantic layer over dbt MetricFlow)",)],
            )
        if "current_schema" in lowered:
            return QueryResult(columns=["current_schema"], rows=[("public",)])
        if "current_database" in lowered:
            return QueryResult(columns=["current_database"], rows=[("flowproxy",)])
        if "current_user" in lowered or "session_user" in lowered:
            return QueryResult(columns=["current_user"], rows=[("flowproxy",)])
        return QueryResult(columns=["?column?"], rows=[(1,)])

    # ------------------------------------------------------------------ #
    # Result serialization: 'T' + 'D'* + 'C'
    # ------------------------------------------------------------------ #
    @staticmethod
    def _describe_columns(result: QueryResult) -> list[PGColumn]:
        sample = result.rows[0] if result.rows else [None] * len(result.columns)
        return [protocol.infer_column(name, sample[i]) for i, name in enumerate(result.columns)]

    def _write_result(self, writer: asyncio.StreamWriter, result: QueryResult) -> None:
        writer.write(protocol.row_description(self._describe_columns(result)))
        for row in result.rows:
            writer.write(protocol.data_row(row))
        writer.write(protocol.command_complete(f"SELECT {result.row_count}"))
        logger.debug("serialized result: %d rows, %d columns", result.row_count, len(result.columns))

    @staticmethod
    def _safe_write(writer: asyncio.StreamWriter, data: bytes) -> None:
        try:
            writer.write(data)
        except (ConnectionResetError, BrokenPipeError):
            pass


def default_port() -> int:
    return int(os.environ.get("FLOWPROXY_PORT", "5432"))

"""Warehouse execution layer.

``WarehouseExecutor`` is the seam where MetricFlow's compiled SQL meets the
actual warehouse. Production deployments implement it over the async driver
for their engine (``snowflake-connector-python`` in a thread pool executor,
``asyncpg``/``redshift_connector``, etc.). ``MockWarehouseExecutor`` is the
placeholder stub used to validate the full QuickSight wire path end-to-end
without warehouse credentials — it returns deterministic synthetic rows.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import zlib
from dataclasses import dataclass
from typing import Any, Sequence

logger = logging.getLogger("flowproxy.executor")


@dataclass(frozen=True)
class QueryResult:
    """Column-ordered result set handed back to the wire serializer."""

    columns: list[str]
    rows: list[tuple[Any, ...]]

    @property
    def row_count(self) -> int:
        return len(self.rows)


class WarehouseExecutor(abc.ABC):
    """Executes compiled warehouse SQL and returns rows."""

    @abc.abstractmethod
    async def execute(self, sql: str, projection: Sequence[str], metric_names: Sequence[str]) -> QueryResult:
        """Run ``sql`` against the warehouse.

        Args:
            sql:          The MetricFlow-compiled warehouse SQL.
            projection:   Output column names in client SELECT order.
            metric_names: Which of those columns are metrics (numeric).
        """


class MockWarehouseExecutor(WarehouseExecutor):
    """Deterministic stand-in for a real warehouse connection.

    Dimension columns get labeled string values; metric columns get stable
    pseudo-random floats derived from CRC32 of the column name, so repeated
    queries return identical data (important when validating QuickSight
    caching behavior).
    """

    def __init__(self, row_count: int = 5, latency_seconds: float = 0.05) -> None:
        self._row_count: int = row_count
        self._latency: float = latency_seconds

    async def execute(self, sql: str, projection: Sequence[str], metric_names: Sequence[str]) -> QueryResult:
        logger.info("mock executor: simulating warehouse round-trip (%d chars of SQL)", len(sql))
        logger.debug("mock executor: would execute:\n%s", sql)
        await asyncio.sleep(self._latency)  # simulate network + queue latency

        metric_set = set(metric_names)
        rows: list[tuple[Any, ...]] = []
        for i in range(self._row_count):
            row: list[Any] = []
            for column in projection:
                if column in metric_set:
                    seed = zlib.crc32(column.encode("utf-8"))
                    row.append(round((seed % 10_000) / 100 + i * 1.75, 2))
                else:
                    row.append(f"{column.rsplit('__', 1)[-1]}_{i + 1}")
            rows.append(tuple(row))

        result = QueryResult(columns=list(projection), rows=rows)
        logger.info("mock executor: returning %d rows x %d cols", result.row_count, len(result.columns))
        return result

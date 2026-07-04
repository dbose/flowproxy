"""Exception hierarchy for the FlowProxy semantic layer.

Every exception carries a PostgreSQL SQLSTATE code so the network layer can
translate failures into wire-native ``ErrorResponse`` packets that BI drivers
(QuickSight's JDBC/ODBC PostgreSQL connectors) render cleanly instead of
dropping the connection.
"""

from __future__ import annotations


class FlowProxyError(Exception):
    """Base class for all FlowProxy failures.

    Attributes:
        sqlstate: Five-character PostgreSQL error code surfaced to the client.
        detail:   Optional human-readable remediation hint.
    """

    sqlstate: str = "XX000"  # internal_error

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message: str = message
        self.detail: str | None = detail


class ManifestNotFoundError(FlowProxyError):
    """``target/semantic_manifest.json`` is absent or unreadable."""

    sqlstate = "58P01"  # undefined_file


class ManifestInvalidError(FlowProxyError):
    """The manifest exists but failed dbt-semantic-interfaces validation."""

    sqlstate = "XX001"  # data_corrupted


class CompilerBootstrapError(FlowProxyError):
    """MetricFlow / dbt adapter machinery could not be initialized."""

    sqlstate = "58000"  # system_error


class UnknownFieldError(FlowProxyError):
    """A requested column maps to neither a metric nor a dimension."""

    sqlstate = "42703"  # undefined_column


class UnknownCubeError(FlowProxyError):
    """The FROM-clause target is not a known virtual cube."""

    sqlstate = "42P01"  # undefined_table


class UnsupportedQueryError(FlowProxyError):
    """The SQL shape cannot be mapped onto a MetricFlow request."""

    sqlstate = "0A000"  # feature_not_supported


class QueryPlanningError(FlowProxyError):
    """MetricFlow accepted the request but could not plan it.

    Typically a broken join path: the requested dimensions are not reachable
    from the semantic models that define the requested metrics.
    """

    sqlstate = "42P17"  # invalid_object_definition


class WarehouseExecutionError(FlowProxyError):
    """The compiled SQL failed downstream in the warehouse."""

    sqlstate = "08006"  # connection_failure


class ProtocolViolationError(FlowProxyError):
    """The client sent bytes that violate the PostgreSQL wire protocol."""

    sqlstate = "08P01"  # protocol_violation

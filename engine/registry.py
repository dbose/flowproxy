"""Semantic name registry derived from the dbt semantic manifest.

The registry is the single source of truth used to classify incoming SQL
column tokens as MetricFlow *metrics* or *dimensions*, and to expand bare
dimension names (``department``) into MetricFlow's entity-qualified form
(``employee__department``) that the query planner requires.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger("flowproxy.engine.registry")

# Time grains MetricFlow accepts as a ``__<grain>`` suffix on time dimensions.
TIME_GRAINS: frozenset[str] = frozenset(
    {"nanosecond", "microsecond", "millisecond", "second", "minute", "hour", "day", "week", "month", "quarter", "year"}
)

# Always-available virtual time dimension in MetricFlow.
METRIC_TIME: str = "metric_time"


@dataclass(frozen=True)
class SemanticRegistry:
    """Immutable lookup tables built once at boot from the semantic manifest."""

    metrics: frozenset[str]
    qualified_dimensions: frozenset[str]
    bare_to_qualified: Mapping[str, str] = field(default_factory=dict)
    entities: frozenset[str] = frozenset()
    time_dimensions: frozenset[str] = frozenset()  # qualified names that are TIME type

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_manifest(cls, manifest: Any) -> "SemanticRegistry":
        """Build the registry from a ``PydanticSemanticManifest``.

        ``manifest`` is typed ``Any`` deliberately: dbt-semantic-interfaces
        moves its pydantic classes between minor versions, and we only rely
        on the stable attribute surface (``metrics``, ``semantic_models``).
        """
        metric_names: set[str] = {m.name for m in manifest.metrics}

        qualified: set[str] = {METRIC_TIME}
        bare_map: dict[str, str] = {}
        entity_names: set[str] = set()
        time_dims: set[str] = {METRIC_TIME}

        for model in manifest.semantic_models:
            # A dimension is qualified by the model's PRIMARY entity. That can
            # be declared either as an entity of type PRIMARY, or via the
            # model-level `primary_entity` key (used when the grain has no
            # single unique entity, e.g. a fact at account×day grain).
            primary_entity: str | None = getattr(model, "primary_entity", None)
            for entity in model.entities:
                entity_names.add(entity.name)
                if str(getattr(entity.type, "value", entity.type)).lower() == "primary":
                    primary_entity = entity.name

            for dim in model.dimensions:
                qualified_name = f"{primary_entity}__{dim.name}" if primary_entity else dim.name
                qualified.add(qualified_name)
                if str(getattr(dim.type, "value", dim.type)).lower() == "time":
                    time_dims.add(qualified_name)
                previous = bare_map.setdefault(dim.name, qualified_name)
                if previous != qualified_name:
                    logger.warning(
                        "dimension name collision: %r maps to both %r and %r; "
                        "clients must use the qualified form",
                        dim.name,
                        previous,
                        qualified_name,
                    )

        logger.info(
            "semantic registry built: %d metrics, %d dimensions (%d time), %d entities",
            len(metric_names),
            len(qualified),
            len(time_dims),
            len(entity_names),
        )
        return cls(
            metrics=frozenset(metric_names),
            qualified_dimensions=frozenset(qualified),
            bare_to_qualified=dict(bare_map),
            entities=frozenset(entity_names),
            time_dimensions=frozenset(time_dims),
        )

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #
    def is_metric(self, name: str) -> bool:
        return name in self.metrics

    def is_time_dimension(self, qualified_name: str) -> bool:
        """True if the (grain-free) qualified name is a TIME dimension."""
        base, _ = self._split_grain(qualified_name)
        return base in self.time_dimensions

    def resolve_dimension(self, name: str) -> str | None:
        """Resolve a column token to a MetricFlow group-by name.

        Accepts:
          * qualified names        -> ``employee__department``
          * bare names             -> ``department``
          * grain-suffixed names   -> ``metric_time__month`` / ``hired_at__year``

        Returns the canonical qualified name (grain suffix preserved), or
        ``None`` when the token is not a known dimension.
        """
        base, grain = self._split_grain(name)

        if base in self.qualified_dimensions:
            return name if grain is None else f"{base}__{grain}"
        if base in self.bare_to_qualified:
            resolved = self.bare_to_qualified[base]
            return resolved if grain is None else f"{resolved}__{grain}"
        return None

    def suggestions(self, name: str, limit: int = 3) -> list[str]:
        """Closest known field names — surfaced in error packets to the BI user."""
        universe = list(self.metrics | self.qualified_dimensions | set(self.bare_to_qualified))
        return difflib.get_close_matches(name, universe, n=limit, cutoff=0.5)

    @staticmethod
    def _split_grain(name: str) -> tuple[str, str | None]:
        head, sep, tail = name.rpartition("__")
        if sep and tail.lower() in TIME_GRAINS:
            return head, tail.lower()
        return name, None

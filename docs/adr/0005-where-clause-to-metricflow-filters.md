# ADR-0005: WHERE/ORDER/LIMIT translation into MetricFlow constructs

Date: 2026-07-04 · Status: **Accepted**

## Context

Dashboard interactions (date pickers, category filters, top-N sorts) arrive
as SQL `WHERE` / `ORDER BY` / `LIMIT` clauses. Today the extractor rejects
them. Filters must not weaken guardrails: a predicate spliced as text into
warehouse SQL would create an injection channel and could bypass MetricFlow's
semantics (e.g. filtering inside a semi-additive window incorrectly).

## Decision

1. sqlglot-parsed predicates are translated into **MetricFlow query-request
   parameters, never into SQL strings**:
   - `metric_time`/time-dimension range predicates (`>=`, `<`, `BETWEEN`)
     → `time_constraint_start` / `time_constraint_end`;
   - categorical predicates (`=`, `IN`, `<>`, `LIKE`) on known dimensions
     → MetricFlow `where` filters referencing **registry-resolved qualified
     names** via the `{{ Dimension('entity__name') }}` templating MetricFlow
     defines; literal values are passed through MetricFlow's own quoting,
     with proxy-side validation (type-checked against dimension type,
     length-capped).
2. `ORDER BY` maps to MetricFlow `order_by_names` (ordinals resolved against
   the projection); `LIMIT n` maps to `limit`, additionally clamped by a
   server-side `FLOWPROXY_MAX_ROWS`.
3. **Deny-by-default**: any predicate shape outside the supported grammar
   (subqueries, OR-trees over mixed dimensions, functions on columns other
   than `DATE_TRUNC`/casts) is rejected with SQLSTATE `0A000` and a message
   naming the unsupported construct — never silently dropped, which would
   return over-broad data (a confidentiality failure in a bank).

## Consequences

- Extraction output grows: `(metrics, dimensions, filters, order_by, limit)`.
- L3 golden tests must cover: date-range on the semi-additive metric
  (window applies *within* the constrained range), IN-list filters, and the
  rejected-construct error path.
- Enforced row-level filters from AuthZ (ADR-0006) merge into the same
  MetricFlow `where` list — one code path, uniformly audited.

## Alternatives considered

- **Predicate passthrough into compiled SQL**: injection surface + guardrail
  bypass; rejected outright.
- **Silently ignoring unsupported predicates**: returns more data than the
  analyst asked for; unacceptable.

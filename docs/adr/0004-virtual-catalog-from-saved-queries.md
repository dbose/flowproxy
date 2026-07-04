# ADR-0004: Virtual catalog — saved queries as discoverable tables

Date: 2026-07-04 · Status: **Accepted — exposure policy superseded by
[ADR-0010](0010-full-semantic-exposure-catalog.md)** (decisions 1–2 below;
the catalog *mechanics* in decisions 3–4 remain in force)

## Context

Analysts browse tables in a UI; they do not hand-write SQL against an empty
pane. QuickSight (JDBC `DatabaseMetaData`) and Power BI (Npgsql) discover
schema through `pg_catalog` / `information_schema` queries. Today FlowProxy
answers those with empty sets → both tools show a blank datasource. Npgsql is
the stricter client: at connect it runs a large composite bootstrap query
against `pg_type` and expects coherent OIDs across `pg_class`,
`pg_attribute`, and `pg_namespace`.

## Decision

1. **MetricFlow `saved_query` objects are the primary catalog unit**: each
   saved query in the manifest becomes one virtual table in schema
   `semantic_layer` (columns = its metrics + group-bys, typed from manifest
   metadata). Saved queries are the OSS analog of dbt Cloud "exports" —
   curated, governed, reviewed in git.
2. Optionally (env flag, default off) one **auto-cube per semantic model**
   exposing its metrics + reachable dimensions, for exploratory use.
3. A dedicated `network/catalog.py` module answers the introspection dialect:
   `pg_type` bootstrap (Npgsql), `pg_class`/`pg_attribute`/`pg_namespace`,
   `information_schema.tables`/`columns`, and JDBC `getTables`/`getColumns`
   shapes — driven by L4 captured corpora (ADR-0003).
4. Synthetic OIDs are **deterministic per manifest** (derived from
   `hash(manifest_sha, object_name)`), so reconnecting clients and pooled
   connections never see OID drift within a deployed manifest version.

## Consequences

- The fixture project must contain at least one saved query (ADR-0003).
- Catalog answers are pure functions of the loaded manifest → hot-swapping a
  manifest (CI deploy, Phase 1 WS7) atomically updates the catalog.
- We deliberately do NOT emulate the entire pg_catalog surface; anything
  outside the captured-corpus contract returns empty sets with a WARN log,
  and the corpus grows as new client versions are qualified.

## Alternatives considered

- **One giant table of all metrics × all dimensions**: invalid dimension
  combinations become discoverable-and-broken; saved queries keep the surface
  curated and always-plannable (enforced by the CI validation gate).
- **Static handwritten catalog config**: drifts from the YAML; rejected —
  the manifest is the single source of truth.

# ADR-0001: PostgreSQL wire protocol as the primary BI interface

Date: 2026-07-04 · Status: **Accepted** (records the founding decision)

## Context

Target consumers are Amazon QuickSight and Power BI, operated by analysts in an
air-gapped bank. Neither supports the dbt Semantic Layer natively outside
dbt Cloud. Both ship first-class PostgreSQL connectors (QuickSight: JDBC;
Power BI: Npgsql). Installing custom drivers on locked-down analyst
workstations or managed BI services is organizationally expensive-to-impossible.

## Decision

FlowProxy emulates a PostgreSQL 15 backend over the v3 wire protocol —
both the simple ('Q') and extended ('P'/'B'/'D'/'E'/'S') query flows — and
presents semantic-layer objects as ordinary tables. No client-side software
beyond the stock PostgreSQL connector is ever required.

## Consequences

- We inherit the obligation to emulate catalog introspection
  (`pg_catalog`, `information_schema`) faithfully enough for Npgsql and JDBC
  metadata discovery (ADR-0004).
- The wire parser consumes untrusted length-prefixed input at the network
  perimeter; lengths are bounded and the parser must be fuzz-tested (ADR-0009).
- In-protocol TLS is declined ('N'); TLS terminates at a fronting NLB/stunnel.
  SCRAM protects credentials even on the plaintext leg (ADR-0006).

## Alternatives considered

- **Arrow Flight SQL** (what dbt Cloud SL JDBC uses): no native QuickSight or
  Power BI support without custom connectors → fails the zero-client-install
  constraint. Retained as a possible Phase 2+ programmatic endpoint.
- **Custom ODBC driver**: driver distribution/patching across a bank's
  estate is a program of work in itself.
- **REST/GraphQL API**: QuickSight cannot consume it as a SQL datasource.

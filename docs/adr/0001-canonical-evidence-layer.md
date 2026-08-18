# ADR 0001: Auremgrid Owns the Canonical Evidence Layer

Status: Accepted

## Context

Several open-source systems can parse documents, build temporal graphs, or provide memory. Combining them directly would create overlapping truth stores and unclear security boundaries.

## Decision

Auremgrid owns the canonical workspace, permission, provenance, temporal, and audit contracts. External systems integrate as replaceable adapters behind those contracts.

## Consequences

- Security and citations can be tested without external services.
- Adapter output must be rechecked against Auremgrid source permissions.
- The initial implementation can be fully local and deterministic.

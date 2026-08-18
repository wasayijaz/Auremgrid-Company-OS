# ADR 0002: Cosmo Operating Contracts Sit Above the Evidence Layer

Status: accepted

## Context

A client brain that only stores documents still leaves Cosmo's real failures unsolved: uncaptured intake, abandoned review, missing decision-makers, and silent accounts.

## Decision

Keep the evidence layer as the source of cited truth, then add first-class Cosmo contracts for work items, Definition of Done, playbooks, client brains, status posts, and touchpoints. Retrieval answers questions. The operating layer makes work move.

## Consequences

- Agents can ask for a client brief instead of assembling context by hand.
- Work cannot skip intake or review rules.
- Future connectors write into these contracts instead of inventing a second workflow model.
- No private Cosmo vault data is required to exercise the system.

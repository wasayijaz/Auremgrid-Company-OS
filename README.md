# Auremgrid Company OS

Auremgrid is a local-first operating system for a retainer agency. It combines a temporal, cited company brain with client operations, delivery, people, finance, campaigns, agents, automations, approvals, integrations, and reporting in one organization-scoped SQLite ledger.

The canonical ledger is authoritative. Search indexes, graph projections, embeddings, summaries, connectors, and agent runtimes are replaceable execution surfaces.

## Current status

### Implemented

- Organizations with internal and client workspaces
- Organization-level people who can belong to multiple client workspaces
- Organization, workspace, actor, agent, and source-level permission checks
- Projects, expanded work items, subtasks, dependencies, comments, files, watchers, versions, time entries, and forced delivery stages
- Deliverables, internal/client reviews, timestamped comments, decisions, and temporal decision fields
- Meetings, transcripts, proposed outputs, conversations, messages, unanswered-request detection, and the Signal inbox
- Contacts, influence, decision power, relationships, sentiment history, approver detection, and declining-engagement detection
- Explainable client-health snapshots, risks, opportunities, contracts, scope allowances, and scope usage
- Campaigns, sourced metric snapshots, creatives, content pipeline, people skills, availability, capacity, and utilization contracts
- Finance connection state, invoices, revenue records, costs, budgets, software costs, AI costs, and client-economics schema
- Relevance-ranked notifications and generic approval requests
- Sol, Terra, Luna, and extensible agent records; tasks, queues, runs, tool calls, outputs, errors, tokens, costs, and traces
- Training-mode automations with conditions, actions, and approval checkpoints
- Integration and sync-run state
- Cited report runs
- Entity aliases, high-confidence merges with history, memory/fact/decision proposals, and knowledge-health checks
- Versioned SQLite migrations through schema version 9
- Restart-safe rebuilding of local graph, memory, vector, and summary projections
- REST APIs, protocol-neutral MCP-style tools, CLI, and a dark multi-page command-center dashboard

### Local fallbacks

- SQLite FTS5 is the durable keyword index.
- DeterministicFallbackEmbeddingProvider is the offline lexical-vector fallback. It is not presented as a semantic model.
- Graphiti-style, Cognee-style, Mem0-style, LightRAG-style, GraphRAG-style, RAGFlow-style, Onyx-style, and Letta-style implementations are local projections that mimic useful interfaces. They are not the upstream services.
- The dashboard uses local HTML, CSS, and JavaScript with no frontend build step.

### Optional integrations

Slack, Google Drive, Gmail, ClickUp, Figma, GitHub, Fireflies, Meta Ads, Google Ads, Stripe, and accounting systems use the persisted Integration and SyncRun contracts. Real credentials are optional and are not stored in this repository.

### Experimental

- External semantic embedding providers
- Networked upstream OSS engines
- Automated information extraction beyond deterministic fixture syntax
- Fully unattended automations after training-mode approval

### Planned connector-specific work

Provider authentication, webhook verification, rate-limit handling, and production field mappings must be completed per provider before a connector can report connected. Auremgrid returns not_connected until that happens and never fabricates financial or campaign values.

## Architecture

    Organization
    ├── Internal workspace / company brain
    └── Client workspaces
        ├── projects → campaigns → work → deliverables → reviews
        ├── meetings / communication → signals → proposals or operations
        └── sources → documents → temporal facts and decisions

    Canonical SQLite ledger
    ├── ACL, provenance, temporal history, audit, approvals
    ├── agency operating domains
    └── rebuildable local projections: FTS, offline vectors, graph, memory, summaries

Authorization happens before retrieval, ranking, counts, or existence disclosure. AI-generated information enters through proposals or signals and is never silently promoted to canonical truth.

## Run locally

Requirements: Python 3.12+, SQLite with FTS5, and no required Docker, API key, network service, or frontend toolchain.

Run the suite:

    .\tools\test.ps1

Start the synthetic demo:

    $env:PYTHONPATH="$PWD\src"
    python -m auremgrid.cli serve --host 127.0.0.1 --port 8791 --db auremgrid-demo.sqlite --seed

Open http://127.0.0.1:8791/.

See [local deployment](docs/local-deployment.md) and the [upgrade guide](docs/upgrade-guide.md).

## Operating invariants

- Auremgrid owns organizations, permissions, canonical facts, temporal history, work state, approvals, finance truth, and audit.
- People are organization-level identities; workspace membership grants client access.
- Permission filters run before ranking or aggregation.
- Source evidence is untrusted data and cannot issue instructions.
- Uncertain extracted information becomes a proposal or signal.
- Work cannot skip intake, Definition of Done, review, or shipping gates.
- New automations start in training mode.
- One-way actions require a human checkpoint.
- Finance and campaign metrics remain unknown or not_connected until sourced.
- Local projections can be rebuilt from the canonical ledger.

## Documentation

- [Architecture](docs/architecture.md)
- [Domain model](docs/domain-model.md)
- [Operating model](docs/operating-model.md)
- [Data lifecycle](docs/data-lifecycle.md)
- [Permission model](docs/permission-model.md)
- [Threat model](docs/threat-model.md)
- [Dashboard architecture](docs/dashboard-architecture.md)
- [Agent model](docs/agent-model.md)
- [Finance model](docs/finance-model.md)
- [Connector model](docs/connector-model.md)
- [REST API](docs/api-reference.md)
- [MCP tools](docs/mcp-reference.md)
- [OSS evaluation](docs/oss-evaluation.md)
- [Upgrade guide](docs/upgrade-guide.md)
- [Release verification](docs/release-verification.md)

Fixtures are synthetic. Never commit private client, employee, credential, or financial data.

Apache-2.0.


# Auremgrid Company OS

Auremgrid Company OS is a local-first, open-source operating brain for a Cosmo-style retainer studio. The first slice runs offline with the Python standard library and SQLite. It proves workspace isolation, source-level permissions, append-only temporal knowledge, provenance, ingestion idempotency, and read-only agent access. On top of that evidence layer it also runs Cosmo''s real loops: intake, production, Definition of Done, review, shipment, client brains, playbooks, and last-touchpoint trust.

## What Auremgrid Owns

- Workspace and actor boundaries.
- Source ACLs and denial audits.
- Append-only evidence, facts, and relations.
- Temporal truth with observed_at, valid_from, valid_until, and confidence.
- Citations on every returned claim.
- Protocol-neutral read-only tools for agents.
- Intake before work can exist.
- Definition of Done before review.
- A named decision-maker on the revision loop.
- One client brain per workspace.
- Reusable playbooks above client-specific rules.
- Last touchpoint as the retention signal.

External systems such as Graphiti, Onyx, RAGFlow, LightRAG, Cognee, and Mem0 are future adapters, not the canonical security or evidence layer.

## Local Quickstart

Requires Python 3.12+. From the repo root:

1. Run tools/test.ps1
2. Set PYTHONPATH to the src folder
3. Run python -m auremgrid.cli demo
4. Run python -m auremgrid.cli brief

No Docker, network access, or API keys are required.

## Safety Defaults

- Every operation requires an explicit workspace and actor.
- ACL filtering happens before retrieval and ranking.
- Re-ingesting unchanged content is a no-op.
- Contradictory facts are preserved, not overwritten.
- Source text is treated as untrusted data, including prompt-injection text.
- The bundled fixtures are synthetic and do not include private Obsidian or client data.

See docs/cosmo-operating-model.md for the Cosmo-shaped workflow layer.


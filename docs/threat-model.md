# Threat Model

Organization and workspace inference leakage is a security boundary: permissions run before lookup, score, count, aggregation, and error detail. Agent tools and workspaces are allow-listed. One-way automation actions and sensitive writes require approval. Connector secrets are never stored in source evidence or fixtures.

## Protected Assets

- Client-specific documents, facts, relations, and memories.
- Source permissions and audit history.
- Private connector material such as Obsidian vault content.

## Main Risks

- Cross-workspace leakage.
- Retrieval ranking before permission filtering.
- Prompt injection in source documents influencing agent behavior.
- Silent overwrite of earlier facts.
- Citation-free answers that cannot be audited.

## Controls

- Workspace id is part of every repository query.
- Source grants are checked before retrieval candidates are built.
- Source text is stored and returned only as evidence, never executed as instruction.
- Facts are append-only observations.
- Denied reads and writes create audit events.

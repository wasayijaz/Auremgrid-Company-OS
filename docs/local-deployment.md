# Local deployment

Use Python 3.12 or later. The default path needs no third-party package, network service, Docker runtime, API key, or frontend build.

Run tests with `.\tools\test.ps1`. For a new agency, activate the database first:

```text
python scripts/auremgrid.py setup-agency --db C:\data\agency.sqlite --agency "Agency Name" --admin-name "Agency Owner" --admin-email owner@agency.example
python scripts/auremgrid.py serve --host 127.0.0.1 --port 8791 --db C:\data\agency.sqlite
```

Copy the one-time `session.token` from the setup receipt, open
`http://127.0.0.1:8791/`, and paste it into **Connect to Auremgrid**.
Use `bootstrap-auth` only for an existing owner/person and actor binding. JSON
endpoints do not trust query/body person or actor identifiers.

The dashboard keeps the supplied token in browser `localStorage`. It is a
temporary login credential, not an AI key or shared agency password. Use one
session per person on a trusted browser profile; never paste it into chat,
screenshots, URLs, tickets, or source control. Use **Sign out** to forget it on
the current browser. An expired or revoked token must be replaced by an
administrator-issued session.

For a real-agency private deployment, bind the server to a private interface
only after firewall review. Before any public exposure, require HTTPS, a trusted
reverse proxy, durable backups and restore rehearsals, a secret manager,
per-person provisioning, and documented revocation. Public multi-tenant login
requires a dedicated identity provider; do not expose the local setup command
or create an unauthenticated token-minting HTTP route.

Keep the SQLite database on durable storage. Use the online `backup` and
`verify-backup` commands before upgrades; do not copy a live WAL file. Run
durable jobs with a separate `worker-once` process. Secrets belong in an
external environment or secret manager and enter SQLite only as references and
fingerprints, never values.

## Optional local semantic model

The default installation uses the deterministic offline provider and needs no
model package. To opt into a model already stored on this machine, install the
optional package with `pip install -e ".[semantic]"` and provide all three
identity fields:

```powershell
python -m auremgrid.cli serve --db auremgrid.sqlite `
  --semantic-model-path D:\models\local-mini `
  --semantic-model local-mini `
  --semantic-version weights-2026-08-01
```

For persistent local process configuration, set
`AUREMGRID_SEMANTIC_MODEL_PATH`, `AUREMGRID_SEMANTIC_MODEL`, and
`AUREMGRID_SEMANTIC_VERSION`. Apply the same values to the server and workers.
The path must exist before use and the loader sets `local_files_only=True`; no
model download is attempted. Missing files, a missing optional dependency, or
a model load error report semantic health as degraded while startup, canonical
records, and full-text retrieval continue working. Changing model version or
dimensions causes the rebuildable vector projection to be regenerated; it does
not require a schema migration.

## Optional Graphiti/Neo4j projection

The local temporal graph remains the default and requires no network or extra
package. To opt in, install `pip install -e ".[graphiti]"` and set every value
below before starting server and worker processes:

```text
AUREMGRID_GRAPHITI_ENABLED=true
AUREMGRID_GRAPHITI_NEO4J_URI=neo4j://...
AUREMGRID_GRAPHITI_NEO4J_USERNAME=...
AUREMGRID_GRAPHITI_NEO4J_PASSWORD=...
AUREMGRID_GRAPHITI_NEO4J_DATABASE=neo4j
AUREMGRID_GRAPHITI_LLM_MODEL=...
AUREMGRID_GRAPHITI_SMALL_MODEL=...
AUREMGRID_GRAPHITI_LLM_BASE_URL=https://...
AUREMGRID_GRAPHITI_EMBEDDER_MODEL=...
AUREMGRID_GRAPHITI_EMBEDDER_BASE_URL=https://...
AUREMGRID_GRAPHITI_EMBEDDING_DIM=1536
AUREMGRID_GRAPHITI_OPENAI_API_KEY=...
```

Missing or invalid configuration, an absent optional dependency, or provider
outage leaves canonical, FTS, and semantic retrieval usable and reports graph
health as unavailable/degraded. Credentials are not written to SQLite or
health output. Upstream graph reads run only for full-workspace ACLs; partial
ACL scopes deliberately skip that channel. Rebuilds stage a generation before
SQLite activates it. Schema 21 stores the append-only mapping from deterministic
canonical episode keys to Graphiti-generated UUIDs. Restart restores complete
mappings without rewriting remote episodes and rebuilds incomplete generations
from canonical evidence.

# Auremgrid Company OS

A local-first operating brain for a retainer agency.

Most company-AI products search documents and hope the model remembers the rest. Auremgrid is built for the actual failure modes of a retainer studio: work that never gets captured, reviews that stall, prices that change, brand rules that live in someone's head, and clients who go quiet before they churn.

It does two jobs at once:

1. Keep a cited, time-aware brain for each client.
2. Make work move through a real operating loop: ask, assign, produce, review, ship, stay in touch.

The first version runs on your machine with Python and SQLite. No Docker. No API keys. No cloud account. Demo fixtures stay synthetic, so the repo never needs private client data. Any agency can onboard their own isolated workspace.

## Why this is useful

If you run a multi-client agency, the expensive problems are rarely "we need a smarter chatbot." They look like this:

- A designer starts a landing page without the current offer or visual rules.
- A price changed in April, but last month's PDF is still what the model cites.
- A client asked for something on a call, and it never became a task.
- Review opened, nobody closed it, the work sat there.
- Two functions keep sending the same asset back and forth because nobody is allowed to end the loop.
- An account goes quiet for 40 days and the team only notices when the client is already leaving.

Auremgrid is useful on day one because it makes those things first-class:

| Problem | What Auremgrid does |
|---|---|
| Which client is this? | Every read and write needs a workspace and an actor. There is no global search. |
| Is this still true? | Facts have validity windows. Old prices stay visible. As-of queries can reconstruct the past. |
| Who said that? | Every result carries a source, locator, content hash, and evidence span. |
| Can this person see it? | Source-level permissions are checked before retrieval, not after. |
| Did we actually take the request? | Work cannot exist until intake has what, which account, who asked, and when it is needed. |
| Is this done? | Review is blocked until Definition of Done is complete. |
| Why is this still in review? | Review must be closed. Abandoned review is a visible failure. |
| How do we start work for this client? | Each workspace has a client brain plus reusable playbooks. |
| Is this account drifting? | The account brief always returns days since last touchpoint. |

That is the difference between a knowledge base and an operating system. Retrieval answers questions. The operating layer makes work move.

## Who it is for

Auremgrid is for:

- retainer studios with multiple clients
- account leads who need a current brief before they start work
- designers, media buyers, and writers who should not have to reconstruct client rules from Slack
- operators who want a durable record of decisions, not another chat transcript
- people building agents that need cited client context without leaking one account into another

It is not yet:

- a hosted SaaS
- a full Slack / ClickUp / Drive replacement
- a login-and-invite product for a whole team
- a magic connector that reads your production workspace by itself

A new agency can clone this, run it locally, learn the model, and start putting their own clients into isolated workspaces. They will still need to add their own live connectors and team access. The contracts are built for that next layer. They are not that layer yet.

## How it is built

Auremgrid owns the canonical contracts. Graphiti, Cognee, Mem0, Onyx, RAGFlow, LightRAG, GraphRAG, and Letta are used in-process as projections. They do not get to become a second source of truth.

```text
Sources
  local markdown, later Slack / Drive / ClickUp / Figma
        |
        v
Ingestion
  hash, permissions, idempotent ingest
        |
        v
Evidence layer
  documents, temporal facts, relations, citations, audit
        |
        v
operating layer
  client brain, playbooks, work items, Definition of Done,
  review, status posts, last touchpoint
        |
        v
Access
  dashboard, REST API, MCP-style agent tools
```

### Evidence layer

This is the client brain underneath everything else.

- Workspace is the tenant boundary. Client Alpha and Client Beta never share a search path.
- Actor is the person or agent making the request. Roles are admin, operator, and read-only agent.
- SourceArtifact records where a file came from, its hash, and who may see it.
- Document is the append-only source text.
- Fact and Relation are temporal claims: subject, predicate, object, valid_from, valid_until, confidence, and a citation.
- Memory is for preferences and interaction notes. It is not allowed to become canonical company truth.
- AuditEvent records reads, writes, and denials.

Facts are never silently overwritten. If the consultation price moves from 149 to 199, both versions remain. If a weaker source later claims 189, that conflict is preserved instead of erasing the current approved price.

### operating layer

This is the operating system that sits on the brain.

Work moves through:

captured -> assigned -> in_progress -> review -> client_review -> shipped

The product refuses the shortcuts that usually wreck delivery:

- no intake, no work item
- incomplete Definition of Done, no review
- review not closed, no ship

Definition of Done is the agency finish line:

1. mobile responsive
2. assets exported
3. creative inside the safe zone
4. copy spell-checked
5. handoff notes written

Each workspace also has a client brain:

- snapshot
- brand rules
- landing-page rules
- ads rules
- design rules
- email rules
- dos / don'ts
- open loops

Reusable process lives in playbooks. Client-specific taste stays in the brain. That is how a new person starts work: open the brain, open the playbook, then use the evidence layer to prove any claim.

The account brief is the thing an agent or account lead should ask for first. It returns the brain, the playbooks, open work, last touchpoint, days of silence, and cited evidence for a query.

### Access layer

There are three ways in:

- Local dashboard at /
- REST endpoints for search, entity, history, neighbors, sources, recent, brief, and work
- Protocol-neutral MCP-style tools with the same names, so Claude, Codex, or another agent can use one contract

Agent access is read-only by default. Writing memory or ingesting sources requires a writable actor. External actions are not implied by a search.

## How a new person should use it

Start with the demo. Do not begin by wiring production Slack.

### 1. Run the tests

From the repo root, on Windows:

```powershell
.\tools\test.ps1
```

That suite is the product contract. It checks isolation, permissions, idempotent ingest, temporal history, conflicting evidence, citations, prompt-injection handling, the agency work loop, and the account brief.

### 2. See a client brief

```powershell
$env:PYTHONPATH="$PWD\src"
python -m auremgrid.cli brief
```

You should get a JSON pack for synthetic Client Alpha: current brand rules, playbooks, one open retargeting job, last touchpoint, and cited consultation-price evidence.

### 3. Open the dashboard

```powershell
$env:PYTHONPATH="$PWD\src"
python -m auremgrid.cli serve --host 127.0.0.1 --port 8791 --db auremgrid-demo.sqlite --seed
```

Then open http://127.0.0.1:8791/.

The dashboard is the human view of the same brief. The raw JSON endpoints are linked on the left so you can inspect search, work, and the cross-workspace leak test.

### 4. Ask the brain a question

```text
GET /search?workspace_id=ws_alpha&actor_id=act_alpha_operator&query=consultation%20price
GET /brief?workspace_id=ws_alpha&actor_id=act_alpha_operator&query=consultation%20price
GET /work?workspace_id=ws_alpha&actor_id=act_alpha_operator
GET /search?workspace_id=ws_beta&actor_id=act_beta_admin&query=consultation%20price
```

The last one should come back unknown. That is the point. Client Beta cannot see Client Alpha.

### 5. Put your own agency on it

Create one workspace per client. Create actors for the people and agents who may touch that client. Ingest only the files that belong there. Write the client brain before anyone starts a page, ad, or deck.

Use the onboard command instead of the demo names:

python -m auremgrid.cli onboard --agency Northwind Studio --workspace ws_northwind --admin Northwind Admin

A good first week looks like this:

1. Pick one live account, not the whole roster.
2. Write the client brain by hand: snapshot, brand rules, dos, don'ts, current offer, current risk.
3. Drop approved source files into that workspace.
4. Capture the next real request through intake instead of Slack-only memory.
5. Refuse to call anything done until Definition of Done and review are closed.
6. Log the next client touchpoint.
7. Use the account brief at the start of every task.

If a fact is not in the brain or the evidence layer, the honest answer is unknown. Do not let the model invent a price, a visual rule, or an approver.

## Repository map

```text
src/auremgrid/
  domain/           contracts for evidence and agency work
  storage/          SQLite + FTS5
  extract/          deterministic fact and relation extraction
  services/         CompanyOS, the product surface
  api/              dashboard, HTTP, MCP-style tools
  adapters/         Graphiti, Cognee, Mem0, Onyx, RAGFlow, LightRAG, GraphRAG, Letta
  cli.py            onboard, demo, brief, serve, sync

docs/
  architecture.md
  operating-model.md
  data-lifecycle.md
  threat-model.md
  oss-evaluation.md
  adr/              why Auremgrid owns truth and the operating layer owns the loop

fixtures/           synthetic Client Alpha / Client Beta only
tests/              isolation, temporal truth, agency work loop
tools/test.ps1      local test runner
```

## Design rules

These are not style preferences. They are the product.

- Every operation needs workspace_id and actor_id.
- Permissions are applied before ranking. Unauthorized text cannot affect scores or existence signals.
- Re-ingesting the same source key and content hash is a no-op.
- Source documents are untrusted data. Prompt-injection text is stored and cited, never obeyed.
- Contradictions are preserved. History is append-only.
- If evidence is insufficient, the system returns unknown.
- Private agency vault data does not belong in this repository. Fixtures stay synthetic.

## What is already wired in

All eight engines are used in the local path as projections, not as a second source of truth:

| Engine | Role |
|---|---|
| Graphiti | temporal client-brain projection |
| Cognee | current-belief control plane |
| Mem0 | preference and interaction memory |
| Onyx | connector catalog and knowledge-shell contract |
| RAGFlow | messy-text cleaner before extraction |
| LightRAG | static-corpus retrieval |
| Microsoft GraphRAG | community and theme summaries |
| Letta | stateful agent identity, never client facts |

A networked extra can replace a local projection later only if it beats this baseline.

## What is still not here

- Live Slack, Drive, task-tracker, or design-tool credentials
- Multi-user login, SSO, or hosted multi-tenant SaaS
- Automatic LLM extraction
- Required installs of the networked Graphiti / Cognee / Mem0 / RAGFlow / LightRAG / GraphRAG / Letta servers

## Requirements

- Python 3.12+
- Windows, macOS, or Linux
- No third-party Python packages for the first slice
- No Docker, network access, or API keys

Apache-2.0. See [LICENSE](LICENSE), [CONTRIBUTING.md](CONTRIBUTING.md), and [docs/operating-model.md](docs/operating-model.md).



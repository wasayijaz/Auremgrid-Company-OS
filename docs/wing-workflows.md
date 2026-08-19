# Wing workflow catalog

The workflow catalog is a neutral set of reusable operating contracts for
cross-wing delivery. It is intentionally independent of any company, client,
product, person, or connector. The Company OS snapshots a validated template
into a versioned, workspace-scoped workflow run so later catalog changes cannot
rewrite history.

## Wings

- **Client Strategy/Marketing** — goals, briefs, client decisions, and narrative
- **Product & Engineering** — implementation, testing, release readiness
- **Paid Media** — channel setup, traffic QA, and performance diagnosis
- **Design** — visual/interface production and experience QA
- **Video Production** — scripting, production, edit, and captioning
- **Operations** — coordination, evidence, release logistics, and reporting

## Template contract

Each template in `fixtures/workflows/catalog.json` contains:

- a stable template identifier and ordered, stable stage identifiers;
- the owning wing and role for every stage;
- an explicit handoff target when the next stage belongs to another wing;
- an approval gate (`none`, `internal`, `client`, `compliance`, or `launch`),
  with approver role and evidence required for every gated stage;
- required evidence/output, expected duration and SLA, escalation trigger,
  recurrence/cadence, dependencies, and a quality checklist;
- a post-launch review and at least one completion or launch outcome.

`order` is a stable presentation sequence for the canonical checklist. It is
not a claim that every stage must run serially: `dependencies` are the
readiness edges, so stages with no unmet dependency may run in parallel. An
optional `on_reject_stage_id` sends a gated stage back to an earlier stage for
rework; it may only point backward and cannot create a dependency cycle.

The catalog currently includes campaign launch, landing page delivery, creative
production, video production, development/release, performance monitoring,
client request handling, and account/project review.

## Loading and validation

`auremgrid.services.workflow_catalog.load_workflow_catalog` reads the fixture
as read-only JSON and returns an immutable `WorkflowCatalog`. Dataclasses and
tuples prevent callers from mutating loaded definitions; `to_dict()` returns
detached lists for serialization. `WorkflowOperations` persists a definition
version and run snapshot, then enforces dependencies, evidence, approval,
handoff, cancellation, optimistic transitions, and overdue escalation.
Workflow gate decisions must link to the existing canonical approval-request
record; the workflow history mirrors that decision but never becomes a second
approval authority.

Validation fails loudly for duplicate template or stage identifiers, unknown
wings or gates, non-contiguous stage ordering, invalid backward dependencies,
missing approvers/evidence on gates, missing cross-wing handoffs, and templates
without a completion/launch outcome. This keeps malformed operating contracts
out of the run engine.

## Operating surfaces

- REST exposes template discovery, run creation/list/detail, stage actions,
  evidence, approval decisions, handoff acknowledgement, cancellation, and
  overdue escalation.
- MCP exposes the same canonical workflow engine for permissioned agents.
- The dashboard shows active workflow counts and workspace run status.
- GitHub verification runs compilation and the complete behavior suite on
  pushes and pull requests.

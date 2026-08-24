# Wing workflow catalog

The workflow catalog is a neutral set of reusable operating contracts for
cross-wing delivery. It is independent of any company, client, product, person,
provider, or connector. The Company OS snapshots a validated template into a
versioned, workspace-scoped workflow run so later catalog changes cannot
rewrite history.

## Wings

- **Client Strategy/Marketing** - client goals, briefs, narrative, decisions,
  relationship continuity, and marketing oversight
- **Product & Engineering** - implementation, testing, and release readiness
- **Paid Media** - campaign setup, trafficking, channel quality, budget control,
  monitoring, and performance diagnosis
- **Design** - visual and interface production, brand governance, and experience
  quality
- **Video Production** - scripting, production, editing, finishing, and captions
- **Operations** - intake coordination, evidence, capacity checks, handoffs,
  release logistics, and reporting

Client success and account roles operate across the catalog rather than forming
a separate production wing. They own touchpoints, request clarity, status,
scope communication, and relationship escalation. Operations owns the delivery
system; discipline leads own discipline quality.

## Role and level guidance

Levels describe the task, not the seniority printed in a title. Typical routing
bands are:

| Responsibility | Typical level |
|---|---|
| Meeting capture, formatting, evidence collection, routine variants | L0 |
| Executive production, client-success follow-up, account coordination, routine monitoring | L0-L1 |
| Strategy, planning, diagnosis, and account communication | L1-L2 |
| Design, marketing, ads, product, and operations lead review | L2 |
| Scope negotiation, portfolio decisions, and material relationship escalation | L2-L3 |

Typical wing ranges are broad because each workflow contains both production
and review work:

- Client Strategy/Marketing: L1-L3
- Product & Engineering: L0-L2
- Paid Media: L0-L2
- Design: L0-L2
- Video Production: L0-L2
- Operations: L0-L2

A workflow stage may require a more specific level than its wing's usual range.
The assigned person or agent must also satisfy workspace access, tool, write,
capacity, business-role, and approval requirements. A business title never
confers platform permission by itself.

## Accountability pattern

Use these boundaries when extending a template:

- Client success or account coordination confirms the request and maintains the
  communication loop.
- The receiving executive or producer owns production evidence and completion.
- The discipline lead owns quality review and exceptions.
- Operations owns cross-wing readiness, capacity visibility, and release
  coordination.
- A client sponsor or explicitly authorized internal approver owns gated
  decisions.
- Meeting capture may propose decisions and actions but cannot approve or assign
  them without the normal routing and permission checks.

## Template contract

Each template in `fixtures/workflows/catalog.json` contains:

- a stable template identifier and ordered, stable stage identifiers;
- the owning wing and role for every stage;
- an explicit handoff target when the next stage belongs to another wing;
- an approval gate (`none`, `internal`, `client`, `compliance`, or `launch`),
  with approver role and evidence required for every gated stage;
- required evidence or output, expected duration and SLA, escalation trigger,
  recurrence or cadence, dependencies, and a quality checklist; and
- a post-launch review and at least one completion or launch outcome.

`order` is a stable presentation sequence for the canonical checklist. It is
not a claim that every stage must run serially. `dependencies` are readiness
edges, so stages with no unmet dependency may run in parallel. An optional
`on_reject_stage_id` sends a gated stage back to an earlier stage for rework; it
may only point backward and cannot create a dependency cycle.

The catalog currently includes campaign launch, landing-page delivery, creative
production, video production, development and release, performance monitoring,
client request handling, and account or project review.

## Loading and validation

`auremgrid.services.workflow_catalog.load_workflow_catalog` reads the fixture as
read-only JSON and returns an immutable `WorkflowCatalog`. Dataclasses and
tuples prevent callers from mutating loaded definitions; `to_dict()` returns
detached lists for serialization. `WorkflowOperations` persists a definition
version and run snapshot, then enforces dependencies, evidence, approval,
handoff, cancellation, optimistic transitions, and overdue escalation.

Workflow gate decisions link to the canonical approval-request record. Workflow
history mirrors the decision but never becomes a second approval authority.
Validation rejects duplicate identifiers, unknown wings or gates, invalid
ordering or dependencies, missing gate evidence, missing cross-wing handoffs,
and templates without a completion or launch outcome.

## Implemented foundation

The runtime currently persists stage owner wing and role, optional person
assignment, dependencies, evidence, approval gates, handoff contracts and
acknowledgements, SLAs, escalation, cancellation, and rework history. REST, MCP,
and dashboard surfaces use the same canonical workflow engine.

Owner-role values resolve against explicit person and agent role assignments.
The engine enforces task-level eligibility, rejects inactive or viewer owners,
preserves the owner snapshot on existing runs, and uses the same workflow and
permission authorities for primary and backup client-success ownership and
capacity rollups.

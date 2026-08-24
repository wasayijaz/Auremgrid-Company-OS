# Agent model

## Capability levels

Work is classified into four capability levels. A level describes the judgment,
risk, and verification required by the task; it is not a provider or model
brand. Deployments may change the underlying provider without changing the
operating contract.

| Level | Name | Use when | Representative capabilities | Relative cost target |
|---|---|---|---|---|
| L0 | Execute | The task is well-defined, repeatable, and easy to check | execute, format, extract, summarize, draft | 0.25x |
| L1 | Operate | Routine agency work needs context and bounded judgment | reason, produce, communicate, route, schedule | 0.5x |
| L2 | Build | The task needs sustained analysis, implementation, or independent verification | build, verify, review, diagnose, implement | 1.0x |
| L3 | Reason | The decision is strategic, ambiguous, high-risk, or difficult to reverse | strategize, architect, assess risk, synthesize, decide | 2.0x |

Higher levels may perform lower-level work. Routing should still select the
lowest level that covers every required capability unless risk policy or a
human override requires escalation. Cost targets are planning weights, not
billing promises.

## Company role mapping

Human roles and automated agents use the same task-level vocabulary, but a job
title does not permanently assign a person to one level. The level is chosen
for each task. Typical ranges are guidance:

| Company role | Typical level | Typical responsibility |
|---|---|---|
| Client meeting capture | L0 | Attendance capture, transcript cleanup, summary, and proposed action items |
| Marketing executive | L0-L1 | Drafts, scheduling, publishing preparation, and routine reporting |
| Design executive | L0-L1 | Asset production, resizing, variants, and production checks |
| Ads executive | L0-L1 | Campaign setup, trafficking, monitoring, and evidence capture |
| Client success coordinator | L1 | Touchpoints, request routing, status updates, and open-loop follow-up |
| Account coordinator | L1 | Intake quality, owner assignment, deadlines, and handoff tracking |
| Content strategist | L1-L2 | Brief shaping, message hierarchy, and narrative decisions |
| Channel planner or performance analyst | L1-L2 | Channel planning, diagnosis, and recommended actions |
| Account strategist | L1-L2 | Account readouts, client communication, and outcome planning |
| Design lead | L2 | Design review, brand governance, and creative quality approval |
| Marketing lead | L2 | Strategy review, campaign oversight, and marketing quality control |
| Ads or performance lead | L2 | Budget governance, campaign approval, and performance review |
| Product engineer | L2 | Implementation, testing, staging, and release verification |
| Operations lead or release coordinator | L2 | Cross-wing coordination, release control, and incident response |
| Account director or client sponsor | L2-L3 | Scope decisions, relationship escalation, and strategic commitments |
| Agency owner | L3 | Portfolio strategy, investment decisions, and organization design |

Leads approve, coach, and resolve exceptions; executives produce and operate
within approved constraints. Client success owns relationship continuity while
account and operations roles own delivery coordination. One person may hold
more than one business role in a small team.

## Business roles are not permissions

Business titles describe accountability and stage eligibility. Authorization
continues to come from organization membership, workspace membership, explicit
capabilities, agent workspace allow-lists, tool declarations, memory policy,
and write permissions. For example, naming someone a Design lead does not grant
workspace administration or approval authority by itself.

Workflow stages may name a wing and business role. The runtime must still check
the caller's permissions before reading or changing records. Sensitive or
one-way actions continue to require the canonical approval policy.

## Routing contract

Intake supplies capability tags. Deterministic policy recommends the cheapest
eligible level that covers those tags. A human or policy may override that
recommendation, and the reason should be retained with the task or run.
Unrecognized or conflicting tags fail closed to review rather than silently
selecting an arbitrary worker.

An eligible agent also needs:

- access to the target workspace;
- the declared tools required by the task;
- sufficient memory and write policy;
- available capacity; and
- any business-role or approval eligibility required by the workflow stage.

Level eligibility never bypasses these constraints.

## Implemented foundation

The Company OS already has organization- and workspace-scoped agents, roles,
tasks, queues, runs, tool calls, traces, outputs, errors, approvals, and cost
records. Agents declare tools, allowed workspaces, memory access, and write
permissions. Cross-wing workflows already retain owner wing and role, evidence
gates, approval decisions, handoff acknowledgements, SLAs, and rework history.
People records already retain titles, departments, managers, skills,
availability, and capacity snapshots.

Approved reversible action execution uses one local catalog enforced by the
executor and exposed through the agent command-center service:
`generate_report`, `create_notification`, `acknowledge_attention`,
`create_risk`, `add_work_comment`, and `create_proposal`. Every catalog entry
is safe, non-one-way, approval-gated, and limited to local canonical ledger
routes. Run detail includes the execution ledger rows plus the replay boundary:
successful same-key replays return the recorded local result, active executions
block parallel replay, and failed same-key executions require a new approved
task or idempotency key.

Agent task handoffs persist `parent_task_id` and `delegation_depth`. Root tasks
start at depth 0; delegated tasks must stay in the parent workspace and advance
by exactly one level. The runtime rejects tasks beyond the configured depth
bound before they enter the queue, and runs retain the accepted depth.

The four-level taxonomy is the routing contract for the current level-routing
slice. It should only be described as enforced end to end when level fields,
resolution, persistence, API output, dashboard output, and behavior tests ship
together.

## Next slices

The next organization layers are intentionally separate from the level core:

1. Persist business-role and wing assignments for people, including primary and
   backup client-success ownership.
2. Resolve workflow role requirements against actual people and agents instead
   of treating owner-role text as a label only.
3. Link estimates, workflow stages, leave, and time entries into derived
   capacity with person, wing, role, and account rollups.
4. Record meeting-capture responsibility and route proposed outputs to named
   owners without bypassing review.
5. Add lead-specific review eligibility through explicit policy, not through
   title-based permission shortcuts.

## Hard constraints

Agents cannot use an undeclared tool or enter an undeclared workspace.
Knowledge writes remain proposals unless explicit policy permits canonical
writes. External and one-way actions use the canonical approval system. Every
run remains attributable, inspectable, and bounded by tenant isolation.

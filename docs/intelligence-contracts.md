# Intelligence completion contracts

Schema 42 adds Auremgrid-native Intelligence definitions for expert profiles
and runbooks. These records are immutable, versioned contracts. They describe
bounded expert perspectives, reasoning methods, allowed domains/tools,
activation sequences, evidence requirements, handoff gates, quality gates, and
stop conditions.

Each `ExpertProfile` explicitly persists `specialty`, `mission`,
`required_inputs`, `allowed_domains`, `allowed_tools`, `required_evidence`,
`reasoning_method`, `output_schema`, `evaluation_criteria`,
`escalation_policy`, `fallback_policy`, `max_context`, `max_iterations`, and
`capability_level`.

Each `IntelligenceRunbook` explicitly persists `trigger`,
`required_domains`, `required_evidence`, `specialists`, `topology`, `stages`,
`quality_gates`, `contradiction_policy`, `scenario_policy`,
`escalation_policy`, `max_iterations`, and `output_contract`.

The default pack contains 13 expert profiles and 12 runbooks. The pack bridges
to existing Auremgrid authorities through explicit `allowed_tool_refs`,
`domains`, and `capability_level` fields rather than by duplicating generic
agent roles, workflow templates, or permission rules. Definitions do not store
tenant context or generative instruction blocks.

`CompanyOS.intelligence_contracts` is the read facade. It requires normal
organization/workspace membership before returning definitions, and filters
tool references when the caller provides an authenticated identity or capability
set. The facade is safe for read-only workspace members; write or external-send
actions are never exposed by this layer.

`CompanyOS.intelligence_orchestrator` is the bounded execution layer over those
contracts. It builds an ACL-scoped situation from the native Intelligence
projection, selects a matching runbook/profile set, runs at most eight
specialists for a bounded number of iterations, validates every specialist
result, drops uncited or unauthorized evidence refs, detects contradictions,
and returns a `trace_id`, `runbook_route`, contributing profiles, trace stages,
limits, degraded state, and a normalized recommendation. The default specialist
path is deterministic; injected specialist handlers must still satisfy the same
shape, timeout, citation, and scope checks.

The orchestrator is intentionally read-only. It does not create facts,
decisions, work items, approvals, reports, connector jobs, external sends, or
canonical truth. Its recommendations are descriptors and analysis outputs until
a caller sends them through an existing permission-checked service.

Schema 43 adds the learning layer. `intelligence_hypotheses` stores
interpretations with supporting and opposing evidence refs, assumptions,
status, confidence, generator identity, optional supersession, resolution, and
outcome fields. `intelligence_recommendations` stores the runbook/profile
contributors, options, recommended option, evidence refs, and evaluation
window. `intelligence_recommendation_lifecycle` appends accepted, rejected,
chosen, and evaluated events. Evaluated events must cite measured outcomes in
the same workspace and inside the original evaluation window. These tables are
append-only or immutable and audited; they do not promote hypotheses into
facts or recommendations into decisions.

Schema 44 adds shadow-only evaluation safety. Evaluation runs can record
provider/model/profile/runbook metadata, task class, trace linkage, latency,
tokens, cost, evidence completeness, evaluator score, human acceptance,
revision count, downstream outcome score, and cap reason. Policy rows define
runtime, token, cost, and breaker caps per organization/task class. Circuit
events are append-only. This layer can block further shadow evaluations when a
breaker is open, but it never changes live agent routing.

Schema 45 adds proactive attention lifecycle state for persisted Intelligence
snapshots. Lifecycle rows dedupe by organization/workspace/person/fingerprint
and keep status, originating snapshot, attention item, orchestration trace,
recommendation id, safe action descriptor, optional approval request, reason,
and timestamps. The allowed statuses are `new`, `acknowledged`, `acted_on`,
`resolved`, `dismissed`, and `resurfaced`. Marking an item `acted_on` requires
an approved, current, same-scope approval request; dismissed or unresolved
descriptors are not executable.

REST and MCP expose these surfaces through the same service boundaries:

- REST: `/dashboard/intelligence/profiles`, `/runbooks`,
  `/orchestrator/run`, `/orchestrator/result`, `/learning`,
  `/hypotheses`, `/recommendations`, `/recommendations/lifecycle`,
  `/evaluation-safety`, `/evaluation/start`, and `/evaluation/complete`.
- MCP: `intelligence.profiles.*`, `intelligence.runbooks.*`,
  `intelligence.orchestrator.*`, `intelligence.learning.get`,
  `intelligence.hypotheses.record`, `intelligence.recommendations.record`,
  `intelligence.recommendations.lifecycle`,
  `intelligence.evaluation_safety.get`, `intelligence.evaluation.start`, and
  `intelligence.evaluation.complete`.

The remaining product gaps are separate from this release: no hosted model
marketplace, no autonomous specialist execution, no externally visible sends,
no workflow-routing mutation from evaluation telemetry, no production
deployment bundle, and no provider write integration are claimed here.

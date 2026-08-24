# Intelligence dependency map

This map records the native Intelligence vertical slice and its existing
Auremgrid authorities. Intelligence remains a derived, read-only reasoning
layer until an approved action crosses the canonical action boundary.

```text
Canonical evidence / ACL scope
  ├─ Brain retrieval, facts, sources, work, risks, capacity, finance, campaigns
  ├─ decisions, workflow state, outcomes, feedback, historical rows
  └─ permissions, workspace membership, capability checks, audit ledger
        │
        ▼
IntelligenceService.workspace / executive_brief
  ├─ findings, hypotheses, opposing evidence, scenarios, analogues
  ├─ recommendation descriptors and decision→action→outcome→learning links
  └─ proactive snapshot projection + attention lifecycle
        │
        ▼
IntelligenceContractService
  ├─ immutable ExpertProfile definitions (13 native profiles)
  └─ immutable IntelligenceRunbook definitions (12 native runbooks)
        │
        ▼
IntelligenceOrchestrator
  ├─ bounded situation builder and runbook router
  ├─ bounded specialist fan-out and validated ExpertResult values
  ├─ contradiction review, balanced synthesis, and reality check
  └─ durable scoped trace (schema 46)
        │
        ▼
Learning / evaluation safety
  ├─ append-only hypotheses and recommendation lifecycle (schema 43)
  ├─ shadow evaluation, cost/runtime caps, circuit breakers (schema 44)
  └─ proactive attention lifecycle and approved action descriptors (schemas 45–48)
        │
        ▼
REST / MCP / Dashboard
  ├─ scoped profile, runbook, orchestrator, learning, and evaluation routes
  ├─ permanent Intelligence rail and executive brief
  └─ canonical approval, workflow, work, and outcome routes for execution
```

The dependency direction is intentionally one-way: projections and model
outputs may cite canonical evidence and propose descriptors, but they cannot
write facts, decisions, workflows, connector state, or external side effects
without the existing permission and approval services.

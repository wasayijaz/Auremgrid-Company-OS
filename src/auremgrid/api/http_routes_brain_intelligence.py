from __future__ import annotations

from typing import Any

from auremgrid.api.http_shared import (
    _evaluation_safety_status, _int, _need, _optional_dt, _optional_int, _optional_str,
    _optional_string_sequence, _required_list, _require_evaluation_scope, _what_if_params,
)
from auremgrid.domain.errors import AuthorizationError, NotFoundError
from auremgrid.services.agent_execution import AGENT_THINK_JOB_TYPE, SQLiteAgentExecutionStore, thinking_surface_enabled


class HttpRoutesBrainIntelligenceMixin:
    def _get_brain_intelligence(self, parsed: Any, params: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/search":
            bundle = self.os.search(
                _need(params, "workspace_id"),
                _need(params, "actor_id"),
                _need(params, "query"),
                as_of=_optional_dt(params.get("as_of")),
                limit=_int(params.get("limit", "8"), "limit"),
            )
            self._json(200, bundle.to_dict())
            return True
        if parsed.path == "/entity/candidates":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, {"candidates": self.os.brain_ops.entity_resolution_candidates(
                scoped.organization_id, workspace_id, scoped, _need(params, "name"), _int(params.get("limit", "8"), "limit")
            )}); return True
        if parsed.path == "/entity":
            self._json(
                200,
                self.os.entity(_need(params, "workspace_id"), _need(params, "actor_id"), _need(params, "name"),
                               as_of=_optional_dt(params.get("as_of"))),
            )
            return True
        if parsed.path == "/history":
            self._json(
                200,
                self.os.history(
                    _need(params, "workspace_id"),
                    _need(params, "actor_id"),
                    _need(params, "subject"),
                    predicate=params.get("predicate"),
                    as_of=_optional_dt(params.get("as_of")),
                ),
            )
            return True
        if parsed.path == "/neighbors":
            self._json(
                200,
                self.os.neighbors(
                    _need(params, "workspace_id"), _need(params, "actor_id"), _need(params, "entity"),
                    as_of=_optional_dt(params.get("as_of")),
                ),
            )
            return True
        if parsed.path == "/sources":
            self._json(200, self.os.sources(_need(params, "workspace_id"), _need(params, "actor_id")))
            return True
        if parsed.path == "/recent":
            self._json(
                200,
                self.os.recent(
                    _need(params, "workspace_id"),
                    _need(params, "actor_id"),
                    limit=int(params.get("limit", "5")),
                ),
            )
            return True
        if parsed.path == "/brief":
            self._json(
                200,
                self.os.account_brief(
                    _need(params, "workspace_id"),
                    _need(params, "actor_id"),
                    query=params.get("query"),
                ).to_dict(),
            )
            return True
        if parsed.path == "/dashboard/brain":
            assert identity is not None
            organization_id, workspace_id, person_id = _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
            self._json(200, self.os.dashboard.brain(identity, organization_id, workspace_id, person_id, _optional_dt(params.get("as_of")))); return True
        if parsed.path == "/dashboard/brain/thinking":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            job = self.os.jobs.get_job(scoped.organization_id, workspace_id, _need(params, "job_id"))
            if job.get("type") != AGENT_THINK_JOB_TYPE:
                raise NotFoundError("thinking job not found")
            store = SQLiteAgentExecutionStore(self.os.store.conn)
            read = store.thinking_read_model(
                scoped.organization_id, workspace_id, str((job.get("payload") or {}).get("run_id") or "")
            ) if thinking_surface_enabled(self.os) and store.available() else None
            self._json(200, {"available": bool(read), "thinking": read, "attempts": (read or {}).get("attempts", [])})
            return True
        if parsed.path == "/brain/customizations/active":
            assert identity is not None
            scoped_workspace = _optional_str(params.get("workspace_id"))
            scoped = self.os.auth.scope_identity(identity, scoped_workspace) if scoped_workspace else identity
            self._json(200, self.os.brain_customizations.active(
                scoped, _need(params, "organization_id"), scoped_workspace,
                _optional_str(params.get("kind")), _optional_dt(params.get("as_of")),
            )); return True
        if parsed.path == "/dashboard/intelligence":
            assert identity is not None
            organization_id, workspace_id, person_id = _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            if scoped.organization_id != organization_id or scoped.person_id != person_id:
                raise AuthorizationError("identity scope mismatch")
            actor_id = None
            try:
                actor_id = self.os.auth.actor_for_identity(scoped, workspace_id)
            except AuthorizationError:
                # Canonical operational findings remain available when a
                # principal has not yet been bound to a brain actor.
                actor_id = None
            self._json(200, self.os.intelligence.workspace(
                organization_id, workspace_id, person_id, actor_id,
                _optional_dt(params.get("as_of")), params.get("query"),
                what_if=_what_if_params(params),
                context_type=_optional_str(params.get("context_type")),
                context_id=_optional_str(params.get("context_id")),
                capabilities=identity.capabilities,
            )); return True
        if parsed.path in {"/dashboard/intelligence/portfolio", "/dashboard/intelligence/executive"}:
            assert identity is not None
            organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
            if identity.organization_id != organization_id or identity.person_id != person_id:
                raise AuthorizationError("identity scope mismatch")
            method = self.os.intelligence.executive_brief if parsed.path.endswith("/executive") else self.os.intelligence.portfolio
            self._json(200, method(
                organization_id, person_id, as_of=_optional_dt(params.get("as_of")),
            )); return True
        if parsed.path == "/dashboard/intelligence/snapshots":
            assert identity is not None
            organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
            workspace_id = _optional_str(params.get("workspace_id"))
            self.os.proactive_intelligence.authorize_read(identity, organization_id, person_id, workspace_id)
            snapshot = self.os.proactive_intelligence.require_latest_snapshot(
                organization_id,
                person_id,
                str(params.get("snapshot_type") or ("workspace" if workspace_id else "executive")),
                workspace_id,
            )
            self._json(200, {"snapshot": snapshot}); return True
        if parsed.path == "/dashboard/intelligence/attention":
            assert identity is not None
            organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
            workspace_id = _optional_str(params.get("workspace_id"))
            self.os.proactive_intelligence.authorize_read(identity, organization_id, person_id, workspace_id)
            self._json(200, {"attention": self.os.proactive_intelligence.attention_queue(
                organization_id, person_id, workspace_id, _int(params.get("limit", "20"), "limit")
            )}); return True
        if parsed.path == "/dashboard/intelligence/refresh-status":
            assert identity is not None
            self._json(200, self.os.proactive_intelligence.refresh_status(
                identity,
                str(params.get("snapshot_type") or ("workspace" if params.get("workspace_id") else "executive")),
                _optional_str(params.get("workspace_id")),
            )); return True
        if parsed.path == "/dashboard/intelligence/profiles":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, {"profiles": list(self.os.intelligence_contracts.list_profiles(
                scoped.organization_id, workspace_id, scoped.person_id,
                domain=_optional_str(params.get("domain")),
                capability_level=_optional_str(params.get("capability_level")),
                capabilities=scoped.capabilities,
            ))}); return True
        if parsed.path == "/dashboard/intelligence/profiles/get":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, {"profile": self.os.intelligence_contracts.get_profile(
                scoped.organization_id, workspace_id, scoped.person_id, _need(params, "profile_id"),
                version=_optional_int(params.get("version")),
                capabilities=scoped.capabilities,
            )}); return True
        if parsed.path == "/dashboard/intelligence/runbooks":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, {"runbooks": list(self.os.intelligence_contracts.list_runbooks(
                scoped.organization_id, workspace_id, scoped.person_id,
                domain=_optional_str(params.get("domain")),
                profile_id=_optional_str(params.get("profile_id")),
                capabilities=scoped.capabilities,
            ))}); return True
        if parsed.path == "/dashboard/intelligence/runbooks/get":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, {"runbook": self.os.intelligence_contracts.get_runbook(
                scoped.organization_id, workspace_id, scoped.person_id, _need(params, "runbook_id"),
                version=_optional_int(params.get("version")),
                capabilities=scoped.capabilities,
            )}); return True
        if parsed.path == "/dashboard/intelligence/orchestrator/result":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            result = self.os.intelligence_orchestrator.get_run(
                _need(params, "trace_id"), scoped.organization_id, workspace_id, scoped.person_id,
            )
            if result is None:
                raise NotFoundError("orchestrator result not found")
            self._json(200, {"result": result}); return True
        if parsed.path == "/dashboard/intelligence/orchestrator/latest":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            result = self.os.intelligence_orchestrator.latest_run(
                scoped.organization_id, workspace_id, scoped.person_id,
            )
            self._json(200, {"result": result}); return True
        if parsed.path == "/dashboard/intelligence/learning":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, self.os.intelligence_learning.workspace_learning(
                scoped.organization_id, workspace_id, scoped.person_id,
            )); return True
        if parsed.path in {"/dashboard/intelligence/recommendation-quality", "/dashboard/intelligence/recommendations/quality"}:
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, self.os.intelligence_learning.recommendation_quality(
                scoped.organization_id, workspace_id, scoped.person_id,
                as_of=_optional_str(params.get("as_of")),
            )); return True
        if parsed.path == "/dashboard/intelligence/evaluation-safety":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, _evaluation_safety_status(
                self.os, scoped.organization_id, workspace_id, scoped.person_id,
                _optional_str(params.get("task_class")) or "reasoning",
            )); return True
        if parsed.path == "/memory-proposals":
            assert identity is not None
            workspace_id = params.get("workspace_id")
            if not workspace_id: raise NotFoundError("proposal scope not found")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self.os._require_person_access(scoped.organization_id, workspace_id, scoped.person_id)
            self._json(200,{"proposals":self.os.brain_ops.list_memory_proposals(
                scoped.organization_id, workspace_id, scoped.person_id, _optional_dt(params.get("as_of"))
            )}); return True
        if parsed.path == "/knowledge-health":
            assert identity is not None
            view = self.os.dashboard.brain(
                identity, _need(params,"organization_id"), _need(params,"workspace_id"),
                _need(params,"person_id"), _optional_dt(params.get("as_of")),
            )
            self._json(200,{
                "generated_at":view["generated_at"],"as_of":view["as_of"],
                "workspace":view["workspace"],"summary":view["summary"],"health":view["health"],
            }); return True
        return False

    def _post_brain_intelligence(self, parsed: Any, payload: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/dashboard/intelligence/refresh":
            item = self.os.proactive_intelligence.enqueue_refresh(
                identity,
                str(payload.get("snapshot_type") or ("workspace" if payload.get("workspace_id") else "executive")),
                _optional_str(payload.get("workspace_id")),
                _optional_str(payload.get("idempotency_key")),
                int(payload.get("priority", 0)),
            )
            self._json(202, {"job": item}); return True
        if parsed.path == "/dashboard/intelligence/orchestrator/run":
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            actor_id = None
            try:
                actor_id = self.os.auth.actor_for_identity(scoped, workspace_id)
            except AuthorizationError:
                actor_id = None
            result = self.os.intelligence_orchestrator.run(
                scoped.organization_id,
                workspace_id,
                scoped.person_id,
                actor_id=actor_id,
                runbook_id=_optional_str(payload.get("runbook_id")),
                profile_ids=_optional_string_sequence(payload.get("profile_ids"), "profile_ids"),
                query=_optional_str(payload.get("query")),
                as_of=_optional_dt(payload.get("as_of")),
                capabilities=scoped.capabilities,
                iterations=_int(payload.get("iterations", 1), "iterations"),
            )
            self._json(200, {"result": result}); return True
        if parsed.path == "/dashboard/intelligence/hypotheses":
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(201, {"hypothesis": self.os.intelligence_learning.record_hypothesis(
                scoped.organization_id, workspace_id, scoped.person_id, _need(payload, "text"),
                subject=_optional_str(payload.get("subject")),
                evidence_for_refs=payload.get("evidence_for_refs"),
                evidence_against_refs=payload.get("evidence_against_refs"),
                status=str(payload.get("status") or "proposed"),
                confidence=float(payload.get("confidence", 0.5)),
                assumptions=payload.get("assumptions"),
                generated_by=payload.get("generated_by"),
                resolution=_optional_str(payload.get("resolution")),
                outcome=payload.get("outcome"),
                supersedes_hypothesis_id=_optional_str(payload.get("supersedes_hypothesis_id")),
                idempotency_key=_optional_str(payload.get("idempotency_key")),
            )}); return True
        if parsed.path == "/dashboard/intelligence/recommendations":
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(201, {"recommendation": self.os.intelligence_learning.record_recommendation(
                scoped.organization_id, workspace_id, scoped.person_id, _need(payload, "summary"),
                runbook_id=_need(payload, "runbook_id"),
                runbook_version=_int(payload.get("runbook_version"), "runbook_version"),
                profile_contributors=_required_list(payload.get("profile_contributors"), "profile_contributors"),
                confidence=float(payload.get("confidence", 0.5)),
                options=_required_list(payload.get("options"), "options"),
                recommended_option_id=_optional_str(payload.get("recommended_option_id")),
                evidence_refs=_required_list(payload.get("evidence_refs"), "evidence_refs"),
                evaluation_window_start=_need(payload, "evaluation_window_start"),
                evaluation_window_end=_need(payload, "evaluation_window_end"),
                generated_by=payload.get("generated_by"),
                idempotency_key=_optional_str(payload.get("idempotency_key")),
            )}); return True
        if parsed.path == "/dashboard/intelligence/recommendations/lifecycle":
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(201, {"event": self.os.intelligence_learning.append_recommendation_event(
                scoped.organization_id, workspace_id, scoped.person_id,
                _need(payload, "recommendation_id"), _need(payload, "event_type"),
                chosen_option_id=_optional_str(payload.get("chosen_option_id")),
                measured_outcomes=payload.get("measured_outcomes"),
                score=None if payload.get("score") is None else float(payload.get("score")),
                lessons=str(payload.get("lessons") or ""),
                evidence_refs=payload.get("evidence_refs"),
                evaluation_window_start=_optional_str(payload.get("evaluation_window_start")),
                evaluation_window_end=_optional_str(payload.get("evaluation_window_end")),
                idempotency_key=_optional_str(payload.get("idempotency_key")),
            )}); return True
        if parsed.path == "/dashboard/intelligence/recommendations/handoff":
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(201, self.os.intelligence_learning.handoff_recommendation(
                scoped.organization_id, workspace_id, scoped.person_id, _need(payload, "trace_id"),
                recommendation_id=_optional_str(payload.get("recommendation_id")),
                summary=_optional_str(payload.get("summary")),
                runbook_id=_optional_str(payload.get("runbook_id")),
                runbook_version=_optional_int(payload.get("runbook_version")),
                profile_contributors=payload.get("profile_contributors"), confidence=float(payload.get("confidence", 0.5)),
                options=payload.get("options"), recommended_option_id=_optional_str(payload.get("recommended_option_id")),
                evidence_refs=payload.get("evidence_refs"),
                evaluation_window_start=_optional_str(payload.get("evaluation_window_start")),
                evaluation_window_end=_optional_str(payload.get("evaluation_window_end")), generated_by=payload.get("generated_by"),
                review_status=str(payload.get("review_status") or "reviewed"), decision_id=_optional_str(payload.get("decision_id")),
                approval_request_id=_optional_str(payload.get("approval_request_id")), work_item_id=_optional_str(payload.get("work_item_id")),
                action_descriptor=payload.get("action_descriptor"), outcome_refs=payload.get("outcome_refs"),
                notes=str(payload.get("notes") or ""), idempotency_key=_optional_str(payload.get("idempotency_key")),
            )); return True
        if parsed.path == "/dashboard/intelligence/evaluation/start":
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(201, {"evaluation": self.os.intelligence_evaluation_safety.start(
                scoped.organization_id, scoped.person_id, _need(payload, "task_class"),
                workspace_id=workspace_id,
                provider=_optional_str(payload.get("provider")),
                model=_optional_str(payload.get("model")),
                specialist_profile_id=_optional_str(payload.get("specialist_profile_id")),
                runbook_id=_optional_str(payload.get("runbook_id")),
                runbook_version=_optional_int(payload.get("runbook_version")),
                trace_id=_optional_str(payload.get("trace_id")),
                agent_run_id=_optional_str(payload.get("agent_run_id")),
            )}); return True
        if parsed.path == "/dashboard/intelligence/evaluation/complete":
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            _require_evaluation_scope(self.os, scoped.organization_id, workspace_id, _need(payload, "evaluation_id"))
            self._json(200, {"evaluation": self.os.intelligence_evaluation_safety.complete(
                scoped.organization_id, scoped.person_id, _need(payload, "evaluation_id"),
                workspace_id=workspace_id,
                input_tokens=_optional_int(payload.get("input_tokens")),
                output_tokens=_optional_int(payload.get("output_tokens")),
                cost_amount=None if payload.get("cost_amount") is None else float(payload.get("cost_amount")),
                cost_currency=_optional_str(payload.get("cost_currency")),
                evidence_completeness=None if payload.get("evidence_completeness") is None else float(payload.get("evidence_completeness")),
                evaluator_score=None if payload.get("evaluator_score") is None else float(payload.get("evaluator_score")),
                human_acceptance=payload.get("human_acceptance") if isinstance(payload.get("human_acceptance"), bool) else None,
                revision_count=_int(payload.get("revision_count", 0), "revision_count"),
                downstream_outcome_score=None if payload.get("downstream_outcome_score") is None else float(payload.get("downstream_outcome_score")),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
            )}); return True
        if parsed.path == "/search":
            bundle = self.os.search(
                _need(payload, "workspace_id"),
                _need(payload, "actor_id"),
                _need(payload, "query"),
                as_of=_optional_dt(payload.get("as_of")),
                limit=_int(payload.get("limit", 8), "limit"),
            )
            self._json(200, bundle.to_dict())
            return True
        if parsed.path == "/remember":
            memory = self.os.remember(
                _need(payload, "workspace_id"),
                _need(payload, "actor_id"),
                _need(payload, "content"),
                kind=str(payload.get("kind", "preference")),
            )
            self._json(200, memory.to_dict())
            return True
        if parsed.path == "/memory-proposals":
            assert identity is not None
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(201,self.os.brain_ops.create_proposal(scoped.organization_id,workspace_id,"person",scoped,_need(payload,"kind"),_need(payload,"content"),payload.get("payload") or {},_need(payload,"evidence"),float(payload.get("confidence",0.5)),_optional_str(payload.get("source_id")))); return True
        if parsed.path == "/memory-proposals/review":
            # The legacy review endpoint is intentionally retired. Use the
            # authenticated brain promotion route, which enforces workspace
            # scope and append-only proposal decisions.
            raise NotFoundError("legacy proposal review route retired; use brain.promote")
        if parsed.path == "/brain/promote":
            assert identity is not None
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            proposal_id, action = _need(payload, "proposal_id"), _need(payload, "action")
            resolution = self.os.store.conn.execute("SELECT 1 FROM entity_resolution_proposals WHERE organization_id=? AND workspace_id=? AND id=?", (scoped.organization_id,workspace_id,proposal_id)).fetchone()
            if resolution is not None:
                result = self.os.brain_ops.brain_promote(scoped.organization_id, workspace_id, scoped, proposal_id, action)
            else:
                result = self.os.brain_ops.brain_promote_fact(scoped, proposal_id, action)
            self._json(200, result); return True
        if parsed.path == "/brain/propose":
            assert identity is not None
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            result = self.os.brain_ops.brain_propose(
                scoped.organization_id, workspace_id, scoped, _need(payload, "kind"),
                [str(item) for item in payload.get("candidate_entity_ids", [])],
                float(payload.get("score", 0.0)), _need(payload, "rationale"),
                _need(payload, "evidence"), _optional_str(payload.get("alias")),
                _optional_str(payload.get("source_id")), _optional_str(payload.get("target_id")),
                payload.get("evidence_refs") or {},
            )
            self._json(201, result); return True
        if parsed.path == "/brain/conflicts/resolve":
            assert identity is not None
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, self.os.brain_ops.resolve_fact_conflict(scoped, _need(payload,"conflict_group"), _need(payload,"winner_fact_id"))); return True
        if parsed.path == "/brain/customizations":
            assert identity is not None
            workspace_id = _optional_str(payload.get("workspace_id"))
            scoped = self.os.auth.scope_identity(identity, workspace_id) if workspace_id else identity
            self._json(201, self.os.brain_customizations.create_version(
                scoped, _need(payload, "organization_id"), _need(payload, "scope_type"),
                _need(payload, "kind"), _need(payload, "name"), _need(payload, "body"),
                payload.get("payload") or {}, workspace_id, str(payload.get("reason", "")),
            )); return True
        if parsed.path == "/brain/customizations/activate":
            assert identity is not None
            self._json(200, self.os.brain_customizations.activate_version(
                identity, _need(payload, "organization_id"), _need(payload, "version_id"),
                _need(payload, "reason"),
            )); return True
        if parsed.path == "/brain/customizations/rollback":
            assert identity is not None
            self._json(200, self.os.brain_customizations.rollback(
                identity, _need(payload, "organization_id"), _need(payload, "target_version_id"),
                _need(payload, "reason"),
            )); return True
        return False

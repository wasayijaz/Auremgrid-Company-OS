from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from auremgrid.domain.models import IngestResult




class BrainOperationsMixin:
        def onboard_agency(
            self,
            agency_name: str,
            workspace_id: str,
            admin_name: str,
            operator_name: str | None = None,
        ) -> dict[str, Any]:
            return self.onboarding.onboard_agency(agency_name, workspace_id, admin_name, operator_name)
    
        def engine_status(self, workspace_id: str, actor_id: str, query: str) -> dict[str, Any]:
            self._require_actor(workspace_id, actor_id)
            self.stack.bind_agent(workspace_id, actor_id)
            return {"workspace_id": workspace_id, "query": query, "engines": self.stack.contributions(workspace_id, query, actor_id)}
    
        def sync_connectors(self, actor_id: str, include_simulated: bool = False) -> list[IngestResult]:
            from auremgrid.connectors.bus import ConnectorBus
            from auremgrid.connectors.local import LocalMarkdownConnector
            from auremgrid.connectors.simulated import SimulatedWorkspaceConnector
    
            bus = ConnectorBus(self, actor_id)
            root = Path(__file__).resolve().parents[3] / "fixtures"
            if (root / "client_alpha").exists():
                bus.register(LocalMarkdownConnector("ws_alpha", root / "client_alpha"))
            if include_simulated:
                self._require_actor("ws_alpha", actor_id)
                bus.register(SimulatedWorkspaceConnector.slack("ws_alpha"))
                bus.register(SimulatedWorkspaceConnector.drive("ws_alpha"))
                bus.register(SimulatedWorkspaceConnector.clickup("ws_alpha"))
                bus.register(SimulatedWorkspaceConnector.figma("ws_alpha"))
            return bus.sync()
    
        def seed_demo(self, fixtures_root: str | Path | None = None) -> dict[str, Any]:
            root = Path(fixtures_root or Path(__file__).resolve().parents[3] / "fixtures")
            organization = self.create_organization("Auremgrid Demo Agency", "org_demo")
            alpha = self.create_workspace("Client Alpha", workspace_id="ws_alpha")
            beta = self.create_workspace("Client Beta", workspace_id="ws_beta")
            if self.company.workspace_scope(alpha.id) is None:
                self.company.attach_workspace(organization.id, alpha.id, "client")
            if self.company.workspace_scope(beta.id) is None:
                self.company.attach_workspace(organization.id, beta.id, "client")
            owner = self.company.get_person(organization.id, "person_demo_owner")
            if owner is None:
                owner = self.create_person(organization.id, "Demo Owner", "owner@demo.invalid", role="owner", person_id="person_demo_owner")
            for workspace in (alpha, beta):
                if self.company.workspace_membership(workspace.id, owner.id) is None:
                    self.add_person_to_workspace(organization.id, workspace.id, owner.id, "admin")
            if self.store.conn.execute("SELECT COUNT(*) FROM agents WHERE organization_id=?",(organization.id,)).fetchone()[0] == 0:
                seeded_agents=self.agent_ops.seed_primary_agents(organization.id,owner.id)
                for agent in seeded_agents:
                    self.agent_ops.configure_agent(organization.id,owner.id,agent["id"],"unconfigured",
                        ["brain.search","work.list","projects.list"],[alpha.id,beta.id],json.loads(agent["write_permissions"]))
            alpha_admin = self.create_actor(alpha.id, "Alpha Admin", "admin", "act_alpha_admin")
            alpha_operator = self.create_actor(alpha.id, "Alpha Operator", "operator", "act_alpha_operator")
            alpha_agent = self.create_actor(alpha.id, "Alpha Agent", "agent", "act_alpha_agent")
            beta_admin = self.create_actor(beta.id, "Beta Admin", "admin", "act_beta_admin")
            for path in sorted((root / "client_alpha").glob("*.md")):
                allowed = ["act_alpha_admin"] if "restricted" in path.name else None
                self.ingest_path(alpha.id, alpha_admin.id, path, allowed_actor_ids=allowed)
            for path in sorted((root / "client_beta").glob("*.md")):
                self.ingest_path(beta.id, beta_admin.id, path)
            self.upsert_playbook(
                alpha_admin.id,
                "ads",
                "Ads playbook",
                "Instrument first. Do not launch claims without a current approved fact and a named decision-maker.",
            )
            self.upsert_playbook(
                alpha_admin.id,
                "landing-pages",
                "Landing page playbook",
                "Read the client brain, then apply the current offer, visual rules, and Definition of Done before review.",
            )
            self.upsert_client_brain(
                alpha.id,
                alpha_admin.id,
                snapshot="Clinic-services retainer. Success is booked consultations, not vanity reach.",
                brand_rules="Navy and cream only. Calm, clinical tone. No gradients.",
                landing_pages="Lead with the current consultation offer and keep pricing claims citation-backed.",
                ads="Local-service and search first. Healthcare claims require current approved copy.",
                design="Export to shared assets and keep creative inside the safe zone.",
                email="Lifecycle copy stays clinical and specific.",
                dos=["Cite the current consultation price", "Name a decision-maker on every revision loop"],
                donts=["Do not invent pricing", "Do not skip Definition of Done"],
                open_loops=["Consultation landing page needs a current price pass"],
            )
            self.upsert_client_brain(
                beta.id,
                beta_admin.id,
                snapshot="Fitness studio retainer. Success is intro-week conversions.",
                brand_rules="Charcoal and lime. Energetic, short copy.",
                ads="Always pair the intro week with a clear next step.",
                dos=["Keep the intro-week offer current"],
                donts=["Do not reuse clinic visual rules"],
                open_loops=["Intro-week creative needs review"],
            )
            work = self.capture_work(
                alpha.id,
                alpha_admin.id,
                title="Consultation landing page",
                request="Update the consultation landing page to the current approved offer.",
                requested_by="Channel Lead",
                needed_by="2026-04-10",
                playbook_id="landing-pages",
                decision_maker="Alpha Operator",
                work_item_id="work_demo_consultation_page",
            )
            if work.status != "shipped":
                work = self.assign_work(alpha.id, alpha_admin.id, work.id, alpha_operator.id)
                work = self.start_work(alpha.id, alpha_operator.id, work.id)
                self.mark_dod(
                    alpha.id,
                    alpha_operator.id,
                    work.id,
                    {
                        "mobile_responsive": True,
                        "assets_exported": True,
                        "creative_safe_zone": True,
                        "copy_spellchecked": True,
                        "handoff_notes": True,
                    },
                )
                work = self.submit_review(alpha.id, alpha_operator.id, work.id)
                work = self.close_review(alpha.id, alpha_admin.id, work.id, approved=True, note="Internal review closed")
                self.ship_work(alpha.id, alpha_admin.id, work.id, note="Shipped current consultation page")
            self.record_touchpoint(
                alpha.id,
                alpha_admin.id,
                "Shared the shipped consultation page and confirmed the current price.",
                occurred_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
                touchpoint_id="tp_demo_consultation_shipped",
            )
            open_work = self.capture_work(
                alpha.id,
                alpha_admin.id,
                title="Retargeting ad set",
                request="Build a retargeting set for people who viewed the consultation page.",
                requested_by="Channel Lead",
                needed_by="2026-04-20",
                playbook_id="ads",
                decision_maker="Alpha Operator",
                work_item_id="work_demo_retargeting_ads",
            )
            if open_work.status == "captured":
                self.assign_work(alpha.id, alpha_admin.id, open_work.id, alpha_operator.id)
            for workspace in (alpha, beta):
                if self.store.conn.execute("SELECT COUNT(*) FROM client_health_snapshots WHERE workspace_id=?",(workspace.id,)).fetchone()[0] == 0:
                    self.client_ops.calculate_health(organization.id,workspace.id,owner.id)
            return {
                "workspaces": [alpha.to_dict(), beta.to_dict()],
                "actors": [
                    alpha_admin.to_dict(),
                    alpha_operator.to_dict(),
                    alpha_agent.to_dict(),
                    beta_admin.to_dict(),
                ],
            }
    
        def seed_realistic_agency_demo(
            self, organization_id: str | None = None, owner_person_id: str | None = None
        ) -> dict[str, Any]:
            """Seed the isolated three-client agency scenario.
    
            Kept separate from :meth:`seed_demo` so existing minimal-demo
            counts and fixtures remain unchanged.
            """
            from auremgrid.demo_agency import seed_realistic_agency_demo
    
            return seed_realistic_agency_demo(self, organization_id, owner_person_id)

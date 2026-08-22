"""Idempotent, realistic synthetic agency scenario.

This fixture is deliberately separate from ``seed_demo``.  It is safe to run
against a fresh database (or after the minimal demo) and never enables finance
connectors or inserts real-provider records.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


ORG_ID = "org_realistic_agency_demo"
SOURCE_PREFIX = "realistic_fixture_"


def _one(os: Any, sql: str, params: tuple[Any, ...]) -> Any:
    return os.store.conn.execute(sql, params).fetchone()


def _person(os: Any, org: str, person_id: str, name: str, role: str, title: str) -> Any:
    existing = os.company.get_person(org, person_id)
    if existing:
        return existing
    return os.create_person(org, name, f"{person_id}@demo.invalid", title=title, department="Agency", role=role, person_id=person_id)


def _project(os: Any, org: str, ws: str, owner: str, name: str, description: str, tags: list[str]) -> Any:
    row = _one(os, "SELECT * FROM projects WHERE organization_id=? AND workspace_id=? AND name=?", (org, ws, name))
    if row:
        return os.company.get_project(ws, row["id"])
    return os.create_project(org, ws, owner, name, description=description, priority="high", due_date="2026-09-30", budget=12000, tags=tags)


def _campaign(os: Any, org: str, ws: str, owner: str, project_id: str, name: str, platform: str) -> dict[str, Any]:
    row = _one(os, "SELECT * FROM campaigns WHERE organization_id=? AND workspace_id=? AND name=?", (org, ws, name))
    if row:
        return dict(row)
    item = os.agency_ops.create_campaign(org, ws, owner, name, "Qualified pipeline growth", platform, project_id=project_id, budget=4500, start_date="2026-08-01", end_date="2026-09-30")
    # Campaign lifecycle is intentionally represented as a local synthetic fixture.
    os.store.conn.execute("UPDATE campaigns SET status='active',updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), item["id"]))
    os.store.conn.commit()
    return item


def _ingest_evidence(os: Any, ws: str, actor: str, client: str) -> None:
    source_key = f"{SOURCE_PREFIX}{ws}"
    expected = (
        (client, "approved offer"),
        (client, "positioning"),
        (client, "success metric"),
        (client, "decision maker"),
    )
    fact_count = _one(
        os,
        "SELECT COUNT(*) FROM facts f JOIN sources s ON s.id=f.source_id WHERE f.workspace_id=? AND s.source_key=? AND f.subject=? AND f.predicate IN (?,?,?,?)",
        (ws, source_key, client, *(predicate for _, predicate in expected)),
    )[0]
    # Existing installations may have the original prose-only source. Add a
    # new version under the same stable key so the source is upgraded in place
    # without duplicating the canonical claims on subsequent runs.
    if fact_count < len(expected):
        content = (
            f"META: confidence=0.98\n"
            f"FACT: {client} | approved offer | A current, approved client offer\n"
            f"FACT: {client} | positioning | Evidence-backed, accessible growth partner\n"
            f"FACT: {client} | success metric | Qualified pipeline and weekly conversion quality\n"
            f"FACT: {client} | decision maker | Client sponsor and agency delivery director\n"
            f"REL: {client} | uses | demo_fixture evidence brief\n"
        )
        os.ingest_text(ws, actor, source_key, content, f"fixture://{source_key}/structured", observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    preference = f"{client} prefers concise, accessible creative with every claim tied to the approved evidence brief."
    if not _one(os, "SELECT id FROM memories WHERE workspace_id=? AND actor_id=? AND kind='preference' AND content=?", (ws, actor, preference)):
        os.remember(ws, actor, preference, kind="preference")


def _ensure_content(os: Any, org: str, ws: str, owner: str, project_id: str, title: str) -> dict[str, Any]:
    row = _one(os, "SELECT * FROM content_items WHERE organization_id=? AND workspace_id=? AND title=?", (org, ws, title))
    if row:
        return dict(row)
    item = os.agency_ops.create_content(org, ws, owner, title, "Build qualified demand", "Operations and buying committee", hook="A clear next step", copy="Evidence-backed, concise message.", project_id=project_id, brain_context="demo_fixture")
    # Advance through the content pipeline to a measured, inspectable record.
    for stage in ("research", "brief", "script", "design", "review", "approved", "scheduled", "published", "measured"):
        item = os.agency_ops.advance_content(org, ws, owner, item["id"], stage)
    if not _one(os, "SELECT id FROM content_performance WHERE content_item_id=? AND source=?", (item["id"], "demo_fixture")):
        os.agency_ops.record_content_performance(org, ws, owner, item["id"], "demo_fixture", impressions=18000, engagements=920, clicks=410, conversions=38)
    return item


def _ensure_work(os: Any, ws: str, actor: str, work_id: str, title: str, target: str, needed_by: str) -> None:
    item = os.capture_work(ws, actor, title, f"Deliver {title} with evidence and handoff notes.", "Account Lead", needed_by=needed_by, decision_maker="Delivery Director", work_item_id=work_id)
    if item.status != "captured":
        return
    if target == "captured":
        return
    item = os.assign_work(ws, actor, item.id, actor)
    if target == "assigned":
        return
    item = os.start_work(ws, actor, item.id)
    if target == "in_progress":
        return
    os.mark_dod(ws, actor, item.id, {"mobile_responsive": True, "assets_exported": True, "creative_safe_zone": True, "copy_spellchecked": True, "handoff_notes": True})
    item = os.submit_review(ws, actor, item.id)
    if target == "review":
        return
    item = os.close_review(ws, actor, item.id, True, "Approved for client handoff")
    if target == "client_review":
        return
    os.ship_work(ws, actor, item.id, "Shipped demo fixture deliverable")


def _ensure_review_variants(os: Any, org: str, ws: str, owner: str, project: Any, brand: str) -> None:
    variants = (
        (f"{brand} channel plan", "report", "internal", None),
        (f"{brand} client-ready creative", "ad_creative", "client", "revision_requested"),
    )
    for title, kind, review_kind, decision in variants:
        row = _one(os, "SELECT * FROM deliverables WHERE workspace_id=? AND title=?", (ws, title))
        deliverable = dict(row) if row else os.create_deliverable(org, ws, owner, project.id, title, kind).to_dict()
        review = _one(os, "SELECT * FROM reviews WHERE deliverable_id=?", (deliverable["id"],))
        if review:
            continue
        opened = os.open_review(org, ws, owner, deliverable["id"], review_kind, reviewer_person_id=owner)
        if decision:
            os.decide_review(org, ws, owner, opened.id, decision)


def seed_realistic_agency_demo(os: Any, organization_id: str | None = None, owner_person_id: str | None = None) -> dict[str, Any]:
    """Seed the realistic agency scenario and return a compact inventory."""
    org_id = organization_id or ORG_ID
    owner_id = owner_person_id or "person_realistic_owner"
    org = os.create_organization("Auremgrid Realistic Agency" if org_id == ORG_ID else "Auremgrid Demo Agency", org_id)
    agency = _person(os, org_id, owner_id, "Mara Chen" if owner_id == "person_realistic_owner" else "Demo Owner", "owner", "Managing Director")
    operator = _person(os, org_id, "person_realistic_operator", "Jon Bell", "admin", "Delivery Director")
    strategist = _person(os, org_id, "person_realistic_strategist", "Priya Shah", "member", "Strategy Lead")
    clients = (
        ("ws_prime_clinics", "Prime Clinics", "Prime Clinics", "person_client_prime"),
        ("ws_base_ryder", "BASE Ryder", "BASE Ryder", "person_client_base"),
        ("ws_evolve", "Evolve", "Evolve", "person_client_evolve"),
    )
    workspaces: list[str] = []
    projects: list[str] = []
    for ws_id, ws_name, brand, client_id in clients:
        ws = os.create_organization_workspace(org_id, ws_name, "client", ws_id)
        workspaces.append(ws.id)
        client = _person(os, org_id, client_id, f"{brand} Client", "client", "Client Sponsor")
        for person, role in ((agency, "admin"), (operator, "operator"), (strategist, "operator"), (client, "client")):
            if not os.company.workspace_membership(ws.id, person.id):
                os.add_person_to_workspace(org_id, ws.id, person.id, role)
        actor_id = f"act_{ws_id}"
        actor = os.create_actor(ws.id, f"{brand} Account Lead", "admin", actor_id)
        _ingest_evidence(os, ws.id, actor.id, brand)
        p1 = _project(os, org_id, ws.id, operator.id, f"{brand} Growth Foundation", f"90-day growth foundation for {brand}.", ["demo_fixture", "strategy"])
        p2 = _project(os, org_id, ws.id, strategist.id, f"{brand} Demand Campaign", f"Integrated demand campaign for {brand}.", ["demo_fixture", "campaign"])
        projects.extend([p1.id, p2.id])
        # One linked work/review/deliverable chain per client.
        work_id = f"work_{ws_id}_brief"
        work = os.capture_work(ws.id, actor.id, f"{brand} campaign brief", f"Approve the evidence-backed campaign brief for {brand}.", "Client Sponsor", needed_by="2026-09-12", decision_maker=operator.name, work_item_id=work_id)
        if work.status == "captured":
            work = os.assign_work(ws.id, actor.id, work.id, actor.id)
            work = os.start_work(ws.id, actor.id, work.id)
            os.mark_dod(ws.id, actor.id, work.id, {"mobile_responsive": True, "assets_exported": True, "creative_safe_zone": True, "copy_spellchecked": True, "handoff_notes": True})
            os.submit_review(ws.id, actor.id, work.id)
            os.close_review(ws.id, actor.id, work.id, True, "Approved for client handoff")
        if not _one(os, "SELECT id FROM deliverables WHERE workspace_id=? AND title=?", (ws.id, f"{brand} campaign brief")):
            deliverable = os.create_deliverable(org_id, ws.id, operator.id, p1.id, f"{brand} campaign brief", "report", work_item_id=work.id)
            review = os.open_review(org_id, ws.id, operator.id, deliverable.id, "internal", reviewer_person_id=str(operator.id))
            os.decide_review(org_id, ws.id, operator.id, review.id, "approved")
        # Keep the operating portfolio differentiated: captured, review,
        # shipped, and client-review work are all visible at once.
        _ensure_work(os, ws.id, actor.id, f"work_{ws_id}_discovery", f"{brand} discovery notes", "captured", "2026-08-18")
        _ensure_work(os, ws.id, actor.id, f"work_{ws_id}_audience", f"{brand} audience segmentation", "review", "2026-09-05")
        _ensure_work(os, ws.id, actor.id, f"work_{ws_id}_handoff", f"{brand} launch handoff", "shipped", "2026-08-28")
        _ensure_review_variants(os, org_id, ws.id, operator.id, p1, brand)
        campaign = _campaign(os, org_id, ws.id, operator.id, p2.id, f"{brand} Q3 Demand", "LinkedIn")
        if not _one(os, "SELECT id FROM campaign_metric_snapshots WHERE campaign_id=? AND source=?", (campaign["id"], "demo_fixture")):
            for spend, leads, impressions, clicks, revenue in ((900, 22, 21000, 620, 4800), (1300, 31, 27500, 790, 6900), (1600, 28, 26000, 710, 6100)):
                os.agency_ops.record_campaign_metrics(org_id, ws.id, operator.id, campaign["id"], "demo_fixture", spend=spend, revenue=revenue, leads=leads, impressions=impressions, clicks=clicks)
        creatives = []
        for title, fmt in ((f"{brand} proof-led carousel", "carousel"), (f"{brand} founder story", "video")):
            row = _one(os, "SELECT * FROM creative_assets WHERE workspace_id=? AND title=?", (ws.id, title))
            asset = dict(row) if row else os.agency_ops.create_creative(org_id, ws.id, strategist.id, title, fmt, project_id=p2.id, campaign_id=campaign["id"], platform="LinkedIn", style_tags=["demo_fixture", "accessible"])
            creatives.append(asset)
            if not _one(os, "SELECT id FROM creative_performance WHERE asset_id=? AND source=?", (asset["id"], "demo_fixture")):
                os.agency_ops.record_creative_performance(org_id, ws.id, strategist.id, asset["id"], "demo_fixture", campaign_id=campaign["id"], impressions=18000, clicks=610, conversions=32, spend=500, revenue=3400)
                os.agency_ops.record_creative_performance(org_id, ws.id, strategist.id, asset["id"], "demo_fixture", campaign_id=campaign["id"], impressions=22000, clicks=880, conversions=49, spend=700, revenue=5200)
        _ensure_content(os, org_id, ws.id, strategist.id, p2.id, f"{brand} proof-led launch post")
        if not _one(os, "SELECT id FROM risks WHERE workspace_id=? AND evidence LIKE ?", (ws.id, "%demo_fixture%")):
            os.client_ops.create_risk(org_id, ws.id, operator.id, "performance", "medium", 0.35, "Lead quality may soften if audience targeting broadens.", "demo_fixture: early lead quality is directional", "Keep weekly quality review and narrow audience if conversion rate drops.", project_id=p2.id)
            os.client_ops.create_risk(org_id, ws.id, operator.id, "delivery", "high", 0.65, "Launch handoff is near due and depends on final client feedback.", "demo_fixture: handoff work is due soon", "Hold a named client review slot and escalate blockers within 24 hours.", project_id=p1.id)
            resolved = os.client_ops.create_risk(org_id, ws.id, operator.id, "relationship", "low", 0.15, "No current relationship signal is concerning.", "demo_fixture: sponsor touchpoint is current", "Keep the next sponsor check-in on the calendar.", project_id=p1.id)
            os.store.conn.execute("UPDATE risks SET status='resolved',resolution='Closed in demo fixture',resolved_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), resolved.id))
            os.store.conn.commit()
        if not _one(os, "SELECT id FROM decisions WHERE workspace_id=? AND tags LIKE ?", (ws.id, "%demo_fixture%")):
            os.create_decision(org_id, operator.id, f"Keep {brand} proof-led creative as the control", "It is the clearest current evidence-backed message and has the strongest qualified response.", workspace_id=ws.id, project_id=p2.id, evidence="demo_fixture campaign and creative performance", tags=["demo_fixture", "creative"])
        if not _one(os, "SELECT id FROM availability WHERE organization_id=? AND person_id=? AND week_start=?", (org_id, strategist.id, "2026-08-24")):
            os.agency_ops.set_availability(org_id, strategist.id, "2026-08-24", 32)
            os.agency_ops.calculate_capacity(org_id, operator.id, strategist.id, "2026-08-24", 24, 12)
        # Generation is idempotent at scenario level: only seed insights once.
        if _one(os, "SELECT id FROM performance_insights WHERE workspace_id=?", (ws.id,)) is None:
            os.performance.generate_insights(org_id, ws.id, operator.id)
    os.store.conn.commit()
    return {"organization_id": org_id, "workspaces": workspaces, "projects": projects, "clients": [item[2] for item in clients], "fixture": "demo_fixture", "finance": "not_connected"}

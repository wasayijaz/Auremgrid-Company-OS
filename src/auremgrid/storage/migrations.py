from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS = (
    Migration(
        2,
        "organization_and_delivery_core",
        """
        CREATE TABLE IF NOT EXISTS organizations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS people (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT,
            title TEXT,
            department TEXT,
            manager_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(manager_id) REFERENCES people(id),
            UNIQUE(organization_id, email)
        );
        CREATE TABLE IF NOT EXISTS organization_memberships (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            person_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(person_id) REFERENCES people(id),
            UNIQUE(organization_id, person_id)
        );
        CREATE TABLE IF NOT EXISTS workspace_memberships (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            person_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(person_id) REFERENCES people(id),
            UNIQUE(workspace_id, person_id)
        );
        CREATE TABLE IF NOT EXISTS workspace_organization (
            workspace_id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('internal', 'client')),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_workspace_org ON workspace_organization(organization_id, kind);
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            owner_person_id TEXT,
            status TEXT NOT NULL,
            priority TEXT NOT NULL,
            start_date TEXT,
            due_date TEXT,
            budget REAL,
            tags TEXT NOT NULL,
            health TEXT NOT NULL,
            progress REAL NOT NULL CHECK(progress >= 0 AND progress <= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(owner_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_projects_workspace ON projects(workspace_id, status);
        CREATE TABLE IF NOT EXISTS deliverables (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            work_item_id TEXT,
            title TEXT NOT NULL,
            type TEXT NOT NULL,
            owner_person_id TEXT,
            current_version INTEGER NOT NULL,
            approval_status TEXT NOT NULL,
            preview_url TEXT,
            final_url TEXT,
            reviewer_person_id TEXT,
            client_approver_contact_id TEXT,
            revision_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            shipped_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(work_item_id) REFERENCES work_items(id)
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            deliverable_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('internal', 'client')),
            status TEXT NOT NULL,
            reviewer_person_id TEXT,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            decision TEXT,
            FOREIGN KEY(deliverable_id) REFERENCES deliverables(id),
            FOREIGN KEY(reviewer_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_reviews_queue ON reviews(workspace_id, status, opened_at);
        CREATE TABLE IF NOT EXISTS review_comments (
            id TEXT PRIMARY KEY,
            review_id TEXT NOT NULL,
            author_person_id TEXT NOT NULL,
            body TEXT NOT NULL,
            timestamp_seconds REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(review_id) REFERENCES reviews(id),
            FOREIGN KEY(author_person_id) REFERENCES people(id)
        );
        CREATE TABLE IF NOT EXISTS decisions (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            project_id TEXT,
            campaign_id TEXT,
            statement TEXT NOT NULL,
            rationale TEXT NOT NULL,
            decided_by_person_id TEXT NOT NULL,
            participant_person_ids TEXT NOT NULL,
            source_id TEXT,
            source_locator TEXT,
            evidence TEXT NOT NULL,
            created_at TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            effective_until TEXT,
            superseded_by TEXT,
            tags TEXT NOT NULL,
            affected_entities TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(decided_by_person_id) REFERENCES people(id),
            FOREIGN KEY(source_id) REFERENCES sources(id),
            FOREIGN KEY(superseded_by) REFERENCES decisions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_decisions_temporal ON decisions(organization_id, workspace_id, effective_from, effective_until);
        """,
    ),
    Migration(
        3,
        "client_operations",
        """
        CREATE TABLE IF NOT EXISTS contacts (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            name TEXT NOT NULL, company TEXT NOT NULL, role TEXT NOT NULL, influence TEXT NOT NULL,
            decision_power TEXT NOT NULL, communication_frequency TEXT, preferences TEXT NOT NULL,
            last_contact_at TEXT, created_at TEXT NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE TABLE IF NOT EXISTS relationships (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            from_contact_id TEXT NOT NULL, to_contact_id TEXT NOT NULL, kind TEXT NOT NULL,
            strength REAL NOT NULL, evidence TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(from_contact_id) REFERENCES contacts(id), FOREIGN KEY(to_contact_id) REFERENCES contacts(id)
        );
        CREATE TABLE IF NOT EXISTS sentiment_snapshots (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            contact_id TEXT, score REAL NOT NULL, label TEXT NOT NULL, evidence TEXT NOT NULL,
            calculated_at TEXT NOT NULL, FOREIGN KEY(contact_id) REFERENCES contacts(id)
        );
        CREATE TABLE IF NOT EXISTS client_health_snapshots (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            overall REAL NOT NULL, relationship REAL NOT NULL, delivery REAL NOT NULL,
            performance REAL, finance REAL, communication REAL NOT NULL, scope REAL NOT NULL,
            sentiment REAL, contributing_signals TEXT NOT NULL, explanation TEXT NOT NULL,
            previous_score REAL, trend TEXT NOT NULL, calculated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_health_workspace_time ON client_health_snapshots(workspace_id, calculated_at DESC);
        CREATE TABLE IF NOT EXISTS risks (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            project_id TEXT, type TEXT NOT NULL, severity TEXT NOT NULL, probability REAL NOT NULL,
            impact TEXT NOT NULL, owner_person_id TEXT, detected_at TEXT NOT NULL, status TEXT NOT NULL,
            evidence TEXT NOT NULL, recommended_action TEXT NOT NULL, resolution TEXT, resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_risks_open ON risks(workspace_id, status, severity);
        CREATE TABLE IF NOT EXISTS opportunities (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            type TEXT NOT NULL, estimated_value REAL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
            recommendation TEXT NOT NULL, owner_person_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contracts (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            kind TEXT NOT NULL, billing_model TEXT NOT NULL, value REAL, currency TEXT NOT NULL,
            start_date TEXT NOT NULL, end_date TEXT, renewal_date TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scope_allowances (
            id TEXT PRIMARY KEY, contract_id TEXT NOT NULL, service_category TEXT NOT NULL,
            period TEXT NOT NULL, included_quantity REAL, included_hours REAL, revision_limit INTEGER,
            FOREIGN KEY(contract_id) REFERENCES contracts(id)
        );
        CREATE TABLE IF NOT EXISTS scope_usage (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            contract_id TEXT NOT NULL, allowance_id TEXT NOT NULL, period_start TEXT NOT NULL,
            delivered_quantity REAL NOT NULL, in_review_quantity REAL NOT NULL, requested_quantity REAL NOT NULL,
            used_hours REAL NOT NULL, calculated_at TEXT NOT NULL,
            FOREIGN KEY(contract_id) REFERENCES contracts(id), FOREIGN KEY(allowance_id) REFERENCES scope_allowances(id)
        );
        CREATE TABLE IF NOT EXISTS meetings (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            title TEXT NOT NULL, occurred_at TEXT NOT NULL, summary TEXT NOT NULL, sentiment REAL,
            source TEXT NOT NULL, recording_url TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meeting_participants (
            meeting_id TEXT NOT NULL, participant_type TEXT NOT NULL, participant_id TEXT NOT NULL,
            PRIMARY KEY(meeting_id,participant_type,participant_id), FOREIGN KEY(meeting_id) REFERENCES meetings(id)
        );
        CREATE TABLE IF NOT EXISTS transcripts (
            id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, content TEXT NOT NULL, source_locator TEXT,
            content_hash TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(meeting_id) REFERENCES meetings(id)
        );
        CREATE TABLE IF NOT EXISTS meeting_outputs (
            id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, kind TEXT NOT NULL, statement TEXT NOT NULL,
            confidence REAL NOT NULL, status TEXT NOT NULL, linked_entity_type TEXT, linked_entity_id TEXT,
            created_at TEXT NOT NULL, FOREIGN KEY(meeting_id) REFERENCES meetings(id)
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            source TEXT NOT NULL, channel TEXT NOT NULL, external_thread_id TEXT, subject TEXT,
            linked_work_item_id TEXT, linked_decision_id TEXT, linked_risk_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS communication_participants (
            conversation_id TEXT NOT NULL, participant_type TEXT NOT NULL, participant_id TEXT NOT NULL,
            role TEXT NOT NULL, PRIMARY KEY(conversation_id,participant_type,participant_id,role),
            FOREIGN KEY(conversation_id) REFERENCES conversations(id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, sender_type TEXT NOT NULL, sender_id TEXT NOT NULL,
            body TEXT NOT NULL, sent_at TEXT NOT NULL, reply_to_id TEXT, sentiment REAL,
            requires_reply INTEGER NOT NULL, replied_at TEXT, important INTEGER NOT NULL, source_locator TEXT,
            FOREIGN KEY(conversation_id) REFERENCES conversations(id), FOREIGN KEY(reply_to_id) REFERENCES messages(id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_unanswered ON messages(requires_reply, replied_at, sent_at);
        CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            type TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT, evidence TEXT NOT NULL,
            confidence REAL NOT NULL, classification TEXT, status TEXT NOT NULL, routed_to TEXT,
            created_at TEXT NOT NULL, resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_signals_inbox ON signals(workspace_id, status, created_at);
        """,
    ),
    Migration(
        4,
        "agency_systems",
        """
        CREATE TABLE IF NOT EXISTS roles (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS departments (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL, lead_person_id TEXT);
        CREATE TABLE IF NOT EXISTS skills (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS person_skills (person_id TEXT NOT NULL, skill_id TEXT NOT NULL, level INTEGER NOT NULL, PRIMARY KEY(person_id,skill_id));
        CREATE TABLE IF NOT EXISTS availability (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, person_id TEXT NOT NULL, week_start TEXT NOT NULL, available_hours REAL NOT NULL, UNIQUE(person_id,week_start));
        CREATE TABLE IF NOT EXISTS leave_records (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, person_id TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, hours REAL NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS capacity_snapshots (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, person_id TEXT NOT NULL, week_start TEXT NOT NULL,
            available_hours REAL NOT NULL, estimated_assigned_hours REAL NOT NULL, booked_hours REAL NOT NULL,
            remaining_hours REAL NOT NULL, overloaded INTEGER NOT NULL, calculated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS utilization_snapshots (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, person_id TEXT NOT NULL, period_start TEXT NOT NULL,
            billable_hours REAL NOT NULL, available_hours REAL NOT NULL, utilization REAL NOT NULL, calculated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS campaigns (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, project_id TEXT,
            name TEXT NOT NULL, objective TEXT NOT NULL, platform TEXT NOT NULL, budget REAL, currency TEXT NOT NULL,
            start_date TEXT, end_date TEXT, status TEXT NOT NULL, owner_person_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, name TEXT NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ad_accounts (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, platform TEXT NOT NULL, external_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ad_sets (id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, external_id TEXT, name TEXT NOT NULL, budget REAL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ads (id TEXT PRIMARY KEY, ad_set_id TEXT NOT NULL, creative_asset_id TEXT, external_id TEXT, name TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS campaign_metric_snapshots (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
            captured_at TEXT NOT NULL, spend REAL, revenue REAL, leads REAL, impressions REAL, clicks REAL,
            cpl REAL, cac REAL, ctr REAL, cvr REAL, roas REAL, source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attribution_snapshots (id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, captured_at TEXT NOT NULL, model TEXT NOT NULL, payload TEXT NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS campaign_anomalies (id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, metric TEXT NOT NULL, severity TEXT NOT NULL, explanation TEXT NOT NULL, evidence TEXT NOT NULL, detected_at TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS creative_assets (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, project_id TEXT,
            campaign_id TEXT, title TEXT NOT NULL, platform TEXT, format TEXT NOT NULL, dimensions TEXT,
            creator_person_id TEXT, reviewer_person_id TEXT, approval_state TEXT NOT NULL, source_url TEXT,
            final_url TEXT, thumbnail_url TEXT, revision_count INTEGER NOT NULL, style_tags TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS creative_versions (id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, version INTEGER NOT NULL, file_url TEXT, notes TEXT NOT NULL, created_by_person_id TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(asset_id,version));
        CREATE TABLE IF NOT EXISTS creative_tags (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL, UNIQUE(organization_id,name));
        CREATE TABLE IF NOT EXISTS creative_asset_tags (asset_id TEXT NOT NULL, tag_id TEXT NOT NULL, PRIMARY KEY(asset_id,tag_id));
        CREATE TABLE IF NOT EXISTS creative_performance (id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, campaign_id TEXT, captured_at TEXT NOT NULL, impressions REAL, clicks REAL, conversions REAL, spend REAL, revenue REAL, ctr REAL, cvr REAL, roas REAL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS content_channels (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, platform TEXT NOT NULL, account_name TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS content_items (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, project_id TEXT,
            channel_id TEXT, title TEXT NOT NULL, stage TEXT NOT NULL, objective TEXT NOT NULL, audience TEXT NOT NULL,
            hook TEXT NOT NULL, copy TEXT NOT NULL, creative_asset_id TEXT, references_json TEXT NOT NULL,
            brain_context TEXT NOT NULL, publish_at TEXT, published_at TEXT, parent_content_id TEXT, owner_person_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS content_performance (id TEXT PRIMARY KEY, content_item_id TEXT NOT NULL, captured_at TEXT NOT NULL, impressions REAL, engagements REAL, clicks REAL, conversions REAL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS finance_connections (organization_id TEXT PRIMARY KEY, status TEXT NOT NULL, provider TEXT, last_sync_at TEXT, last_error TEXT);
        CREATE TABLE IF NOT EXISTS revenues (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, project_id TEXT, amount REAL NOT NULL, currency TEXT NOT NULL, kind TEXT NOT NULL, recognized_at TEXT NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS invoices (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, external_id TEXT, amount REAL NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, issued_at TEXT NOT NULL, due_at TEXT NOT NULL, paid_at TEXT, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payments (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, invoice_id TEXT, amount REAL NOT NULL, currency TEXT NOT NULL, received_at TEXT NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS costs (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, amount REAL NOT NULL, currency TEXT NOT NULL, category TEXT NOT NULL, incurred_at TEXT NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS budgets (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, project_id TEXT, amount REAL NOT NULL, currency TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS expenses (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, amount REAL NOT NULL, currency TEXT NOT NULL, category TEXT NOT NULL, incurred_at TEXT NOT NULL, description TEXT NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS software_costs (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, vendor TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL, period_start TEXT NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_usage_costs (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, agent_id TEXT, provider TEXT NOT NULL, model TEXT NOT NULL, tokens INTEGER NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL, occurred_at TEXT NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS client_economics (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, period_start TEXT NOT NULL, revenue REAL NOT NULL, labor_cost REAL NOT NULL, software_cost REAL NOT NULL, ai_cost REAL NOT NULL, other_cost REAL NOT NULL, gross_contribution REAL NOT NULL, margin REAL, calculated_at TEXT NOT NULL, UNIQUE(workspace_id,period_start));
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, recipient_person_id TEXT NOT NULL,
            priority REAL NOT NULL, reason TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT,
            workspace_id TEXT, actionable INTEGER NOT NULL, created_at TEXT NOT NULL, read_at TEXT, resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_attention ON notifications(recipient_person_id,resolved_at,priority DESC);
        CREATE TABLE IF NOT EXISTS approval_requests (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, requested_by_type TEXT NOT NULL,
            requested_by_id TEXT NOT NULL, requested_for TEXT NOT NULL, action_type TEXT NOT NULL, payload TEXT NOT NULL,
            reason TEXT NOT NULL, approver_person_id TEXT, policy TEXT NOT NULL, status TEXT NOT NULL,
            approved_at TEXT, rejected_at TEXT, comments TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """,
    ),
    Migration(
        5,
        "agents_automations_integrations_reports",
        """
        CREATE TABLE IF NOT EXISTS agent_roles (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, default_tools TEXT NOT NULL, default_write_permissions TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL, role_id TEXT NOT NULL,
            model TEXT NOT NULL, tools TEXT NOT NULL, allowed_workspace_ids TEXT NOT NULL, memory_access TEXT NOT NULL,
            write_permissions TEXT NOT NULL, status TEXT NOT NULL, current_task_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_tasks (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, agent_id TEXT NOT NULL, title TEXT NOT NULL, instructions TEXT NOT NULL, priority INTEGER NOT NULL, status TEXT NOT NULL, approval_request_id TEXT, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT);
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, agent_id TEXT NOT NULL,
            task_id TEXT, status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
            runtime_ms INTEGER, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
            cost REAL, error_id TEXT, output_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(organization_id,status,started_at DESC);
        CREATE TABLE IF NOT EXISTS tool_calls (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_name TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, result_preview TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS run_traces (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, metadata TEXT NOT NULL, recorded_at TEXT NOT NULL, UNIQUE(run_id,sequence));
        CREATE TABLE IF NOT EXISTS run_outputs (id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, content TEXT NOT NULL, source_refs TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS run_errors (id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, message TEXT NOT NULL, detail TEXT NOT NULL, retryable INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_queue_items (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, agent_id TEXT NOT NULL, task_id TEXT NOT NULL, priority INTEGER NOT NULL, status TEXT NOT NULL, enqueued_at TEXT NOT NULL, claimed_at TEXT, UNIQUE(task_id));
        CREATE TABLE IF NOT EXISTS automations (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL, approval_policy TEXT NOT NULL, created_by_person_id TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_triggers (id TEXT PRIMARY KEY, automation_id TEXT NOT NULL, type TEXT NOT NULL, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_conditions (id TEXT PRIMARY KEY, automation_id TEXT NOT NULL, field TEXT NOT NULL, operator TEXT NOT NULL, value TEXT NOT NULL, sequence INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_actions (id TEXT PRIMARY KEY, automation_id TEXT NOT NULL, type TEXT NOT NULL, config TEXT NOT NULL, sequence INTEGER NOT NULL, one_way INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_runs (id TEXT PRIMARY KEY, automation_id TEXT NOT NULL, trigger_type TEXT NOT NULL, trigger_payload TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, approval_request_id TEXT, output TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_errors (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, action_id TEXT, message TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS integrations (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
            workspace_mappings TEXT NOT NULL, permissions TEXT NOT NULL, sync_cursor TEXT,
            last_sync_at TEXT, last_error TEXT, object_count INTEGER NOT NULL, health TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(organization_id,source)
        );
        CREATE TABLE IF NOT EXISTS sync_runs (id TEXT PRIMARY KEY, integration_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, cursor_before TEXT, cursor_after TEXT, object_count INTEGER NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS report_runs (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, type TEXT NOT NULL, requested_by_person_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, citations TEXT NOT NULL, generated_at TEXT NOT NULL);
        """,
    ),
    Migration(
        6,
        "brain_maturity",
        """
        CREATE TABLE IF NOT EXISTS entities (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, canonical_name TEXT NOT NULL, type TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(organization_id,workspace_id,canonical_name,type));
        CREATE TABLE IF NOT EXISTS entity_aliases (id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, alias TEXT NOT NULL, normalized_alias TEXT NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL, source_id TEXT, created_at TEXT NOT NULL, UNIQUE(entity_id,normalized_alias));
        CREATE INDEX IF NOT EXISTS idx_entity_alias_lookup ON entity_aliases(normalized_alias,status);
        CREATE TABLE IF NOT EXISTS entity_merge_history (id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, source_entity_id TEXT NOT NULL, target_entity_id TEXT NOT NULL, decided_by_person_id TEXT NOT NULL, confidence REAL NOT NULL, reason TEXT NOT NULL, merged_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_proposals (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, kind TEXT NOT NULL,
            proposed_by_type TEXT NOT NULL, proposed_by_id TEXT NOT NULL, content TEXT NOT NULL,
            structured_payload TEXT NOT NULL, source_id TEXT, evidence TEXT NOT NULL, confidence REAL NOT NULL,
            status TEXT NOT NULL, reviewed_by_person_id TEXT, reviewed_at TEXT, promoted_type TEXT, promoted_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_health_issues (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT, type TEXT NOT NULL,
            severity TEXT NOT NULL, entity_type TEXT, entity_id TEXT, explanation TEXT NOT NULL,
            evidence TEXT NOT NULL, status TEXT NOT NULL, detected_at TEXT NOT NULL, resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS projection_state (
            name TEXT NOT NULL, workspace_id TEXT NOT NULL, status TEXT NOT NULL, document_count INTEGER NOT NULL,
            fact_count INTEGER NOT NULL, last_rebuilt_at TEXT, last_error TEXT, PRIMARY KEY(name,workspace_id)
        );
        """,
    ),
    Migration(
        7,
        "expanded_work_system",
        """
        ALTER TABLE work_items ADD COLUMN project_id TEXT;
        ALTER TABLE work_items ADD COLUMN campaign_id TEXT;
        ALTER TABLE work_items ADD COLUMN parent_id TEXT;
        ALTER TABLE work_items ADD COLUMN owner_person_id TEXT;
        ALTER TABLE work_items ADD COLUMN assignee_person_id TEXT;
        ALTER TABLE work_items ADD COLUMN reviewer_person_id TEXT;
        ALTER TABLE work_items ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal';
        ALTER TABLE work_items ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE work_items ADD COLUMN estimate_hours REAL;
        ALTER TABLE work_items ADD COLUMN actual_effort_hours REAL NOT NULL DEFAULT 0;
        ALTER TABLE work_items ADD COLUMN start_date TEXT;
        ALTER TABLE work_items ADD COLUMN deadline TEXT;
        ALTER TABLE work_items ADD COLUMN blocking_reason TEXT;
        ALTER TABLE work_items ADD COLUMN brief TEXT NOT NULL DEFAULT '';
        ALTER TABLE work_items ADD COLUMN brain_context TEXT NOT NULL DEFAULT '';
        ALTER TABLE work_items ADD COLUMN financial_value REAL;
        CREATE INDEX IF NOT EXISTS idx_work_project ON work_items(workspace_id,project_id,status);
        CREATE INDEX IF NOT EXISTS idx_work_parent ON work_items(parent_id);
        CREATE TABLE IF NOT EXISTS work_watchers (work_item_id TEXT NOT NULL,person_id TEXT NOT NULL,PRIMARY KEY(work_item_id,person_id));
        CREATE TABLE IF NOT EXISTS work_dependencies (work_item_id TEXT NOT NULL,depends_on_work_item_id TEXT NOT NULL,kind TEXT NOT NULL,PRIMARY KEY(work_item_id,depends_on_work_item_id));
        CREATE TABLE IF NOT EXISTS work_files (id TEXT PRIMARY KEY,work_item_id TEXT NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL,source TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS work_links (id TEXT PRIMARY KEY,work_item_id TEXT NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS work_comments (id TEXT PRIMARY KEY,work_item_id TEXT NOT NULL,author_type TEXT NOT NULL,author_id TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,edited_at TEXT);
        CREATE TABLE IF NOT EXISTS work_versions (id TEXT PRIMARY KEY,work_item_id TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,created_by_type TEXT NOT NULL,created_by_id TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(work_item_id,version));
        CREATE TABLE IF NOT EXISTS time_entries (id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,workspace_id TEXT NOT NULL,work_item_id TEXT NOT NULL,person_id TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT,duration_hours REAL,notes TEXT NOT NULL,billable INTEGER NOT NULL);
        """,
    ),
    Migration(
        8,
        "promotion_sync_and_delivery_details",
        """
        CREATE TABLE IF NOT EXISTS canonical_knowledge (
            id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,workspace_id TEXT,kind TEXT NOT NULL,
            content TEXT NOT NULL,structured_payload TEXT NOT NULL,source_id TEXT,evidence TEXT NOT NULL,
            approved_by_person_id TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS initiatives (
            id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,workspace_id TEXT NOT NULL,project_id TEXT NOT NULL,
            name TEXT NOT NULL,description TEXT NOT NULL,status TEXT NOT NULL,owner_person_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deliverable_files (id TEXT PRIMARY KEY,deliverable_id TEXT NOT NULL,version INTEGER NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL,kind TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deliverable_versions (id TEXT PRIMARY KEY,deliverable_id TEXT NOT NULL,version INTEGER NOT NULL,notes TEXT NOT NULL,created_by_person_id TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(deliverable_id,version));
        CREATE TABLE IF NOT EXISTS ledger_audit (
            id TEXT PRIMARY KEY,organization_id TEXT,workspace_id TEXT,principal_type TEXT NOT NULL,principal_id TEXT NOT NULL,
            action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT,detail TEXT NOT NULL,recorded_at TEXT NOT NULL
        );
        """,
    ),
    Migration(
        9,
        "automatic_ledger_audit",
        """
        CREATE TRIGGER IF NOT EXISTS audit_projects_insert AFTER INSERT ON projects BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','project',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_work_insert AFTER INSERT ON work_items BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),(SELECT organization_id FROM workspace_organization WHERE workspace_id=NEW.workspace_id),NEW.workspace_id,'system','sqlite','create','work_item',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_work_update AFTER UPDATE ON work_items BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),(SELECT organization_id FROM workspace_organization WHERE workspace_id=NEW.workspace_id),NEW.workspace_id,'system','sqlite','update','work_item',NEW.id,'canonical update',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_deliverables_insert AFTER INSERT ON deliverables BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','deliverable',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_reviews_insert AFTER INSERT ON reviews BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','review',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_decisions_insert AFTER INSERT ON decisions BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','decision',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_signals_insert AFTER INSERT ON signals BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','signal',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_risks_insert AFTER INSERT ON risks BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','risk',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_opportunities_insert AFTER INSERT ON opportunities BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','opportunity',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_campaigns_insert AFTER INSERT ON campaigns BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','campaign',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_creative_insert AFTER INSERT ON creative_assets BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','creative_asset',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_content_insert AFTER INSERT ON content_items BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','content_item',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_approvals_insert AFTER INSERT ON approval_requests BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','sqlite','create','approval_request',NEW.id,'canonical insert',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_agent_runs_insert AFTER INSERT ON agent_runs BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'agent',NEW.agent_id,'create','agent_run',NEW.id,'run started',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_automation_runs_insert AFTER INSERT ON automation_runs BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),(SELECT organization_id FROM automations WHERE id=NEW.automation_id),NULL,'automation',NEW.automation_id,'create','automation_run',NEW.id,'run triggered',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_proposals_insert AFTER INSERT ON memory_proposals BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,NEW.proposed_by_type,NEW.proposed_by_id,'create','memory_proposal',NEW.id,'proposal created',CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_invoices_insert AFTER INSERT ON invoices BEGIN
          INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'system','finance_connector','create','invoice',NEW.id,'sourced finance insert',CURRENT_TIMESTAMP);
        END;
        """,
    ),
    Migration(
        10,
        "workflow_run_engine",
        """
        CREATE TABLE IF NOT EXISTS workflow_definitions (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            key TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(organization_id, key)
        );
        CREATE TABLE IF NOT EXISTS workflow_definition_versions (
            id TEXT PRIMARY KEY,
            definition_id TEXT NOT NULL,
            version TEXT NOT NULL,
            snapshot TEXT NOT NULL,
            created_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(definition_id) REFERENCES workflow_definitions(id),
            UNIQUE(definition_id, version)
        );
        CREATE TABLE IF NOT EXISTS workflow_definition_steps (
            id TEXT PRIMARY KEY,
            definition_version_id TEXT NOT NULL,
            step_key TEXT NOT NULL,
            name TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            assignee_wing TEXT NOT NULL,
            assignee_role TEXT NOT NULL,
            required_evidence TEXT NOT NULL,
            requires_approval INTEGER NOT NULL,
            handoff_contract TEXT NOT NULL,
            on_reject_step_key TEXT,
            FOREIGN KEY(definition_version_id) REFERENCES workflow_definition_versions(id),
            UNIQUE(definition_version_id, step_key)
        );
        CREATE TABLE IF NOT EXISTS workflow_definition_edges (
            id TEXT PRIMARY KEY,
            definition_version_id TEXT NOT NULL,
            from_step_key TEXT NOT NULL,
            to_step_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            FOREIGN KEY(definition_version_id) REFERENCES workflow_definition_versions(id)
        );

        CREATE TABLE IF NOT EXISTS workflow_runs (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            definition_id TEXT NOT NULL,
            definition_version_id TEXT NOT NULL,
            definition_key TEXT NOT NULL,
            definition_version TEXT NOT NULL,
            definition_name TEXT NOT NULL,
            template_snapshot TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','in_progress','waiting_approval','blocked','completed','cancelled')),
            created_by_person_id TEXT NOT NULL,
            idempotency_key TEXT,
            due_at TEXT,
            sla_minutes INTEGER,
            escalation_at TEXT,
            blocked_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            cancelled_at TEXT,
            version INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_idempotency
            ON workflow_runs(organization_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_workflow_runs_active
            ON workflow_runs(organization_id, workspace_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_workflow_runs_overdue
            ON workflow_runs(organization_id, status, due_at, escalation_at);

        CREATE TABLE IF NOT EXISTS workflow_stage_runs (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage_key TEXT NOT NULL,
            name TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','in_progress','waiting_approval','blocked','completed','cancelled')),
            assignee_wing TEXT NOT NULL,
            assignee_role TEXT NOT NULL,
            assignee_person_id TEXT,
            required_evidence TEXT NOT NULL,
            requires_approval INTEGER NOT NULL,
            handoff_to_wing TEXT,
            handoff_to_role TEXT,
            handoff_to_person_id TEXT,
            on_reject_stage_key TEXT,
            due_at TEXT,
            blocked_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            cancelled_at TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(run_id) REFERENCES workflow_runs(id),
            UNIQUE(run_id, stage_key)
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_stage_runs_run
            ON workflow_stage_runs(run_id, sequence);
        CREATE INDEX IF NOT EXISTS idx_workflow_stage_runs_status
            ON workflow_stage_runs(status, due_at);

        CREATE TABLE IF NOT EXISTS workflow_stage_dependencies (
            run_id TEXT NOT NULL,
            stage_run_id TEXT NOT NULL,
            depends_on_stage_run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(stage_run_id, depends_on_stage_run_id),
            FOREIGN KEY(run_id) REFERENCES workflow_runs(id),
            FOREIGN KEY(stage_run_id) REFERENCES workflow_stage_runs(id),
            FOREIGN KEY(depends_on_stage_run_id) REFERENCES workflow_stage_runs(id)
        );

        CREATE TABLE IF NOT EXISTS workflow_evidence (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage_run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            uri TEXT,
            text TEXT,
            metadata TEXT NOT NULL,
            object_type TEXT,
            object_id TEXT,
            locator TEXT,
            content_hash TEXT,
            submitted_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES workflow_runs(id),
            FOREIGN KEY(stage_run_id) REFERENCES workflow_stage_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_evidence_stage
            ON workflow_evidence(stage_run_id, created_at);

        CREATE TABLE IF NOT EXISTS workflow_approval_decisions (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage_run_id TEXT NOT NULL,
            approval_request_id TEXT,
            decision TEXT NOT NULL CHECK(decision IN ('approve','reject','request_changes')),
            approver_person_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES workflow_runs(id),
            FOREIGN KEY(stage_run_id) REFERENCES workflow_stage_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_approval_stage
            ON workflow_approval_decisions(stage_run_id, created_at);

        CREATE TABLE IF NOT EXISTS workflow_handoff_acknowledgements (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            from_stage_run_id TEXT NOT NULL,
            to_stage_run_id TEXT,
            acknowledged_by_person_id TEXT NOT NULL,
            from_wing TEXT NOT NULL,
            from_role TEXT NOT NULL,
            from_person_id TEXT,
            source_stage_version INTEGER NOT NULL,
            to_wing TEXT NOT NULL,
            to_role TEXT NOT NULL,
            to_person_id TEXT,
            artifact_contract TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES workflow_runs(id),
            FOREIGN KEY(from_stage_run_id) REFERENCES workflow_stage_runs(id),
            FOREIGN KEY(to_stage_run_id) REFERENCES workflow_stage_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_handoff_stage
            ON workflow_handoff_acknowledgements(from_stage_run_id, to_stage_run_id, created_at);

        CREATE TABLE IF NOT EXISTS workflow_transition_history (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage_run_id TEXT,
            actor_person_id TEXT NOT NULL,
            action TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            reason TEXT NOT NULL,
            metadata TEXT NOT NULL,
            idempotency_key TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES workflow_runs(id),
            FOREIGN KEY(stage_run_id) REFERENCES workflow_stage_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_history_run
            ON workflow_transition_history(run_id, created_at);

        CREATE TABLE IF NOT EXISTS workflow_idempotency_keys (
            organization_id TEXT NOT NULL,
            key TEXT NOT NULL,
            operation TEXT NOT NULL,
            result_type TEXT NOT NULL,
            result_id TEXT NOT NULL,
            response TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(organization_id, key, operation)
        );

        CREATE TRIGGER IF NOT EXISTS workflow_definition_version_no_update
        BEFORE UPDATE ON workflow_definition_versions
        BEGIN
            SELECT RAISE(ABORT, 'workflow definition versions are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_definition_version_no_delete
        BEFORE DELETE ON workflow_definition_versions
        BEGIN
            SELECT RAISE(ABORT, 'workflow definition versions are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_definition_step_no_update
        BEFORE UPDATE ON workflow_definition_steps
        BEGIN
            SELECT RAISE(ABORT, 'workflow definition steps are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_definition_step_no_delete
        BEFORE DELETE ON workflow_definition_steps
        BEGIN
            SELECT RAISE(ABORT, 'workflow definition steps are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_definition_edge_no_update
        BEFORE UPDATE ON workflow_definition_edges
        BEGIN
            SELECT RAISE(ABORT, 'workflow definition edges are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_definition_edge_no_delete
        BEFORE DELETE ON workflow_definition_edges
        BEGIN
            SELECT RAISE(ABORT, 'workflow definition edges are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_run_snapshot_no_update
        BEFORE UPDATE OF definition_id, definition_version_id, definition_key, definition_version,
            definition_name, template_snapshot ON workflow_runs
        BEGIN
            SELECT RAISE(ABORT, 'workflow run snapshots are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_stage_contract_no_update
        BEFORE UPDATE OF run_id, stage_key, name, sequence, assignee_wing, assignee_role,
            required_evidence, requires_approval, handoff_to_wing, handoff_to_role,
            on_reject_stage_key ON workflow_stage_runs
        BEGIN
            SELECT RAISE(ABORT, 'workflow stage contracts are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS workflow_evidence_no_update BEFORE UPDATE ON workflow_evidence
        BEGIN
            SELECT RAISE(ABORT, 'workflow evidence is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_evidence_no_delete BEFORE DELETE ON workflow_evidence
        BEGIN
            SELECT RAISE(ABORT, 'workflow evidence is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_approval_no_update BEFORE UPDATE ON workflow_approval_decisions
        BEGIN
            SELECT RAISE(ABORT, 'workflow approval decisions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_approval_no_delete BEFORE DELETE ON workflow_approval_decisions
        BEGIN
            SELECT RAISE(ABORT, 'workflow approval decisions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_handoff_no_update BEFORE UPDATE ON workflow_handoff_acknowledgements
        BEGIN
            SELECT RAISE(ABORT, 'workflow handoff acknowledgements are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_handoff_no_delete BEFORE DELETE ON workflow_handoff_acknowledgements
        BEGIN
            SELECT RAISE(ABORT, 'workflow handoff acknowledgements are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_history_no_update BEFORE UPDATE ON workflow_transition_history
        BEGIN
            SELECT RAISE(ABORT, 'workflow transition history is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS workflow_history_no_delete BEFORE DELETE ON workflow_transition_history
        BEGIN
            SELECT RAISE(ABORT, 'workflow transition history is immutable');
        END;
        """,
    ),
    Migration(
        11,
        "auth_jobs_and_outbox",
        """
        CREATE TABLE IF NOT EXISTS auth_principals (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            person_id TEXT,
            email TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','disabled','revoked')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(person_id) REFERENCES people(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_principals_person
            ON auth_principals(organization_id, person_id)
            WHERE person_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_principals_email
            ON auth_principals(organization_id, email)
            WHERE email IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_auth_principals_org_status
            ON auth_principals(organization_id, status);

        CREATE TABLE IF NOT EXISTS auth_sessions (
            id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            last_seen_at TEXT,
            FOREIGN KEY(principal_id) REFERENCES auth_principals(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_sessions_token_hash
            ON auth_sessions(token_hash);
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_principal_active
            ON auth_sessions(principal_id, revoked_at, expires_at);

        CREATE TABLE IF NOT EXISTS api_tokens (
            id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            scopes TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            last_used_at TEXT,
            FOREIGN KEY(principal_id) REFERENCES auth_principals(id),
            UNIQUE(principal_id, name)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_api_tokens_token_hash
            ON api_tokens(token_hash);
        CREATE INDEX IF NOT EXISTS idx_api_tokens_principal_active
            ON api_tokens(principal_id, revoked_at, expires_at);

        CREATE TABLE IF NOT EXISTS principal_actor_bindings (
            principal_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(principal_id, workspace_id),
            FOREIGN KEY(principal_id) REFERENCES auth_principals(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(actor_id) REFERENCES actors(id),
            UNIQUE(actor_id)
        );
        CREATE INDEX IF NOT EXISTS idx_principal_actor_bindings_actor
            ON principal_actor_bindings(actor_id);

        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS secret_bindings (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            integration_id TEXT,
            name TEXT NOT NULL,
            provider TEXT NOT NULL,
            reference TEXT NOT NULL,
            scopes TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','unverified','revoked')),
            last_verified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(integration_id) REFERENCES integrations(id),
            UNIQUE(organization_id, reference)
        );
        CREATE INDEX IF NOT EXISTS idx_secret_bindings_scope
            ON secret_bindings(organization_id, workspace_id, status);

        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            principal_id TEXT NOT NULL,
            type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','leased','running','succeeded','failed','retry_wait','dead_letter','cancelled')),
            priority INTEGER NOT NULL,
            attempts INTEGER NOT NULL,
            max_attempts INTEGER NOT NULL,
            available_at TEXT NOT NULL,
            lease_owner TEXT,
            lease_expires_at TEXT,
            progress REAL NOT NULL CHECK(progress >= 0 AND progress <= 1),
            idempotency_key TEXT,
            payload_hash TEXT NOT NULL,
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            cancelled_at TEXT,
            lease_token TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(principal_id) REFERENCES auth_principals(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
            ON jobs(organization_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_jobs_queue
            ON jobs(status, available_at, priority DESC, created_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_scope
            ON jobs(organization_id, workspace_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_lease
            ON jobs(status, lease_expires_at);

        CREATE TABLE IF NOT EXISTS job_events (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            actor TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_job_events_job
            ON job_events(job_id, created_at);

        CREATE TABLE IF NOT EXISTS outbox_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            idempotency_key TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending','published','failed')),
            attempts INTEGER NOT NULL,
            max_attempts INTEGER NOT NULL,
            next_attempt_at TEXT NOT NULL,
            lease_owner TEXT,
            lease_expires_at TEXT,
            lease_token TEXT,
            published_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_idempotency
            ON outbox_events(organization_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_outbox_claim
            ON outbox_events(status, next_attempt_at, lease_expires_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_outbox_scope
            ON outbox_events(organization_id, workspace_id, status, updated_at);

        CREATE TRIGGER IF NOT EXISTS job_events_no_update BEFORE UPDATE ON job_events
        BEGIN
            SELECT RAISE(ABORT, 'job events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS job_events_no_delete BEFORE DELETE ON job_events
        BEGIN
            SELECT RAISE(ABORT, 'job events are append-only');
        END;
        """,
    ),
    Migration(
        12,
        "connector_inbox_cursor_dedupe",
        """
        ALTER TABLE secret_bindings ADD COLUMN generation INTEGER NOT NULL DEFAULT 1;

        CREATE TABLE IF NOT EXISTS connector_cursors (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            cursor_type TEXT NOT NULL,
            cursor_value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            UNIQUE(organization_id, workspace_id, connector, account_key, cursor_type)
        );
        CREATE INDEX IF NOT EXISTS idx_connector_cursors_scope
            ON connector_cursors(organization_id, workspace_id, connector, account_key);

        CREATE TABLE IF NOT EXISTS connector_ingest_batches (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            cursor_type TEXT NOT NULL,
            cursor_before TEXT,
            cursor_version_before INTEGER NOT NULL DEFAULT 0,
            cursor_after TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','failed','rate_limited')),
            event_count INTEGER NOT NULL,
            rate_limit_retry_after_seconds INTEGER,
            rate_limit_reset_at TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE INDEX IF NOT EXISTS idx_connector_batches_scope
            ON connector_ingest_batches(organization_id, workspace_id, connector, account_key, created_at);

        CREATE TABLE IF NOT EXISTS connector_source_events (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            external_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source_key TEXT NOT NULL,
            locator TEXT NOT NULL,
            media_type TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload TEXT NOT NULL,
            observed_at TEXT,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','leased','ingested','skipped','failed','quarantined')),
            ingest_error TEXT,
            ingested_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at TEXT NOT NULL,
            lease_owner TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            quarantine_reason TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(batch_id) REFERENCES connector_ingest_batches(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE INDEX IF NOT EXISTS idx_connector_source_events_batch
            ON connector_source_events(batch_id, status);
        CREATE INDEX IF NOT EXISTS idx_connector_source_events_scope
            ON connector_source_events(organization_id, workspace_id, connector, status, received_at);

        CREATE TABLE IF NOT EXISTS connector_batch_events (
            batch_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(batch_id, event_id),
            FOREIGN KEY(batch_id) REFERENCES connector_ingest_batches(id),
            FOREIGN KEY(event_id) REFERENCES connector_source_events(id)
        );
        CREATE INDEX IF NOT EXISTS idx_connector_batch_events_event
            ON connector_batch_events(event_id);

        CREATE TABLE IF NOT EXISTS connector_dedupe_keys (
            organization_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            first_event_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(organization_id, connector, account_key, dedupe_key),
            FOREIGN KEY(first_event_id) REFERENCES connector_source_events(id)
        );

        CREATE TABLE IF NOT EXISTS connector_stream_locks (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            stream_key TEXT NOT NULL,
            job_id TEXT NOT NULL,
            mapping_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','released','cancelled','replaced')),
            lease_owner TEXT NOT NULL,
            reservation_token TEXT NOT NULL,
            reserved_at TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            released_at TEXT,
            cancelled_at TEXT,
            replaced_by_id TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(job_id) REFERENCES jobs(id),
            FOREIGN KEY(replaced_by_id) REFERENCES connector_stream_locks(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_connector_stream_locks_active
            ON connector_stream_locks(organization_id, workspace_id, connector, stream_key)
            WHERE status='active';
        CREATE INDEX IF NOT EXISTS idx_connector_stream_locks_job
            ON connector_stream_locks(job_id, status);

        CREATE TRIGGER IF NOT EXISTS connector_source_events_no_payload_update
        BEFORE UPDATE OF batch_id, organization_id, workspace_id, connector, account_key, dedupe_key,
            external_id, event_type, source_key, locator, media_type, content, content_hash, payload,
            observed_at, received_at ON connector_source_events
        BEGIN
            SELECT RAISE(ABORT, 'connector source event payloads are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS connector_source_events_no_delete BEFORE DELETE ON connector_source_events
        BEGIN
            SELECT RAISE(ABORT, 'connector source events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS connector_dedupe_no_update BEFORE UPDATE ON connector_dedupe_keys
        BEGIN
            SELECT RAISE(ABORT, 'connector dedupe keys are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS connector_dedupe_no_delete BEFORE DELETE ON connector_dedupe_keys
        BEGIN
            SELECT RAISE(ABORT, 'connector dedupe keys are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS connector_stream_lock_identity_no_update
        BEFORE UPDATE OF organization_id, workspace_id, connector, account_key, stream_key, job_id, mapping_hash
            ON connector_stream_locks
        BEGIN
            SELECT RAISE(ABORT, 'connector stream lock identity is immutable');
        END;

        ALTER TABLE integrations ADD COLUMN expected_account_id TEXT;
        ALTER TABLE integrations ADD COLUMN provider_account_id TEXT;
        ALTER TABLE integrations ADD COLUMN provider_account_name TEXT;
        ALTER TABLE integrations ADD COLUMN granted_permissions TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE integrations ADD COLUMN credential_verified_at TEXT;
        UPDATE integrations SET status='not_connected', health='never_synced', sync_cursor=NULL,
            last_error=NULL, provider_account_id=NULL, provider_account_name=NULL,
            granted_permissions='[]', credential_verified_at=NULL;
        """,
    ),
    Migration(
        13,
        "durable_provider_routes_and_evidence_lifecycle",
        """
        CREATE TABLE IF NOT EXISTS source_lifecycle_intervals (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_key TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            retired_at TEXT,
            effective_from TEXT NOT NULL,
            effective_until TEXT,
            activation_reason TEXT NOT NULL,
            retirement_reason TEXT,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(source_id) REFERENCES sources(id),
            CHECK(retired_at IS NULL OR retired_at >= activated_at),
            CHECK(effective_until IS NULL OR effective_until >= effective_from)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_source_lifecycle_current
            ON source_lifecycle_intervals(workspace_id, source_key)
            WHERE retired_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_source_lifecycle_as_of
            ON source_lifecycle_intervals(workspace_id, source_key, effective_from, effective_until, source_id);

        INSERT INTO source_lifecycle_intervals(
            id, workspace_id, source_id, source_key, activated_at, retired_at,
            effective_from, effective_until, activation_reason, retirement_reason
        )
        SELECT 'slife_' || source.id, source.workspace_id, source.id, source.source_key,
               source.recorded_at,
               (
                   SELECT MIN(newer.recorded_at) FROM sources newer
                   WHERE newer.workspace_id=source.workspace_id
                     AND newer.source_key=source.source_key
                     AND newer.version > source.version
               ),
               source.observed_at, NULL,
               'schema_13_backfill',
               CASE WHEN EXISTS (
                   SELECT 1 FROM sources newer
                   WHERE newer.workspace_id=source.workspace_id
                     AND newer.source_key=source.source_key
                     AND newer.version > source.version
               ) THEN 'schema_13_newer_version' ELSE NULL END
        FROM sources source
        WHERE NOT EXISTS (
            SELECT 1 FROM source_lifecycle_intervals lifecycle
            WHERE lifecycle.workspace_id=source.workspace_id
              AND lifecycle.source_id=source.id
        );

        CREATE TABLE IF NOT EXISTS provider_object_routes (
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            external_id TEXT NOT NULL,
            route_key TEXT NOT NULL,
            source_key TEXT NOT NULL,
            active_source_id TEXT,
            provider_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','retired')),
            activated_at TEXT,
            retired_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, connector, account_key, external_id, route_key),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(active_source_id) REFERENCES sources(id),
            CHECK(
                (status='active' AND active_source_id IS NOT NULL AND activated_at IS NOT NULL AND retired_at IS NULL)
                OR (status='retired' AND active_source_id IS NULL AND retired_at IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_provider_object_routes_source
            ON provider_object_routes(workspace_id, active_source_id, status);
        CREATE INDEX IF NOT EXISTS idx_provider_object_routes_object
            ON provider_object_routes(workspace_id, connector, account_key, external_id, status);

        CREATE TABLE IF NOT EXISTS provider_object_ancestry (
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            external_id TEXT NOT NULL,
            parent_ids TEXT NOT NULL,
            root_route_keys TEXT NOT NULL,
            is_container INTEGER NOT NULL CHECK(is_container IN (0,1)),
            provider_version TEXT NOT NULL,
            reconciliation_status TEXT NOT NULL
                CHECK(reconciliation_status IN ('resolved','required','descendants_required')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, connector, account_key, external_id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE INDEX IF NOT EXISTS idx_provider_object_ancestry_status
            ON provider_object_ancestry(workspace_id, connector, account_key, reconciliation_status);

        CREATE TABLE IF NOT EXISTS provider_route_mutation_staging (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            external_id TEXT NOT NULL,
            route_key TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_id TEXT,
            provider_version TEXT NOT NULL,
            operation TEXT NOT NULL CHECK(operation IN ('activate','retire')),
            occurred_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('staged','applied')),
            created_at TEXT NOT NULL,
            applied_at TEXT,
            FOREIGN KEY(batch_id) REFERENCES connector_ingest_batches(id),
            FOREIGN KEY(event_id) REFERENCES connector_source_events(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(source_id) REFERENCES sources(id),
            UNIQUE(event_id, route_key, provider_version, operation)
        );
        CREATE INDEX IF NOT EXISTS idx_provider_route_mutation_batch
            ON provider_route_mutation_staging(batch_id, status, created_at);

        CREATE TABLE IF NOT EXISTS provider_sync_tasks (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            stream_key TEXT NOT NULL,
            generation_id TEXT,
            task_type TEXT NOT NULL CHECK(task_type IN ('backfill','reconcile','descendants')),
            external_id TEXT,
            route_key TEXT,
            page_token TEXT,
            payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','leased','completed','cancelled')),
            lease_owner TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            UNIQUE(workspace_id, connector, account_key, stream_key, task_type, external_id, route_key, page_token, generation_id)
        );
        CREATE INDEX IF NOT EXISTS idx_provider_sync_tasks_claim
            ON provider_sync_tasks(workspace_id, connector, account_key, stream_key, status, created_at);

        CREATE TABLE IF NOT EXISTS provider_sync_generations (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            stream_key TEXT NOT NULL,
            route_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','completed','cancelled')),
            baseline_cursor TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_sync_generation_running
            ON provider_sync_generations(workspace_id, connector, account_key, stream_key, route_key)
            WHERE status='running';
        CREATE TABLE IF NOT EXISTS provider_object_generation_seen (
            generation_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            external_id TEXT NOT NULL,
            route_key TEXT NOT NULL,
            seen_at TEXT NOT NULL,
            PRIMARY KEY(generation_id, external_id, route_key),
            FOREIGN KEY(generation_id) REFERENCES provider_sync_generations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );

        CREATE TABLE IF NOT EXISTS provider_object_route_events (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            external_id TEXT NOT NULL,
            route_key TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_id TEXT,
            provider_version TEXT NOT NULL,
            operation TEXT NOT NULL CHECK(operation IN ('activate','retire')),
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(source_id) REFERENCES sources(id),
            UNIQUE(workspace_id, connector, account_key, external_id, route_key, provider_version, operation)
        );
        CREATE INDEX IF NOT EXISTS idx_provider_route_events_object
            ON provider_object_route_events(workspace_id, connector, account_key, external_id, occurred_at);

        CREATE TRIGGER IF NOT EXISTS source_lifecycle_identity_no_update
        BEFORE UPDATE OF id, workspace_id, source_id, source_key, activated_at, effective_from, activation_reason
            ON source_lifecycle_intervals
        BEGIN
            SELECT RAISE(ABORT, 'source lifecycle identity is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS source_lifecycle_no_delete
        BEFORE DELETE ON source_lifecycle_intervals
        BEGIN
            SELECT RAISE(ABORT, 'source lifecycle history is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS source_lifecycle_close_once
        BEFORE UPDATE OF retired_at, retirement_reason ON source_lifecycle_intervals
        WHEN OLD.retired_at IS NOT NULL OR NEW.retired_at IS NULL OR OLD.retirement_reason IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'source lifecycle retirement is immutable once closed');
        END;
        CREATE TRIGGER IF NOT EXISTS source_lifecycle_effective_close_once
        BEFORE UPDATE OF effective_until ON source_lifecycle_intervals
        WHEN OLD.effective_until IS NOT NULL OR NEW.effective_until IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'source lifecycle semantic retirement is immutable once closed');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_route_events_no_update
        BEFORE UPDATE ON provider_object_route_events
        BEGIN
            SELECT RAISE(ABORT, 'provider route events are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_route_events_no_delete
        BEFORE DELETE ON provider_object_route_events
        BEGIN
            SELECT RAISE(ABORT, 'provider route events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_routes_identity_no_update
        BEFORE UPDATE OF workspace_id,connector,account_key,external_id,route_key,source_key
            ON provider_object_routes
        BEGIN
            SELECT RAISE(ABORT, 'provider route identity is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_mutation_identity_no_update
        BEFORE UPDATE OF id,batch_id,event_id,workspace_id,connector,account_key,external_id,
            route_key,source_key,source_id,provider_version,operation,occurred_at,created_at
            ON provider_route_mutation_staging
        BEGIN
            SELECT RAISE(ABORT, 'provider mutation identity is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_sync_task_identity_no_update
        BEFORE UPDATE OF id,workspace_id,connector,account_key,stream_key,generation_id,task_type,
            external_id,route_key,page_token,payload,created_at
            ON provider_sync_tasks
        BEGIN
            SELECT RAISE(ABORT, 'provider sync task identity is immutable');
        END;
        """,
    ),
    Migration(
        14,
        "atomic_provider_sync_coordination",
        """
        ALTER TABLE provider_route_mutation_staging ADD COLUMN event_dedupe_key TEXT;
        UPDATE provider_route_mutation_staging
        SET event_dedupe_key=(
            SELECT dedupe_key FROM connector_source_events event
            WHERE event.id=provider_route_mutation_staging.event_id
        )
        WHERE event_dedupe_key IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_mutation_exact_event
            ON provider_route_mutation_staging(
                batch_id,event_dedupe_key,route_key,provider_version,operation
            );
        CREATE TRIGGER provider_mutation_event_key_required
        BEFORE INSERT ON provider_route_mutation_staging
        WHEN NEW.event_dedupe_key IS NULL OR NEW.event_dedupe_key=''
        BEGIN
            SELECT RAISE(ABORT, 'provider mutation requires exact event dedupe key');
        END;

        ALTER TABLE provider_sync_generations ADD COLUMN cancelled_at TEXT;

        DROP TRIGGER IF EXISTS provider_mutation_identity_no_update;
        CREATE TRIGGER provider_mutation_identity_no_update
        BEFORE UPDATE OF id,batch_id,event_id,event_dedupe_key,workspace_id,connector,account_key,
            external_id,route_key,source_key,provider_version,operation,occurred_at,created_at
            ON provider_route_mutation_staging
        BEGIN
            SELECT RAISE(ABORT, 'provider mutation identity is immutable');
        END;
        CREATE TRIGGER provider_mutation_source_bind_once
        BEFORE UPDATE OF source_id ON provider_route_mutation_staging
        WHEN OLD.source_id IS NOT NULL OR NEW.source_id IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'provider mutation source binding is one-way');
        END;
        CREATE TRIGGER provider_mutation_apply_once
        BEFORE UPDATE OF status,applied_at ON provider_route_mutation_staging
        WHEN OLD.status != 'staged' OR NEW.status != 'applied'
             OR OLD.applied_at IS NOT NULL OR NEW.applied_at IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'provider mutation application is one-way');
        END;
        """,
    ),
    Migration(
        15,
        "provider_sync_operation_identity_and_quarantine",
        """
        ALTER TABLE provider_sync_tasks ADD COLUMN operation_key TEXT;
        UPDATE provider_sync_tasks
        SET operation_key=COALESCE(
            NULLIF(json_extract(payload, '$.operation_key'), ''),
            'legacy:' || id
        )
        WHERE operation_key IS NULL OR operation_key='';
        CREATE TABLE provider_sync_tasks_v15 (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            account_key TEXT NOT NULL,
            stream_key TEXT NOT NULL,
            generation_id TEXT,
            task_type TEXT NOT NULL CHECK(task_type IN ('backfill','reconcile','descendants')),
            external_id TEXT,
            route_key TEXT,
            page_token TEXT,
            operation_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','leased','completed','cancelled')),
            lease_owner TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            UNIQUE(workspace_id,connector,account_key,stream_key,operation_key)
        );
        INSERT INTO provider_sync_tasks_v15(
            id,workspace_id,connector,account_key,stream_key,generation_id,task_type,
            external_id,route_key,page_token,operation_key,payload,status,lease_owner,
            lease_token,lease_expires_at,created_at,updated_at,completed_at
        ) SELECT id,workspace_id,connector,account_key,stream_key,generation_id,task_type,
            external_id,route_key,page_token,operation_key,payload,status,lease_owner,
            lease_token,lease_expires_at,created_at,updated_at,completed_at
            FROM provider_sync_tasks;
        DROP TABLE provider_sync_tasks;
        ALTER TABLE provider_sync_tasks_v15 RENAME TO provider_sync_tasks;
        CREATE INDEX IF NOT EXISTS idx_provider_sync_tasks_claim
            ON provider_sync_tasks(workspace_id,connector,account_key,stream_key,status,created_at);
        CREATE TRIGGER provider_sync_task_operation_key_required
        BEFORE INSERT ON provider_sync_tasks
        WHEN NEW.operation_key IS NULL OR NEW.operation_key=''
        BEGIN
            SELECT RAISE(ABORT, 'provider sync task requires operation key');
        END;
        DROP TRIGGER IF EXISTS provider_sync_task_identity_no_update;
        CREATE TRIGGER provider_sync_task_identity_no_update
        BEFORE UPDATE OF id,workspace_id,connector,account_key,stream_key,generation_id,
            task_type,external_id,route_key,page_token,operation_key,payload,created_at
            ON provider_sync_tasks
        BEGIN
            SELECT RAISE(ABORT, 'provider sync task identity is immutable');
        END;

        CREATE TABLE IF NOT EXISTS provider_sync_quarantines (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            integration_id TEXT NOT NULL,
            connector TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('open','resolved')),
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(integration_id) REFERENCES integrations(id),
            UNIQUE(integration_id,reason_code,evidence_digest,status)
        );
        CREATE INDEX IF NOT EXISTS idx_provider_sync_quarantine_scope
            ON provider_sync_quarantines(organization_id,integration_id,status,created_at);
        """,
    ),
    Migration(
        16,
        "durable_semantic_embedding_projection",
        """
        CREATE TABLE IF NOT EXISTS document_embedding_projection (
            workspace_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            provider_version TEXT NOT NULL,
            dimensions INTEGER NOT NULL CHECK(dimensions > 0),
            vector BLOB NOT NULL,
            health TEXT NOT NULL CHECK(health IN ('healthy','degraded')),
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, document_id, provider, model),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        CREATE INDEX IF NOT EXISTS idx_document_embedding_scope
            ON document_embedding_projection(workspace_id, provider, model, provider_version);
        """,
    ),
    Migration(
        17,
        "graph_projection_generations",
        """
        CREATE TABLE IF NOT EXISTS graph_projection_generations (
            workspace_id TEXT NOT NULL,
            generation TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('building','active','failed','retired')),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            failed_at TEXT,
            error_code TEXT,
            snapshot_watermark TEXT NOT NULL DEFAULT '',
            expected_prior_generation TEXT,
            ordinal INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(workspace_id,generation),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_projection_active
            ON graph_projection_generations(workspace_id) WHERE status='active';
        """,
    ),
    Migration(
        18,
        "entity_resolution_and_knowledge_state_events",
        """
        ALTER TABLE entities ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
        ALTER TABLE entities ADD COLUMN merged_into TEXT;
        ALTER TABLE entities ADD COLUMN updated_at TEXT;
        UPDATE entities SET updated_at=created_at WHERE updated_at IS NULL;
        ALTER TABLE entity_aliases ADD COLUMN reviewed_by_person_id TEXT;
        ALTER TABLE entity_aliases ADD COLUMN reviewed_at TEXT;
        ALTER TABLE entity_aliases ADD COLUMN evidence TEXT;
        ALTER TABLE entity_aliases ADD COLUMN retired_at TEXT;
        CREATE TABLE IF NOT EXISTS entity_resolution_proposals (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT,
            kind TEXT NOT NULL CHECK(kind IN ('alias','merge')),
            alias TEXT, source_entity_id TEXT, target_entity_id TEXT,
            candidate_entity_ids TEXT NOT NULL, score REAL NOT NULL,
            rationale TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
            proposed_by_person_id TEXT NOT NULL, reviewed_by_person_id TEXT,
            evidence_source_id TEXT, evidence TEXT NOT NULL,
            evidence_refs TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, reviewed_at TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE INDEX IF NOT EXISTS idx_entity_resolution_scope
            ON entity_resolution_proposals(organization_id,workspace_id,status,created_at);
        CREATE TABLE IF NOT EXISTS entity_alias_state_events (
            id TEXT PRIMARY KEY, alias_id TEXT NOT NULL, organization_id TEXT NOT NULL,
            workspace_id TEXT, state TEXT NOT NULL CHECK(state IN ('active','retired')),
            reason TEXT NOT NULL, actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(alias_id) REFERENCES entity_aliases(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_entity_alias_state_lookup
            ON entity_alias_state_events(alias_id,created_at,id);
        CREATE TABLE IF NOT EXISTS entity_resolution_decisions (
            id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, organization_id TEXT NOT NULL,
            workspace_id TEXT, action TEXT NOT NULL CHECK(action IN ('approve','reject')),
            reviewer_person_id TEXT NOT NULL, rationale TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(proposal_id) REFERENCES entity_resolution_proposals(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_resolution_one_decision ON entity_resolution_decisions(proposal_id);
        CREATE TABLE IF NOT EXISTS knowledge_proposal_decisions (
            id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, organization_id TEXT NOT NULL,
            workspace_id TEXT, action TEXT NOT NULL CHECK(action IN ('approve','reject')),
            reviewer_person_id TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(proposal_id), FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_proposal_decision_scope
            ON knowledge_proposal_decisions(organization_id,workspace_id,created_at);
        INSERT OR IGNORE INTO knowledge_proposal_decisions(id,proposal_id,organization_id,workspace_id,action,reviewer_person_id,created_at)
            SELECT 'legacy_decision_'||id,id,organization_id,workspace_id,
                   CASE WHEN status='approved' THEN 'approve' ELSE 'reject' END,
                   COALESCE(reviewed_by_person_id,'system'),COALESCE(reviewed_at,created_at)
            FROM memory_proposals WHERE status IN ('approved','rejected');
        CREATE TRIGGER IF NOT EXISTS knowledge_proposal_decision_no_update BEFORE UPDATE ON knowledge_proposal_decisions BEGIN
            SELECT RAISE(ABORT,'knowledge proposal decisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS knowledge_proposal_decision_no_delete BEFORE DELETE ON knowledge_proposal_decisions BEGIN
            SELECT RAISE(ABORT,'knowledge proposal decisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS entity_resolution_decision_no_update BEFORE UPDATE ON entity_resolution_decisions BEGIN
            SELECT RAISE(ABORT,'entity resolution decisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS entity_resolution_decision_no_delete BEFORE DELETE ON entity_resolution_decisions BEGIN
            SELECT RAISE(ABORT,'entity resolution decisions are append-only'); END;
        CREATE TABLE IF NOT EXISTS knowledge_state_events (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT,
            subject_type TEXT NOT NULL CHECK(subject_type IN ('fact','relation','canonical','proposal')),
            subject_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('verified','high_confidence','inferred','conflicted','stale','proposed')),
            reason TEXT NOT NULL, evidence_source_id TEXT, actor_id TEXT NOT NULL,
            effective_from TEXT NOT NULL, effective_until TEXT, recorded_at TEXT NOT NULL,
            event_sequence INTEGER NOT NULL,
            supersedes_event_id TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_state_lookup
            ON knowledge_state_events(workspace_id,subject_type,subject_id,effective_from,event_sequence);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_state_sequence
            ON knowledge_state_events(organization_id,COALESCE(workspace_id,''),subject_type,subject_id,event_sequence);
        CREATE TRIGGER IF NOT EXISTS knowledge_state_monotonic_insert BEFORE INSERT ON knowledge_state_events BEGIN
            SELECT CASE WHEN NEW.event_sequence != COALESCE((
                SELECT MAX(event_sequence) FROM knowledge_state_events
                WHERE organization_id=NEW.organization_id AND workspace_id IS NEW.workspace_id
                  AND subject_type=NEW.subject_type AND subject_id=NEW.subject_id
            ),0)+1 THEN RAISE(ABORT,'knowledge state sequence is not monotonic') END;
            SELECT CASE WHEN NEW.supersedes_event_id IS NOT (
                SELECT id FROM knowledge_state_events
                WHERE organization_id=NEW.organization_id AND workspace_id IS NEW.workspace_id
                  AND subject_type=NEW.subject_type AND subject_id=NEW.subject_id
                ORDER BY event_sequence DESC LIMIT 1
            ) THEN RAISE(ABORT,'knowledge state supersedes link is invalid') END;
        END;
        CREATE TRIGGER IF NOT EXISTS entity_resolution_no_delete BEFORE DELETE ON entity_resolution_proposals BEGIN
            SELECT RAISE(ABORT,'entity resolution proposals are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS entity_resolution_no_update BEFORE UPDATE ON entity_resolution_proposals BEGIN
            SELECT RAISE(ABORT,'entity resolution proposals are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS knowledge_state_no_update BEFORE UPDATE ON knowledge_state_events BEGIN
            SELECT RAISE(ABORT,'knowledge state events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS knowledge_state_no_delete BEFORE DELETE ON knowledge_state_events BEGIN
            SELECT RAISE(ABORT,'knowledge state events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS entity_aliases_no_update BEFORE UPDATE OF entity_id,alias,normalized_alias,confidence,status,source_id,created_at ON entity_aliases BEGIN
            SELECT RAISE(ABORT,'entity aliases are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS entity_aliases_no_update_lifecycle BEFORE UPDATE OF retired_at,reviewed_by_person_id,reviewed_at,evidence ON entity_aliases BEGIN
            SELECT RAISE(ABORT,'entity alias lifecycle is append-only events'); END;
        CREATE TRIGGER IF NOT EXISTS entity_aliases_no_delete BEFORE DELETE ON entity_aliases BEGIN
            SELECT RAISE(ABORT,'entity aliases are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS entity_alias_state_no_update BEFORE UPDATE ON entity_alias_state_events BEGIN
            SELECT RAISE(ABORT,'entity alias state events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS entity_alias_state_no_delete BEFORE DELETE ON entity_alias_state_events BEGIN
            SELECT RAISE(ABORT,'entity alias state events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS entity_merge_history_no_update BEFORE UPDATE ON entity_merge_history BEGIN
            SELECT RAISE(ABORT,'entity merge history is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS entity_merge_history_no_delete BEFORE DELETE ON entity_merge_history BEGIN
            SELECT RAISE(ABORT,'entity merge history is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS memory_proposals_no_update BEFORE UPDATE ON memory_proposals BEGIN
            SELECT RAISE(ABORT,'memory proposals are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS memory_proposals_no_delete BEFORE DELETE ON memory_proposals BEGIN
            SELECT RAISE(ABORT,'memory proposals are append-only'); END;
        """,
    ),
    Migration(
        19,
        "agent_level_routing",
        """
        ALTER TABLE agents ADD COLUMN level TEXT NOT NULL DEFAULT 'L1';
        ALTER TABLE agents ADD COLUMN capability_tags TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE agent_tasks ADD COLUMN intent_tags TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE agent_tasks ADD COLUMN recommended_level TEXT NOT NULL DEFAULT 'L0';
        ALTER TABLE agent_tasks ADD COLUMN selected_level TEXT NOT NULL DEFAULT 'L0';
        ALTER TABLE agent_tasks ADD COLUMN level_override_reason TEXT;
        CREATE TABLE IF NOT EXISTS agent_level_overrides (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            requested_by_person_id TEXT NOT NULL,
            recommended_level TEXT NOT NULL,
            selected_level TEXT NOT NULL,
            intent_tags TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(task_id) REFERENCES agent_tasks(id),
            FOREIGN KEY(requested_by_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_agent_level_overrides_task
            ON agent_level_overrides(organization_id,task_id,created_at);
        CREATE TRIGGER IF NOT EXISTS agent_level_overrides_no_update BEFORE UPDATE ON agent_level_overrides BEGIN
            SELECT RAISE(ABORT,'agent level overrides are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS agent_level_overrides_no_delete BEFORE DELETE ON agent_level_overrides BEGIN
            SELECT RAISE(ABORT,'agent level overrides are append-only'); END;
        """,
    ),
    Migration(
        20,
        "client_account_rosters",
        """
        CREATE TABLE IF NOT EXISTS client_account_rosters (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            effective_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by_person_id TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            UNIQUE(workspace_id,version),
            UNIQUE(workspace_id,effective_at),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(created_by_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_client_account_rosters_lookup
            ON client_account_rosters(organization_id,workspace_id,effective_at DESC,created_at DESC,id DESC);
        CREATE TABLE IF NOT EXISTS client_account_roster_roles (
            id TEXT PRIMARY KEY,
            roster_id TEXT NOT NULL,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            role_key TEXT NOT NULL CHECK(role_key IN (
                'client_success_dri','client_success_backup','account_lead','account_executive',
                'wing_lead','wing_executive','cadence_owner','escalation_owner',
                'default_meeting_facilitator','default_meeting_note_taker'
            )),
            wing TEXT,
            person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(roster_id) REFERENCES client_account_rosters(id),
            FOREIGN KEY(person_id) REFERENCES people(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_client_account_roster_role_singleton
            ON client_account_roster_roles(roster_id,role_key,COALESCE(wing,''));
        CREATE INDEX IF NOT EXISTS idx_client_account_roster_roles_person
            ON client_account_roster_roles(organization_id,workspace_id,person_id);
        CREATE TABLE IF NOT EXISTS meeting_responsibility_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            meeting_id TEXT NOT NULL,
            event_sequence INTEGER NOT NULL CHECK(event_sequence > 0),
            roster_id TEXT,
            facilitator_person_id TEXT,
            note_taker_person_id TEXT,
            reason TEXT NOT NULL,
            created_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(meeting_id) REFERENCES meetings(id),
            FOREIGN KEY(roster_id) REFERENCES client_account_rosters(id),
            FOREIGN KEY(facilitator_person_id) REFERENCES people(id),
            FOREIGN KEY(note_taker_person_id) REFERENCES people(id),
            FOREIGN KEY(created_by_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_meeting_responsibility_events_lookup
            ON meeting_responsibility_events(organization_id,workspace_id,meeting_id,created_at DESC,event_sequence DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_responsibility_events_sequence
            ON meeting_responsibility_events(meeting_id,event_sequence);
        CREATE TRIGGER IF NOT EXISTS client_account_rosters_client_workspace BEFORE INSERT ON client_account_rosters BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM workspace_organization
                WHERE workspace_id=NEW.workspace_id AND organization_id=NEW.organization_id AND kind='client'
            ) THEN RAISE(ABORT,'client roster workspace must be a client workspace in the organization') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM people p JOIN workspace_memberships wm ON wm.person_id=p.id
                WHERE p.id=NEW.created_by_person_id AND p.organization_id=NEW.organization_id
                  AND p.status='active' AND wm.workspace_id=NEW.workspace_id
            ) THEN RAISE(ABORT,'client roster creator must be an active workspace member') END;
        END;
        CREATE TRIGGER IF NOT EXISTS client_account_roster_roles_valid_insert BEFORE INSERT ON client_account_roster_roles BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM client_account_rosters r
                WHERE r.id=NEW.roster_id AND r.organization_id=NEW.organization_id AND r.workspace_id=NEW.workspace_id
            ) THEN RAISE(ABORT,'client roster role scope mismatch') END;
            SELECT CASE WHEN NEW.role_key IN ('wing_lead','wing_executive')
                AND (NEW.wing IS NULL OR TRIM(NEW.wing)='')
                THEN RAISE(ABORT,'wing is required for wing roster roles') END;
            SELECT CASE WHEN NEW.role_key NOT IN ('wing_lead','wing_executive')
                AND NEW.wing IS NOT NULL
                THEN RAISE(ABORT,'wing is only valid for wing roster roles') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM people p JOIN workspace_memberships wm ON wm.person_id=p.id
                WHERE p.id=NEW.person_id AND p.organization_id=NEW.organization_id
                  AND p.status='active' AND wm.workspace_id=NEW.workspace_id
            ) THEN RAISE(ABORT,'roster role person must be an active workspace member in the organization') END;
            SELECT CASE WHEN NEW.role_key='client_success_backup' AND EXISTS (
                SELECT 1 FROM client_account_roster_roles existing
                WHERE existing.roster_id=NEW.roster_id
                  AND existing.role_key='client_success_dri'
                  AND existing.person_id=NEW.person_id
            ) THEN RAISE(ABORT,'client success DRI and backup must be distinct') END;
            SELECT CASE WHEN NEW.role_key='client_success_dri' AND EXISTS (
                SELECT 1 FROM client_account_roster_roles existing
                WHERE existing.roster_id=NEW.roster_id
                  AND existing.role_key='client_success_backup'
                  AND existing.person_id=NEW.person_id
            ) THEN RAISE(ABORT,'client success DRI and backup must be distinct') END;
        END;
        CREATE TRIGGER IF NOT EXISTS meeting_responsibility_events_valid_insert BEFORE INSERT ON meeting_responsibility_events BEGIN
            SELECT CASE WHEN NEW.event_sequence != COALESCE((
                SELECT MAX(event_sequence) FROM meeting_responsibility_events
                WHERE meeting_id=NEW.meeting_id
            ),0)+1 THEN RAISE(ABORT,'meeting responsibility event sequence is not monotonic') END;
            SELECT CASE WHEN NEW.facilitator_person_id IS NULL AND NEW.note_taker_person_id IS NULL
                THEN RAISE(ABORT,'meeting responsibility event must name at least one person') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM meetings
                WHERE id=NEW.meeting_id AND organization_id=NEW.organization_id AND workspace_id=NEW.workspace_id
            ) THEN RAISE(ABORT,'meeting responsibility event meeting scope mismatch') END;
            SELECT CASE WHEN NEW.roster_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM client_account_rosters
                WHERE id=NEW.roster_id AND organization_id=NEW.organization_id AND workspace_id=NEW.workspace_id
            ) THEN RAISE(ABORT,'meeting responsibility event roster scope mismatch') END;
            SELECT CASE WHEN NEW.facilitator_person_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM people p JOIN workspace_memberships wm ON wm.person_id=p.id
                WHERE p.id=NEW.facilitator_person_id AND p.organization_id=NEW.organization_id
                  AND p.status='active' AND wm.workspace_id=NEW.workspace_id
            ) THEN RAISE(ABORT,'meeting facilitator must be an active workspace member in the organization') END;
            SELECT CASE WHEN NEW.note_taker_person_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM people p JOIN workspace_memberships wm ON wm.person_id=p.id
                WHERE p.id=NEW.note_taker_person_id AND p.organization_id=NEW.organization_id
                  AND p.status='active' AND wm.workspace_id=NEW.workspace_id
            ) THEN RAISE(ABORT,'meeting note taker must be an active workspace member in the organization') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM people p JOIN workspace_memberships wm ON wm.person_id=p.id
                WHERE p.id=NEW.created_by_person_id AND p.organization_id=NEW.organization_id
                  AND p.status='active' AND wm.workspace_id=NEW.workspace_id
            ) THEN RAISE(ABORT,'meeting responsibility actor must be an active workspace member') END;
        END;
        CREATE TRIGGER IF NOT EXISTS client_account_rosters_no_update BEFORE UPDATE ON client_account_rosters BEGIN
            SELECT RAISE(ABORT,'client account rosters are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS client_account_rosters_no_delete BEFORE DELETE ON client_account_rosters BEGIN
            SELECT RAISE(ABORT,'client account rosters are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS client_account_roster_roles_no_update BEFORE UPDATE ON client_account_roster_roles BEGIN
            SELECT RAISE(ABORT,'client account roster roles are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS client_account_roster_roles_no_delete BEFORE DELETE ON client_account_roster_roles BEGIN
            SELECT RAISE(ABORT,'client account roster roles are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS meeting_responsibility_events_no_update BEFORE UPDATE ON meeting_responsibility_events BEGIN
            SELECT RAISE(ABORT,'meeting responsibility events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS meeting_responsibility_events_no_delete BEFORE DELETE ON meeting_responsibility_events BEGIN
            SELECT RAISE(ABORT,'meeting responsibility events are append-only'); END;
        """,
    ),
    Migration(
        21,
        "graphiti_episode_sidecar",
        """
        CREATE TABLE IF NOT EXISTS graphiti_episode_mappings (
            episode_key TEXT PRIMARY KEY,
            remote_episode_uuid TEXT NOT NULL UNIQUE,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            generation TEXT NOT NULL,
            source_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(source_id) REFERENCES sources(id),
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        CREATE INDEX IF NOT EXISTS idx_graphiti_episode_scope
            ON graphiti_episode_mappings(organization_id,workspace_id,generation,source_id,document_id);
        CREATE TRIGGER IF NOT EXISTS graphiti_episode_mappings_valid_insert
        BEFORE INSERT ON graphiti_episode_mappings BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM workspace_organization
                WHERE workspace_id=NEW.workspace_id AND organization_id=NEW.organization_id
            ) THEN RAISE(ABORT,'Graphiti episode workspace scope mismatch') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM sources
                WHERE id=NEW.source_id AND workspace_id=NEW.workspace_id
            ) THEN RAISE(ABORT,'Graphiti episode source scope mismatch') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM documents
                WHERE id=NEW.document_id AND workspace_id=NEW.workspace_id AND source_id=NEW.source_id
            ) THEN RAISE(ABORT,'Graphiti episode document scope mismatch') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM graph_projection_generations
                WHERE workspace_id=NEW.workspace_id AND generation=NEW.generation
            ) THEN RAISE(ABORT,'Graphiti episode generation scope mismatch') END;
        END;
        CREATE TRIGGER IF NOT EXISTS graphiti_episode_mappings_no_update
        BEFORE UPDATE ON graphiti_episode_mappings BEGIN
            SELECT RAISE(ABORT,'Graphiti episode mappings are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS graphiti_episode_mappings_no_delete
        BEFORE DELETE ON graphiti_episode_mappings BEGIN
            SELECT RAISE(ABORT,'Graphiti episode mappings are append-only'); END;
        """,
    ),
    Migration(
        22,
        "client_portal_intake",
        """
        CREATE TABLE IF NOT EXISTS client_intake_requests (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            submitted_by_person_id TEXT NOT NULL,
            title TEXT NOT NULL,
            request TEXT NOT NULL,
            needed_by TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending','accepted','declined')) DEFAULT 'pending',
            work_item_id TEXT,
            decided_by_person_id TEXT,
            decision_note TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(submitted_by_person_id) REFERENCES people(id),
            FOREIGN KEY(work_item_id) REFERENCES work_items(id)
        );
        CREATE INDEX IF NOT EXISTS idx_client_intake_queue ON client_intake_requests(workspace_id,status,created_at);
        CREATE TRIGGER IF NOT EXISTS client_intake_requests_valid_insert
        BEFORE INSERT ON client_intake_requests BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM workspace_organization
                WHERE workspace_id=NEW.workspace_id AND organization_id=NEW.organization_id AND kind='client'
            ) THEN RAISE(ABORT,'client intake requires a client workspace') END;
        END;
        """,
    ),
    Migration(
        23,
        "feedback_learning",
        """
        CREATE TABLE IF NOT EXISTS feedback_patterns (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN ('design','copy','approval','stakeholder','process','other')),
            pattern_key TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            sample_evidence TEXT NOT NULL DEFAULT '[]',
            proposed_preference_id TEXT,
            preference_status TEXT NOT NULL DEFAULT 'observing' CHECK(preference_status IN ('observing','proposed','approved','rejected')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            UNIQUE(organization_id, workspace_id, category, pattern_key)
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_patterns_ws ON feedback_patterns(organization_id, workspace_id, category);
        CREATE TABLE IF NOT EXISTS feedback_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            pattern_id TEXT,
            category TEXT NOT NULL,
            raw_feedback TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT,
            recorded_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(pattern_id) REFERENCES feedback_patterns(id)
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_events_ws ON feedback_events(organization_id, workspace_id, created_at);
        """,
    ),
    Migration(
        24,
        "performance_insights",
        """
        CREATE TABLE IF NOT EXISTS performance_insights (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            insight_type TEXT NOT NULL CHECK(insight_type IN ('creative_comparison','channel_comparison','trend','anomaly','client_preference')),
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            comparison_subject_id TEXT,
            metric_name TEXT NOT NULL,
            metric_value_a REAL,
            metric_value_b REAL,
            delta REAL,
            direction TEXT NOT NULL CHECK(direction IN ('positive','negative','neutral')),
            confidence REAL NOT NULL DEFAULT 0.0,
            evidence_summary TEXT NOT NULL,
            source_snapshot_ids TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','approved','rejected','superseded')),
            approved_by_person_id TEXT,
            approved_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE INDEX IF NOT EXISTS idx_perf_insights_ws ON performance_insights(organization_id, workspace_id, insight_type, status);
        """,
    ),
    Migration(
        25,
        "forecasts",
        """
        CREATE TABLE IF NOT EXISTS forecasts (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id,
            forecast_type TEXT NOT NULL CHECK(forecast_type IN ('client_renewal','revenue','capacity','scope_consumption','utilization','delivery_pressure')),
            subject_id TEXT,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            predicted_value REAL NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.0,
            basis TEXT NOT NULL DEFAULT '[]',
            data_points INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','expired','superseded')),
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE INDEX IF NOT EXISTS idx_forecasts_org ON forecasts(organization_id, forecast_type, status);
        CREATE INDEX IF NOT EXISTS idx_forecasts_ws ON forecasts(organization_id, workspace_id, forecast_type);
        """,
    ),
    Migration(
        26,
        "retention_and_deletion",
        """
        CREATE TABLE IF NOT EXISTS retention_policies (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            scope TEXT NOT NULL CHECK(scope IN ('organization','workspace','connector')),
            scope_id TEXT,
            data_category TEXT NOT NULL,
            max_age_days INTEGER NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('archive','delete','redact')),
            created_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE TABLE IF NOT EXISTS deletion_audit (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id,
            table_name TEXT NOT NULL,
            record_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            initiated_by TEXT NOT NULL,
            retention_policy_id TEXT,
            snapshot_json TEXT,
            deleted_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_deletion_audit_org ON deletion_audit(organization_id, deleted_at);
        CREATE INDEX IF NOT EXISTS idx_retention_policies_org ON retention_policies(organization_id, scope);
        """
    ),
    Migration(
        27,
        "brain_workspace_persistence",
        """
        CREATE TABLE IF NOT EXISTS brain_folders (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            parent_id TEXT,
            name TEXT NOT NULL,
            created_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(parent_id) REFERENCES brain_folders(id),
            CHECK(parent_id IS NULL OR parent_id <> id),
            UNIQUE(workspace_id, parent_id, name),
            UNIQUE(workspace_id, created_by_person_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_folders_workspace ON brain_folders(workspace_id, parent_id, name);

        CREATE TABLE IF NOT EXISTS brain_collections (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            folder_id TEXT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            owner_person_id TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK(visibility IN ('owner','shared')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(folder_id) REFERENCES brain_folders(id),
            UNIQUE(workspace_id, folder_id, name),
            UNIQUE(workspace_id, owner_person_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_collections_workspace ON brain_collections(workspace_id, folder_id, visibility);

        CREATE TABLE IF NOT EXISTS brain_tags (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            color TEXT,
            created_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            UNIQUE(workspace_id, normalized_name),
            UNIQUE(workspace_id, created_by_person_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_tags_workspace ON brain_tags(workspace_id, normalized_name);

        CREATE TABLE IF NOT EXISTS brain_source_tags (
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            tagged_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            idempotency_key TEXT,
            PRIMARY KEY(workspace_id, source_id, tag_id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(source_id) REFERENCES sources(id),
            FOREIGN KEY(tag_id) REFERENCES brain_tags(id),
            UNIQUE(workspace_id, tagged_by_person_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_source_tags_tag ON brain_source_tags(workspace_id, tag_id);

        CREATE TABLE IF NOT EXISTS brain_document_tags (
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            tagged_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            idempotency_key TEXT,
            PRIMARY KEY(workspace_id, document_id, tag_id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(document_id) REFERENCES documents(id),
            FOREIGN KEY(tag_id) REFERENCES brain_tags(id),
            UNIQUE(workspace_id, tagged_by_person_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_document_tags_tag ON brain_document_tags(workspace_id, tag_id);

        CREATE TABLE IF NOT EXISTS brain_collection_items (
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            collection_id TEXT NOT NULL,
            item_type TEXT NOT NULL CHECK(item_type IN ('source','document')),
            item_id TEXT NOT NULL,
            added_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            idempotency_key TEXT,
            PRIMARY KEY(workspace_id, collection_id, item_type, item_id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(collection_id) REFERENCES brain_collections(id),
            UNIQUE(workspace_id, added_by_person_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_collection_items_collection ON brain_collection_items(workspace_id, collection_id, item_type);

        CREATE TABLE IF NOT EXISTS brain_saved_views (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            folder_id TEXT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            owner_person_id TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK(visibility IN ('owner','shared')),
            query_json TEXT NOT NULL,
            filters_json TEXT NOT NULL,
            sort_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(folder_id) REFERENCES brain_folders(id),
            UNIQUE(workspace_id, folder_id, name),
            UNIQUE(workspace_id, owner_person_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_saved_views_workspace ON brain_saved_views(workspace_id, folder_id, visibility);

        CREATE TABLE IF NOT EXISTS brain_saved_view_versions (
            id TEXT PRIMARY KEY,
            saved_view_id TEXT NOT NULL,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK(visibility IN ('owner','shared')),
            query_json TEXT NOT NULL,
            filters_json TEXT NOT NULL,
            sort_json TEXT NOT NULL,
            changed_by_person_id TEXT NOT NULL,
            change_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(saved_view_id) REFERENCES brain_saved_views(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            UNIQUE(saved_view_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_saved_view_versions_view ON brain_saved_view_versions(saved_view_id, version);

        CREATE TABLE IF NOT EXISTS brain_mutation_audit (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            action TEXT NOT NULL,
            actor_person_id TEXT NOT NULL,
            version INTEGER,
            idempotency_key TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_mutation_audit_entity ON brain_mutation_audit(workspace_id, entity_type, entity_id, created_at);
        """
    ),
    Migration(
        28,
        "secure_provider_integrations",
        """
        CREATE TABLE IF NOT EXISTS local_secret_vault (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            name TEXT NOT NULL,
            reference TEXT,
            ciphertext TEXT NOT NULL,
            key_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(organization_id, workspace_id, name),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE TABLE IF NOT EXISTS provider_installations (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            provider TEXT NOT NULL CHECK(provider IN ('google','slack','figma','github')),
            account_id TEXT NOT NULL,
            account_label TEXT,
            client_id TEXT,
            redirect_uri TEXT NOT NULL,
            webhook_secret_reference TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','revoked')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            UNIQUE(organization_id, workspace_id, provider, account_id)
        );
        CREATE INDEX IF NOT EXISTS idx_provider_installations_scope
            ON provider_installations(organization_id, workspace_id, provider, status);
        CREATE TABLE IF NOT EXISTS oauth_states (
            id TEXT PRIMARY KEY,
            state_digest TEXT NOT NULL UNIQUE,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            installation_id TEXT,
            provider TEXT NOT NULL,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            scope TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(installation_id) REFERENCES provider_installations(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_oauth_states_expiry ON oauth_states(expires_at, used_at);
        CREATE TABLE IF NOT EXISTS webhook_events (
            id TEXT PRIMARY KEY,
            installation_id TEXT NOT NULL,
            organization_id TEXT NOT NULL,
            event_digest TEXT NOT NULL,
            signature_digest TEXT NOT NULL,
            provider_event_id TEXT,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('accepted','duplicate','rejected')),
            FOREIGN KEY(installation_id) REFERENCES provider_installations(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            UNIQUE(installation_id, event_digest)
        );
        CREATE INDEX IF NOT EXISTS idx_webhook_events_installation
            ON webhook_events(installation_id, received_at);
        CREATE TABLE IF NOT EXISTS outbound_send_intents (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            installation_id TEXT NOT NULL,
            approval_request_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','sent','failed','blocked')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(installation_id) REFERENCES provider_installations(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(approval_request_id) REFERENCES approval_requests(id),
            UNIQUE(organization_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_outbound_send_intents_status
            ON outbound_send_intents(organization_id, workspace_id, status, updated_at);
        """,
    ),
    Migration(
        29,
        "asset_backup_recovery",
        """
        CREATE TABLE IF NOT EXISTS asset_registry (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            name TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            sha256 TEXT NOT NULL,
            locator TEXT NOT NULL,
            retention_class TEXT NOT NULL CHECK(retention_class IN ('ephemeral','standard','critical','legal_hold')),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deleted')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            UNIQUE(organization_id, workspace_id, sha256, locator)
        );
        CREATE INDEX IF NOT EXISTS idx_asset_registry_scope ON asset_registry(organization_id, workspace_id, retention_class, status);
        CREATE TABLE IF NOT EXISTS backup_manifests (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            locator TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            schema_version INTEGER NOT NULL,
            integrity TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'recorded' CHECK(status IN ('recorded','verified','failed','expired')),
            created_at TEXT NOT NULL,
            verified_at TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            UNIQUE(organization_id, sha256, locator)
        );
        CREATE INDEX IF NOT EXISTS idx_backup_manifests_org ON backup_manifests(organization_id, status, created_at);
        CREATE TABLE IF NOT EXISTS recovery_plans (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            backup_manifest_id TEXT NOT NULL,
            external_provider TEXT NOT NULL,
            target_locator TEXT NOT NULL,
            rpo_minutes INTEGER NOT NULL CHECK(rpo_minutes >= 0),
            rto_minutes INTEGER NOT NULL CHECK(rto_minutes >= 0),
            status TEXT NOT NULL CHECK(status IN ('planned','ready','executing','completed','failed','blocked')),
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(backup_manifest_id) REFERENCES backup_manifests(id)
        );
        CREATE INDEX IF NOT EXISTS idx_recovery_plans_scope ON recovery_plans(organization_id, workspace_id, status);
        CREATE TABLE IF NOT EXISTS asset_recovery_audit (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_asset_recovery_audit_entity ON asset_recovery_audit(organization_id, entity_type, entity_id, created_at);
        """,
    ),
    Migration(
        30,
        "rich_review_annotations",
        """
        CREATE TABLE IF NOT EXISTS review_annotations (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            review_id TEXT NOT NULL,
            deliverable_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            author_person_id TEXT NOT NULL,
            annotation_type TEXT NOT NULL CHECK(annotation_type IN ('general_comment','image_point','image_region','document_page','document_region','video_timestamp','video_range')),
            body TEXT NOT NULL,
            source_locator TEXT,
            coordinates_json TEXT NOT NULL DEFAULT '{}',
            page_number INTEGER,
            start_seconds REAL,
            end_seconds REAL,
            created_at TEXT NOT NULL,
            idempotency_key TEXT,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(review_id) REFERENCES reviews(id),
            FOREIGN KEY(deliverable_id) REFERENCES deliverables(id),
            FOREIGN KEY(author_person_id) REFERENCES people(id),
            UNIQUE(organization_id, workspace_id, author_person_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_review_annotations_review ON review_annotations(workspace_id, review_id, created_at);
        CREATE TABLE IF NOT EXISTS review_annotation_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            annotation_id TEXT NOT NULL,
            actor_person_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('created','resolved','superseded')),
            replacement_annotation_id TEXT,
            idempotency_key TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(annotation_id) REFERENCES review_annotations(id),
            FOREIGN KEY(replacement_annotation_id) REFERENCES review_annotations(id),
            FOREIGN KEY(actor_person_id) REFERENCES people(id),
            UNIQUE(organization_id, workspace_id, actor_person_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_review_annotation_events_annotation ON review_annotation_events(annotation_id, created_at);
        CREATE TRIGGER IF NOT EXISTS review_annotations_no_update BEFORE UPDATE ON review_annotations BEGIN SELECT RAISE(ABORT, 'review annotations are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS review_annotations_no_delete BEFORE DELETE ON review_annotations BEGIN SELECT RAISE(ABORT, 'review annotations are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS review_annotation_events_no_update BEFORE UPDATE ON review_annotation_events BEGIN SELECT RAISE(ABORT, 'review annotation events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS review_annotation_events_no_delete BEFORE DELETE ON review_annotation_events BEGIN SELECT RAISE(ABORT, 'review annotation events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_review_annotation_events_insert AFTER INSERT ON review_annotation_events BEGIN
            INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,NEW.actor_person_id,'person',NEW.action,'review_annotation',NEW.annotation_id,NEW.payload_json,CURRENT_TIMESTAMP);
        END;
        """,
    ),
    Migration(
        31,
        "proactive_intelligence_snapshots",
        """
        CREATE TABLE IF NOT EXISTS proactive_intelligence_snapshots (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            person_id TEXT NOT NULL,
            snapshot_type TEXT NOT NULL CHECK(snapshot_type IN ('executive','workspace')),
            version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ready','degraded','insufficient_evidence')),
            degraded_reason TEXT,
            projection_fingerprint TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(person_id) REFERENCES people(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_proactive_intelligence_snapshot_version
            ON proactive_intelligence_snapshots(organization_id, COALESCE(workspace_id, ''), person_id, snapshot_type, version);
        CREATE INDEX IF NOT EXISTS idx_proactive_intelligence_latest
            ON proactive_intelligence_snapshots(organization_id, COALESCE(workspace_id, ''), person_id, snapshot_type, generated_at DESC, version DESC);
        CREATE INDEX IF NOT EXISTS idx_proactive_intelligence_status
            ON proactive_intelligence_snapshots(organization_id, COALESCE(workspace_id, ''), person_id, snapshot_type, status, generated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_proactive_intelligence_fingerprint
            ON proactive_intelligence_snapshots(organization_id, COALESCE(workspace_id, ''), person_id, snapshot_type, projection_fingerprint);
        CREATE TABLE IF NOT EXISTS proactive_intelligence_attention_items (
            id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            person_id TEXT NOT NULL,
            rank INTEGER NOT NULL,
            title TEXT NOT NULL,
            narrative TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('open','degraded','insufficient_evidence')),
            evidence_refs_json TEXT NOT NULL DEFAULT '{}',
            generated_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES proactive_intelligence_snapshots(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_proactive_intelligence_attention_latest
            ON proactive_intelligence_attention_items(organization_id, COALESCE(workspace_id, ''), person_id, generated_at DESC, rank);
        CREATE TRIGGER IF NOT EXISTS proactive_intelligence_snapshots_no_update BEFORE UPDATE ON proactive_intelligence_snapshots BEGIN
            SELECT RAISE(ABORT, 'proactive intelligence snapshots are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS proactive_intelligence_snapshots_no_delete BEFORE DELETE ON proactive_intelligence_snapshots BEGIN
            SELECT RAISE(ABORT, 'proactive intelligence snapshots are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS proactive_intelligence_attention_no_update BEFORE UPDATE ON proactive_intelligence_attention_items BEGIN
            SELECT RAISE(ABORT, 'proactive intelligence attention is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS proactive_intelligence_attention_no_delete BEFORE DELETE ON proactive_intelligence_attention_items BEGIN
            SELECT RAISE(ABORT, 'proactive intelligence attention is append-only');
        END;
        """,
    ),
    Migration(
        32,
        "work_transition_idempotency",
        """
        CREATE TABLE IF NOT EXISTS work_idempotency_keys (
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            key TEXT NOT NULL,
            operation TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            response TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(organization_id, workspace_id, key, operation),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        """,
    ),
    Migration(
        33,
        "client_operations_lifecycle_events",
        """
        CREATE TABLE IF NOT EXISTS risk_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            risk_id TEXT NOT NULL,
            actor_person_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('created','resolved','reopened')),
            from_status TEXT,
            to_status TEXT NOT NULL,
            note TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(risk_id) REFERENCES risks(id),
            FOREIGN KEY(actor_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_risk_events_risk ON risk_events(risk_id, created_at, id);
        CREATE TABLE IF NOT EXISTS opportunity_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            opportunity_id TEXT NOT NULL,
            actor_person_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('created','advanced','closed')),
            from_status TEXT,
            to_status TEXT NOT NULL,
            note TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(opportunity_id) REFERENCES opportunities(id),
            FOREIGN KEY(actor_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_events_opportunity
            ON opportunity_events(opportunity_id, created_at, id);
        CREATE TRIGGER IF NOT EXISTS risk_events_no_update BEFORE UPDATE ON risk_events BEGIN
            SELECT RAISE(ABORT, 'risk events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS risk_events_no_delete BEFORE DELETE ON risk_events BEGIN
            SELECT RAISE(ABORT, 'risk events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_events_no_update BEFORE UPDATE ON opportunity_events BEGIN
            SELECT RAISE(ABORT, 'opportunity events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_events_no_delete BEFORE DELETE ON opportunity_events BEGIN
            SELECT RAISE(ABORT, 'opportunity events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS audit_risk_events_insert AFTER INSERT ON risk_events BEGIN
            INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,NEW.actor_person_id,'person',CASE WHEN NEW.action='created' THEN 'create' ELSE NEW.action END,'risk',NEW.risk_id,NEW.note,CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_opportunity_events_insert AFTER INSERT ON opportunity_events BEGIN
            INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,NEW.actor_person_id,'person',CASE WHEN NEW.action='created' THEN 'create' ELSE NEW.action END,'opportunity',NEW.opportunity_id,NEW.note,CURRENT_TIMESTAMP);
        END;
        """,
    ),
    Migration(
        34,
        "campaign_creative_lifecycle_events",
        """
        CREATE TABLE IF NOT EXISTS campaign_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL,
            actor_person_id TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(campaign_id) REFERENCES campaigns(id),
            FOREIGN KEY(actor_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_campaign_events_campaign
            ON campaign_events(campaign_id, created_at, id);
        CREATE TABLE IF NOT EXISTS creative_review_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            actor_person_id TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(asset_id) REFERENCES creative_assets(id),
            FOREIGN KEY(actor_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_creative_review_events_asset
            ON creative_review_events(asset_id, created_at, id);
        CREATE TRIGGER IF NOT EXISTS campaign_events_no_update BEFORE UPDATE ON campaign_events BEGIN
            SELECT RAISE(ABORT, 'campaign events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS campaign_events_no_delete BEFORE DELETE ON campaign_events BEGIN
            SELECT RAISE(ABORT, 'campaign events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS creative_review_events_no_update BEFORE UPDATE ON creative_review_events BEGIN
            SELECT RAISE(ABORT, 'creative review events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS creative_review_events_no_delete BEFORE DELETE ON creative_review_events BEGIN
            SELECT RAISE(ABORT, 'creative review events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS audit_campaign_events_insert AFTER INSERT ON campaign_events BEGIN
            INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,NEW.actor_person_id,'person','transition','campaign',NEW.campaign_id,NEW.note,CURRENT_TIMESTAMP);
        END;
        CREATE TRIGGER IF NOT EXISTS audit_creative_review_events_insert AFTER INSERT ON creative_review_events BEGIN
            INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,NEW.actor_person_id,'person','review','creative',NEW.asset_id,NEW.note,CURRENT_TIMESTAMP);
        END;
        """,
    ),
    Migration(
        35,
        "agency_revenue_operations",
        """
        CREATE TABLE IF NOT EXISTS sales_prospects (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            name TEXT NOT NULL, company_name TEXT NOT NULL, contact_email TEXT,
            status TEXT NOT NULL CHECK(status IN ('new','qualified','proposal','won','lost','converted')),
            owner_person_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id), FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(owner_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sales_prospects_scope ON sales_prospects(organization_id,workspace_id,status,updated_at);
        CREATE TABLE IF NOT EXISTS sales_proposals (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, prospect_id TEXT NOT NULL,
            title TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','sent','won','lost')),
            valid_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(prospect_id) REFERENCES sales_prospects(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sales_proposals_scope ON sales_proposals(organization_id,workspace_id,status,updated_at);
        CREATE TABLE IF NOT EXISTS sales_events (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, event_type TEXT NOT NULL,
            actor_person_id TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id), FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(actor_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sales_events_entity ON sales_events(organization_id,workspace_id,entity_type,entity_id,created_at,id);
        CREATE TRIGGER IF NOT EXISTS sales_events_no_update BEFORE UPDATE ON sales_events BEGIN SELECT RAISE(ABORT,'sales events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS sales_events_no_delete BEFORE DELETE ON sales_events BEGIN SELECT RAISE(ABORT,'sales events are append-only'); END;
        CREATE TABLE IF NOT EXISTS sales_conversions (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, prospect_id TEXT NOT NULL,
            proposal_id TEXT NOT NULL, client_workspace_id TEXT NOT NULL, contract_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(organization_id,idempotency_key), UNIQUE(proposal_id),
            FOREIGN KEY(prospect_id) REFERENCES sales_prospects(id), FOREIGN KEY(proposal_id) REFERENCES sales_proposals(id),
            FOREIGN KEY(client_workspace_id) REFERENCES workspaces(id), FOREIGN KEY(contract_id) REFERENCES contracts(id)
        );
        CREATE TABLE IF NOT EXISTS campaign_budget_signals (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
            rule TEXT NOT NULL, threshold REAL NOT NULL, actual REAL, status TEXT NOT NULL CHECK(status IN ('open','resolved')),
            evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT,
            UNIQUE(campaign_id,rule,status)
        );
        CREATE TABLE IF NOT EXISTS report_pack_requests (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, report_run_id TEXT,
            requested_by_person_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','delivered_internal')),
            note TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT, decided_by_person_id TEXT,
            FOREIGN KEY(report_run_id) REFERENCES report_runs(id)
        );
        CREATE TABLE IF NOT EXISTS report_pack_events (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, request_id TEXT NOT NULL,
            action TEXT NOT NULL, actor_person_id TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(request_id) REFERENCES report_pack_requests(id), FOREIGN KEY(actor_person_id) REFERENCES people(id)
        );
        CREATE TRIGGER IF NOT EXISTS report_pack_events_no_update BEFORE UPDATE ON report_pack_events BEGIN SELECT RAISE(ABORT,'report pack events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS report_pack_events_no_delete BEFORE DELETE ON report_pack_events BEGIN SELECT RAISE(ABORT,'report pack events are append-only'); END;
        """,
    ),
    Migration(
        36,
        "onboarding_csv_imports",
        """
        CREATE TABLE IF NOT EXISTS onboarding_import_batches (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            person_id TEXT NOT NULL,
            import_type TEXT NOT NULL CHECK(import_type IN ('client_workspaces','campaigns','campaign_metrics')),
            idempotency_key TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            template_version TEXT NOT NULL,
            total_rows INTEGER NOT NULL CHECK(total_rows >= 0),
            valid_rows INTEGER NOT NULL CHECK(valid_rows >= 0),
            invalid_rows INTEGER NOT NULL CHECK(invalid_rows >= 0),
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(person_id) REFERENCES people(id),
            UNIQUE(organization_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_onboarding_batches_scope
            ON onboarding_import_batches(organization_id, workspace_id, created_at);
        CREATE TABLE IF NOT EXISTS onboarding_import_rows (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            row_number INTEGER NOT NULL CHECK(row_number > 0),
            row_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('preview_valid','quarantined')),
            raw_json TEXT NOT NULL,
            normalized_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES onboarding_import_batches(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            UNIQUE(batch_id, row_number)
        );
        CREATE INDEX IF NOT EXISTS idx_onboarding_rows_batch
            ON onboarding_import_rows(batch_id, status, row_number);
        CREATE TABLE IF NOT EXISTS onboarding_import_errors (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            row_id TEXT,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            row_number INTEGER NOT NULL CHECK(row_number > 0),
            field TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES onboarding_import_batches(id),
            FOREIGN KEY(row_id) REFERENCES onboarding_import_rows(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE INDEX IF NOT EXISTS idx_onboarding_errors_batch
            ON onboarding_import_errors(batch_id, row_number);
        CREATE TABLE IF NOT EXISTS onboarding_import_receipts (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            person_id TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('preview','commit')),
            status TEXT NOT NULL CHECK(status IN ('previewed','committed','committed_with_errors','failed','replayed')),
            idempotency_key TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES onboarding_import_batches(id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(person_id) REFERENCES people(id),
            UNIQUE(organization_id, phase, idempotency_key)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_onboarding_commit_once
            ON onboarding_import_receipts(batch_id, phase)
            WHERE phase='commit' AND status='committed';
        CREATE INDEX IF NOT EXISTS idx_onboarding_receipts_scope
            ON onboarding_import_receipts(organization_id, workspace_id, created_at);
        CREATE TRIGGER IF NOT EXISTS onboarding_import_batches_no_update BEFORE UPDATE ON onboarding_import_batches BEGIN
            SELECT RAISE(ABORT, 'onboarding import batches are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS onboarding_import_batches_no_delete BEFORE DELETE ON onboarding_import_batches BEGIN
            SELECT RAISE(ABORT, 'onboarding import batches are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS onboarding_import_rows_no_update BEFORE UPDATE ON onboarding_import_rows BEGIN
            SELECT RAISE(ABORT, 'onboarding import rows are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS onboarding_import_rows_no_delete BEFORE DELETE ON onboarding_import_rows BEGIN
            SELECT RAISE(ABORT, 'onboarding import rows are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS onboarding_import_errors_no_update BEFORE UPDATE ON onboarding_import_errors BEGIN
            SELECT RAISE(ABORT, 'onboarding import errors are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS onboarding_import_errors_no_delete BEFORE DELETE ON onboarding_import_errors BEGIN
            SELECT RAISE(ABORT, 'onboarding import errors are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS onboarding_import_receipts_no_update BEFORE UPDATE ON onboarding_import_receipts BEGIN
            SELECT RAISE(ABORT, 'onboarding import receipts are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS onboarding_import_receipts_no_delete BEFORE DELETE ON onboarding_import_receipts BEGIN
            SELECT RAISE(ABORT, 'onboarding import receipts are append-only');
        END;
        """,
    ),
    Migration(
        37,
        "brain_customization_controls",
        """
        CREATE TABLE IF NOT EXISTS brain_customization_versions (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            scope_type TEXT NOT NULL CHECK(scope_type IN ('organization','workspace')),
            kind TEXT NOT NULL CHECK(kind IN ('instructions','policy','settings')),
            version INTEGER NOT NULL CHECK(version > 0),
            name TEXT NOT NULL,
            body TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(created_by_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_customization_versions_scope
            ON brain_customization_versions(organization_id, workspace_id, scope_type, kind, version);
        CREATE TABLE IF NOT EXISTS brain_customization_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            scope_type TEXT NOT NULL CHECK(scope_type IN ('organization','workspace')),
            kind TEXT NOT NULL CHECK(kind IN ('instructions','policy','settings')),
            target_version_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('created','activated','rolled_back')),
            reason TEXT NOT NULL,
            active_version_id TEXT,
            previous_version_id TEXT,
            actor_principal_id TEXT NOT NULL,
            actor_person_id TEXT NOT NULL,
            event_sequence INTEGER NOT NULL CHECK(event_sequence > 0),
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(target_version_id) REFERENCES brain_customization_versions(id),
            FOREIGN KEY(active_version_id) REFERENCES brain_customization_versions(id),
            FOREIGN KEY(previous_version_id) REFERENCES brain_customization_versions(id),
            FOREIGN KEY(actor_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_brain_customization_events_active
            ON brain_customization_events(organization_id, workspace_id, scope_type, kind, event_sequence);
        CREATE TRIGGER IF NOT EXISTS brain_customization_versions_no_update BEFORE UPDATE ON brain_customization_versions BEGIN
            SELECT RAISE(ABORT, 'brain customization versions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS brain_customization_versions_no_delete BEFORE DELETE ON brain_customization_versions BEGIN
            SELECT RAISE(ABORT, 'brain customization versions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS brain_customization_events_no_update BEFORE UPDATE ON brain_customization_events BEGIN
            SELECT RAISE(ABORT, 'brain customization events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS brain_customization_events_no_delete BEFORE DELETE ON brain_customization_events BEGIN
            SELECT RAISE(ABORT, 'brain customization events are append-only');
        END;
        """,
    ),
    Migration(
        38,
        "read_only_provider_imports",
        """
        CREATE TABLE IF NOT EXISTS provider_import_records (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            provider TEXT NOT NULL CHECK(provider IN ('stripe_accounting','meta_ads')),
            object_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            occurred_at TEXT,
            amount REAL,
            currency TEXT,
            payload_hash TEXT NOT NULL,
            source TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            UNIQUE(organization_id, provider, object_type, external_id),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE INDEX IF NOT EXISTS idx_provider_import_records_scope
            ON provider_import_records(organization_id, workspace_id, provider, object_type, imported_at);
        CREATE TABLE IF NOT EXISTS provider_import_quarantines (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            object_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_provider_import_quarantine_scope
            ON provider_import_quarantines(organization_id, provider, created_at);
        CREATE TABLE IF NOT EXISTS provider_import_cursors (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            account_id TEXT NOT NULL,
            resource TEXT NOT NULL,
            cursor_value TEXT,
            status TEXT NOT NULL CHECK(status IN ('not_connected','configured','syncing','degraded')),
            last_error TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(organization_id, workspace_id, provider, account_id, resource),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE TRIGGER IF NOT EXISTS provider_import_records_no_update BEFORE UPDATE ON provider_import_records BEGIN
            SELECT RAISE(ABORT, 'provider import records are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_import_records_no_delete BEFORE DELETE ON provider_import_records BEGIN
            SELECT RAISE(ABORT, 'provider import records are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_import_quarantines_no_update BEFORE UPDATE ON provider_import_quarantines BEGIN
            SELECT RAISE(ABORT, 'provider import quarantines are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS provider_import_quarantines_no_delete BEFORE DELETE ON provider_import_quarantines BEGIN
            SELECT RAISE(ABORT, 'provider import quarantines are append-only');
        END;
        """,
    ),
    Migration(
        39,
        "portal_report_delivery",
        """
        CREATE TABLE IF NOT EXISTS portal_report_versions (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            report_run_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            title TEXT NOT NULL,
            report_type TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            approval_request_id TEXT NOT NULL,
            published_by_person_id TEXT NOT NULL,
            supersedes_version_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(report_run_id) REFERENCES report_runs(id),
            FOREIGN KEY(approval_request_id) REFERENCES approval_requests(id),
            FOREIGN KEY(published_by_person_id) REFERENCES people(id),
            FOREIGN KEY(supersedes_version_id) REFERENCES portal_report_versions(id),
            UNIQUE(organization_id, workspace_id, report_type, version)
        );
        CREATE INDEX IF NOT EXISTS idx_portal_report_versions_scope
            ON portal_report_versions(organization_id, workspace_id, report_type, version);
        CREATE TABLE IF NOT EXISTS portal_report_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            portal_report_version_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('published','superseded','revoked','viewed','downloaded')),
            actor_person_id TEXT NOT NULL,
            actor_role TEXT NOT NULL CHECK(actor_role IN ('staff','client')),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(portal_report_version_id) REFERENCES portal_report_versions(id),
            FOREIGN KEY(actor_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_portal_report_events_version
            ON portal_report_events(portal_report_version_id, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_portal_report_events_scope
            ON portal_report_events(organization_id, workspace_id, created_at);
        CREATE TRIGGER IF NOT EXISTS portal_report_versions_no_update BEFORE UPDATE ON portal_report_versions BEGIN
            SELECT RAISE(ABORT, 'portal report versions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS portal_report_versions_no_delete BEFORE DELETE ON portal_report_versions BEGIN
            SELECT RAISE(ABORT, 'portal report versions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS portal_report_events_no_update BEFORE UPDATE ON portal_report_events BEGIN
            SELECT RAISE(ABORT, 'portal report events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS portal_report_events_no_delete BEFORE DELETE ON portal_report_events BEGIN
            SELECT RAISE(ABORT, 'portal report events are append-only');
        END;
        """,
    ),
    Migration(
        40,
        "durable_scheduler_operator_health",
        """
        CREATE TABLE IF NOT EXISTS scheduler_heartbeats (
            worker_id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('running','idle','completed','paused','stopped','degraded','never_started')),
            heartbeat_at TEXT NOT NULL,
            last_result TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        CREATE INDEX IF NOT EXISTS idx_scheduler_heartbeats_scope
            ON scheduler_heartbeats(organization_id, workspace_id, updated_at);
        CREATE TABLE IF NOT EXISTS scheduler_controls (
            organization_id TEXT PRIMARY KEY,
            workspace_id TEXT,
            paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
            reason TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        """,
    ),
    Migration(
        41,
        "release_blocker_scoped_scheduler_and_import_quarantines",
        """
        ALTER TABLE provider_import_quarantines ADD COLUMN quarantine_details TEXT;

        ALTER TABLE scheduler_heartbeats RENAME TO scheduler_heartbeats_v40;
        CREATE TABLE scheduler_heartbeats (
            worker_id TEXT NOT NULL,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            scope_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','idle','completed','paused','stopped','degraded','never_started')),
            heartbeat_at TEXT NOT NULL,
            last_result TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(worker_id, organization_id, scope_key),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        INSERT INTO scheduler_heartbeats(worker_id,organization_id,workspace_id,scope_key,status,heartbeat_at,last_result,last_error,updated_at)
            SELECT worker_id,organization_id,workspace_id,COALESCE(workspace_id,'__organization__'),status,heartbeat_at,last_result,last_error,updated_at
            FROM scheduler_heartbeats_v40;
        DROP TABLE scheduler_heartbeats_v40;
        CREATE INDEX IF NOT EXISTS idx_scheduler_heartbeats_scope
            ON scheduler_heartbeats(organization_id, workspace_id, updated_at);

        ALTER TABLE scheduler_controls RENAME TO scheduler_controls_v40;
        CREATE TABLE scheduler_controls (
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            scope_key TEXT NOT NULL,
            paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
            reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(organization_id, scope_key),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        INSERT INTO scheduler_controls(organization_id,workspace_id,scope_key,paused,reason,updated_at)
            SELECT organization_id,workspace_id,COALESCE(workspace_id,'__organization__'),paused,reason,updated_at
            FROM scheduler_controls_v40;
        DROP TABLE scheduler_controls_v40;
        """,
    ),
    Migration(
        42,
        "intelligence_expert_runbook_foundation",
        """
        CREATE TABLE IF NOT EXISTS expert_profiles (
            id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            name TEXT NOT NULL,
            specialty TEXT NOT NULL,
            mission TEXT NOT NULL,
            required_inputs_json TEXT NOT NULL,
            allowed_domains_json TEXT NOT NULL,
            allowed_tools_json TEXT NOT NULL,
            required_evidence_json TEXT NOT NULL,
            reasoning_method TEXT NOT NULL,
            output_schema_json TEXT NOT NULL,
            evaluation_criteria_json TEXT NOT NULL,
            escalation_policy TEXT NOT NULL,
            fallback_policy TEXT NOT NULL,
            max_context INTEGER NOT NULL CHECK(max_context > 0),
            max_iterations INTEGER NOT NULL CHECK(max_iterations > 0),
            capability_level TEXT NOT NULL CHECK(capability_level IN ('L0','L1','L2','L3')),
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            domains_json TEXT NOT NULL,
            allowed_tool_refs_json TEXT NOT NULL,
            reasoning_methods_json TEXT NOT NULL,
            activation_triggers_json TEXT NOT NULL,
            outputs_json TEXT NOT NULL,
            handoff_targets_json TEXT NOT NULL,
            quality_gates_json TEXT NOT NULL,
            constraints_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','retired')),
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_expert_profiles_active
            ON expert_profiles(status, name, id);
        CREATE TRIGGER IF NOT EXISTS expert_profiles_no_update BEFORE UPDATE ON expert_profiles BEGIN
            SELECT RAISE(ABORT, 'expert profiles are immutable versioned contracts');
        END;
        CREATE TRIGGER IF NOT EXISTS expert_profiles_no_delete BEFORE DELETE ON expert_profiles BEGIN
            SELECT RAISE(ABORT, 'expert profiles are immutable versioned contracts');
        END;

        CREATE TABLE IF NOT EXISTS intelligence_runbooks (
            id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            name TEXT NOT NULL,
            trigger TEXT NOT NULL,
            required_domains_json TEXT NOT NULL,
            required_evidence_json TEXT NOT NULL,
            specialists_json TEXT NOT NULL,
            topology TEXT NOT NULL,
            stages_json TEXT NOT NULL,
            quality_gates_json TEXT NOT NULL,
            contradiction_policy TEXT NOT NULL,
            scenario_policy TEXT NOT NULL,
            escalation_policy TEXT NOT NULL,
            max_iterations INTEGER NOT NULL CHECK(max_iterations > 0),
            output_contract_json TEXT NOT NULL,
            capability_level TEXT NOT NULL CHECK(capability_level IN ('L0','L1','L2','L3')),
            summary TEXT NOT NULL,
            intent TEXT NOT NULL,
            domains_json TEXT NOT NULL,
            profile_ids_json TEXT NOT NULL,
            activation_sequence_json TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            handoff_gates_json TEXT NOT NULL,
            required_inputs_json TEXT NOT NULL,
            outputs_json TEXT NOT NULL,
            stop_conditions_json TEXT NOT NULL,
            allowed_tool_refs_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','retired')),
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_intelligence_runbooks_active
            ON intelligence_runbooks(status, name, id);
        CREATE TRIGGER IF NOT EXISTS intelligence_runbooks_no_update BEFORE UPDATE ON intelligence_runbooks BEGIN
            SELECT RAISE(ABORT, 'intelligence runbooks are immutable versioned contracts');
        END;
        CREATE TRIGGER IF NOT EXISTS intelligence_runbooks_no_delete BEFORE DELETE ON intelligence_runbooks BEGIN
            SELECT RAISE(ABORT, 'intelligence runbooks are immutable versioned contracts');
        END;
        """,
    ),
    Migration(
        43,
        "intelligence_learning_persistence",
        """
        CREATE TABLE IF NOT EXISTS intelligence_hypotheses (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            text TEXT NOT NULL,
            evidence_for_refs_json TEXT NOT NULL,
            evidence_against_refs_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('proposed','supported','challenged','refuted','resolved','retired')),
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            assumptions_json TEXT NOT NULL,
            generated_by_type TEXT NOT NULL CHECK(generated_by_type IN ('person','agent','expert_profile','runbook','model','system')),
            generated_by_id TEXT NOT NULL,
            recorded_by_person_id TEXT NOT NULL,
            supersedes_hypothesis_id TEXT,
            resolution TEXT,
            outcome_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(recorded_by_person_id) REFERENCES people(id),
            FOREIGN KEY(supersedes_hypothesis_id) REFERENCES intelligence_hypotheses(id)
        );
        CREATE INDEX IF NOT EXISTS idx_intelligence_hypotheses_scope
            ON intelligence_hypotheses(organization_id, workspace_id, created_at, id);
        CREATE TRIGGER IF NOT EXISTS intelligence_hypotheses_no_update BEFORE UPDATE ON intelligence_hypotheses BEGIN
            SELECT RAISE(ABORT, 'intelligence hypotheses are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS intelligence_hypotheses_no_delete BEFORE DELETE ON intelligence_hypotheses BEGIN
            SELECT RAISE(ABORT, 'intelligence hypotheses are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS audit_intelligence_hypotheses_insert AFTER INSERT ON intelligence_hypotheses BEGIN
            INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'person',NEW.recorded_by_person_id,'create','intelligence_hypothesis',NEW.id,NEW.status,CURRENT_TIMESTAMP);
        END;

        CREATE TABLE IF NOT EXISTS intelligence_recommendations (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            runbook_id TEXT NOT NULL,
            runbook_version INTEGER NOT NULL CHECK(runbook_version > 0),
            profile_contributors_json TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            options_json TEXT NOT NULL,
            recommended_option_id TEXT,
            evidence_refs_json TEXT NOT NULL,
            generated_by_type TEXT NOT NULL CHECK(generated_by_type IN ('person','agent','expert_profile','runbook','model','system')),
            generated_by_id TEXT NOT NULL,
            recorded_by_person_id TEXT NOT NULL,
            evaluation_window_start TEXT NOT NULL,
            evaluation_window_end TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(recorded_by_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_intelligence_recommendations_scope
            ON intelligence_recommendations(organization_id, workspace_id, created_at, id);
        CREATE TRIGGER IF NOT EXISTS intelligence_recommendations_no_update BEFORE UPDATE ON intelligence_recommendations BEGIN
            SELECT RAISE(ABORT, 'intelligence recommendations are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS intelligence_recommendations_no_delete BEFORE DELETE ON intelligence_recommendations BEGIN
            SELECT RAISE(ABORT, 'intelligence recommendations are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS audit_intelligence_recommendations_insert AFTER INSERT ON intelligence_recommendations BEGIN
            INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'person',NEW.recorded_by_person_id,'create','intelligence_recommendation',NEW.id,NEW.runbook_id,CURRENT_TIMESTAMP);
        END;

        CREATE TABLE IF NOT EXISTS intelligence_recommendation_lifecycle (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            recommendation_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('accepted','rejected','chosen','evaluated')),
            accepted INTEGER CHECK(accepted IN (0,1)),
            rejected INTEGER CHECK(rejected IN (0,1)),
            chosen_option_id TEXT,
            evaluation_window_start TEXT,
            evaluation_window_end TEXT,
            measured_outcomes_json TEXT NOT NULL,
            score REAL CHECK(score IS NULL OR (score >= 0 AND score <= 1)),
            lessons TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL,
            recorded_by_person_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(recommendation_id) REFERENCES intelligence_recommendations(id),
            FOREIGN KEY(recorded_by_person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_intelligence_recommendation_lifecycle
            ON intelligence_recommendation_lifecycle(organization_id, workspace_id, recommendation_id, created_at, id);
        CREATE TRIGGER IF NOT EXISTS intelligence_recommendation_lifecycle_no_update BEFORE UPDATE ON intelligence_recommendation_lifecycle BEGIN
            SELECT RAISE(ABORT, 'intelligence recommendation lifecycle is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS intelligence_recommendation_lifecycle_no_delete BEFORE DELETE ON intelligence_recommendation_lifecycle BEGIN
            SELECT RAISE(ABORT, 'intelligence recommendation lifecycle is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS audit_intelligence_recommendation_lifecycle_insert AFTER INSERT ON intelligence_recommendation_lifecycle BEGIN
            INSERT INTO ledger_audit VALUES ('audit_'||lower(hex(randomblob(8))),NEW.organization_id,NEW.workspace_id,'person',NEW.recorded_by_person_id,NEW.event_type,'intelligence_recommendation',NEW.recommendation_id,NEW.id,CURRENT_TIMESTAMP);
        END;

        CREATE TABLE IF NOT EXISTS intelligence_learning_idempotency_keys (
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            key TEXT NOT NULL,
            operation TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            response TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(organization_id, workspace_id, key, operation),
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );
        """,
    ),
    Migration(
        44,
        "intelligence_evaluation_safety",
        """
        CREATE TABLE IF NOT EXISTS intelligence_evaluation_runs (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            agent_run_id TEXT,
            trace_id TEXT,
            task_class TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            specialist_profile_id TEXT,
            runbook_id TEXT,
            runbook_version INTEGER,
            status TEXT NOT NULL CHECK(status IN ('running','completed','capped','failed','shadow_only')),
            shadow_only INTEGER NOT NULL CHECK(shadow_only IN (0,1)),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            latency_ms INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_amount REAL,
            cost_currency TEXT,
            evidence_completeness REAL CHECK(evidence_completeness IS NULL OR (evidence_completeness >= 0 AND evidence_completeness <= 1)),
            evaluator_score REAL CHECK(evaluator_score IS NULL OR (evaluator_score >= 0 AND evaluator_score <= 1)),
            human_acceptance INTEGER CHECK(human_acceptance IS NULL OR human_acceptance IN (0,1)),
            revision_count INTEGER NOT NULL DEFAULT 0,
            downstream_outcome_score REAL CHECK(downstream_outcome_score IS NULL OR (downstream_outcome_score >= 0 AND downstream_outcome_score <= 1)),
            cap_reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(agent_run_id) REFERENCES agent_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_intelligence_evaluation_scope
            ON intelligence_evaluation_runs(organization_id, workspace_id, task_class, created_at DESC);
        CREATE TABLE IF NOT EXISTS intelligence_evaluation_policies (
            organization_id TEXT NOT NULL,
            task_class TEXT NOT NULL,
            max_runtime_ms INTEGER NOT NULL,
            max_cost_amount REAL NOT NULL,
            max_tokens INTEGER NOT NULL,
            breaker_threshold INTEGER NOT NULL,
            breaker_window_seconds INTEGER NOT NULL,
            breaker_open_seconds INTEGER NOT NULL,
            failure_count INTEGER NOT NULL DEFAULT 0,
            breaker_open_until TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(organization_id, task_class),
            FOREIGN KEY(organization_id) REFERENCES organizations(id)
        );
        CREATE TABLE IF NOT EXISTS intelligence_evaluation_circuit_events (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            task_class TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('opened','closed','cap_exceeded','failure')),
            evaluation_id TEXT,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(evaluation_id) REFERENCES intelligence_evaluation_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_intelligence_evaluation_circuit
            ON intelligence_evaluation_circuit_events(organization_id, task_class, created_at DESC);
        CREATE TRIGGER IF NOT EXISTS intelligence_evaluation_circuit_no_update BEFORE UPDATE ON intelligence_evaluation_circuit_events BEGIN
            SELECT RAISE(ABORT, 'intelligence evaluation circuit events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS intelligence_evaluation_circuit_no_delete BEFORE DELETE ON intelligence_evaluation_circuit_events BEGIN
            SELECT RAISE(ABORT, 'intelligence evaluation circuit events are append-only');
        END;
        """,
    ),
    Migration(
        45,
        "proactive_intelligence_attention_lifecycle",
        """
        CREATE TABLE IF NOT EXISTS proactive_intelligence_attention_lifecycle (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT,
            person_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            attention_item_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('new','acknowledged','acted_on','resolved','dismissed','resurfaced')),
            trace_id TEXT,
            recommendation_id TEXT,
            action_descriptor_json TEXT NOT NULL DEFAULT '{}',
            approval_request_id TEXT,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(person_id) REFERENCES people(id),
            FOREIGN KEY(snapshot_id) REFERENCES proactive_intelligence_snapshots(id),
            FOREIGN KEY(attention_item_id) REFERENCES proactive_intelligence_attention_items(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_proactive_attention_lifecycle_current
            ON proactive_intelligence_attention_lifecycle(organization_id, COALESCE(workspace_id,''), person_id, fingerprint);
        CREATE INDEX IF NOT EXISTS idx_proactive_attention_lifecycle_scope
            ON proactive_intelligence_attention_lifecycle(organization_id, COALESCE(workspace_id,''), person_id, updated_at DESC);
        CREATE TRIGGER IF NOT EXISTS proactive_attention_lifecycle_no_delete BEFORE DELETE ON proactive_intelligence_attention_lifecycle BEGIN
            SELECT RAISE(ABORT, 'proactive attention lifecycle is append-only');
        END;
        """,
    ),
    Migration(
        46,
        "intelligence_orchestrator_runs",
        """
        CREATE TABLE IF NOT EXISTS intelligence_orchestrator_runs (
            trace_id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            person_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ready','degraded')),
            result_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(organization_id) REFERENCES organizations(id),
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY(person_id) REFERENCES people(id)
        );
        CREATE INDEX IF NOT EXISTS idx_intelligence_orchestrator_scope
            ON intelligence_orchestrator_runs(organization_id,workspace_id,person_id,created_at DESC);
        CREATE TRIGGER IF NOT EXISTS intelligence_orchestrator_runs_no_update
            BEFORE UPDATE ON intelligence_orchestrator_runs BEGIN
            SELECT RAISE(ABORT, 'intelligence orchestrator runs are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS intelligence_orchestrator_runs_no_delete
            BEFORE DELETE ON intelligence_orchestrator_runs BEGIN
            SELECT RAISE(ABORT, 'intelligence orchestrator runs are append-only');
        END;
        """,
    ),
    Migration(
        47,
        "supervised_reversible_agent_actions",
        """
        ALTER TABLE agent_tasks ADD COLUMN action_descriptor_json TEXT;
        ALTER TABLE agent_tasks ADD COLUMN orchestrator_trace_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_agent_tasks_orchestrator_trace ON agent_tasks(orchestrator_trace_id);
        """,
    ),
)

_AGENT_LEVEL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "L0": ("execute", "format", "extract", "summarize", "draft"),
    "L1": ("execute", "format", "extract", "summarize", "draft", "reason", "produce", "communicate", "route", "schedule"),
    "L2": (
        "execute", "format", "extract", "summarize", "draft",
        "reason", "produce", "communicate", "route", "schedule",
        "build", "verify", "review", "diagnose", "implement",
    ),
    "L3": (
        "execute", "format", "extract", "summarize", "draft",
        "reason", "produce", "communicate", "route", "schedule",
        "build", "verify", "review", "diagnose", "implement",
        "strategize", "architect", "assess_risk", "synthesize", "decide",
    ),
}


_PRIMARY_AGENT_LEVELS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("Sol", ("advisor_reviewer", "strategic_reviewer"), "L3"),
    ("Terra", ("builder",), "L2"),
    ("Luna", ("executor", "operator"), "L1"),
)


def _json_compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _backfill_primary_agent_levels(conn: sqlite3.Connection) -> None:
    """Upgrade only the stable seeded-agent semantics without changing identity fields."""

    for agent_name, role_names, level in _PRIMARY_AGENT_LEVELS:
        conn.execute(
            f"""UPDATE agents
                SET level=?, capability_tags=?
                WHERE name=?
                  AND role_id IN (
                    SELECT id FROM agent_roles
                    WHERE agent_roles.organization_id=agents.organization_id
                      AND name IN ({",".join("?" for _ in role_names)})
                  )""",
            (level, _json_compact(list(_AGENT_LEVEL_CAPABILITIES[level])), agent_name, *role_names),
        )


def _supervised_reversible_agent_actions_sql(conn: sqlite3.Connection) -> str:
    task_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_tasks)").fetchall()}
    statements: list[str] = []
    for column in ("action_descriptor_json", "orchestrator_trace_id"):
        if column not in task_columns:
            statements.append(f"ALTER TABLE agent_tasks ADD COLUMN {column} TEXT;")
    statements.append(
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_orchestrator_trace ON agent_tasks(orchestrator_trace_id);"
    )
    return "\n".join(statements)


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    if 1 not in applied:
        conn.execute("INSERT INTO schema_migrations(version, name) VALUES (1, 'v0_1_kernel')")
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        sql = migration.sql
        # A few operators rebuild the migration ledger from a backup while
        # retaining already-created tables.  Keep the operation-key migration
        # replay-safe instead of failing on SQLite's non-idempotent ALTER.
        if migration.version == 15:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(provider_sync_tasks)").fetchall()
            }
            if "operation_key" in columns:
                sql = sql.replace("ALTER TABLE provider_sync_tasks ADD COLUMN operation_key TEXT;", "")
        if migration.version == 18:
            entity_columns = {row[1] for row in conn.execute("PRAGMA table_info(entities)").fetchall()}
            alias_columns = {row[1] for row in conn.execute("PRAGMA table_info(entity_aliases)").fetchall()}
            proposal_columns = {row[1] for row in conn.execute("PRAGMA table_info(entity_resolution_proposals)").fetchall()}
            state_columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_state_events)").fetchall()}
            for column in ("status", "merged_into", "updated_at"):
                if column in entity_columns:
                    sql = sql.replace(f"ALTER TABLE entities ADD COLUMN {column} TEXT NOT NULL DEFAULT 'active';", "")
                    sql = sql.replace(f"ALTER TABLE entities ADD COLUMN {column} TEXT;", "")
            for column in ("reviewed_by_person_id", "reviewed_at", "evidence", "retired_at"):
                if column in alias_columns:
                    sql = sql.replace(f"ALTER TABLE entity_aliases ADD COLUMN {column} TEXT;", "")
            if proposal_columns and "evidence_refs" not in proposal_columns:
                conn.execute("ALTER TABLE entity_resolution_proposals ADD COLUMN evidence_refs TEXT NOT NULL DEFAULT '{}'")
            for column, definition in (
                ("event_sequence", "INTEGER NOT NULL DEFAULT 0"),
                ("supersedes_event_id", "TEXT"),
            ):
                if state_columns and column not in state_columns:
                    conn.execute(f"ALTER TABLE knowledge_state_events ADD COLUMN {column} {definition}")
            if state_columns and conn.execute(
                "SELECT 1 FROM knowledge_state_events WHERE event_sequence=0 LIMIT 1"
            ).fetchone():
                conn.execute("DROP TRIGGER IF EXISTS knowledge_state_no_update")
                prior_by_subject: dict[tuple[object, object, object, object], tuple[int, str]] = {}
                for row in conn.execute("""SELECT id,organization_id,workspace_id,subject_type,subject_id
                    FROM knowledge_state_events
                    ORDER BY organization_id,workspace_id,subject_type,subject_id,effective_from,recorded_at,id""").fetchall():
                    key=(row[1],row[2],row[3],row[4]); prior=prior_by_subject.get(key)
                    sequence=1 if prior is None else prior[0]+1
                    conn.execute(
                        "UPDATE knowledge_state_events SET event_sequence=?,supersedes_event_id=? WHERE id=?",
                        (sequence,None if prior is None else prior[1],row[0]),
                    )
                    prior_by_subject[key]=(sequence,row[0])
        if migration.version == 19:
            agent_columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            task_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_tasks)").fetchall()}
            for column, definition in (
                ("level", "TEXT NOT NULL DEFAULT 'L1'"),
                ("capability_tags", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if column in agent_columns:
                    sql = sql.replace(f"ALTER TABLE agents ADD COLUMN {column} {definition};", "")
            for column, definition in (
                ("intent_tags", "TEXT NOT NULL DEFAULT '[]'"),
                ("recommended_level", "TEXT NOT NULL DEFAULT 'L0'"),
                ("selected_level", "TEXT NOT NULL DEFAULT 'L0'"),
                ("level_override_reason", "TEXT"),
            ):
                if column in task_columns:
                    sql = sql.replace(f"ALTER TABLE agent_tasks ADD COLUMN {column} {definition};", "")
        if migration.version == 41:
            quarantine_columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_import_quarantines)").fetchall()}
            if "quarantine_details" in quarantine_columns:
                sql = sql.replace("ALTER TABLE provider_import_quarantines ADD COLUMN quarantine_details TEXT;", "")
        if migration.version == 47:
            sql = _supervised_reversible_agent_actions_sql(conn)
        with conn:
            conn.executescript(sql)
            if migration.version == 19:
                _backfill_primary_agent_levels(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
    # Schema-18 databases created before alias lifecycle events were added are
    # upgraded in place without changing the public schema number.  This is
    # intentionally idempotent and keeps append-only guards true for both new
    # installs and already-opened v18 stores.
    if 18 in {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}:
        with conn:
            proposal_columns = {row[1] for row in conn.execute("PRAGMA table_info(entity_resolution_proposals)").fetchall()}
            if "evidence_refs" not in proposal_columns:
                conn.execute("ALTER TABLE entity_resolution_proposals ADD COLUMN evidence_refs TEXT NOT NULL DEFAULT '{}'")
            state_columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_state_events)").fetchall()}
            if "event_sequence" not in state_columns:
                conn.execute("ALTER TABLE knowledge_state_events ADD COLUMN event_sequence INTEGER NOT NULL DEFAULT 0")
            if "supersedes_event_id" not in state_columns:
                conn.execute("ALTER TABLE knowledge_state_events ADD COLUMN supersedes_event_id TEXT")
            if conn.execute("SELECT 1 FROM knowledge_state_events WHERE event_sequence=0 LIMIT 1").fetchone():
                conn.execute("DROP TRIGGER IF EXISTS knowledge_state_no_update")
                rows = conn.execute("""
                    SELECT id,organization_id,workspace_id,subject_type,subject_id
                    FROM knowledge_state_events
                    ORDER BY organization_id,workspace_id,subject_type,subject_id,effective_from,recorded_at,id
                """).fetchall()
                previous: dict[tuple[object, object, object, object], tuple[int, str]] = {}
                for row in rows:
                    key = (row[1], row[2], row[3], row[4])
                    prior = previous.get(key)
                    sequence = 1 if prior is None else prior[0] + 1
                    conn.execute(
                        "UPDATE knowledge_state_events SET event_sequence=?,supersedes_event_id=? WHERE id=?",
                        (sequence, None if prior is None else prior[1], row[0]),
                    )
                    previous[key] = (sequence, row[0])
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS entity_alias_state_events (
                id TEXT PRIMARY KEY, alias_id TEXT NOT NULL, organization_id TEXT NOT NULL,
                workspace_id TEXT, state TEXT NOT NULL CHECK(state IN ('active','retired')),
                reason TEXT NOT NULL, actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(alias_id) REFERENCES entity_aliases(id),
                FOREIGN KEY(organization_id) REFERENCES organizations(id)
            );
            CREATE INDEX IF NOT EXISTS idx_entity_alias_state_lookup ON entity_alias_state_events(alias_id,created_at,id);
            CREATE TRIGGER IF NOT EXISTS entity_aliases_no_update_lifecycle BEFORE UPDATE OF retired_at,reviewed_by_person_id,reviewed_at,evidence ON entity_aliases BEGIN
                SELECT RAISE(ABORT,'entity alias lifecycle is append-only events'); END;
            CREATE TRIGGER IF NOT EXISTS entity_alias_state_no_update BEFORE UPDATE ON entity_alias_state_events BEGIN
                SELECT RAISE(ABORT,'entity alias state events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS entity_alias_state_no_delete BEFORE DELETE ON entity_alias_state_events BEGIN
                SELECT RAISE(ABORT,'entity alias state events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS knowledge_state_no_update BEFORE UPDATE ON knowledge_state_events BEGIN
                SELECT RAISE(ABORT,'knowledge state events are append-only'); END;
            DROP INDEX IF EXISTS idx_knowledge_state_lookup;
            DROP INDEX IF EXISTS idx_knowledge_state_sequence;
            CREATE INDEX IF NOT EXISTS idx_knowledge_state_lookup
                ON knowledge_state_events(workspace_id,subject_type,subject_id,effective_from,event_sequence);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_state_sequence
                ON knowledge_state_events(organization_id,COALESCE(workspace_id,''),subject_type,subject_id,event_sequence);
            CREATE TRIGGER IF NOT EXISTS knowledge_state_monotonic_insert BEFORE INSERT ON knowledge_state_events BEGIN
                SELECT CASE WHEN NEW.event_sequence != COALESCE((
                    SELECT MAX(event_sequence) FROM knowledge_state_events
                    WHERE organization_id=NEW.organization_id AND workspace_id IS NEW.workspace_id
                      AND subject_type=NEW.subject_type AND subject_id=NEW.subject_id
                ),0)+1 THEN RAISE(ABORT,'knowledge state sequence is not monotonic') END;
                SELECT CASE WHEN NEW.supersedes_event_id IS NOT (
                    SELECT id FROM knowledge_state_events
                    WHERE organization_id=NEW.organization_id AND workspace_id IS NEW.workspace_id
                      AND subject_type=NEW.subject_type AND subject_id=NEW.subject_id
                    ORDER BY event_sequence DESC LIMIT 1
                ) THEN RAISE(ABORT,'knowledge state supersedes link is invalid') END;
            END;
            """)
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)

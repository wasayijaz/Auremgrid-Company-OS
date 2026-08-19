from __future__ import annotations

import sqlite3
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
)


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
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)

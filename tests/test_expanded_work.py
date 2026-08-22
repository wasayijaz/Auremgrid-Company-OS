from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS


class ExpandedWorkTests(unittest.TestCase):
    def setUp(self)->None:
        self.os=CompanyOS(":memory:");self.org=self.os.create_organization("Agency");self.ws=self.os.create_organization_workspace(self.org.id,"Prime","client")
        self.owner=self.os.create_person(self.org.id,"Owner",role="owner");self.worker=self.os.create_person(self.org.id,"Worker")
        self.os.add_person_to_workspace(self.org.id,self.ws.id,self.owner.id,"admin");self.os.add_person_to_workspace(self.org.id,self.ws.id,self.worker.id,"operator")
        self.project=self.os.create_project(self.org.id,self.ws.id,self.owner.id,"Campaign")

    def tearDown(self)->None:self.os.close()

    def test_full_work_shape_hierarchy_comments_files_and_versions(self)->None:
        parent=self.os.work_ops.create(self.org.id,self.ws.id,self.owner.id,"Landing page","Build page","Client",self.project.id,priority="high",tags=["web"],estimate_hours=10,brief="Approved brief",brain_context="Current offer")
        child=self.os.work_ops.create(self.org.id,self.ws.id,self.owner.id,"Mobile pass","QA responsive","Lead",self.project.id,parent_id=parent.id)
        self.os.work_ops.watch(self.org.id,self.ws.id,self.owner.id,parent.id,self.worker.id)
        self.os.work_ops.add_comment(self.org.id,self.ws.id,self.worker.id,parent.id,"Ready for review")
        self.os.work_ops.add_file(self.org.id,self.ws.id,self.worker.id,parent.id,"Preview","https://example.test/preview")
        updated=self.os.work_ops.update(self.org.id,self.ws.id,self.owner.id,parent.id,{"blocking_reason":"Waiting for copy"})
        detail=self.os.work_ops.detail(self.org.id,self.ws.id,self.owner.id,parent.id)
        self.assertEqual(updated.project_id,self.project.id);self.assertEqual(detail["subtasks"][0]["id"],child.id)
        self.assertEqual(detail["watchers"],[self.worker.id]);self.assertEqual(len(detail["versions"]),2)
        self.assertEqual(detail["files"][0]["title"],"Preview")
        self.assertEqual(detail["deliverables"],[]);self.assertEqual(detail["reviews"],[])
        self.assertEqual(detail["review_comments"],[])
        self.assertEqual(detail["annotation_capabilities"]["video_timestamps"]["status"],"not_available")
        self.assertEqual(detail["annotation_capabilities"]["image_points"]["status"],"not_available")

    def test_dependency_cycles_are_rejected(self)->None:
        first=self.os.work_ops.create(self.org.id,self.ws.id,self.owner.id,"A","A","Lead")
        second=self.os.work_ops.create(self.org.id,self.ws.id,self.owner.id,"B","B","Lead")
        self.os.work_ops.add_dependency(self.org.id,self.ws.id,self.owner.id,first.id,second.id)
        with self.assertRaises(ValidationError):self.os.work_ops.add_dependency(self.org.id,self.ws.id,self.owner.id,second.id,first.id)

    def test_parent_work_item_must_stay_in_the_same_project(self)->None:
        first_project=self.os.create_project(self.org.id,self.ws.id,self.owner.id,"First")
        second_project=self.os.create_project(self.org.id,self.ws.id,self.owner.id,"Second")
        parent=self.os.work_ops.create(self.org.id,self.ws.id,self.owner.id,"Parent","Parent","Lead",first_project.id)
        with self.assertRaises(ValidationError):
            self.os.work_ops.create(self.org.id,self.ws.id,self.owner.id,"Child","Child","Lead",second_project.id,parent_id=parent.id)

    def test_time_tracking_updates_actual_effort(self)->None:
        item=self.os.work_ops.create(self.org.id,self.ws.id,self.owner.id,"Edit","Edit video","Client",estimate_hours=3)
        start=datetime.now(timezone.utc);self.os.work_ops.log_time(self.org.id,self.ws.id,self.worker.id,item.id,start,start+timedelta(hours=2.5))
        self.assertEqual(self.os.work_ops.detail(self.org.id,self.ws.id,self.owner.id,item.id)["work_item"]["actual_effort_hours"],2.5)

    def test_viewer_cannot_mutate_expanded_work(self)->None:
        viewer=self.os.create_person(self.org.id,"Viewer");self.os.add_person_to_workspace(self.org.id,self.ws.id,viewer.id,"viewer")
        with self.assertRaises(AuthorizationError):self.os.work_ops.create(self.org.id,self.ws.id,viewer.id,"No","No","No")

    def test_work_transition_is_versioned_idempotent_and_audited(self)->None:
        item=self.os.work_ops.create(self.org.id,self.ws.id,self.owner.id,"Lifecycle","Move safely","Client")
        detail=self.os.work_ops.detail(self.org.id,self.ws.id,self.owner.id,item.id)
        self.assertEqual(detail["allowed_transitions"],["assigned"])
        result=self.os.work_ops.transition(
            self.org.id,self.ws.id,self.owner.id,item.id,"assigned","Accepted by owner",
            expected_version=detail["version"],idempotency_key="work-transition-1",
        )
        self.assertEqual(result["work_item"]["status"],"assigned")
        self.assertEqual(result["version"],detail["version"]+1)
        replay=self.os.work_ops.transition(
            self.org.id,self.ws.id,self.owner.id,item.id,"assigned","Accepted by owner",
            expected_version=detail["version"],idempotency_key="work-transition-1",
        )
        self.assertEqual(replay,result)
        self.assertEqual(
            1,
            self.os.store.conn.execute(
                "SELECT COUNT(*) FROM work_events WHERE work_item_id=? AND action='status_transition'",
                (item.id,),
            ).fetchone()[0],
        )
        audits=[dict(row) for row in self.os.store.conn.execute(
            "SELECT action,entity_type,entity_id FROM ledger_audit WHERE workspace_id=? AND entity_id=?",
            (self.ws.id,item.id),
        ).fetchall()]
        self.assertTrue(any(row["action"]=="update" and row["entity_type"]=="work_item" for row in audits))
        with self.assertRaises(ValidationError):
            self.os.work_ops.transition(
                self.org.id,self.ws.id,self.owner.id,item.id,"in_progress","Different payload",
                expected_version=result["version"],idempotency_key="work-transition-1",
            )

    def test_work_transition_rejects_stale_illegal_and_hides_transitions_for_viewer(self)->None:
        item=self.os.work_ops.create(self.org.id,self.ws.id,self.owner.id,"Guarded","Move safely","Client")
        with self.assertRaises(ValidationError):
            self.os.work_ops.transition(self.org.id,self.ws.id,self.owner.id,item.id,"shipped",expected_version=1)
        with self.assertRaises(ValidationError):
            self.os.work_ops.transition(self.org.id,self.ws.id,self.owner.id,item.id,"assigned",expected_version=0)
        viewer=self.os.create_person(self.org.id,"Read only")
        self.os.add_person_to_workspace(self.org.id,self.ws.id,viewer.id,"viewer")
        detail=self.os.work_ops.detail(self.org.id,self.ws.id,viewer.id,item.id)
        self.assertEqual(detail["allowed_transitions"],[])
        with self.assertRaises(AuthorizationError):
            self.os.work_ops.transition(self.org.id,self.ws.id,viewer.id,item.id,"assigned",expected_version=detail["version"])


if __name__=="__main__":unittest.main()

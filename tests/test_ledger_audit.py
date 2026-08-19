from __future__ import annotations
import unittest
from auremgrid.services.brain import CompanyOS

class LedgerAuditTests(unittest.TestCase):
    def test_major_domain_writes_create_automatic_audit_rows(self):
        os=CompanyOS(":memory:");org=os.create_organization("Agency");ws=os.create_organization_workspace(org.id,"Prime","client");owner=os.create_person(org.id,"Owner",role="owner");os.add_person_to_workspace(org.id,ws.id,owner.id,"admin")
        project=os.create_project(org.id,ws.id,owner.id,"Project");os.work_ops.create(org.id,ws.id,owner.id,"Work","Request","Client",project.id);os.client_ops.create_risk(org.id,ws.id,owner.id,"delivery","high",.8,"Late","Evidence","Recover")
        rows=os.store.conn.execute("SELECT entity_type,action FROM ledger_audit ORDER BY rowid").fetchall();os.close()
        self.assertTrue({"project","work_item","risk"}<={row["entity_type"] for row in rows});self.assertTrue(all(row["action"]=="create" for row in rows))

if __name__=="__main__":unittest.main()

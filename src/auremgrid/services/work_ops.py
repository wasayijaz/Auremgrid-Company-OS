from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import NotFoundError, ValidationError
from auremgrid.domain.ops import WorkItem, default_dod


def _now() -> datetime: return datetime.now(timezone.utc).replace(microsecond=0)


class WorkOperations:
    def __init__(self, store: Any, company: Any, new_id: Callable[[str],str], authorize: Callable[...,Any]) -> None:
        self.store,self.conn,self.company,self.new_id,self.authorize=store,store.conn,company,new_id,authorize

    def create(self, organization_id: str, workspace_id: str, person_id: str, title: str, request: str,
        requested_by: str, project_id: str | None = None, campaign_id: str | None = None,
        parent_id: str | None = None, priority: str = "normal", tags: list[str] | None = None,
        estimate_hours: float | None = None, deadline: str | None = None, brief: str = "",
        brain_context: str = "", financial_value: float | None = None) -> WorkItem:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if not all(value.strip() for value in (title,request,requested_by)): raise ValidationError("title, request, and requested_by are required")
        if priority not in {"low","normal","high","urgent"}: raise ValidationError("invalid priority")
        if project_id and self.company.get_project(workspace_id,project_id) is None: raise NotFoundError("project not found")
        if campaign_id and not self.conn.execute("SELECT id FROM campaigns WHERE workspace_id=? AND id=?",(workspace_id,campaign_id)).fetchone(): raise NotFoundError("campaign not found")
        if parent_id:
            parent = self.store.get_work_item(workspace_id, parent_id)
            if parent is None:
                raise NotFoundError("parent work item not found")
            # A work hierarchy is project-scoped.  Do not let a child from one
            # project silently appear beneath a parent belonging to another.
            if project_id and parent.project_id and project_id != parent.project_id:
                raise ValidationError("parent work item belongs to a different project")
        now=_now(); item=WorkItem(self.new_id("work"),workspace_id,title.strip(),request.strip(),requested_by.strip(),deadline,
            "captured",None,None,None,default_dod(),now,now,project_id,campaign_id,parent_id,person_id,None,None,priority,
            tuple(tags or ()),estimate_hours,0,None,deadline,None,brief,brain_context,financial_value)
        self.store.upsert_work_item(item); self.snapshot(item,"person",person_id); return item

    def update(self, organization_id: str, workspace_id: str, person_id: str, work_item_id: str, changes: dict[str,Any]) -> WorkItem:
        self.authorize(organization_id,workspace_id,person_id,write=True); item=self._item(workspace_id,work_item_id)
        allowed={"title","request","priority","tags","estimate_hours","deadline","blocking_reason","brief","brain_context","financial_value","reviewer_person_id"}
        unknown=set(changes)-allowed
        if unknown: raise ValidationError(f"unsupported work fields: {', '.join(sorted(unknown))}")
        values={**item.__dict__,**changes,"updated_at":_now()}
        if "tags" in changes: values["tags"]=tuple(changes["tags"])
        updated=WorkItem(**values); self.store.upsert_work_item(updated); self.snapshot(updated,"person",person_id); return updated

    def assign(self, organization_id: str, workspace_id: str, person_id: str, work_item_id: str, assignee_person_id: str) -> WorkItem:
        self.authorize(organization_id,workspace_id,person_id,write=True); self.authorize(organization_id,workspace_id,assignee_person_id)
        item=self._item(workspace_id,work_item_id)
        if item.status!="captured": raise ValidationError("only captured work can be assigned")
        updated=WorkItem(**{**item.__dict__,"status":"assigned","assignee_person_id":assignee_person_id,"updated_at":_now()})
        self.store.upsert_work_item(updated); self.snapshot(updated,"person",person_id); return updated

    def add_dependency(self, organization_id: str, workspace_id: str, person_id: str, work_item_id: str,
        depends_on_id: str, kind: str = "blocks") -> dict[str,str]:
        self.authorize(organization_id,workspace_id,person_id,write=True); self._item(workspace_id,work_item_id); self._item(workspace_id,depends_on_id)
        if work_item_id==depends_on_id or self._reachable(depends_on_id,work_item_id): raise ValidationError("work dependency would create a cycle")
        self.conn.execute("INSERT INTO work_dependencies VALUES (?,?,?)",(work_item_id,depends_on_id,kind));self.conn.commit()
        return {"work_item_id":work_item_id,"depends_on_work_item_id":depends_on_id,"kind":kind}

    def add_comment(self, organization_id: str, workspace_id: str, person_id: str, work_item_id: str, body: str) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True);self._item(workspace_id,work_item_id)
        item={"id":self.new_id("comment"),"work_item_id":work_item_id,"author_type":"person","author_id":person_id,"body":body,"created_at":_now().isoformat(),"edited_at":None}
        self.conn.execute("INSERT INTO work_comments VALUES (?,?,?,?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def watch(self, organization_id: str, workspace_id: str, person_id: str, work_item_id: str, watcher_person_id: str) -> None:
        self.authorize(organization_id,workspace_id,person_id,write=True);self.authorize(organization_id,workspace_id,watcher_person_id);self._item(workspace_id,work_item_id)
        self.conn.execute("INSERT OR IGNORE INTO work_watchers VALUES (?,?)",(work_item_id,watcher_person_id));self.conn.commit()

    def add_file(self, organization_id: str, workspace_id: str, person_id: str, work_item_id: str,
        title: str, url: str, source: str = "manual") -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True);self._item(workspace_id,work_item_id)
        item={"id":self.new_id("file"),"work_item_id":work_item_id,"title":title,"url":url,"source":source,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO work_files VALUES (?,?,?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def log_time(self, organization_id: str, workspace_id: str, person_id: str, work_item_id: str,
        started_at: datetime, ended_at: datetime, notes: str = "", billable: bool = True) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True);item=self._item(workspace_id,work_item_id)
        hours=(ended_at-started_at).total_seconds()/3600
        if hours<=0: raise ValidationError("time entry must have positive duration")
        entry={"id":self.new_id("time"),"organization_id":organization_id,"workspace_id":workspace_id,"work_item_id":work_item_id,
            "person_id":person_id,"started_at":started_at.isoformat(),"ended_at":ended_at.isoformat(),"duration_hours":hours,"notes":notes,"billable":int(billable)}
        self.conn.execute("INSERT INTO time_entries VALUES (?,?,?,?,?,?,?,?,?,?)",tuple(entry.values()))
        updated=WorkItem(**{**item.__dict__,"actual_effort_hours":item.actual_effort_hours+hours,"updated_at":_now()});self.store.upsert_work_item(updated);self.conn.commit();return {**entry,"billable":billable}

    def detail(self, organization_id: str, workspace_id: str, person_id: str, work_item_id: str) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id);item=self._item(workspace_id,work_item_id)
        result={"work_item":item.to_dict()}
        for key,table in (("comments","work_comments"),("versions","work_versions"),("files","work_files"),("links","work_links"),("time_entries","time_entries")):
            result[key]=[dict(r) for r in self.conn.execute(f"SELECT * FROM {table} WHERE work_item_id=?",(work_item_id,)).fetchall()]
        result["watchers"]=[r[0] for r in self.conn.execute("SELECT person_id FROM work_watchers WHERE work_item_id=?",(work_item_id,)).fetchall()]
        result["dependencies"]=[dict(r) for r in self.conn.execute("SELECT * FROM work_dependencies WHERE work_item_id=?",(work_item_id,)).fetchall()]
        result["subtasks"]=[w.to_dict() for w in self.store.list_work_items(workspace_id) if w.parent_id==work_item_id]
        return result

    def snapshot(self,item:WorkItem,actor_type:str,actor_id:str)->None:
        version=self.conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM work_versions WHERE work_item_id=?",(item.id,)).fetchone()[0]
        self.conn.execute("INSERT INTO work_versions VALUES (?,?,?,?,?,?,?)",(self.new_id("workversion"),item.id,version,json.dumps(item.to_dict()),actor_type,actor_id,_now().isoformat()));self.conn.commit()
    def _item(self,workspace_id:str,item_id:str)->WorkItem:
        item=self.store.get_work_item(workspace_id,item_id)
        if item is None:raise NotFoundError("work item not found")
        return item
    def _reachable(self,start:str,target:str)->bool:
        seen=set();stack=[start]
        while stack:
            current=stack.pop()
            if current==target:return True
            if current in seen:continue
            seen.add(current);stack.extend(r[0] for r in self.conn.execute("SELECT depends_on_work_item_id FROM work_dependencies WHERE work_item_id=?",(current,)).fetchall())
        return False

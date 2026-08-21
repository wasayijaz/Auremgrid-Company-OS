"""Bounded, exact-file Figma connector."""
from __future__ import annotations
import hashlib,json
from dataclasses import dataclass
from typing import Any,Iterable,Mapping
from urllib.parse import quote
from auremgrid.connectors.google_auth import ConnectorSourceEvent,RouteLifecycleMutation
from auremgrid.connectors.http import ConnectorTransportError,HttpTransport,sanitize_content
from auremgrid.domain.errors import ValidationError

FIGMA_REQUIRED_PERMISSIONS=frozenset({"current_user:read","file_metadata:read","file_content:read"})
FIGMA_OPTIONAL_PERMISSIONS=frozenset({"file_versions:read","comments:read"})
FIGMA_MAX_FRAME_EVENTS=200
FIGMA_MAX_FRAME_TEXT=8000
FIGMA_MAX_FRAME_PATH_ITEMS=32
FIGMA_FRAME_TYPES=frozenset({"FRAME","SECTION"})
FIGMA_MAX_VERSION_EVENTS=50
FIGMA_MAX_COMMENTS=100
FIGMA_MAX_COMMENT_TEXT=4000
class FigmaMappingOverlap(ValidationError):
    def __init__(self,evidence_digest:str): super().__init__("Figma mapping overlap");self.evidence_digest=evidence_digest
@dataclass(frozen=True)
class FigmaAccountIdentity: user_id:str;email:str|None;granted_permissions:frozenset[str]
@dataclass(frozen=True)
class FigmaPullResult:
    events:tuple[ConnectorSourceEvent,...];next_cursor:str|None;has_more:bool=False;lifecycle_mutations:tuple[RouteLifecycleMutation,...]=()

class FigmaConnector:
    name="figma"
    def __init__(self,access_token:str,transport:Any|None=None,*,file_workspace_mappings:Mapping[str,str]|None=None,
                 expected_account_id:str|None=None,granted_permissions:Iterable[str]=(),
                 route_state:Mapping[str,Iterable[str]]|None=None,owned_route_key:str|None=None):
        if not isinstance(access_token,str) or not access_token.strip(): raise ValidationError("Figma access token is required")
        self.token=access_token;self.transport=transport or HttpTransport();self.mappings=dict(file_workspace_mappings or {})
        self.expected_account_id=expected_account_id;self.granted_permissions=frozenset(str(v) for v in granted_permissions);self.route_state={str(k):set(v) for k,v in (route_state or {}).items()};self.owned_route_key=owned_route_key or (next(iter(self.mappings)) if len(self.mappings)==1 else None)
        if not self.mappings or any(not str(k).startswith("file:") or not str(k)[5:].strip() for k in self.mappings): raise ValidationError("Figma mappings must use file:<key>")
    def verify_credentials(self)->FigmaAccountIdentity:
        p=self._get("https://api.figma.com/v1/me");uid=str(p.get("id") or "")
        if not uid or self.expected_account_id and uid!=self.expected_account_id: raise ConnectorTransportError("Figma account identity mismatch",status=401)
        for route in self.mappings:
            key=quote(route[5:],safe='')
            self._metadata(key)
            self._get(f"https://api.figma.com/v1/files/{key}?depth=1")
            if "file_versions:read" in self.granted_permissions:
                self._get(f"https://api.figma.com/v1/files/{key}/versions?page_size=1")
            if "comments:read" in self.granted_permissions:
                self._get(f"https://api.figma.com/v1/files/{key}/comments?page_size=1")
        proven=set(FIGMA_REQUIRED_PERMISSIONS)
        if "file_versions:read" in self.granted_permissions:proven.add("file_versions:read")
        if "comments:read" in self.granted_permissions:proven.add("comments:read")
        return FigmaAccountIdentity(uid,_text(p.get("email")),frozenset(proven))
    def pull(self,cursor:str|None=None)->FigmaPullResult:
        state=_parse_cursor(cursor);route=self.owned_route_key
        if route not in self.mappings: raise ValidationError("Figma pull requires one owned file mapping")
        key=route[5:]
        if state and state["file_key"]!=key: raise ValidationError("Figma cursor belongs to a different file")
        external=f"figma/files/{key}";conflicts={r for r in self.route_state.get(external,set()) if r!=route and self.mappings.get(r)!=self.mappings[route]}
        if conflicts: raise FigmaMappingOverlap(_digest(external,route,*sorted(conflicts)))
        try:
            metadata=self._metadata(quote(key,safe=''))
            version=str(metadata.get("version") or metadata.get("last_touched_at") or "").strip()
            if not version:raise ConnectorTransportError("Figma metadata response has no current version")
        except ConnectorTransportError as exc:
            if exc.status!=404: raise
            if state is None: raise ConnectorTransportError("Figma mapped file is inaccessible",status=404,retryable=False) from exc
            if state["provider_version"].startswith("deleted:"):return FigmaPullResult((),_cursor(key,state["provider_version"]))
            version=f"deleted:{state['provider_version'] if state else 'unknown'}";dedupe=_digest(external,version)
            event=ConnectorSourceEvent(dedupe,external,"tombstone",external,f"https://www.figma.com/file/{key}","",{"workspace_ids":[self.mappings[route]],"route_keys":[route]},version)
            return FigmaPullResult((event,),_cursor(key,version),False,(RouteLifecycleMutation(external,route,self.mappings[route],"tombstone",version,dedupe),))
        if state and state["provider_version"]==version:return FigmaPullResult((),_cursor(key,version))
        # The metadata response gives us the exact provider version to fence
        # the content read against.  Without this query parameter, a file can
        # change between the metadata and content requests and the event would
        # contain a snapshot newer (or older) than the cursor's version.
        p=self._get(f"https://api.figma.com/v1/files/{quote(key,safe='')}?version={quote(version,safe='')}")
        document=sanitize_content(p.get("document") or {},(self.token,))
        content=json.dumps(document,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        dedupe=_digest(external,version,hashlib.sha256(content.encode()).hexdigest())
        payload={"workspace_ids":[self.mappings[route]],"route_keys":[route],"file_key":key,"name":_text(p.get("name") or metadata.get("name")),"provider_version":version}
        observed=_text(metadata.get("last_touched_at") or p.get("lastModified"))
        event=ConnectorSourceEvent(dedupe,external,"file",external,f"https://www.figma.com/file/{key}",content,payload,observed,"application/json")
        comment_snapshot=_optional_collection(self,"comments:read",f"https://api.figma.com/v1/files/{quote(key,safe='')}/comments?page_size={FIGMA_MAX_COMMENTS}","comments")
        versions_snapshot=_optional_collection(self,"file_versions:read",f"https://api.figma.com/v1/files/{quote(key,safe='')}/versions?page_size={FIGMA_MAX_VERSION_EVENTS}","versions")
        events=(event,*_frame_events(key,version,route,self.mappings[route],document,observed),*_version_events(key,version,route,self.mappings[route],versions_snapshot),*_comment_events(key,version,route,self.mappings[route],comment_snapshot))
        return FigmaPullResult(events,_cursor(key,version),False,(RouteLifecycleMutation(external,route,self.mappings[route],"upsert",version,dedupe),))
    def _get(self,url:str)->dict[str,Any]:
        response=self.transport.request("GET",url,{"X-Figma-Token":self.token})
        try:value=response.json() if hasattr(response,"json") else response.json_body
        except Exception as exc:raise ConnectorTransportError("Figma response was not valid JSON") from exc
        if not isinstance(value,dict):raise ConnectorTransportError("Figma response shape is invalid")
        return value
    def _metadata(self,key:str)->dict[str,Any]:
        value=self._get(f"https://api.figma.com/v1/files/{key}/meta")
        # Figma's /meta endpoint uses the documented {"file": {...}}
        # envelope.  Do not silently accept another top-level shape: doing so
        # would turn an API contract drift into a misleading version fence.
        file_payload=value.get("file")
        if not isinstance(file_payload,dict):
            raise ConnectorTransportError("Figma metadata response envelope is invalid")
        return file_payload
def _parse_cursor(cursor):
    if cursor is None:return None
    try:value=json.loads(cursor)
    except Exception as exc:raise ValidationError("Figma cursor is invalid") from exc
    if not isinstance(value,dict) or set(value)!={"v","file_key","provider_version"} or value.get("v")!=1 or not all(isinstance(value.get(k),str) and value[k] for k in ("file_key","provider_version")):raise ValidationError("Figma cursor is invalid")
    return value
def _cursor(key,version):return json.dumps({"v":1,"file_key":key,"provider_version":version},sort_keys=True,separators=(",",":"))
def _digest(*parts):return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
def _text(value):
    text=str(value).strip() if value is not None else "";return text or None
def _frame_events(key,version,route,workspace,document,observed):
    events=[];seen=set()
    for node,path in _walk_nodes(document,()):
        if len(events)>=FIGMA_MAX_FRAME_EVENTS:break
        if not isinstance(node,dict) or str(node.get("type") or "").upper() not in FIGMA_FRAME_TYPES:continue
        node_id=_text(node.get("id"))
        if not node_id or node_id in seen:continue
        seen.add(node_id);node_type=str(node.get("type") or "").upper()
        frame=_frame_payload(node,key,version,route,workspace,path)
        content=json.dumps(frame,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        external=f"figma/files/{key}/nodes/{node_id}"
        dedupe=_digest(external,version,hashlib.sha256(content.encode()).hexdigest())
        locator=f"https://www.figma.com/file/{quote(key,safe='')}?node-id={quote(node_id,safe='')}"
        events.append(ConnectorSourceEvent(dedupe,external,node_type.lower(),external,locator,content,frame,observed,"application/json"))
    return tuple(events)
def _walk_nodes(node,path):
    if not isinstance(node,dict):return
    current=_bounded_path((*path,{"id":_text(node.get("id")),"name":_bounded_text(node.get("name"),160),"type":_text(node.get("type"))}))
    yield node,current
    for child in node.get("children") or ():
        yield from _walk_nodes(child,current)
def _frame_payload(node,key,version,route,workspace,path):
    node_id=_text(node.get("id"));node_type=str(node.get("type") or "").upper()
    payload={"workspace_ids":[workspace],"route_keys":[route],"file_key":key,"provider_version":version,"node_id":node_id,"node_type":node_type,"name":_bounded_text(node.get("name"),240),"path":[item for item in path if item.get("id")],"texts":_node_texts(node)}
    bounds=_bounds(node.get("absoluteBoundingBox") or node.get("absoluteRenderBounds"))
    if bounds:payload["bounds"]=bounds
    visible=node.get("visible")
    if isinstance(visible,bool):payload["visible"]=visible
    return payload
def _optional_collection(connector,permission,url,key):
    if permission not in connector.granted_permissions:return ()
    value=connector._get(url)
    items=value.get(key)
    if not isinstance(items,list):raise ConnectorTransportError("Figma optional response shape is invalid")
    return tuple(sanitize_content(items,(connector.token,)))
def _version_events(key,provider_version,route,workspace,versions):
    events=[];seen=set()
    for index,item in enumerate(versions):
        if len(events)>=FIGMA_MAX_VERSION_EVENTS:break
        if not isinstance(item,dict):continue
        version_id=_text(item.get("id")) or _digest(key,"version",str(index),json.dumps(item,sort_keys=True,separators=(",",":"),ensure_ascii=False))[:16]
        if version_id in seen:continue
        seen.add(version_id)
        payload={"workspace_ids":[workspace],"route_keys":[route],"file_key":key,"provider_version":provider_version,"version_id":version_id,"label":_bounded_text(item.get("label") or item.get("name"),240),"description":_bounded_text(item.get("description"),1200),"created_at":_text(item.get("created_at"))}
        user=item.get("user")
        if isinstance(user,dict):payload["user"]={k:_bounded_text(user.get(k),240) for k in ("id","handle","email") if _bounded_text(user.get(k),240)}
        payload={k:v for k,v in payload.items() if v is not None and v!=""}
        content=json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        external=f"figma/files/{key}/versions/{version_id}"
        dedupe=_digest(external,provider_version,hashlib.sha256(content.encode()).hexdigest())
        events.append(ConnectorSourceEvent(dedupe,external,"version",external,f"https://www.figma.com/file/{quote(key,safe='')}?version-id={quote(version_id,safe='')}",content,payload,payload.get("created_at"),"application/json"))
    return tuple(events)
def _comment_events(key,version,route,workspace,comments):
    events=[];seen=set()
    for index,item in enumerate(comments):
        if len(events)>=FIGMA_MAX_COMMENTS:break
        if not isinstance(item,dict):continue
        comment_id=_text(item.get("id")) or _digest(key,"comment",str(index),json.dumps(item,sort_keys=True,separators=(",",":"),ensure_ascii=False))[:16]
        if comment_id in seen:continue
        seen.add(comment_id)
        msg=_bounded_text(item.get("body"),FIGMA_MAX_COMMENT_TEXT)
        if not msg:continue
        user=item.get("user")
        payload={"workspace_ids":[workspace],"route_keys":[route],"file_key":key,"provider_version":version,"comment_id":comment_id,"message":msg,"parent_id":_text(item.get("parent_id")),"resolved":bool(item.get("resolved"))}
        if isinstance(user,dict):payload["user"]={k:_bounded_text(user.get(k),240) for k in ("id","name","handle") if _bounded_text(user.get(k),240)}
        payload={k:v for k,v in payload.items() if v is not None and v!=""}
        content=json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        external=f"figma/files/{key}/comments/{comment_id}"
        dedupe=_digest(external,version,hashlib.sha256(content.encode()).hexdigest())
        events.append(ConnectorSourceEvent(dedupe,external,"comment",external,f"https://www.figma.com/file/{quote(key,safe='')}",content,payload,_text(item.get("created_at")),"application/json"))
    return tuple(events)

def _node_texts(node):
    values=[]
    for child,_path in _walk_nodes(node,()):
        if not isinstance(child,dict) or child.get("type")!="TEXT":continue
        text=_bounded_text(child.get("characters"),FIGMA_MAX_FRAME_TEXT)
        if text and text not in values:values.append(text)
        if sum(len(item) for item in values)>=FIGMA_MAX_FRAME_TEXT:break
    joined=[];total=0
    for value in values:
        remaining=FIGMA_MAX_FRAME_TEXT-total
        if remaining<=0:break
        clipped=value[:remaining]
        joined.append(clipped);total+=len(clipped)
    return joined
def _bounded_text(value,limit):
    text=_text(value)
    if text is None:return None
    return text[:limit]
def _bounded_path(path):
    if len(path)<=FIGMA_MAX_FRAME_PATH_ITEMS:return path
    return ({"id":"__truncated__","name":f"{len(path)-FIGMA_MAX_FRAME_PATH_ITEMS} ancestors omitted","type":"TRUNCATED"},*path[-FIGMA_MAX_FRAME_PATH_ITEMS:])
def _bounds(value):
    if not isinstance(value,dict):return None
    result={}
    for key in ("x","y","width","height"):
        item=value.get(key)
        if isinstance(item,(int,float)):result[key]=round(float(item),3)
    return result if result else None

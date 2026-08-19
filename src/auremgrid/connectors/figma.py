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
FIGMA_OPTIONAL_PERMISSIONS=frozenset({"file_versions:read"})
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
        proven=set(FIGMA_REQUIRED_PERMISSIONS)
        if "file_versions:read" in self.granted_permissions:proven.add("file_versions:read")
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
        content=json.dumps(sanitize_content(p.get("document") or {},(self.token,)),sort_keys=True,separators=(",",":"),ensure_ascii=False)
        dedupe=_digest(external,version,hashlib.sha256(content.encode()).hexdigest())
        event=ConnectorSourceEvent(dedupe,external,"file",external,f"https://www.figma.com/file/{key}",content,{"workspace_ids":[self.mappings[route]],"route_keys":[route],"file_key":key,"name":_text(p.get("name") or metadata.get("name")),"provider_version":version},_text(metadata.get("last_touched_at") or p.get("lastModified")),"application/json")
        return FigmaPullResult((event,),_cursor(key,version),False,(RouteLifecycleMutation(external,route,self.mappings[route],"upsert",version,dedupe),))
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

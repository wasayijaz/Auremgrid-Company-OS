"""Bounded Fireflies connector for meeting transcripts."""
from __future__ import annotations
import hashlib,json
from dataclasses import dataclass
from typing import Any,Mapping
from auremgrid.connectors.google_auth import ConnectorSourceEvent,RouteLifecycleMutation
from auremgrid.connectors.http import ConnectorTransportError,HttpTransport,sanitize_content
from auremgrid.domain.errors import ValidationError

FIREFLIES_REQUIRED_SCOPES=frozenset({"transcripts:read"})
FIREFLIES_MAX_TRANSCRIPT_EVENTS=50
FIREFLIES_MAX_SENTENCE_CHARS=8000
FIREFLIES_MAX_PARTICIPANTS=50
FIREFLIES_BASE="https://api.fireflies.ai/v2"

class FirefliesMappingOverlap(ValidationError):
    def __init__(self,evidence_digest:str): super().__init__("Fireflies mapping overlap");self.evidence_digest=evidence_digest

@dataclass(frozen=True)
class FirefliesAccountIdentity: user_id:str;email:str|None;granted_scopes:frozenset[str]
@dataclass(frozen=True)
class FirefliesPullResult:
    events:tuple[ConnectorSourceEvent,...];next_cursor:str|None;has_more:bool=False;lifecycle_mutations:tuple[RouteLifecycleMutation,...]=()

class FirefliesConnector:
    """Fireflies scopes every API key to exactly one account with no
    per-team or per-workspace query filter, so unlike Figma/Drive/Gmail this
    connector accepts exactly one `account:<id>` mapping to one Auremgrid
    workspace; it cannot fan out transcripts across multiple mapped routes."""
    name="fireflies"
    def __init__(self,api_key:str,transport:Any|None=None,*,workspace_mappings:Mapping[str,str]|None=None,
                 expected_account_id:str|None=None):
        if not isinstance(api_key,str) or not api_key.strip(): raise ValidationError("Fireflies API key is required")
        self.api_key=api_key;self.transport=transport or HttpTransport();self.mappings=dict(workspace_mappings or {})
        if len(self.mappings)!=1 or any(not str(k).startswith("account:") or not str(k)[8:].strip() for k in self.mappings):
            raise ValidationError("Fireflies requires exactly one account:<id> mapping")
        self.route=next(iter(self.mappings));self.workspace_id=self.mappings[self.route]
        self.expected_account_id=expected_account_id
    def verify_credentials(self)->FirefliesAccountIdentity:
        p=self._get(f"{FIREFLIES_BASE}/auth/profile")
        uid=str(p.get("id") or "").strip()
        if not uid or self.expected_account_id and uid!=self.expected_account_id: raise ConnectorTransportError("Fireflies account identity mismatch",status=401)
        email=_text(p.get("email"))
        return FirefliesAccountIdentity(uid,email,FIREFLIES_REQUIRED_SCOPES)
    def pull(self,cursor:str|None=None)->FirefliesPullResult:
        state=_parse_cursor(cursor)
        from_date=state["provider_date"] if state else None
        url=f"{FIREFLIES_BASE}/transcripts?include_summary_only=false&limit={FIREFLIES_MAX_TRANSCRIPT_EVENTS}"
        if from_date: url+=f"&from_date={from_date}"
        data=self._get(url)
        transcripts=data if isinstance(data,list) else data.get("transcripts") or []
        if not isinstance(transcripts,list): raise ConnectorTransportError("Fireflies transcripts response shape is invalid")
        events=_transcript_events(transcripts,self.route,self.workspace_id,self.api_key)
        has_more=len(transcripts)>=FIREFLIES_MAX_TRANSCRIPT_EVENTS
        last_id=state["meeting_id"] if state else None;last_date=state["provider_date"] if state else None
        mutations=[]
        for event in events:
            meta=event.payload or {}
            mid=meta.get("meeting_id");date=meta.get("date")
            if mid:last_id=mid
            if date:last_date=date
            mutations.append(RouteLifecycleMutation(event.external_id,self.route,self.workspace_id,"upsert",date or "",event.dedupe_key))
        return FirefliesPullResult(tuple(events),_cursor(last_id,last_date) if last_id else cursor,has_more,tuple(mutations))
    def _get(self,url:str)->dict[str,Any]:
        response=self.transport.request("GET",url,{"Authorization":f"Bearer {self.api_key}"})
        try:value=response.json() if hasattr(response,"json") else response.json_body
        except Exception as exc:raise ConnectorTransportError("Fireflies response was not valid JSON") from exc
        if not isinstance(value,dict) and not isinstance(value,list):raise ConnectorTransportError("Fireflies response shape is invalid")
        return value if isinstance(value,dict) else {"transcripts":value}

def _parse_cursor(cursor):
    if cursor is None:return None
    try:value=json.loads(cursor)
    except Exception as exc:raise ValidationError("Fireflies cursor is invalid") from exc
    if not isinstance(value,dict) or value.get("v")!=1:raise ValidationError("Fireflies cursor is invalid")
    return value
def _cursor(meeting_id,date):return json.dumps({"v":1,"meeting_id":meeting_id,"provider_date":date},sort_keys=True,separators=(",",":")) if meeting_id and date else None
def _digest(*parts):return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()
def _text(value):
    text=str(value).strip() if value is not None else "";return text or None
def _bounded_text(value,limit):
    text=_text(value)
    if text is None:return None
    return text[:limit]
def _sanitize_sentences(sentences,max_chars):
    if not isinstance(sentences,list):return []
    result=[];total=0
    for s in sentences:
        if not isinstance(s,dict):continue
        text=_bounded_text(s.get("text") or s.get("content"),max(80,max_chars-total))
        if not text:continue
        speaker=_bounded_text(s.get("speaker") or s.get("speaker_name"),240)
        entry={"speaker":speaker,"text":text}
        if s.get("start_time") is not None:entry["start_time"]=s["start_time"]
        if s.get("end_time") is not None:entry["end_time"]=s["end_time"]
        result.append(entry);total+=len(text)
        if total>=max_chars:break
    return result
def _transcript_events(transcripts,route,workspace_id,api_key):
    events=[];seen=set()
    for t in transcripts:
        if len(events)>=FIREFLIES_MAX_TRANSCRIPT_EVENTS:break
        if not isinstance(t,dict):continue
        mid=_text(t.get("id")) or _text(t.get("meeting_id"))
        if not mid or mid in seen:continue
        seen.add(mid)
        title=_bounded_text(t.get("title"),320);date=_text(t.get("date") or t.get("start_time"))
        duration=t.get("duration");participants=t.get("participants") or []
        if not isinstance(participants,list):participants=[]
        participant_names=[_bounded_text(p.get("name") or p.get("displayName") or p,240) for p in participants[:FIREFLIES_MAX_PARTICIPANTS] if _bounded_text(p.get("name") or p.get("displayName") or p,240)]
        sentiment=_bounded_text(t.get("sentiment") or t.get("overall_sentiment"),80)
        summary_data=t.get("summary") or {}
        if isinstance(summary_data,dict):
            summary_short=_bounded_text(summary_data.get("short") or summary_data.get("summary") or summary_data.get("brief"),2000)
            summary_long=_bounded_text(summary_data.get("long") or summary_data.get("detailed") or summary_data.get("overview"),8000)
        elif isinstance(summary_data,str):
            summary_short=_bounded_text(summary_data,2000);summary_long=None
        else:
            summary_short=None;summary_long=None
        speakers_data=t.get("speakers") or []
        if not isinstance(speakers_data,list):speakers_data=[]
        speaker_labels={}
        for sp in speakers_data[:FIREFLIES_MAX_PARTICIPANTS]:
            if not isinstance(sp,dict):continue
            sid=_text(sp.get("id") or sp.get("speaker_id"))
            sname=_bounded_text(sp.get("name") or sp.get("displayName"),240)
            if sid and sname:speaker_labels[sid]=sname
        all_sentences=[]
        for sp in speakers_data:
            if not isinstance(sp,dict):continue
            for s in (sp.get("sentences") or []):
                if isinstance(s,dict):s["speaker"]=sp.get("name") or sp.get("displayName") or speaker_labels.get(sp.get("id") or sp.get("speaker_id"),"");all_sentences.append(s)
        if not all_sentences:
            all_sentences=t.get("sentences") or []
        sentences=_sanitize_sentences(all_sentences,FIREFLIES_MAX_SENTENCE_CHARS)
        recording_url=_text(t.get("recording_url") or t.get("download_url")) or ""
        transcript_content={"meeting_id":mid,"title":title,"date":date,"duration":duration,"participants":participant_names,"sentiment":sentiment,"summary_short":summary_short,"summary_long":summary_long,"speakers_sentences":sentences}
        transcript_content={k:v for k,v in transcript_content.items() if v is not None and v!="" and v!=[] and v!={}}
        transcript_content=sanitize_content(transcript_content,(api_key,))
        content=json.dumps(transcript_content,sort_keys=True,separators=(",",":"),ensure_ascii=False)
        external=f"fireflies/meetings/{mid}"
        dedupe=_digest(external,date or "",hashlib.sha256(content.encode()).hexdigest())
        payload={"workspace_ids":[workspace_id],"route_keys":[route],"meeting_id":mid,"title":title,"date":date,"duration":duration,"participant_count":len(participant_names),"sentiment":sentiment,"summary_short":summary_short,"summary_long":summary_long}
        payload={k:v for k,v in payload.items() if v is not None and v!=""}
        events.append(ConnectorSourceEvent(dedupe,external,"transcript",external,recording_url,content,payload,date or "","application/json"))
    return events

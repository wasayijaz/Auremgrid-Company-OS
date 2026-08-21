const fs=require("fs");
const fp=process.argv[2];
let c=fs.readFileSync(fp,"utf8");
c=c.replace('FIGMA_OPTIONAL_PERMISSIONS=frozenset({"file_versions:read"})','FIGMA_OPTIONAL_PERMISSIONS=frozenset({"file_versions:read","comments:read"})');
c=c.replace('FIGMA_MAX_VERSION_EVENTS=50','FIGMA_MAX_VERSION_EVENTS=50
FIGMA_MAX_COMMENTS=100
FIGMA_MAX_COMMENT_TEXT=4000');
const vl="        versions_snapshot=_optional_collection(self,"file_versions:read",f"https://api.figma.com/v1/files/{quote(key,safe='')}"+"/versions?page_size={FIGMA_MAX_VERSION_EVENTS}","versions")";
const cl="        comment_snapshot=_optional_collection(self,"comments:read",f"https://api.figma.com/v1/files/{quote(key,safe='')}"+"/comments?page_size={FIGMA_MAX_COMMENTS}","comments")";
c=c.replace(vl,cl+"
"+vl);
c=c.replace("*_version_events(key,version,route,self.mappings[route],versions_snapshot))","*_version_events(key,version,route,self.mappings[route],versions_snapshot),*_comment_events(key,version,route,self.mappings[route],comment_snapshot))");
const ce="def _comment_events(key,version,route,workspace,comments):
"+"    events=[];seen=set()
"+"    for index,item in enumerate(comments):
"+"        if len(events)>=FIGMA_MAX_COMMENTS:break
"+"        if not isinstance(item,dict):continue
"+"        comment_id=_text(item.get("id")) or _digest(key,"comment",str(index),json.dumps(item,sort_keys=True,separators=(",",":"),ensure_ascii=False))[:16]
"+"        if comment_id in seen:continue
"+"        seen.add(comment_id)
"+"        msg=_bounded_text(item.get("body"),FIGMA_MAX_COMMENT_TEXT)
"+"        if not msg:continue
"+"        user=item.get("user")
"+"        payload={"workspace_ids":[workspace],"route_keys":[route],"file_key":key,"provider_version":version,"comment_id":comment_id,"message":msg,"parent_id":_text(item.get("parent_id")),"resolved":bool(item.get("resolved"))}
"+"        if isinstance(user,dict):payload["user"]={k:_bounded_text(user.get(k),240) for k in ("id","name","handle") if _bounded_text(user.get(k),240)}
"+"        payload={k:v for k,v in payload.items() if v is not None and v!=""}
"+"        content=json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=False)
"+"        external=f"figma/files/{key}/comments/{comment_id}"
"+"        dedupe=_digest(external,version,hashlib.sha256(content.encode()).hexdigest())
"+"        events.append(ConnectorSourceEvent(dedupe,external,"comment",external,f"https://www.figma.com/file/{quote(key,safe='')}",content,payload,_text(item.get("created_at")),"application/json"))
"+"    return tuple(events)
";
c=c.replace("def _node_texts(node):",ce+"def _node_texts(node):");
fs.writeFileSync(fp,c,"utf8");
console.log("OK");
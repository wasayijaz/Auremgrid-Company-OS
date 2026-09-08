/* Tri-pane Workbench behavior: panel sizing, navigation collapse, and overlays. */
(function(){
  'use strict';
  const shell=document.querySelector('.workbench-shell');
  if(!shell)return;
  const side=shell.querySelector('.side'),queue=shell.querySelector('.queue-panel'),intel=shell.querySelector('.intelligence-rail'),content=shell.querySelector('.content');
  const navToggle=document.getElementById('nav-collapse-toggle'),intelToggle=document.getElementById('intelligence-toggle'),intelClose=document.getElementById('intelligence-close'),queueToggle=document.getElementById('queue-toggle'),queueOpenToggle=document.getElementById('queue-open-toggle'),queueToggles=[queueToggle,queueOpenToggle].filter(Boolean);
  const storageKey='auremgrid_workbench_layout_v1',defaults={nav:200,queue:300,intelligence:320},bounds={nav:[180,260],queue:[240,420],intelligence:[280,480]};
  const clamp=(value,min,max)=>Math.min(max,Math.max(min,value));
  const finite=(value,fallback)=>Number.isFinite(Number(value))?Number(value):fallback;
  const loadPrefs=()=>{try{const value=JSON.parse(localStorage.getItem(storageKey)||'{}');return {nav:clamp(finite(value.nav,defaults.nav),...bounds.nav),queue:clamp(finite(value.queue,defaults.queue),...bounds.queue),intelligence:clamp(finite(value.intelligence,defaults.intelligence),...bounds.intelligence)}}catch(_){return {...defaults}}};
  let prefs=loadPrefs(),effective={...prefs},raf=0,activeResize=null,restoreFocus=null,intelligenceOpen=true;
  const savePrefs=()=>{try{localStorage.setItem(storageKey,JSON.stringify({nav:prefs.nav,queue:prefs.queue,intelligence:prefs.intelligence}))}catch(_){}};
  const overlayIntelligence=()=>window.innerWidth<1200;
  const overlayQueue=()=>window.innerWidth<960;
  const overlayNav=()=>window.innerWidth<1200;

  function fitWidths(){
    const collapsed=shell.classList.contains('nav-collapsed'),available=Math.max(0,shell.clientWidth-48),next={nav:collapsed?64:prefs.nav,queue:queue&&queue.hidden&&!overlayQueue()?0:prefs.queue,intelligence:!intelligenceOpen||overlayIntelligence()?0:prefs.intelligence};
    if(overlayQueue())next.queue=0;
    if(overlayIntelligence())next.intelligence=0;
    if(overlayNav())next.nav=64;
    const canvasMin=360,total=next.nav+next.queue+next.intelligence,short=Math.max(0,total+canvasMin-available);
    let remaining=short;
    for(const key of ['queue','intelligence','nav']){if(!remaining||!next[key])continue;const min=key==='nav'&&collapsed?64:bounds[key][0],cut=Math.min(remaining,Math.max(0,next[key]-min));next[key]-=cut;remaining-=cut}
    if(remaining){for(const key of ['queue','intelligence','nav']){if(!remaining||!next[key])continue;const cut=Math.min(remaining,Math.max(0,next[key]-(key==='nav'&&collapsed?64:0)));next[key]-=cut;remaining-=cut}}
    effective=next;
    shell.style.setProperty('--nav-width',`${next.nav}px`);shell.style.setProperty('--queue-width',`${next.queue}px`);shell.style.setProperty('--intelligence-width',`${next.intelligence}px`);
  }
  function ensureHandle(id,label){let handle=document.getElementById(id);if(handle)return handle;handle=document.createElement('button');handle.id=id;handle.className='workbench-resizer';handle.type='button';handle.setAttribute('role','separator');handle.setAttribute('aria-orientation','vertical');handle.setAttribute('aria-label',label);shell.appendChild(handle);return handle}
  const handles=[{id:'resize-nav-queue',label:'Resize navigation and queue',left:'nav',right:'queue',delta:'nav'},{id:'resize-queue-canvas',label:'Resize queue and canvas',left:'queue',right:'main',delta:'queue'},{id:'resize-canvas-intelligence',label:'Resize canvas and intelligence',left:'main',right:'intelligence',delta:'intelligence'}];
  function positionHandles(){
    const root=shell.getBoundingClientRect();
    for(const item of handles){
      const handle=document.getElementById(item.id);
      if(!handle)continue;
      let visible=true;
      if(item.id==='resize-nav-queue')visible=window.innerWidth>=1200&&!shell.classList.contains('nav-collapsed');
      if(item.id==='resize-queue-canvas')visible=window.innerWidth>=960;
      if(item.id==='resize-canvas-intelligence')visible=window.innerWidth>=1200&&intelligenceOpen&&!intel.hidden;
      handle.hidden=!visible;
      if(!visible)continue;
      const left=shell.querySelector(`.${item.left}`);
      const right=shell.querySelector(`.${item.right}`);
      if(!left||!right){handle.hidden=true;continue}
      const a=left.getBoundingClientRect();
      const b=right.getBoundingClientRect();
      handle.style.left=`${(a.right+b.left)/2-root.left-4}px`;
      handle.setAttribute('aria-valuemin',String(bounds[item.delta][0]));
      handle.setAttribute('aria-valuemax',String(bounds[item.delta][1]));
      handle.setAttribute('aria-valuenow',String(Math.round(prefs[item.delta])));
    }
  }
  function applyLayout(){fitWidths();positionHandles()}
  function scheduleLayout(){if(raf)return;raf=requestAnimationFrame(()=>{raf=0;applyLayout()})}
  function changePreference(key,value){prefs[key]=clamp(Math.round(value),...bounds[key]);savePrefs();scheduleLayout()}
  function startResize(event,item){if(event.button!==0||document.getElementById(item.id).hidden)return;event.preventDefault();const startX=event.clientX,start=prefs[item.delta];activeResize={item,startX,start};shell.classList.add('is-resizing');const move=moveEvent=>{if(!activeResize)return;const direction=item.id==='resize-canvas-intelligence'?-1:1;const delta=(moveEvent.clientX-startX)*direction;changePreference(item.delta,start+delta)};const end=()=>{activeResize=null;shell.classList.remove('is-resizing');window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',end);window.removeEventListener('pointercancel',end)};window.addEventListener('pointermove',move);window.addEventListener('pointerup',end);window.addEventListener('pointercancel',end)}
  function keyboardResize(event,item){const key=event.key;if(!['ArrowLeft','ArrowRight','Home','End'].includes(key))return;event.preventDefault();let value=prefs[item.delta],step=event.shiftKey?40:10;if(key==='Home')value=bounds[item.delta][0];else if(key==='End')value=bounds[item.delta][1];else{const direction=item.id==='resize-canvas-intelligence'?-1:1;value+=((key==='ArrowRight'?1:-1)*step*direction)}changePreference(item.delta,value)}
  handles.forEach(item=>{const handle=ensureHandle(item.id,item.label);handle.addEventListener('pointerdown',event=>startResize(event,item));handle.addEventListener('keydown',event=>keyboardResize(event,item))});
  if(intelToggle){intelToggle.classList.add('icon-button');intelToggle.setAttribute('aria-label','Toggle intelligence');intelToggle.setAttribute('aria-controls','intelligence-rail');intelToggle.setAttribute('title','Toggle intelligence');intelToggle.innerHTML='<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"></circle><path d="M12 8v8M8 12h8"></path></svg>'}

  function createBackdrop(){let backdrop=document.querySelector('.workbench-backdrop');if(backdrop)return backdrop;backdrop=document.createElement('button');backdrop.type='button';backdrop.className='workbench-backdrop';backdrop.tabIndex=-1;backdrop.setAttribute('aria-label','Close open panel');backdrop.hidden=true;backdrop.addEventListener('click',()=>{setQueueOpen(false);setIntelligenceOpen(false);closeNavOverlay()});document.body.appendChild(backdrop);return backdrop}
  const backdrop=createBackdrop();
  function setBackdrop(open){backdrop.hidden=!open;shell.classList.toggle('has-overlay',open)}
  function focusFirst(panel){(panel&&panel.querySelector('button:not([disabled]),input,textarea,select,[tabindex]:not([tabindex="-1"])'))?.focus()}
  function setIntelligenceOpen(open){
    open=Boolean(open);intelligenceOpen=open;const overlay=overlayIntelligence();if(open){setQueueOpen(false);restoreFocus=document.activeElement;shell.classList.add('intelligence-open');intel.hidden=false;intel.inert=false;if(overlay)setBackdrop(true);else setBackdrop(false);intelToggle?.setAttribute('aria-expanded','true');if(overlay)focusFirst(intel)}else{shell.classList.remove('intelligence-open');intelToggle?.setAttribute('aria-expanded','false');intel.hidden=true;intel.inert=true;if(overlay&&!shell.classList.contains('queue-open')&&!shell.classList.contains('nav-overlay-open'))setBackdrop(false);if(restoreFocus&&typeof restoreFocus.focus==='function'){restoreFocus.focus();restoreFocus=null}}
    scheduleLayout();
  }
  function setQueueOpen(open){
    if(!queue)return;const overlay=overlayQueue();if(open){setIntelligenceOpen(false);shell.classList.add('queue-open');queue.hidden=false;queue.inert=false;if(overlay)setBackdrop(true);queueToggles.forEach(toggle=>toggle.setAttribute('aria-expanded','true'));if(overlay)focusFirst(queue)}else{shell.classList.remove('queue-open');queueToggles.forEach(toggle=>toggle.setAttribute('aria-expanded','false'));if(overlay){queue.hidden=true;queue.inert=true;if(!shell.classList.contains('intelligence-open')&&!shell.classList.contains('nav-overlay-open'))setBackdrop(false)}}scheduleLayout();
  }
  function closeNavOverlay(){if(!side)return;side.classList.remove('nav-overlay-open');shell.classList.remove('nav-overlay-open');navToggle?.setAttribute('aria-expanded','false');navToggle?.setAttribute('aria-label','Expand navigation');if(!shell.classList.contains('queue-open')&&!shell.classList.contains('intelligence-open'))setBackdrop(false)}
  function toggleNavigation(){if(overlayNav()){if(shell.classList.contains('nav-collapsed')){shell.classList.remove('nav-collapsed');side.classList.add('nav-overlay-open');shell.classList.add('nav-overlay-open');navToggle?.setAttribute('aria-expanded','true');navToggle?.setAttribute('aria-label','Collapse navigation');setBackdrop(true);focusFirst(side)}else if(side.classList.contains('nav-overlay-open')){shell.classList.add('nav-collapsed');closeNavOverlay()}}else{const collapsed=shell.classList.toggle('nav-collapsed');navToggle?.setAttribute('aria-expanded',String(!collapsed));navToggle?.setAttribute('aria-label',collapsed?'Expand navigation':'Collapse navigation');scheduleLayout()}}
  navToggle?.addEventListener('click',toggleNavigation);queueToggles.forEach(toggle=>toggle.addEventListener('click',()=>setQueueOpen(!shell.classList.contains('queue-open'))));
  document.addEventListener('keydown',event=>{if(event.key!=='Escape')return;if(shell.classList.contains('intelligence-open'))setIntelligenceOpen(false);else if(shell.classList.contains('queue-open'))setQueueOpen(false);else if(side?.classList.contains('nav-overlay-open'))closeNavOverlay()});
  document.addEventListener('click',event=>{const space=event.target.closest&&event.target.closest('[data-space-id]');if(space){if(document.getElementById('attention'))document.getElementById('attention').innerHTML='';if(document.getElementById('pulse'))document.getElementById('pulse').innerHTML='';if(content)content.scrollTop=0}if(overlayQueue()&&shell.classList.contains('queue-open')&&!event.target.closest('.queue-panel')&&!event.target.closest('#queue-toggle'))setQueueOpen(false);if(overlayIntelligence()&&shell.classList.contains('intelligence-open')&&!event.target.closest('.intelligence-rail')&&!event.target.closest('#intelligence-toggle'))setIntelligenceOpen(false);if(overlayNav()&&side?.classList.contains('nav-overlay-open')&&!event.target.closest('.side')&&!event.target.closest('#nav-collapse-toggle'))closeNavOverlay()},{capture:true});
  function setQueueVisible(visible){if(!queue)return;const allowed=Boolean(visible);queue.hidden=!allowed;queueToggles.forEach(toggle=>{toggle.hidden=!allowed;if(!allowed)toggle.setAttribute('aria-hidden','true');else toggle.removeAttribute('aria-hidden')});if(!allowed){shell.classList.remove('queue-open');queue.inert=true}else{queue.hidden=overlayQueue()&&!shell.classList.contains('queue-open');queue.inert=overlayQueue()&&!shell.classList.contains('queue-open')}scheduleLayout()}
  function refreshAccess(){scheduleLayout()}
  function resetLayout(){prefs={...defaults};savePrefs();shell.classList.toggle('nav-collapsed',overlayNav());navToggle?.setAttribute('aria-expanded',String(!shell.classList.contains('nav-collapsed')));navToggle?.setAttribute('aria-label',shell.classList.contains('nav-collapsed')?'Expand navigation':'Collapse navigation');scheduleLayout()}
  window.auremgridWorkbench={setIntelligenceOpen, setQueueOpen, setQueueVisible, refreshAccess, resetLayout, applyLayout};
  function removeDuplicateActionCard(){const host=document.getElementById('dashboard-completion-overview');if(!host)return;host.querySelectorAll(':scope > section').forEach(section=>{const heading=section.querySelector('h2');if(heading&&heading.textContent.trim()==='Action table')section.remove()})}
  const completion=document.getElementById('dashboard-completion-overview');if(completion){new MutationObserver(removeDuplicateActionCard).observe(completion,{childList:true,subtree:true});removeDuplicateActionCard()}
  const reset=document.createElement('button');reset.type='button';reset.className='ghost workbench-reset-layout';reset.textContent='Reset panel sizes';reset.addEventListener('click',resetLayout);side?.querySelector('.side-foot')?.appendChild(reset);
  function syncMode(){if(overlayIntelligence()){if(intelligenceOpen)setIntelligenceOpen(false);else{intel.hidden=true;intel.inert=true}}else{intel.hidden=!intelligenceOpen;intel.inert=!intelligenceOpen;shell.classList.toggle('intelligence-open',intelligenceOpen);intelToggle?.setAttribute('aria-expanded',String(intelligenceOpen))}if(overlayQueue()){if(!shell.classList.contains('queue-open')&&queue&&!queue.hidden){queue.hidden=true;queue.inert=true}}else{if(queue&&!queue.hidden)queue.hidden=false;if(queue)queue.inert=false;shell.classList.remove('queue-open')}if(overlayNav()){if(!shell.classList.contains('nav-overlay-open')){shell.classList.add('nav-collapsed');navToggle?.setAttribute('aria-expanded','false');navToggle?.setAttribute('aria-label','Expand navigation')}}else{side?.classList.remove('nav-overlay-open');shell.classList.remove('nav-overlay-open')}scheduleLayout()}
  const observer=new ResizeObserver(syncMode);observer.observe(shell);window.addEventListener('resize',syncMode);syncMode();setTimeout(refreshAccess,0);
})();

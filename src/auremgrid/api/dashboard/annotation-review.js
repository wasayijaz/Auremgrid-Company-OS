(function(){
  'use strict';

  const TYPE_CAPABILITIES = {
    general_comment: 'general_comments',
    image_point: 'image_points',
    image_region: 'image_regions',
    document_page: 'document_pages',
    document_region: 'document_regions',
    video_timestamp: 'video_timestamps',
    video_range: 'video_ranges'
  };
  const REGION_TYPES = new Set(['image_region', 'document_region']);
  const IMAGE_TYPES = new Set(['image_point', 'image_region']);
  const DOCUMENT_TYPES = new Set(['document_page', 'document_region']);
  const VIDEO_TYPES = new Set(['video_timestamp', 'video_range']);
  const REVIEW_GROUPS = [
    'waiting_for_me',
    'waiting_for_team',
    'waiting_for_client',
    'revision_requested',
    'stalled',
    'approved_today'
  ];

  let activeReview = null;
  let identityPromise = null;

  function token(){
    return localStorage.getItem('auremgrid_session') || '';
  }

  function esc(value){
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  }

  async function api(path, options){
    const headers = Object.assign(
      {Authorization: 'Bearer ' + token()},
      (options && options.headers) || {}
    );
    const response = await fetch(path, Object.assign({}, options || {}, {headers}));
    const body = await response.json().catch(() => ({}));
    if(!response.ok) throw new Error(body.error || body.message || response.statusText);
    return body;
  }

  function identity(){
    if(!identityPromise) identityPromise = api('/auth/me');
    return identityPromise;
  }

  function reviewRows(data){
    return REVIEW_GROUPS.flatMap(group => Array.isArray(data[group]) ? data[group] : []);
  }

  function findReview(data, reviewId){
    return reviewRows(data).find(row => String(row.id) === String(reviewId)) || null;
  }

  function capabilityFor(review, type){
    const key = TYPE_CAPABILITIES[type];
    const capability = ((review && review.annotation_capabilities) || {})[key];
    if(type === 'general_comment' && !capability) return {status: 'ready'};
    return capability || {status: 'not_available', reason: 'This review did not return that annotation capability.'};
  }

  function optionReason(review, type){
    const cap = capabilityFor(review, type);
    return cap.status === 'ready' ? '' : (cap.reason || 'The attached source does not support this annotation type.');
  }

  function sourceKind(review){
    const locator = String(review && review.source_locator || '').split('?')[0].toLowerCase();
    const type = String(review && review.deliverable_type || '').toLowerCase();
    if(type === 'video' || /\.(mp4|webm|mov|m4v|ogv)$/.test(locator)) return 'video';
    if(['document', 'report', 'presentation', 'copy', 'landing_page', 'website'].includes(type) || /\.(pdf|doc|docx|ppt|pptx|txt|md|html?)$/.test(locator)) return 'document';
    if(['design_asset', 'ad_creative'].includes(type) || /\.(png|jpe?g|webp|gif|avif|svg)$/.test(locator)) return 'image';
    return locator ? 'source' : 'none';
  }

  function requiredFields(type){
    if(type === 'image_point') return ['coordinates'];
    if(type === 'image_region') return ['coordinates'];
    if(type === 'document_page') return ['page_number'];
    if(type === 'document_region') return ['page_number', 'coordinates'];
    if(type === 'video_timestamp') return ['start_seconds'];
    if(type === 'video_range') return ['start_seconds', 'end_seconds'];
    return [];
  }

  function parsedNumber(values, key){
    const raw = values.get ? values.get(key) : values[key];
    if(raw === null || raw === undefined || raw === '') return null;
    const number = Number(raw);
    if(!Number.isFinite(number)) throw new Error(`${key.replaceAll('_', ' ')} must be a number`);
    return number;
  }

  function parsedCoordinates(values){
    const raw = values.get ? values.get('coordinates') : values.coordinates;
    if(!raw) return {};
    const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if(parsed == null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Captured coordinates are invalid');
    return parsed;
  }

  function parsedMetadata(values){
    const raw = values.get ? values.get('metadata') : values.metadata;
    if(!raw) return {};
    const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if(parsed == null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Annotation metadata is invalid');
    return parsed;
  }

  function buildAnnotationPayload(auth, review, values, idempotencyKey){
    if(!review || !review.id || !review.workspace_id) throw new Error('Choose a review before annotating');
    const type = String(values.get ? values.get('annotation_type') : values.annotation_type);
    const body = String(values.get ? values.get('body') : values.body || '').trim();
    const sourceLocator = review.source_locator || null;
    const payload = {
      organization_id: auth.organization_id,
      workspace_id: review.workspace_id,
      person_id: auth.person_id,
      review_id: review.id,
      annotation_type: type,
      body,
      coordinates: parsedCoordinates(values),
      metadata: parsedMetadata(values),
      idempotency_key: idempotencyKey
    };
    if(sourceLocator) payload.source_locator = sourceLocator;
    const pageNumber = parsedNumber(values, 'page_number');
    const startSeconds = parsedNumber(values, 'start_seconds');
    const endSeconds = parsedNumber(values, 'end_seconds');
    if(pageNumber !== null) payload.page_number = pageNumber;
    if(startSeconds !== null) payload.start_seconds = startSeconds;
    if(endSeconds !== null) payload.end_seconds = endSeconds;
    return payload;
  }

  function host(){
    let box = document.getElementById('annotation-ia');
    const page = document.getElementById('page-review');
    if(!page) return null;
    if(!box){
      box = document.createElement('section');
      box.id = 'annotation-ia';
      page.appendChild(box);
    }
    if(box.dataset.owner === 'annotation-review') return box;
    box.dataset.owner = 'annotation-review';
    box.className = 'card annotation-ia';
    box.innerHTML = [
      '<div class="annotation-panel-head">',
      '<div><h2>Review annotations</h2><p class="sub">Choose Annotate from a review card. The panel uses that review source, workspace, and capabilities.</p></div>',
      '<span class="state" data-annotation-context>No review selected</span>',
      '</div>',
      '<div class="annotation-status" data-annotation-status role="status"></div>',
      '<div class="annotation-capabilities" data-annotation-capabilities></div>',
      '<div class="annotation-source" data-annotation-source><span class="sub">No review selected.</span></div>',
      '<div class="annotation-history" data-annotation-history></div>',
      '<form class="annotation-entry" data-annotation-form>',
      '<input type="hidden" name="review_id">',
      '<input type="hidden" name="coordinates">',
      '<input type="hidden" name="metadata">',
      '<label>Annotation type<select name="annotation_type" required>',
      '<option value="general_comment">General comment</option>',
      '<option value="image_point">Image point</option>',
      '<option value="image_region">Image region</option>',
      '<option value="document_page">Document page</option>',
      '<option value="document_region">Document region</option>',
      '<option value="video_timestamp">Video timestamp</option>',
      '<option value="video_range">Video range</option>',
      '</select></label>',
      '<label>Comment<textarea name="body" required></textarea></label>',
      '<div class="annotation-entry-grid">',
      '<label data-page-field>Page<input name="page_number" type="number" min="1" step="1"></label>',
      '<label data-start-field>Start seconds<input name="start_seconds" type="number" min="0" step="0.01"></label>',
      '<label data-end-field>End seconds<input name="end_seconds" type="number" min="0" step="0.01"></label>',
      '</div>',
      '<div class="annotation-region-grid" data-region-fields>',
      '<label>X<input name="coord_x" type="number" min="0" max="1" step="0.0001"></label>',
      '<label>Y<input name="coord_y" type="number" min="0" max="1" step="0.0001"></label>',
      '<label>Width<input name="coord_width" type="number" min="0" max="1" step="0.0001"></label>',
      '<label>Height<input name="coord_height" type="number" min="0" max="1" step="0.0001"></label>',
      '</div>',
      '<p class="sub" data-coordinate-note>Choose a review source to capture coordinates.</p>',
      '<button type="submit">Add annotation</button>',
      '</form>'
    ].join('');
    bind(box);
    return box;
  }

  function setStatus(box, message, tone){
    const target = box && box.querySelector('[data-annotation-status]');
    if(!target) return;
    target.textContent = message || '';
    target.dataset.tone = tone || '';
  }

  function bind(box){
    const form = box.querySelector('[data-annotation-form]');
    const type = form.querySelector('[name="annotation_type"]');
    type.addEventListener('change', () => syncTypeControls(box));
    ['coord_x', 'coord_y', 'coord_width', 'coord_height'].forEach(name => {
      form.querySelector(`[name="${name}"]`).addEventListener('input', () => syncManualCoordinates(box));
    });
    form.addEventListener('submit', submitAnnotation);
  }

  function setFormCoordinates(box, coordinates){
    const form = box.querySelector('[data-annotation-form]');
    const normalized = Object.fromEntries(Object.entries(coordinates || {}).map(([key, value]) => [key, Number(Number(value).toFixed(4))]));
    form.querySelector('[name="coordinates"]').value = JSON.stringify(normalized);
    form.querySelector('[name="coord_x"]').value = normalized.x ?? '';
    form.querySelector('[name="coord_y"]').value = normalized.y ?? '';
    form.querySelector('[name="coord_width"]').value = normalized.width ?? '';
    form.querySelector('[name="coord_height"]').value = normalized.height ?? '';
    const label = normalized.width != null
      ? `Region captured at x ${normalized.x}, y ${normalized.y}, width ${normalized.width}, height ${normalized.height}.`
      : `Point captured at x ${normalized.x}, y ${normalized.y}.`;
    setStatus(box, label, 'ok');
  }

  function clearCoordinates(box){
    const form = box.querySelector('[data-annotation-form]');
    form.querySelector('[name="coordinates"]').value = '';
    ['coord_x', 'coord_y', 'coord_width', 'coord_height'].forEach(name => { form.querySelector(`[name="${name}"]`).value = ''; });
  }

  function syncManualCoordinates(box){
    const form = box.querySelector('[data-annotation-form]');
    const type = form.querySelector('[name="annotation_type"]').value;
    if(type !== 'document_region') return;
    const values = {
      x: form.querySelector('[name="coord_x"]').value,
      y: form.querySelector('[name="coord_y"]').value,
      width: form.querySelector('[name="coord_width"]').value,
      height: form.querySelector('[name="coord_height"]').value
    };
    if(Object.values(values).every(value => value !== '')){
      setFormCoordinates(box, Object.fromEntries(Object.entries(values).map(([key, value]) => [key, Number(value)])));
    }
  }

  function syncTypeControls(box){
    const form = box.querySelector('[data-annotation-form]');
    const type = form.querySelector('[name="annotation_type"]').value;
    const page = form.querySelector('[name="page_number"]');
    const start = form.querySelector('[name="start_seconds"]');
    const end = form.querySelector('[name="end_seconds"]');
    const note = box.querySelector('[data-coordinate-note]');
    const regionFields = box.querySelector('[data-region-fields]');
    const submit = form.querySelector('[type="submit"]');
    page.disabled = !DOCUMENT_TYPES.has(type);
    start.disabled = !VIDEO_TYPES.has(type);
    end.disabled = type !== 'video_range';
    regionFields.hidden = !REGION_TYPES.has(type);
    regionFields.querySelectorAll('input').forEach(input => {
      input.disabled = !REGION_TYPES.has(type) || type === 'image_region';
    });
    if(page.disabled) page.value = '';
    if(start.disabled) start.value = '';
    if(end.disabled) end.value = '';
    if(!REGION_TYPES.has(type) && !IMAGE_TYPES.has(type)) clearCoordinates(box);
    note.textContent = type === 'image_point'
      ? 'Click the preview image to set a normalized point before submitting.'
      : type === 'image_region'
        ? 'Drag across the preview image to capture a normalized region before submitting.'
        : type === 'document_region'
          ? 'Enter a page number, then capture a normalized region with the page pad or numeric fields.'
          : type === 'video_timestamp'
            ? 'Use the video controls, then capture the current time as the timestamp.'
            : type === 'video_range'
              ? 'Use the video controls, then capture start and end seconds for the range.'
              : 'General comments need only a body.';
    submit.disabled = !activeReview || !Array.from(form.querySelector('[name="annotation_type"]').options).some(option => !option.disabled);
  }

  function renderOptions(box, review){
    const select = box.querySelector('[name="annotation_type"]');
    select.querySelectorAll('option').forEach(option => {
      const type = option.value;
      const reason = optionReason(review, type);
      const enabled = Boolean(review) && capabilityFor(review, type).status === 'ready';
      option.disabled = !enabled;
      option.title = enabled ? '' : reason;
      option.textContent = option.textContent.replace(/\s+\(.+\)$/, '') + (enabled ? '' : ` (${reason})`);
    });
    if(select.selectedOptions[0] && select.selectedOptions[0].disabled){
      const first = Array.from(select.options).find(option => !option.disabled);
      if(first) select.value = first.value;
    }
    syncTypeControls(box);
    renderCapabilitySummary(box, review);
  }

  function renderCapabilitySummary(box, review){
    const target = box.querySelector('[data-annotation-capabilities]');
    if(!target) return;
    const labels = [
      ['general_comment', 'Comment'],
      ['image_point', 'Image point'],
      ['image_region', 'Image region'],
      ['document_page', 'Document page'],
      ['document_region', 'Document region'],
      ['video_timestamp', 'Video timestamp'],
      ['video_range', 'Video range']
    ];
    target.innerHTML = labels.map(([type, label]) => {
      const cap = capabilityFor(review, type);
      const ready = Boolean(review) && cap.status === 'ready';
      return `<span class="annotation-capability ${ready ? 'ready' : 'off'}" title="${esc(ready ? 'Backend route is available.' : optionReason(review, type))}">${esc(label)} - ${ready ? 'ready' : 'off'}</span>`;
    }).join('');
  }

  function normalizedRegion(startEvent, endEvent, element){
    const rect = element.getBoundingClientRect();
    const startX = Math.max(0, Math.min(1, (startEvent.clientX - rect.left) / rect.width));
    const startY = Math.max(0, Math.min(1, (startEvent.clientY - rect.top) / rect.height));
    const endX = Math.max(0, Math.min(1, (endEvent.clientX - rect.left) / rect.width));
    const endY = Math.max(0, Math.min(1, (endEvent.clientY - rect.top) / rect.height));
    return {
      x: Math.min(startX, endX),
      y: Math.min(startY, endY),
      width: Math.abs(endX - startX),
      height: Math.abs(endY - startY)
    };
  }

  function installRegionCapture(box, element, type){
    let start = null;
    element.addEventListener('pointerdown', event => {
      if(box.querySelector('[name="annotation_type"]').value !== type) return;
      start = event;
      element.setPointerCapture && element.setPointerCapture(event.pointerId);
    });
    element.addEventListener('pointerup', event => {
      if(!start || box.querySelector('[name="annotation_type"]').value !== type) return;
      const region = normalizedRegion(start, event, element);
      start = null;
      if(region.width <= 0.001 || region.height <= 0.001){
        setStatus(box, 'Drag a larger region before submitting.', 'error');
        return;
      }
      setFormCoordinates(box, region);
    });
  }

  function renderSource(box, review){
    const source = box.querySelector('[data-annotation-source]');
    const form = box.querySelector('[data-annotation-form]');
    clearCoordinates(box);
    const locator = review && review.source_locator;
    if(!locator){
      source.innerHTML = '<span class="sub">No attached source is available for this review.</span>';
      return;
    }
    const kind = sourceKind(review);
    source.innerHTML = `<div class="annotation-source-head"><a href="${esc(locator)}" target="_blank" rel="noreferrer">Open attached source</a><span class="state">${esc(kind)}</span></div><span class="sub">${esc(locator)}</span>`;
    if(kind === 'image'){
      const frame = document.createElement('div');
      frame.className = 'annotation-preview-frame';
      const preview = document.createElement('img');
      preview.src = locator;
      preview.alt = 'Attached review source';
      preview.loading = 'lazy';
      preview.className = 'annotation-preview';
      preview.onerror = () => {
        frame.remove();
        source.insertAdjacentHTML('beforeend', '<span class="sub">Image preview is unavailable; the source link remains available.</span>');
      };
      preview.onclick = event => {
        if(form.querySelector('[name="annotation_type"]').value !== 'image_point') return;
        const rect = preview.getBoundingClientRect();
        setFormCoordinates(box, {
          x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
          y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))
        });
      };
      installRegionCapture(box, preview, 'image_region');
      frame.appendChild(preview);
      source.appendChild(frame);
      return;
    }
    if(kind === 'document'){
      const viewer = document.createElement('iframe');
      viewer.src = locator;
      viewer.title = 'Attached document source';
      viewer.className = 'annotation-document-preview';
      viewer.loading = 'lazy';
      source.appendChild(viewer);
      if(capabilityFor(review, 'document_region').status === 'ready'){
        const pad = document.createElement('button');
        pad.type = 'button';
        pad.className = 'annotation-page-pad';
        pad.innerHTML = '<span>Drag here to capture a normalized page region</span>';
        installRegionCapture(box, pad, 'document_region');
        source.appendChild(pad);
      }
      return;
    }
    if(kind === 'video'){
      const video = document.createElement('video');
      video.src = locator;
      video.controls = true;
      video.preload = 'metadata';
      video.className = 'annotation-video-preview';
      video.onerror = () => source.insertAdjacentHTML('beforeend', '<span class="sub">Video preview is unavailable; the source link remains available.</span>');
      const controls = document.createElement('div');
      controls.className = 'annotation-video-tools';
      controls.innerHTML = '<button type="button" data-video-start>Use current time as start</button><button type="button" data-video-end>Use current time as end</button><span class="sub" data-video-clock>0.00s</span>';
      video.ontimeupdate = () => {
        const clock = controls.querySelector('[data-video-clock]');
        if(clock) clock.textContent = `${video.currentTime.toFixed(2)}s`;
      };
      controls.querySelector('[data-video-start]').onclick = () => {
        form.querySelector('[name="start_seconds"]').value = video.currentTime.toFixed(2);
        setStatus(box, `Start set to ${video.currentTime.toFixed(2)}s.`, 'ok');
      };
      controls.querySelector('[data-video-end]').onclick = () => {
        form.querySelector('[name="end_seconds"]').value = video.currentTime.toFixed(2);
        setStatus(box, `End set to ${video.currentTime.toFixed(2)}s.`, 'ok');
      };
      source.appendChild(video);
      source.appendChild(controls);
    }
  }

  async function renderHistory(box){
    const history = box.querySelector('[data-annotation-history]');
    if(!activeReview){
      history.innerHTML = '';
      return;
    }
    const auth = await identity();
    const data = await api('/reviews/annotations?' + new URLSearchParams({
      organization_id: auth.organization_id,
      workspace_id: activeReview.workspace_id,
      person_id: auth.person_id,
      review_id: activeReview.id,
      include_closed: '1'
    }));
    const rows = data.annotations || [];
    history.innerHTML = rows.length ? rows.map(row => [
      '<div class="annotation-history-row">',
      `<b>${esc(row.annotation_type)}</b>`,
      `<span class="state">${esc(row.status)}</span>`,
      `<p>${esc(row.body)}</p>`,
      row.coordinates && Object.keys(row.coordinates).length ? `<small>Coordinates ${esc(JSON.stringify(row.coordinates))}</small>` : '',
      row.page_number ? `<small>Page ${esc(row.page_number)}</small>` : '',
      row.start_seconds != null ? `<small>Start ${esc(row.start_seconds)}s</small>` : '',
      row.end_seconds != null ? `<small>End ${esc(row.end_seconds)}s</small>` : '',
      row.status === 'open' ? `<button type="button" data-resolve-annotation="${esc(row.id)}">Resolve</button>` : '',
      '</div>'
    ].join('')).join('') : '<span class="sub">No annotations yet.</span>';
    history.querySelectorAll('[data-resolve-annotation]').forEach(button => {
      button.onclick = () => resolveAnnotation(button.dataset.resolveAnnotation);
    });
  }

  async function openReviewAnnotation(reviewId){
    const box = host();
    if(!box) return;
    setStatus(box, 'Loading review annotation workspace...', '');
    try{
      const auth = await identity();
      const reviewCenter = await api('/dashboard/review-center?' + new URLSearchParams({
        organization_id: auth.organization_id,
        person_id: auth.person_id
      }));
      const review = findReview(reviewCenter, reviewId);
      if(!review) throw new Error('Review is not available in your organization-wide review queue.');
      activeReview = review;
      const form = box.querySelector('[data-annotation-form]');
      form.querySelector('[name="review_id"]').value = review.id;
      box.querySelector('[data-annotation-context]').textContent = `${review.client || review.workspace_id} - ${review.deliverable_title || review.id}`;
      renderOptions(box, review);
      renderSource(box, review);
      await renderHistory(box);
      setStatus(box, 'Ready to add an annotation.', 'ok');
      form.scrollIntoView({behavior: 'smooth', block: 'center'});
      form.querySelector('[name="body"]').focus();
    }catch(error){
      setStatus(box, error.message, 'error');
    }
  }

  async function submitAnnotation(event){
    event.preventDefault();
    const box = host();
    const form = event.currentTarget;
    try{
      if(!activeReview) throw new Error('Choose a review before annotating.');
      const values = new FormData(form);
      const type = String(values.get('annotation_type'));
      if(capabilityFor(activeReview, type).status !== 'ready') throw new Error(optionReason(activeReview, type));
      if(requiredFields(type).includes('coordinates') && !values.get('coordinates')) throw new Error(REGION_TYPES.has(type) ? 'Capture a normalized region before submitting.' : 'Click the image preview to capture a point first.');
      if(DOCUMENT_TYPES.has(type) && !values.get('page_number')) throw new Error('Enter the page number for this document annotation.');
      if(['video_timestamp', 'video_range'].includes(type) && !values.get('start_seconds')) throw new Error('Enter the start time for this video annotation.');
      if(type === 'video_range' && !values.get('end_seconds')) throw new Error('Enter the end time for this video range.');
      const auth = await identity();
      const payload = buildAnnotationPayload(auth, activeReview, values, `dashboard:annotation:${activeReview.id}:${Date.now()}`);
      if(payload.end_seconds != null && payload.start_seconds != null && payload.end_seconds < payload.start_seconds){
        throw new Error('End seconds must be greater than or equal to start seconds.');
      }
      await api('/reviews/annotations', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      form.querySelector('[name="body"]').value = '';
      form.querySelector('[name="coordinates"]').value = '';
      setStatus(box, 'Annotation added.', 'ok');
      await renderHistory(box);
    }catch(error){
      setStatus(box, error.message, 'error');
    }
  }

  async function resolveAnnotation(annotationId){
    const box = host();
    try{
      if(!activeReview) throw new Error('Choose a review before resolving an annotation.');
      const auth = await identity();
      await api('/reviews/annotations/resolve', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          organization_id: auth.organization_id,
          workspace_id: activeReview.workspace_id,
          person_id: auth.person_id,
          annotation_id: annotationId,
          idempotency_key: `dashboard:resolve:${annotationId}`
        })
      });
      setStatus(box, 'Annotation resolved.', 'ok');
      await renderHistory(box);
    }catch(error){
      setStatus(box, error.message, 'error');
    }
  }

  function install(){
    const box = host();
    if(box) renderOptions(box, activeReview);
  }

  window.AuremgridAnnotationReview = {
    install,
    openReviewAnnotation,
    _test: {buildAnnotationPayload, findReview, reviewRows, requiredFields, sourceKind, parsedMetadata}
  };
  window.openReviewAnnotation = openReviewAnnotation;

  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, {once: true});
  else install();
})();

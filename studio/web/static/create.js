(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const all = selector => [...document.querySelectorAll(selector)];
  const base = '/api/creator';
  const terminal = new Set(['succeeded', 'completed', 'complete', 'failed', 'cancelled', 'canceled', 'blocked']);
  const state = {project:null, system:null, jobs:[], job:null, dirty:false, briefDirty:false,
    busy:false, connected:false, conflict:false, token:'', resultKey:null, media:[], poll:null,
    refreshing:false, generation:0};
  try { state.token = sessionStorage.getItem('pond-token') || ''; } catch { /* Cookie auth works without storage. */ }
  const clone = value => JSON.parse(JSON.stringify(value));
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function notice(message, error = false) {
    $('notice').hidden = !message;
    $('notice').textContent = message;
    $('notice').dataset.error = String(error);
    $('notice').setAttribute('role', error ? 'alert' : 'status');
  }
  function unlock() {
    state.connected = false;
    if (!$('unlock').open) $('unlock').showModal();
    update();
  }
  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    let response;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.long ? 180000 : 30000);
    try {
      response = await fetch(path, {method:options.method || 'GET', credentials:'same-origin',
        headers, signal:controller.signal,
        body:options.body === undefined ? undefined : JSON.stringify(options.body)});
    } catch {
      state.connected = false;
      update();
      throw new Error('Connection interrupted. Reconnect to check the saved project and jobs before trying again.');
    } finally { clearTimeout(timeout); }
    if (!response.ok) {
      if (response.status >= 500) state.connected = false;
      if (response.status === 401) {
        state.token = '';
        try { sessionStorage.removeItem('pond-token'); } catch { /* Session cookie remains usable. */ }
        unlock();
      }
      let message = `Studio request failed (${response.status}).`;
      try {
        const body = await response.json();
        if (typeof body.detail === 'string') message = body.detail;
        else if (Array.isArray(body.detail)) message = body.detail.map(item => `${(item.loc || []).slice(1).join(' · ')}: ${item.msg}`).join('; ');
      } catch { /* Keep the bounded generic error. */ }
      const error = new Error(message); error.status = response.status; throw error;
    }
    state.connected = true;
    return options.blob ? response.blob() : response.status === 204 ? null : response.json();
  }
  async function act(work) {
    if (state.busy) return;
    state.busy = true; notice(''); update();
    try { await work(); }
    catch (error) { notice(error.message, true); }
    finally { state.busy = false; update(); schedule(); }
  }
  function isActive(job) { return job && !terminal.has(job.status); }
  function currentMode() { return document.querySelector('input[name="mode"]:checked').value; }
  function step(index) {
    all('.steps li').forEach((item, i) => {
      if (i === index) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
  }
  function narrationNote() {
    const audio = $('audio').value;
    const availability = state.system?.cloud?.narration_available;
    const speaking = 'Uses the editable shot narration as a system-generated synthetic speaking voice, not singing.';
    $('audio-note').textContent = audio === 'narration'
      ? `${speaking} ${availability === false ? 'Automatic narration is currently unavailable.' : availability === true ? 'Narration service is available.' : 'Narration availability has not been confirmed by the server.'}`
      : audio === 'silent' ? 'No soundtrack or spoken narration.' : 'Ambient soundtrack only; no spoken narration.';
    $('narration-note').textContent = audio === 'narration'
      ? `${speaking} Timing previews may use guide audio; check the output notes.`
      : 'Narration is planning text only with this audio choice. It will not be spoken. Choose Automatic narration to use it as a synthetic voice.';
  }
  function reason() {
    if (!state.connected) return 'Connect to your studio to generate.';
    if (!state.project?.plan || state.briefDirty) return 'Create and save a plan first.';
    if (state.dirty) return 'Save your edits before generating.';
    if (state.conflict) return 'Resolve the saved revision conflict first.';
    if (isActive(state.job)) return 'Your video is in the queue. You can cancel below.';
    if (state.system?.active_job) return 'The studio is working on another video. Check status when it finishes.';
    const resources = state.system?.resources;
    if (!resources || resources.pressure !== 'green') return 'Generation is paused until studio resource pressure is green.';
    if (!(Number(resources.free_disk_gb) >= 10)) return 'Generation needs at least 10 GB of free disk space.';
    if (currentMode() === 'cloud') {
      const cloud = state.system?.cloud;
      if (cloud?.available !== true) return cloud?.reason || 'Cloud video is currently unavailable.';
      if (cloud.enabled === false) return 'Cloud video has not been enabled for this studio.';
      const budget = Number($('budget').value);
      if (!Number.isFinite(budget) || budget <= 0 || budget > 20) return 'Set a job budget reservation greater than $0, up to $20.';
      const rate = Number(cloud.cost_per_second_usd);
      if (!(rate > 0)) return 'A cloud generation cost estimate is not available yet.';
      const estimate = rate * Number($('duration').value);
      if (budget < estimate) return `Estimated generation cost is $${estimate.toFixed(2)}. Your reservation must cover this estimate.`;
      if (cloud.remaining_budget_usd !== undefined && budget > Number(cloud.remaining_budget_usd)) return 'Your reservation exceeds the studio’s remaining cloud budget. Lower the reservation or check studio status.';
      if ($('audio').value === 'narration' && cloud.narration_available === false) return 'Automatic narration is currently unavailable. Choose ambient or silent audio, or try again later.';
    }
    return currentMode() === 'preview' ? 'A draft animatic to check pacing. No generated anime or live-action footage.' : 'Cloud generation uses your saved plan. Review the result before sharing.';
  }
  function canGenerate() {
    if (!state.connected || !state.project?.plan || state.briefDirty || state.dirty || state.conflict || isActive(state.job) || state.system?.active_job) return false;
    if (state.system?.resources?.pressure !== 'green' || !(Number(state.system.resources.free_disk_gb) >= 10)) return false;
    if (currentMode() === 'preview') return true;
    const cloud = state.system.cloud || {}, budget = Number($('budget').value), rate = Number(cloud.cost_per_second_usd);
    return cloud.available === true && cloud.enabled !== false && rate > 0 && budget > 0 && budget <= 20
      && budget >= rate * Number($('duration').value)
      && (cloud.remaining_budget_usd === undefined || budget <= Number(cloud.remaining_budget_usd))
      && !($('audio').value === 'narration' && cloud.narration_available === false);
  }
  function update() {
    const active = isActive(state.job);
    $('connection').textContent = state.connected ? 'Connected' : 'Offline';
    $('save-state').textContent = state.conflict ? 'Revision conflict' : state.busy ? 'Working…' : state.dirty ? 'Unsaved changes' : state.project ? `Saved · revision ${state.project.revision}` : 'Not saved yet';
    $('brief-form').inert = state.busy || active;
    $('plan-fields').inert = state.busy || active;
    $('shots').inert = state.busy || active;
    $('plan-cast')?.toggleAttribute('inert', state.busy || active);
    $('generate').disabled = state.busy || !canGenerate();
    $('generate').textContent = state.busy ? 'Working…' : currentMode() === 'preview' ? 'Make timing preview' : 'Generate video';
    $('save-plan').disabled = state.busy || active || state.conflict;
    $('new-project').disabled = state.busy;
    all('#project-list button').forEach(button => { button.disabled = state.busy; });
    $('plan-button').textContent = state.busy ? 'Working on your project…' : 'Create my plan →';
    const planCast = $('plan-cast');
    if (planCast && $('add-plan-character')) $('add-plan-character').disabled = planCast.children.length >= 4;
    $('reload-project').disabled = state.busy;
    $('budget-wrap').hidden = currentMode() !== 'cloud';
    $('generation-reason').textContent = reason();
    $('cancel-job').disabled = state.busy;
    $('cancel-job').hidden = !active;
    $('conflict').hidden = !state.conflict;
    $('duration-value').value = `${$('duration').value} seconds`;
    $('add-character').disabled = $('characters').children.length >= 4;
    narrationNote();
  }
  function audience() {
    const kids = $('audience').value === 'kids';
    all('input[name="platform"]').forEach(input => {
      input.disabled = kids;
      if (kids) input.checked = input.value === 'youtube';
    });
    $('kids-note').hidden = !kids;
  }
  function field(parent, label, value, {id, max = 120, min = 0, area = false} = {}) {
    const name = el('label', label); name.htmlFor = id;
    const input = el(area ? 'textarea' : 'input');
    input.id = id; input.value = value || ''; input.maxLength = max; input.minLength = min;
    input.required = min > 0; if (area) input.rows = 2;
    parent.append(name, input); return input;
  }
  function castRow(parent, character = {}, plan = false) {
    if (parent.children.length >= 4) return;
    const row = el('div', undefined, 'character-row');
    const unique = `cast-${plan ? 'plan' : 'brief'}-${crypto.randomUUID()}`;
    field(row, 'Character name', character.name, {id:`${unique}-name`, min:1});
    field(row, 'Appearance & personality', character.description, {id:`${unique}-description`, max:1200, min:3, area:true});
    const remove = el('button', 'Remove character', 'quiet'); remove.type = 'button';
    remove.addEventListener('click', () => { row.remove(); dirty(!plan); });
    row.append(remove); parent.append(row); update();
  }
  function readCast(parent) {
    return [...parent.children].map(row => ({name:row.querySelector('input').value.trim(), description:row.querySelector('textarea').value.trim()}));
  }
  function readBrief() {
    return {title:$('title').value.trim(), idea:$('idea').value.trim(), style:document.querySelector('input[name="style"]:checked').value,
      duration:Number($('duration').value), audience:$('audience').value, language:'en', audio:$('audio').value,
      platforms:all('input[name="platform"]:checked').map(input => input.value), characters:readCast($('characters'))};
  }
  function readPlan() {
    if (!state.project?.plan || state.briefDirty) return null;
    const plan = clone(state.project.plan);
    plan.title = $('plan-title').value.trim(); plan.hook = $('plan-hook').value.trim();
    plan.description = $('plan-description').value;
    plan.hashtags = $('plan-hashtags').value.split(/[\s,]+/).filter(Boolean);
    if (plan.hashtags.length > 12) throw new Error('Use up to 12 hashtags.');
    plan.characters = readCast($('plan-cast'));
    plan.shots = plan.shots.map((shot, i) => ({...shot, prompt:$(`shot-${i}-prompt`).value.trim(), caption:$(`shot-${i}-caption`).value, narration:$(`shot-${i}-narration`).value}));
    return plan;
  }
  function dirty(brief = false) {
    state.dirty = true;
    if (brief && state.project?.plan && !state.briefDirty) {
      state.briefDirty = true;
      $('plan-section').hidden = true;
      notice('Your idea changed. Create a new plan to match it; saving the idea will clear the previous plan.');
      step(0);
    }
    update();
  }
  function validate() {
    if (!$('brief-form').reportValidity()) throw new Error('Please complete the highlighted brief fields.');
    if (!readBrief().platforms.length) throw new Error('Choose at least one platform.');
    if (!state.briefDirty && state.project?.plan) {
      for (const input of all('#plan-fields input, #plan-fields textarea, #plan-cast input, #plan-cast textarea, #shots input, #shots textarea')) {
        if (!input.reportValidity()) throw new Error('Please complete the highlighted plan fields.');
      }
    }
  }
  function renderPlan() {
    const plan = state.project?.plan;
    $('plan-section').hidden = !plan || state.briefDirty;
    $('plan-fields').replaceChildren(); $('shots').replaceChildren();
    if (!plan || state.briefDirty) return;
    $('planner-label').textContent = plan.planner === 'template' ? 'Template plan' : 'AI-assisted plan';
    const fields = $('plan-fields');
    field(fields, 'Video title', plan.title, {id:'plan-title', min:1});
    field(fields, 'Opening hook', plan.hook, {id:'plan-hook', max:500, min:1, area:true});
    field(fields, 'Description', plan.description, {id:'plan-description', max:2000, area:true});
    field(fields, 'Hashtags · up to 12', (plan.hashtags || []).join(' '), {id:'plan-hashtags', max:1200});
    const castHeading = el('h3', 'Proposed characters');
    castHeading.style.fontSize = '14px';
    const help = el('p', 'Start with just an idea: the planner can propose a cast. Descriptions guide identity; identical character appearance across shots is not guaranteed.', 'help');
    const cast = el('div'); cast.id = 'plan-cast';
    fields.append(castHeading, help, cast);
    (plan.characters || []).forEach(character => castRow(cast, character, true));
    const add = el('button', '+ Add proposed character', 'quiet'); add.type = 'button'; add.id = 'add-plan-character';
    add.addEventListener('click', () => { castRow(cast, {}, true); dirty(); add.disabled = cast.children.length >= 4; });
    add.disabled = cast.children.length >= 4;
    fields.append(add);
    plan.shots.forEach((shot, i) => {
      const card = el('div', undefined, 'shot');
      const heading = el('div', undefined, 'section-heading');
      heading.append(el('strong', `Scene ${shot.number}`), el('small', `${i * 5}–${(i + 1) * 5} sec`));
      card.append(heading);
      field(card, 'Visual prompt', shot.prompt, {id:`shot-${i}-prompt`, max:2400, min:5, area:true});
      field(card, 'Caption', shot.caption, {id:`shot-${i}-caption`});
      field(card, 'Narration text', shot.narration, {id:`shot-${i}-narration`, max:500, area:true});
      $('shots').append(card);
    });
    narrationNote();
  }
  async function projects() {
    const data = await request(`${base}/projects`);
    $('project-list').replaceChildren();
    for (const project of data.projects || []) {
      const button = el('button', project.plan?.title || project.brief?.title || project.brief?.idea?.slice(0, 70) || 'Untitled video');
      const date = new Date(project.updated_at);
      button.append(el('small', `${project.brief?.duration || 0}s · ${Number.isNaN(date.getTime()) ? 'Saved project' : date.toLocaleDateString()}`));
      if (state.project?.id === project.id) button.setAttribute('aria-current', 'true');
      button.disabled = state.busy;
      button.addEventListener('click', () => act(async () => {
        if (state.dirty && !window.confirm('Discard unsaved edits and open this project?')) return;
        await openProject(project.id);
        if (innerWidth < 821) toggleLibrary(false);
      }));
      $('project-list').append(button);
    }
    if (!data.projects?.length) $('project-list').append(el('p', 'Your saved ideas will appear here.', 'help'));
  }
  function clearMedia() {
    state.generation++;
    $('video').pause(); $('video').removeAttribute('src'); $('video').removeAttribute('poster'); $('video').load();
    state.media.forEach(url => URL.revokeObjectURL(url)); state.media = []; state.resultKey = null;
    $('result').hidden = true;
  }
  function populate(project) {
    state.project = project; state.dirty = false; state.briefDirty = false; state.conflict = false;
    const brief = project?.brief;
    $('brief-form').reset(); $('characters').replaceChildren();
    if (brief) {
      for (const key of ['idea', 'title', 'duration', 'audience', 'audio']) $(key).value = brief[key];
      all('input[name="style"]').forEach(input => { input.checked = input.value === brief.style; });
      all('input[name="platform"]').forEach(input => { input.checked = brief.platforms.includes(input.value); });
      (brief.characters || []).forEach(character => castRow($('characters'), character));
    }
    audience(); clearMedia(); renderPlan(); state.job = null; state.jobs = [];
    $('job-panel').hidden = true; step(project?.plan ? 1 : 0); update();
  }
  async function openProject(id) {
    const project = await request(`${base}/projects/${encodeURIComponent(id)}`);
    populate(project);
    const data = await request(`${base}/jobs?project_id=${encodeURIComponent(id)}`);
    state.jobs = data.jobs || [];
    state.jobs.sort((a,b) => String(b.created_at).localeCompare(String(a.created_at)));
    await showJob(state.jobs.find(isActive) || state.jobs[0] || null);
    await projects();
  }
  async function save() {
    if (state.conflict) throw new Error('Load the saved revision before saving again. Copy any edits you want to keep first.');
    validate();
    if (state.project && !state.dirty) return state.project;
    const brief = readBrief();
    let saved;
    if (!state.project) saved = await request(`${base}/projects`, {method:'POST', body:brief});
    else {
      try {
        saved = await request(`${base}/projects/${encodeURIComponent(state.project.id)}`, {
          method:'PUT', body:{revision:state.project.revision, brief, plan:readPlan()}});
      } catch (error) {
        if (error.status === 409) state.conflict = true;
        throw error;
      }
    }
    state.project = saved; state.dirty = false; state.briefDirty = false; state.conflict = false;
    clearMedia(); renderPlan(); await projects(); update(); return saved;
  }
  function safeMediaURL(value) {
    if (typeof value !== 'string' || !value) throw new Error('This output is not available yet.');
    const url = new URL(value, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith(`${base}/jobs/`)) throw new Error('The server returned an unexpected media location.');
    return url.pathname + url.search;
  }
  async function showJob(job) {
    state.job = job;
    $('job-panel').hidden = !job;
    if (!job) return;
    $('job-status').textContent = job.status === 'queued' ? 'Waiting in queue' : job.status === 'running' ? 'Making your video' : String(job.status || 'Unknown status');
    const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
    $('job-progress').value = progress; $('job-progress').textContent = `${progress}%`;
    $('job-message').textContent = job.error || job.message || '';
    step(isActive(job) ? 2 : job.video_url ? 3 : 1);
    if (!job.video_url || isActive(job)) { update(); return; }
    const key = `${job.id}:${job.video_url}`;
    if (state.resultKey === key) { update(); return; }
    clearMedia();
    const generation = state.generation;
    const blob = await request(safeMediaURL(job.video_url), {blob:true});
    if (generation !== state.generation || state.job?.id !== job.id) return;
    const url = URL.createObjectURL(blob); state.media.push(url); $('video').src = url;
    state.resultKey = key;
    $('result-kind').textContent = job.mode === 'preview' ? 'Timing preview · draft' : 'Human review required';
    const stale = job.stale || job.project_revision !== state.project?.revision;
    $('result-note').textContent = (job.mode === 'preview' ? 'Draft motion-graphics animatic. This checks timing and is not footage generated in your selected visual style.' : 'Cloud-generated output. Check every scene and the audio before sharing.') + (stale ? ' This video belongs to an earlier saved revision.' : '') + (job.guide_audio ? ' Guide audio keeps this output a draft.' : '');
    $('result-meta').textContent = `Project revision ${job.project_revision} · ${state.project?.brief?.duration || ''} seconds${Number(job.estimated_cost_usd) > 0 ? ` · Estimated cost $${Number(job.estimated_cost_usd).toFixed(2)}` : ''}. A 1080 × 1920 export does not imply native HD source footage.`;
    $('download-package').disabled = !job.package_url;
    $('download-video').disabled = !job.download_url;
    $('result').hidden = false; update();
  }
  async function status() {
    state.system = await request(`${base}/status`);
    const system = state.system;
    $('planner-status').textContent = system.planner?.available ? 'AI-assisted planning available' : 'Template planning' ;
    $('cloud-status').textContent = system.cloud?.available ? 'Available · budget required' : system.cloud?.reason || 'Unavailable';
    const resource = system.resources || {};
    const disk = Number(resource.free_disk_gb);
    $('resource-status').textContent = `${resource.pressure === 'green' ? 'Ready' : resource.pressure === 'yellow' ? 'Paused · memory pressure' : resource.pressure === 'red' ? 'Paused · high memory pressure' : 'Resource check unavailable'}${Number.isFinite(disk) ? ` · ${disk.toFixed(1)} GB free` : ''}`;
    update();
  }
  function schedule() {
    clearTimeout(state.poll);
    if (document.hidden || !state.connected || $('unlock').open) return;
    if (isActive(state.job) || state.system?.active_job) state.poll = setTimeout(poll, 3000);
  }
  async function poll() {
    if (state.refreshing || state.busy || document.hidden) { schedule(); return; }
    state.refreshing = true;
    try {
      if (isActive(state.job)) await showJob(await request(`${base}/jobs/${encodeURIComponent(state.job.id)}`));
      await status();
    } catch (error) { notice(error.message, true); }
    finally { state.refreshing = false; update(); schedule(); }
  }
  async function connect() {
    if (state.token) await request('/api/session', {method:'POST', body:{token:state.token}});
    await status(); await projects();
    if (state.project) {
      const data = await request(`${base}/jobs?project_id=${encodeURIComponent(state.project.id)}`);
      const jobs = (data.jobs || []).sort((a,b) => String(b.created_at).localeCompare(String(a.created_at)));
      await showJob(jobs.find(isActive) || jobs[0] || null);
    }
  }
  async function download(kind) {
    const job = state.job;
    if (!job) return;
    const path = kind === 'package' ? job.package_url : job.download_url;
    const blob = await request(safeMediaURL(path), {blob:true});
    const url = URL.createObjectURL(blob), anchor = el('a');
    anchor.href = url; anchor.download = `video-${job.id}${kind === 'package' ? '-review.zip' : '.mp4'}`;
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  function toggleLibrary(open = $('library').hidden) {
    $('library').hidden = !open; $('projects-toggle').setAttribute('aria-expanded', String(open));
  }
  $('projects-toggle').addEventListener('click', () => toggleLibrary());
  $('new-project').addEventListener('click', () => {
    if (state.dirty && !window.confirm('Discard unsaved edits and start a new project?')) return;
    populate(null); notice(''); $('idea').focus(); schedule();
  });
  $('brief-form').addEventListener('input', event => { if (event.target.id === 'audience') audience(); dirty(true); });
  $('brief-form').addEventListener('change', event => { if (event.target.id === 'audience') audience(); update(); });
  $('plan-section').addEventListener('input', () => dirty());
  $('add-character').addEventListener('click', () => { castRow($('characters')); dirty(true); });
  $('save-brief').addEventListener('click', () => act(async () => { await save(); notice('Idea saved in your studio.'); }));
  $('save-plan').addEventListener('click', () => act(async () => { await save(); notice('Plan saved. Choose a timing preview or cloud generation.'); }));
  $('brief-form').addEventListener('submit', event => {
    event.preventDefault();
    act(async () => {
      const project = await save();
      notice('Creating your scene-by-scene plan…');
      state.project = await request(`${base}/projects/${encodeURIComponent(project.id)}/plan`, {method:'POST', long:true});
      state.dirty = false; renderPlan(); step(1); await projects();
      notice(state.project.plan?.planner === 'template' ? 'Your template plan is ready to edit. AI-assisted planning was not used.' : 'Your plan is ready. Refine the shots and proposed characters before generating.');
      $('plan-section').scrollIntoView({behavior:'instant', block:'start'});
    });
  });
  $('reload-project').addEventListener('click', () => act(async () => {
    if (!window.confirm('Load the saved version? Your unsaved edits will be discarded.')) return;
    await openProject(state.project.id); notice('Loaded the latest saved revision.');
  }));
  all('input[name="mode"]').forEach(input => input.addEventListener('change', update));
  $('budget').addEventListener('input', update);
  $('generate').addEventListener('click', () => act(async () => {
    await status();
    if (!canGenerate()) throw new Error(reason());
    const mode = currentMode();
    const job = await request(`${base}/projects/${encodeURIComponent(state.project.id)}/jobs`, {
      method:'POST', body:{mode, max_cost_usd:mode === 'cloud' ? Number($('budget').value) : 0}});
    clearMedia(); await showJob(job); notice('Your video is queued. You can reopen this project to follow its progress.');
  }));
  $('cancel-job').addEventListener('click', () => act(async () => {
    await showJob(await request(`${base}/jobs/${encodeURIComponent(state.job.id)}/cancel`, {method:'POST'}));
    notice('Cancellation requested. Work already submitted to a cloud service may still incur charges.');
  }));
  $('refresh').addEventListener('click', () => act(connect));
  $('check-status').addEventListener('click', () => act(status));
  $('download-video').addEventListener('click', () => act(() => download('video')));
  $('download-package').addEventListener('click', () => act(() => download('package')));
  $('unlock-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try {
      const token = $('token').value.trim();
      await request('/api/session', {method:'POST', body:{token}});
      state.token = token;
      try { sessionStorage.setItem('pond-token', token); } catch { /* Cookie auth needs no browser storage. */ }
      $('token').value = ''; $('unlock-error').textContent = '';
      $('unlock').close(); await act(connect);
    } catch (error) { $('unlock-error').textContent = error.message; }
    finally { button.disabled = false; }
  });
  const samples = {
    fantasy:['watercolor-anime', 'A young mapmaker follows a glowing paper bird through a floating mountain village at dusk. Gentle hand-painted fantasy, original characters, warm lanterns and a surprising discovery.'],
    cartoon:['cartoon', 'A tiny robot opens a café on an asteroid. Its first customer is a curious comet. A playful original cartoon with a warm visual punchline.'],
    product:['motion-graphics', 'Reveal a reusable water bottle through a rhythmic dance of geometric shapes and flowing water-inspired forms. Finish with a clean product silhouette.'],
    live:['live-action', 'A cinematic journey through a fictional city after the rain: neon reflections, a passing tram and sunrise breaking through the clouds. No identifiable real people.']
  };
  all('[data-sample]').forEach(button => button.addEventListener('click', () => {
    const [style, idea] = samples[button.dataset.sample]; $('idea').value = idea;
    all('input[name="style"]').forEach(input => { input.checked = input.value === style; });
    dirty(true); $('idea').focus();
  }));
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault(); if (!isActive(state.job)) act(save);
    }
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(state.poll); else schedule(); });
  window.addEventListener('online', () => { if (!state.connected && !$('unlock').open) act(connect); });
  window.addEventListener('beforeunload', event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
  $('video').addEventListener('error', () => { if ($('video').getAttribute('src')) notice('Video playback failed. Try downloading the MP4 or reconnecting.', true); });
  update(); act(connect);
})();

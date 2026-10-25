/* Pond Studio. Buildless, same-origin client for API contract 30, v1. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const enc = encodeURIComponent;
  const clone = value => JSON.parse(JSON.stringify(value));
  const uid = prefix => `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
  const store = {get(key, session = false) {try {return (session ? sessionStorage : localStorage).getItem(key);} catch {return null;}}, set(key, value, session = false) {try {const target = session ? sessionStorage : localStorage; value ? target.setItem(key, value) : target.removeItem(key);} catch { /* Storage may be disabled; cookie still works. */ }}};
  const state = {projects: [], project: null, scene: null, view: 'projects', system: null, jobs: [], dirty: false, edit: 0, saving: false, busy: false, connected: false, playing: false, playTime: 0, token: store.get('pond-token', true) || '', conflict: false};
  let noticeTimer, pollTimer, playbackTimer, polling = false, authPending = false;
  const modal = $('#modal');
  const active = job => ['queued', 'running'].includes(job.status);
  const resourceBlocked = () => !state.system || state.system.pressure !== 'green' || Number(state.system.free_disk_gb) < 10;
  const total = project => (project?.scenes || []).reduce((sum, scene) => sum + Number(scene.duration), 0);
  const path = (suffix = '', project = state.project) => `/api/projects/${enc(project.id)}${suffix}`;
  const button = (action, label, cls = '', attrs = '') => `<button type="button" data-action="${action}" class="${cls}" ${attrs}>${label}</button>`;
  let fieldId = 0;
  const field = (label, body, help = '') => {
    const id = `pond-field-${++fieldId}`;
    const control = body.replace(/<(input|select|textarea)\b/, `<$1 id="${id}"${help ? ` aria-describedby="${id}-help"` : ''}`);
    return `<div class="field"><label for="${id}">${label}</label>${control}${help ? `<span id="${id}-help" class="help">${help}</span>` : ''}</div>`;
  };
  const input = (name, value, attrs = '') => `<input name="${name}" value="${esc(value)}" ${attrs}>`;
  const options = (values, current) => values.map(value => {const [id, label] = Array.isArray(value) ? value : [value, value[0].toUpperCase() + value.slice(1)]; return `<option value="${esc(id)}" ${id === current ? 'selected' : ''}>${esc(label)}</option>`;}).join('');
  const select = (name, values, current) => `<select name="${name}">${options(values, current)}</select>`;
  const icons = {projects:'<rect x="3" y="5" width="18" height="15" rx="3"/><path d="M3 10h18M8 5V3"/>',characters:'<circle cx="12" cy="9" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2M7 5 5 2m12 3 2-3"/>',storyboard:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10M15 10v10"/>',review:'<path d="M8 3h8l4 4v14H4V3h4m1 9 2 2 5-5"/>',system:'<path d="M3 12h4l3-8 4 16 3-8h4"/>'};
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.projects}</svg>`;
  function notify(message, error = false) {const el = $('#notice'); el.textContent = message; el.className = `notice${error ? ' error' : ''}`; el.hidden = false; clearTimeout(noticeTimer); noticeTimer = setTimeout(() => {el.hidden = true;}, error ? 10000 : 5000);}
  class ApiError extends Error {constructor(status, message) {super(message); this.status = status;}}
  async function api(url, {method = 'GET', body, blob = false} = {}) {
    const headers = {};
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';
    let response;
    try {response = await fetch(url, {method, headers, credentials: 'same-origin', cache: 'no-store', body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined});}
    catch {throw new ApiError(0, 'Cannot reach the studio. Check that your local server is running, then reconnect.');}
    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      try {const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; else if (Array.isArray(data.detail)) message = data.detail.map(item => `${item.loc?.slice(-1)[0] || 'Field'}: ${item.msg}`).join('; ');} catch { /* Keep HTTP status. */ }
      if (response.status === 401) {state.token = ''; store.set('pond-token', '', true); unlock();}
      throw new ApiError(response.status, message);
    }
    if (blob) return response.blob();
    return response.status === 204 ? null : response.json();
  }
  // All art is drawn here from original native shapes; no remote assets or fonts.
  function animal(character = {}, expression = false) {
    const color = /^#[0-9a-f]{6}$/i.test(character.color) ? character.color : '#ad7956';
    const species = character.species || 'otter';
    const ears = species === 'rabbit' ? `<ellipse cx="81" cy="51" rx="17" ry="43" fill="${color}"/><ellipse cx="125" cy="51" rx="17" ry="43" fill="${color}"/><ellipse cx="81" cy="48" rx="8" ry="29" fill="#efd1b9"/><ellipse cx="125" cy="48" rx="8" ry="29" fill="#efd1b9"/>` : species === 'frog' ? `<circle cx="75" cy="67" r="22" fill="${color}"/><circle cx="133" cy="67" r="22" fill="${color}"/>` : `<circle cx="65" cy="65" r="18" fill="${color}"/><circle cx="139" cy="65" r="18" fill="${color}"/><circle cx="65" cy="65" r="10" fill="#e7bd9d"/><circle cx="139" cy="65" r="10" fill="#e7bd9d"/>`;
    return `<svg viewBox="0 0 210 250" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><ellipse cx="106" cy="232" rx="64" ry="10" fill="#47604c" opacity=".12"/><path d="M150 198q51 5 31 24-31 14-43-4" fill="${color}"/><ellipse cx="104" cy="174" rx="51" ry="58" fill="${color}"/><ellipse cx="104" cy="181" rx="32" ry="40" fill="#f6e5cb"/><ellipse cx="57" cy="175" rx="14" ry="30" fill="${color}" transform="rotate(24 57 175)"/><ellipse cx="153" cy="174" rx="14" ry="29" fill="${color}" transform="rotate(-32 153 174)"/><ellipse cx="76" cy="223" rx="23" ry="12" fill="${color}"/><ellipse cx="133" cy="223" rx="23" ry="12" fill="${color}"/>${ears}<ellipse cx="103" cy="99" rx="58" ry="48" fill="${color}"/><ellipse cx="103" cy="115" rx="38" ry="27" fill="#f6e5cb"/><ellipse cx="67" cy="111" rx="10" ry="6" fill="#df9d85" opacity=".65"/><ellipse cx="139" cy="111" rx="10" ry="6" fill="#df9d85" opacity=".65"/>${expression ? '<path d="m76 95 6-4 6 4m31 0 6-4 6 4" fill="none" stroke="#3d342b" stroke-width="4" stroke-linecap="round"/>' : '<ellipse cx="82" cy="94" rx="4" ry="6" fill="#3d342b"/><ellipse cx="125" cy="94" rx="4" ry="6" fill="#3d342b"/><circle cx="83" cy="92" r="1.3" fill="white"/><circle cx="126" cy="92" r="1.3" fill="white"/>'}<path d="M96 107q7-6 14 0-1 8-7 8t-7-8" fill="#514134"/><path d="M103 115v6m-11 0q11 12 22 0" fill="none" stroke="#514134" stroke-width="2.5" stroke-linecap="round"/>${character.accessory === 'scarf' ? '<path d="M62 138q42 15 82-1l-1 17q-43 11-80-1Z" fill="#4b9081"/><path d="m112 149 20 1 5 41-21-6Z" fill="#4b9081"/><path d="m119 169 16 2" stroke="#8fc1ad" stroke-width="4"/>' : character.accessory === 'bow' ? '<path d="M125 57q-30-24-22 8 16 11 23-5 25 21 25-9-14-13-26 6" fill="#dc9977"/><circle cx="126" cy="60" r="7" fill="#bd765e"/>' : ''}</svg>`;
  }
  function sceneArt(scene, project = state.project) {
    const background = scene?.background || 'pond';
    const night = background === 'bedtime';
    const characters = (project?.characters || []).filter(c => scene?.character_ids?.includes(c.id));
    const palette = {pond:['#e1eddd','#b5cf9f','#8fba99'],meadow:['#f5eecf','#c6d69a','#acc187'],bedtime:['#394f67','#596d78','#4b7277'],studio:['#eee6d7','#e1d4c0','#d2c3a9']}[background] || ['#e1eddd','#b5cf9f','#8fba99'];
    const cast = characters.slice(0, 8).map((c, i, list) => {
      const width = Math.min(205, 660 / Math.max(list.length, 1));
      const x = 400 - list.length * width / 2 + i * width;
      const art = c.asset_id ? `<image href="${esc(path(`/assets/${enc(c.asset_id)}`, project))}" width="210" height="250" preserveAspectRatio="xMidYMid meet"/>` : animal(c).replace(/<svg[^>]*>|<\/svg>/g, '');
      return `<g transform="translate(${x} ${scene?.camera === 'close' ? 80 : 145}) scale(${scene?.camera === 'close' ? width / 165 : width / 215})"><g class="scene-character motion-${esc(scene?.motion || 'sway')}">${art}</g></g>`;
    }).join('');
    return `<svg viewBox="0 0 800 450" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="${esc(background)} illustration with ${characters.length} characters"><rect width="800" height="450" fill="${palette[0]}"/>${night ? '<circle cx="657" cy="77" r="29" fill="#f6e8b6"/><circle cx="670" cy="67" r="26" fill="#394f67"/><g fill="#eee7c6"><circle cx="100" cy="65" r="3"/><circle cx="270" cy="90" r="2"/><circle cx="508" cy="38" r="3"/><circle cx="732" cy="139" r="2"/></g>' : '<circle cx="668" cy="78" r="34" fill="#f3cf83"/><path d="M80 82q10-20 28-8 12-30 35-9 23-10 27 17Z" fill="#fbfcf2"/><path d="M494 57q9-18 24-7 10-24 30-7 18-8 23 14Z" fill="#fbfcf2"/>'}<path d="M0 252Q170 137 368 254T800 225V450H0Z" fill="${palette[1]}"/><path d="M0 326Q228 233 449 319T800 299V450H0Z" fill="${palette[2]}"/>${background === 'pond' ? '<ellipse cx="404" cy="390" rx="266" ry="75" fill="#8fc7bd"/><path d="M209 385h68m237 20h74m-224-5h47" stroke="#c0e1cd" stroke-width="3" stroke-linecap="round"/><ellipse cx="227" cy="399" rx="32" ry="10" fill="#6c9f6f"/><path d="m218 397 10-12 5 15" fill="#f3d6b3"/>' : ''}<g fill="${night ? '#788f80' : '#72985c'}"><ellipse cx="50" cy="327" rx="21" ry="45" transform="rotate(-25 50 327)"/><ellipse cx="70" cy="326" rx="15" ry="38" transform="rotate(22 70 326)"/><ellipse cx="751" cy="340" rx="18" ry="48" transform="rotate(20 751 340)"/></g>${cast}<g fill="none" stroke="${night ? '#a1bdba' : '#f9fff6'}" stroke-width="2" opacity=".7"><circle cx="180" cy="182" r="17"/><circle cx="607" cy="190" r="25"/><circle cx="646" cy="250" r="12"/><circle cx="233" cy="119" r="8"/></g></svg>`;
  }
  function showModal(content, {locked = false} = {}) {
    modal.innerHTML = `${locked ? '' : button('close-modal', '×', 'quiet close-modal', 'aria-label="Close dialog"')}${content}`;
    modal.dataset.locked = String(locked);
    if (!modal.open) modal.showModal();
  }
  function unlock() {
    if (authPending) return;
    authPending = true;
    showModal(`<div class="eyebrow">Private workspace</div><h2 id="modal-title">Unlock your studio</h2><p class="muted">Enter your server access token. It stays in this browser tab’s session storage; media uses a same-origin session cookie.</p><form id="unlock-form" class="fields">${field('Access token', '<input name="token" type="password" autocomplete="off" required maxlength="4096">')}<p id="unlock-error" role="alert" class="help"></p><button class="primary" type="submit">Unlock studio →</button></form>`, {locked:true});
    setTimeout(() => $('input', modal)?.focus(), 0);
  }
  function shell(body) {
    const p = state.project;
    const nav = [['projects','My projects'],['characters','Characters'],['storyboard','Storyboard'],['review','Review & export']];
    $('#app').innerHTML = `<div class="shell"><aside class="sidebar"><a href="/" class="brand" data-action="home"><span class="brand-mark">${animal({color:'#c69b73',accessory:'scarf'})}</span><span><strong>Pond Studio</strong><small>A LITTLE WORLD OF YOUR OWN</small></span></a><nav aria-label="Workspace"><a class="nav-item" href="/create">✦ Create any short video</a><p class="nav-label">Your workspace</p>${nav.map(([name,label]) => `<button class="nav-item ${state.view === name || (name === 'storyboard' && state.view === 'timeline') ? 'active' : ''}" data-action="nav" data-view="${name}" ${name !== 'projects' && !p ? 'disabled' : ''} ${state.view === name ? 'aria-current="page"' : ''}>${icon(name)}<span>${label}</span></button>`).join('')}<button class="nav-item" data-action="system">${icon('system')}<span>Studio status</span></button></nav><div class="sidebar-bottom"><p><span class="dot ${state.connected ? '' : 'bad'}"></span>${state.connected ? 'Your private studio' : 'Waiting for server'}</p><p>For grown-up creators.<br>Made for little imaginations.</p><span>Original Plinkety Pond IP</span></div></aside><div><header class="topbar"><div class="crumb">Workspace <span>/</span> <strong>${esc(p?.name || 'My projects')}</strong></div><div class="top-actions"><span id="save-status" class="help">${state.dirty ? 'Unsaved changes' : p ? 'All changes saved' : 'Local-first creation'}</span>${p ? button('save', 'Save changes', '', state.saving ? 'disabled' : '') : ''}${state.system?.auth_required ? button('lock', 'Lock', 'quiet') : ''}<span class="avatar" aria-label="Creator workspace">PS</span></div></header><main id="workspace" class="content" tabindex="-1">${body}</main></div></div>`;
  }
  function render() {
    stopPlayback();
    if (!state.connected) {shell(`<section class="connection panel"><div class="connection-art">${animal({color:'#b58b66',accessory:'scarf'})}</div><div class="eyebrow">A little story starts here</div><h1>Welcome to your pond.</h1><p class="muted">An original world of characters, songs, and small adventures. Connect to your Pond Studio server to open your projects.</p>${button('reconnect','Connect to studio →','primary')}<p class="help">Local or your private EC2 server · no external services required</p></section>`); return;}
    if (state.view === 'projects' || !state.project) {renderProjects(); return;}
    const p = state.project;
    const headings = {characters:['Meet your little cast','Create a familiar face for every small adventure.'],storyboard:['Give your story a little shape.','A scene at a time, bring your pond to life.'],timeline:['Find your story’s rhythm.','Arrange the moments. Leave a little room for wonder.'],review:['One last look, then it’s yours.','Review the story and download a local video package.']};
    const [heading, intro] = headings[state.view] || headings.storyboard;
    const steps = [['characters','Characters'],['storyboard','Storyboard'],['timeline','Timeline'],['review','Review & export']];
    shell(`<div class="heading"><div><div class="eyebrow">Plinkety Pond / ${esc(state.view)}</div><h1>${heading}</h1><p class="muted compact-title">${intro}</p></div><span class="pill">✦ Original by you</span></div><nav class="steps" aria-label="Creation steps">${steps.map(([name,label], i) => `<button data-action="nav" data-view="${name}" class="step ${state.view === name ? 'active' : ''}" ${state.view === name ? 'aria-current="step"' : ''}><span class="step-number">${i + 1}</span>${label}</button>`).join('')}</nav>${state.view === 'characters' ? charactersView() : state.view === 'review' ? reviewView() : storyboardView()}<footer class="footer-line"><span>Made with care, in your own little pond.</span><span>Made for kids · AI disclosure on · Local export only</span></footer>`);
    updateJobs();
  }
  function renderProjects() {
    shell(`<div class="heading"><div><div class="eyebrow">Your creative corner</div><h1>A little story starts here.</h1><p class="muted compact-title">Original characters. Small adventures. Entirely your own.</p></div>${button('new-project','+ New project','primary')}</div><div class="note green">Welcome to Pond Studio, your adult creator workspace for original Plinkety Pond nursery stories. Native illustrations work without any AI models.</div><section class="section-gap"><div class="panel-head"><h2>My projects <span class="muted">(${state.projects.length})</span></h2>${button('refresh','Refresh','quiet small')}</div>${state.projects.length ? `<div class="project-grid">${state.projects.map(p => `<article class="panel project-card"><div class="project-art">${sceneArt(p.scenes?.[0], p)}</div><div class="eyebrow">${esc(p.format || 'landscape')} · ${total(p)} seconds</div><h2>${esc(p.name)}</h2><p class="muted">${esc(p.topic || 'A new little adventure')}</p><p class="help">${p.scenes?.length || 0} scenes · ${p.characters?.length || 0} characters</p><div class="row section-gap">${button('open-project','Open project →','primary small',`data-id="${esc(p.id)}"`)}${button('delete-project','Delete','quiet small danger',`data-id="${esc(p.id)}"`)}</div></article>`).join('')}</div>` : `<div class="panel empty">${animal({accessory:'scarf'})}<h2>There’s room for a new adventure.</h2><p class="muted">Start with bubbles, counting, or a quiet bedtime story.</p>${button('new-project','Create your first project','primary')}</div>`}</section>`);
  }
  function projectHeading() {return `<div class="panel-head"><div><label class="field"><span class="eyebrow">Your story</span><input class="title-input" name="project-name" value="${esc(state.project.name)}" aria-label="Project name" maxlength="120" required></label></div><span class="pill">${total(state.project)}s / 120s</span></div>`;}
  function charactersView() {
    const p = state.project;
    return `<div class="panel-head"><div><h2>Your cast <span class="muted">${p.characters.length} / 8</span></h2><p class="help">Original native illustrations. Edit a character to make it your own.</p></div>${button('new-character','+ Add character','primary',p.characters.length >= 8 ? 'disabled' : '')}</div><div class="character-grid">${p.characters.map(c => `<article class="panel character-card"><div class="character-art">${c.asset_id ? `<img src="${path(`/characters/${enc(c.id)}/preview`)}?revision=${p.revision}" alt="${esc(c.name)} illustration">` : animal(c)}</div><div class="character-info"><div class="eyebrow">${esc(c.species)} · ${c.asset_id ? 'Custom image' : 'Native illustration'}</div><h3>${esc(c.name)}</h3><p>${esc(c.description || 'A new friend in your pond.')}</p><div class="row">${button('edit-character','Edit character','small',`data-id="${esc(c.id)}"`)}${button('generate-character','Create image','quiet small',`data-id="${esc(c.id)}"`)}</div></div></article>`).join('')}</div><div class="note section-gap">Native illustrations are ready to use. Optional mflux images run only through the server queue when a cached model is available. No models are downloaded.</div><section id="jobs" class="section-gap" aria-label="Character jobs"></section><div class="row section-gap">${button('nav','Continue to storyboard →','primary','data-view="storyboard"')}</div>`;
  }
  function selectedScene() {return state.project?.scenes.find(s => s.id === state.scene) || state.project?.scenes[0];}
  function storyboardView() {
    const p = state.project, scene = selectedScene();
    state.scene = scene?.id;
    return `<div class="workspace-grid"><div><section class="panel">${projectHeading()}<div class="scene-layout"><div><div class="panel-head"><span class="scene-list-heading">Scenes · ${p.scenes.length}/24</span>${button('add-scene','+ Add','small',p.scenes.length >= 24 || total(p) >= 120 ? 'disabled' : '')}</div><div class="scene-list" aria-label="Scenes">${p.scenes.map((s,i) => `<button class="scene-card ${s.id === scene?.id ? 'selected' : ''}" data-action="scene" data-id="${esc(s.id)}" ${s.id === scene?.id ? 'aria-pressed="true"' : 'aria-pressed="false"'}><span class="mini">${sceneArt(s)}</span><span>${String(i + 1).padStart(2,'0')} · ${esc(s.title)}<small>${Number(s.duration)} seconds · ${esc(s.motion)}</small></span></button>`).join('')}</div></div><div>${scene ? `<div id="stage-preview" class="preview ${p.format === 'portrait' ? 'portrait' : ''}">${sceneArt(scene)}<span class="preview-tag">Illustration preview</span><span class="preview-caption">${esc(scene.caption)}</span></div><div class="preview-meta"><span>${esc(scene.title)} · ${scene.duration}s</span><span>${esc(p.format)} · ${esc(scene.camera)}</span></div><div class="row">${button('play','▷ Play storyboard','small','id="play-button"')}${button('move-scene','← Earlier','quiet small','data-direction="-1"')}${button('move-scene','Later →','quiet small','data-direction="1"')}${button('remove-scene','Remove','quiet small danger',p.scenes.length <= 1 ? 'disabled' : '')}</div><p class="help">Browser animation is a timing sketch. Render a draft to review the final camera, audio, and motion.</p>` : '<p>Add a scene to start your storyboard.</p>'}</div></div></section>${timelineView()}<section class="panel section-gap"><div class="panel-head"><div><h2>Shape the story</h2><p class="help">A deterministic starter storyboard — template-based, not LLM generated.</p></div>${button('plan','Change topic & plan','small')}</div><p class="muted compact-title">${esc(p.topic || 'Choose a topic for your original story.')}</p></section></div><aside class="panel inspector"><div class="panel-head"><h2>Scene details</h2><span class="eyebrow" style="margin:0">${p.scenes.indexOf(scene) + 1} / ${p.scenes.length}</span></div>${scene ? sceneFields(scene) : ''}<div class="rule"></div><h3>Story audio</h3><p class="help">${p.audio.mode === 'uploaded' && p.audio.asset_id ? 'Uploaded audio · confirm vocals and lyrics in review.' : 'Draft guide music · instrumental timing guide, not real vocals.'}</p><div class="fields section-gap">${field('Volume', `<input name="audio-volume" type="range" min="0" max="1" step="0.05" value="${p.audio.volume}" aria-valuetext="${Math.round(p.audio.volume * 100)} percent">`)}${button('upload-audio', p.audio.asset_id ? 'Replace audio…' : 'Upload real audio…','small')}${p.audio.asset_id ? button('guide-audio','Use guide music','quiet small') : ''}</div><div class="rule"></div><div class="note">Guide music contains no real vocals. Upload a rights-cleared song for a review-ready export.</div></aside></div><div class="row section-gap" style="justify-content:flex-end">${button('nav','Review & export →','primary','data-view="review"')}</div>`;
  }
  function sceneFields(s) {return `<div class="fields">${field('Scene title',input('scene-title',s.title,'maxlength="120" required'))}<div class="two-col">${field('Duration (seconds)',input('scene-duration',s.duration,'type="number" min="1" max="15" step="0.5" required'))}${field('Camera',select('scene-camera',['wide','close'],s.camera))}</div>${field('Caption',`<textarea name="scene-caption" maxlength="300" rows="2">${esc(s.caption)}</textarea>`)}${field('Narration / lyrics',`<textarea name="scene-narration" maxlength="2000" rows="3">${esc(s.narration)}</textarea>`,'Script notes only; guide mode does not synthesize vocals.')}<div class="two-col">${field('Background',select('scene-background',['pond','meadow','bedtime','studio'],s.background))}${field('Motion',select('scene-motion',['bounce','sway','float'],s.motion))}</div><fieldset style="border:0;padding:0;margin:0"><legend class="field" style="margin-bottom:8px">Characters in this scene</legend><div class="checks">${state.project.characters.map(c => `<label class="check"><input type="checkbox" name="scene-character" value="${esc(c.id)}" ${s.character_ids.includes(c.id) ? 'checked' : ''}>${esc(c.name)}</label>`).join('')}</div></fieldset></div>`;}
  function timelineView() {const p = state.project; let start = 0; return `<section class="panel timeline"><div class="panel-head"><div><h2>Story timeline</h2><p class="help">Select a scene to edit. Use Earlier / Later to reorder.</p></div><span class="pill amber">${p.audio.mode === 'guide' ? '♪ Guide music' : '♪ Uploaded audio'}</span></div><div class="timeline-track" aria-label="Scene timeline">${p.scenes.map((s,i) => {const time = start; start += Number(s.duration); return `<button class="timeline-clip ${s.id === state.scene ? 'active' : ''}" style="--seconds:${Number(s.duration)}" data-action="scene" data-id="${esc(s.id)}" aria-label="Scene ${i+1}, ${esc(s.title)}, starts at ${time} seconds, duration ${s.duration} seconds"><span>${String(i + 1).padStart(2,'0')} · ${esc(s.title)}</span><small>${time.toFixed(1)}s — ${start.toFixed(1)}s</small></button>`;}).join('')}</div><div class="audio-track"><span>♫</span><span>${p.audio.mode === 'guide' ? 'Draft instrumental guide · not real vocals' : 'Rights-confirmed uploaded audio'}</span><span class="wave" aria-hidden="true"></span><span>${total(p)}s</span></div></section>`;}
  const reviewLabels = {originality:'Original story, characters, and designs; no copied franchise IP',rights:'I hold the rights to all uploaded images, music, and vocals',child_safety:'I have checked the story for child safety and suitability',lyrics:'I have listened to the real audio and checked every lyric',visuals:'I have watched the full video and checked every scene'};
  function reviewView() {const p = state.project; return `<div class="review-layout"><div><section class="panel">${projectHeading()}<p class="muted">Render a draft to check your timing. Final renders use your selected format and saved project revision.</p><div class="render-options">${field('Frame',select('project-format',[['landscape','Landscape · 16:9'],['portrait','Portrait · 9:16']],p.format))}${field('Quality',select('project-resolution',[['draft','Draft preview'],['final','Final video']],p.resolution))}${button('render','Render video →','primary',(state.busy || resourceBlocked()) ? 'disabled title="Rendering waits for green memory pressure and sufficient disk space"' : '')}</div><div class="note section-gap ${p.audio.mode === 'uploaded' ? 'green' : ''}">${p.audio.mode === 'guide' ? 'Draft guide music only — not real vocals. You can download a preview bundle; upload real audio and complete review for a review-ready package.' : 'Uploaded audio selected. Listen carefully and complete the checklist before treating this video as ready.'}</div><div id="system-inline" class="system-info">${systemSummary()}</div></section><section id="jobs" class="section-gap" aria-label="Render jobs"></section></div><aside class="panel"><div class="eyebrow">The finishing touches</div><h2>Creator review</h2><p class="help">These are your review notes, not publishing approval. Save them, then render again to bind them to a new video.</p><div class="checks section-gap">${Object.entries(reviewLabels).map(([key,label]) => `<label class="check"><input type="checkbox" name="review-${key}" ${p.review[key] ? 'checked' : ''}>${label}</label>`).join('')}</div><div class="rule"></div><div class="note green">Always classified as made for kids.<br>AI disclosure is always on.</div><p class="help section-gap">Downloads stay local. The ZIP includes video, poster, description, metadata, captions, and a checklist. Review readiness comes from the server; this workspace never approves or posts.</p>${button('upload-audio','Upload real audio…','full-width section-gap')}</aside></div>`;}
  function systemSummary() {const s = state.system; return s ? `<span><span class="dot ${s.pressure !== 'green' ? 'bad' : ''}"></span>Pressure: ${esc(s.pressure)}</span><span>${Number(s.free_disk_gb).toFixed(1)} GB free</span><span>${esc(s.platform)}</span>` : '<span>Server status unavailable</span>';}
  function jobMarkup(job) {
    const current = job.project_revision === state.project.revision && !state.dirty;
    const succeeded = job.status === 'succeeded';
    return `<article class="job" data-job="${esc(job.id)}"><div class="panel-head"><strong>${job.kind === 'character' ? 'Character image' : `${job.resolution === 'final' ? 'Final' : 'Draft'} · ${esc(job.format)}`}</strong><span class="pill ${job.status === 'failed' ? 'error' : active(job) ? 'amber' : ''}" data-job-status>${esc(job.status)}</span></div><div data-job-progress ${active(job) ? '' : 'hidden'}><progress value="${Math.max(0,Math.min(100,Number(job.progress)||0))}" max="100" aria-label="Job progress"></progress></div><p data-job-message>${esc(job.error || job.message || '')}</p><p data-job-revision class="${current ? 'help' : 'job-stale'}">${current ? 'Matches your saved project' : `Snapshot revision ${Number(job.project_revision)} · project has changed; render again for current edits`}</p>${succeeded && job.kind === 'render' ? `<video controls playsinline preload="metadata" poster="/api/jobs/${enc(job.id)}/poster" src="/api/jobs/${enc(job.id)}/video" aria-label="Rendered video preview"></video><p class="help">${job.guide_audio ? 'Draft guide music · not real vocals' : 'Uploaded audio'} · ${current && job.review_ready ? 'Server marked review-ready' : 'Preview bundle — review readiness incomplete or outdated'}</p>` : succeeded && job.kind === 'character' ? `<img src="/api/jobs/${enc(job.id)}/character" alt="Generated character result" style="max-width:220px;border-radius:10px"><p class="help">The server attaches this image only when the saved project revision still matches.</p>` : ''}<div class="row" data-job-actions>${active(job) ? button('cancel-job','Cancel job','small',`data-id="${esc(job.id)}"`) : ''}${succeeded ? button('download',job.kind === 'render' ? '↓ Download MP4' : '↓ Download PNG','small',`data-id="${esc(job.id)}" data-file="${job.kind === 'render' ? 'download' : 'character'}"`) : ''}${succeeded && job.kind === 'render' ? button('download','↓ Export ZIP','primary small',`data-id="${esc(job.id)}" data-file="package"`) : ''}</div></article>`;
  }
  function updateJobs() {
    const el = $('#jobs'); if (!el || !state.project) return;
    const jobs = state.jobs.filter(job => job.project_id === state.project.id && (state.view === 'characters' ? job.kind === 'character' : job.kind === 'render'));
    if (!jobs.length) {el.innerHTML = `<div class="panel empty" style="padding:30px"><h3>${state.view === 'characters' ? 'A home for your character images' : 'Your video will appear here'}</h3><p class="help">${state.view === 'characters' ? 'Use Create image to queue a native illustration or an available cached model.' : 'Render a draft, watch it through, then download your video and ZIP.'}</p></div>`; return;}
    // Preserve playing video elements as polling updates progress and messages.
    if (!el.querySelector('[data-job]')) el.innerHTML = '';
    for (const job of jobs) {
      let card = [...el.querySelectorAll('[data-job]')].find(node => node.dataset.job === job.id);
      const key = `${job.status}:${job.project_revision}:${state.project.revision}:${state.dirty}`;
      if (!card) {el.insertAdjacentHTML('beforeend',jobMarkup(job)); card = el.lastElementChild; card.dataset.renderKey = key;}
      else if (card.dataset.renderKey !== key) {card.outerHTML = jobMarkup(job); card = [...el.querySelectorAll('[data-job]')].find(node => node.dataset.job === job.id); card.dataset.renderKey = key;}
      else {const progress = $('progress',card); if (progress) progress.value = Number(job.progress)||0; $('[data-job-message]',card).textContent = job.error || job.message || '';}
    }
  }
  function markDirty() {state.dirty = true; state.edit++; const status = $('#save-status'); if (status) status.textContent = 'Unsaved changes'; updateJobs();}
  function validateProject(p) {
    if (!p.name.trim() || p.name.length > 120) throw new Error('Give your project a name of 1–120 characters.');
    if (p.scenes.length < 1 || p.scenes.length > 24 || total(p) > 120) throw new Error('Use 1–24 scenes and no more than 120 seconds in total.');
    if (p.characters.length > 8) throw new Error('A project can have up to 8 characters.');
    if (p.scenes.some(s => !Number.isFinite(Number(s.duration)) || s.duration < 1 || s.duration > 15 || !s.title.trim())) throw new Error('Every scene needs a title and a duration from 1 to 15 seconds.');
    const ids = new Set(p.characters.map(c => c.id));
    if (ids.size !== p.characters.length || new Set(p.scenes.map(s => s.id)).size !== p.scenes.length || p.scenes.some(s => s.character_ids.some(id => !ids.has(id)))) throw new Error('Scene and character IDs must be unique and references must exist.');
    if (!p.made_for_kids || !p.ai_disclosure) throw new Error('Made-for-kids classification and AI disclosure must remain enabled.');
  }
  async function save() {
    if (!state.project || !state.dirty) return true;
    if (state.saving) {notify('A save is already in progress. Please wait.'); return false;}
    if (state.conflict) {conflictDialog(); return false;}
    validateProject(state.project);
    const snapshot = clone(state.project), edit = state.edit;
    state.saving = true;
    const saveButton = $('[data-action="save"]'); if (saveButton) saveButton.disabled = true;
    try {
      const saved = await api(path('', snapshot), {method:'PUT', body:snapshot});
      if (state.project?.id !== saved.id) return false;
      if (state.edit === edit) {state.project = saved; state.dirty = false;} else {state.project.revision = saved.revision; state.project.updated_at = saved.updated_at;}
      upsertProject(saved);
      $('#save-status').textContent = state.dirty ? 'New edits not yet saved' : 'All changes saved';
      updateJobs(); return !state.dirty;
    } catch (error) {
      if (error.status === 409) {state.conflict = true; conflictDialog(); return false;}
      throw error;
    } finally {state.saving = false; if (saveButton?.isConnected) saveButton.disabled = false;}
  }
  function upsertProject(project) {const i = state.projects.findIndex(p => p.id === project.id); if (i >= 0) state.projects[i] = project; else state.projects.unshift(project);}
  function conflictDialog() {showModal(`<h2 id="modal-title">This story changed elsewhere.</h2><p class="muted">Your edits are still here. Download a copy before loading the latest server version. We won’t overwrite another revision.</p><div class="note">Reloading replaces the edits in this tab. A JSON copy lets you keep your text and scene settings for reference.</div><div class="row">${button('backup','Download my edits','small')}${button('close-modal','Keep editing','small')}${button('reload-project','Load server version','primary')}</div>`);}
  function downloadBlob(blob, filename) {const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);}
  async function openProject(id) {
    const project = await api(`/api/projects/${enc(id)}`);
    state.project = project; state.scene = project.scenes[0]?.id; state.dirty = false; state.conflict = false; state.jobs = []; state.edit++;
    store.set('pond-selected-project', id); state.view = 'storyboard'; render(); $('#workspace')?.focus({preventScroll:true}); await loadJobs();
  }
  async function leaveThen(callback) {
    if (state.saving) {notify('Please wait for the current save.'); return;}
    if (!state.dirty) return callback();
    showModal(`<h2 id="modal-title">Keep your latest changes?</h2><p class="muted">Save this story before leaving, or discard the edits in this tab.</p><div class="row">${button('close-modal','Stay here')}${button('discard-leave','Discard','danger')}${button('save-leave','Save & continue','primary')}</div>`);
    modal._leave = callback;
  }
  async function connect() {
    try {
      state.system = await api('/api/system');
      if (state.token) await api('/api/session', {method:'POST',body:{token:state.token}});
      const data = await api('/api/projects'); state.projects = data.projects; state.connected = true; authPending = false;
      if (modal.open && modal.dataset.locked === 'true') modal.close();
      const selected = store.get('pond-selected-project');
      if (!state.project && selected && state.projects.some(p => p.id === selected)) await openProject(selected); else render();
      schedulePoll();
    } catch (error) {if (error.status !== 401) {state.connected = false; render(); notify(error.message,true);}}
  }
  async function loadJobs() {
    if (!state.project) return;
    const id = state.project.id;
    const data = await api(`/api/jobs?project_id=${enc(id)}`);
    if (state.project?.id !== id) return;
    state.jobs = data.jobs.sort((a,b) => String(b.created_at).localeCompare(String(a.created_at)));
    updateJobs();
  }
  function schedulePoll() {clearTimeout(pollTimer); pollTimer = setTimeout(poll, 2500);}
  async function poll() {
    if (polling || !state.connected || authPending) {schedulePoll(); return;}
    polling = true;
    try {
      const oldActive = state.jobs.filter(active);
      if (state.project && (oldActive.length || ['review','characters'].includes(state.view))) await loadJobs();
      if (oldActive.some(job => job.kind === 'character' && state.jobs.find(j => j.id === job.id)?.status === 'succeeded')) {
        if (!state.dirty && !state.saving) {const latest = await api(path()); state.project = latest; upsertProject(latest); if (state.view === 'characters' && !modal.open) render();}
        else notify('Character image completed. Save or back up your edits, then reload the project to see whether the server attached it.');
      }
      state.system = await api('/api/system');
      const inline = $('#system-inline'); if (inline) inline.innerHTML = systemSummary();
      const renderButton = $('[data-action="render"]'); if (renderButton) renderButton.disabled = state.busy || resourceBlocked();
    } catch (error) {if (error.status !== 401) {const inline = $('#system-inline'); if (inline) inline.textContent = 'Connection interrupted. Retrying…';}}
    finally {polling = false; schedulePoll();}
  }
  function stopPlayback() {clearInterval(playbackTimer); state.playing = false; $('#stage-preview')?.classList.remove('playing'); const button = $('#play-button'); if (button) button.textContent = '▷ Play storyboard';}
  function play() {
    if (state.playing) {stopPlayback(); return;}
    const scenes = state.project.scenes;
    if (!scenes.length) return;
    state.playing = true; let index = Math.max(0,scenes.findIndex(s => s.id === state.scene)), elapsed = 0;
    const paint = () => {state.scene = scenes[index].id; const el = $('#stage-preview'); if (el) {el.innerHTML = `${sceneArt(scenes[index])}<span class="preview-tag">Timing sketch · no audio</span><span class="preview-caption">${esc(scenes[index].caption)}</span>`; el.classList.add('playing');} $('#play-button').textContent = 'Ⅱ Pause preview';};
    paint(); playbackTimer = setInterval(() => {elapsed += 0.1; if (elapsed >= scenes[index].duration) {elapsed = 0; index++; if (index >= scenes.length) {stopPlayback(); render(); return;} paint();}},100);
  }
  function newProjectDialog() {showModal(`<div class="eyebrow">A fresh page</div><h2 id="modal-title">What shall we make?</h2><form id="new-project-form" class="fields">${field('Project name',input('name','', 'placeholder="A bubble-filled afternoon" maxlength="120" required autofocus'))}${field('Story topic',input('topic','','placeholder="Small discoveries around the pond" maxlength="1000"'))}${field('Start with a template',select('template',[['bubbles','Bubbles · a playful little adventure'],['bedtime','Bedtime · a softer kind of story'],['counting','Counting · one small discovery at a time']],'bubbles'))}<p class="help">An original 15-second starter, using native illustrations and instrumental guide music. Template-based, not AI generated.</p><div class="row">${button('close-modal','Cancel')}<button class="primary" type="submit">Create story →</button></div></form>`);}
  function characterDialog(character) {
    const c = character || {id:uid('character'),name:'',species:'otter',color:'#b1845f',accessory:'scarf',description:'',asset_id:null};
    showModal(`<div class="eyebrow">A new friend</div><h2 id="modal-title">${character ? 'Edit your character' : 'Meet your next character'}</h2><form id="character-form" data-id="${esc(c.id)}" class="fields">${field('Name',input('name',c.name,'maxlength="80" required'))}<div class="two-col">${field('Species',select('species',['otter','rabbit','bear','frog'],c.species))}${field('Colour',input('color',c.color,'type="color"'))}</div>${field('Accessory',select('accessory',['scarf','bow','none'],c.accessory))}${field('Personality / image direction',`<textarea name="description" maxlength="1000">${esc(c.description)}</textarea>`,'Keep designs original. Model imagery contains no text.')}<div id="character-live" style="height:130px;display:flex;justify-content:center">${animal(c)}</div>${c.asset_id ? '<div class="note">This character uses a custom image. Native colour and accessory edits appear after you choose Reset to native.</div>' : ''}<div class="row">${character ? button('delete-character','Remove','quiet small danger',`data-id="${esc(c.id)}"`) : ''}${c.asset_id ? button('reset-character','Reset to native','small',`data-id="${esc(c.id)}"`) : ''}${character ? button('upload-image','Upload image','small',`data-id="${esc(c.id)}"`) : ''}<button class="primary" type="submit">Keep character</button></div></form>`);
  }
  function uploadDialog(kind, characterId) {showModal(`<h2 id="modal-title">${kind === 'audio' ? 'Bring your own song' : 'Give your character a new look'}</h2><p class="muted">${kind === 'audio' ? 'Upload a rights-cleared WAV or MP3 with the real vocals you want in your story.' : 'Upload an original PNG, JPEG, or WebP. SVG files are not accepted.'}</p><form id="upload-form" data-kind="${kind}" data-character="${esc(characterId || '')}" class="fields"><label class="field upload-label">Choose ${kind === 'audio' ? 'audio' : 'an image'}<input name="file" type="file" accept="${kind === 'audio' ? '.wav,.mp3,audio/wav,audio/mpeg' : '.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp'}" required></label><label class="check"><input type="checkbox" name="rights" required>I own or have permission to use this file, including any music, voices, and likenesses.</label><p class="help">Server file-size and content validation also apply. Files stay in this project.</p><div class="row">${button('close-modal','Cancel')}<button class="primary" type="submit">Upload & use</button></div></form>`);}
  async function generateDialog(characterId) {
    const c = state.project.characters.find(c => c.id === characterId);
    const providers = state.system?.providers || [{id:'illustration',label:'Native illustration',available:true,reason:'Original local drawing'}];
    const options = providers.map(p => `<option value="${esc(p.id)}" ${p.available ? '' : 'disabled'}>${esc(p.label)}${p.available ? '' : ' · unavailable'}</option>`).join('');
    const blocked = resourceBlocked();
    showModal(`<h2 id="modal-title">Create ${esc(c.name)}’s image</h2><p class="muted">Choose an original native illustration or an available AI image provider on this server. Results attach only if your saved project has not changed.</p><form id="generate-form" data-id="${esc(c.id)}" class="fields">${field('Image provider',`<select name="provider">${options}</select>`)}<div class="note">${blocked ? 'Image creation is paused until memory pressure is green and disk space is available.' : 'Images use the guarded background queue. Models are never downloaded during a job.'}</div><div class="row">${button('close-modal','Cancel')}<button type="submit" class="primary" ${blocked ? 'disabled' : ''}>Queue image</button></div></form>`);
  }
  function planDialog() {showModal(`<h2 id="modal-title">Give the story a new start</h2><p class="muted">This replaces the saved storyboard with an original template-based plan. Save your current edits first.</p><form id="plan-form" class="fields">${field('Story topic',input('topic',state.project.topic,'maxlength="1000" required'))}${field('Template',select('template',['bubbles','bedtime','counting'],'bubbles'))}<div class="row">${button('close-modal','Keep current story')}<button type="submit" class="primary">Replace storyboard</button></div></form>`);}
  async function queueJob(body) {
    if (resourceBlocked()) {notify('Wait for green memory pressure and at least 10 GB free disk before generating.',true); return false;}
    if (!await save()) return false;
    const job = await api(path('/jobs'),{method:'POST',body}); state.jobs.unshift(job); updateJobs(); notify('Added to the studio queue.');
    return true;
  }
  function systemDialog() {const s = state.system; showModal(`<h2 id="modal-title">Your studio, at a glance</h2><div class="system-info">${systemSummary()}</div><div class="rule"></div><h3>Available providers</h3><div class="fields">${(s?.providers || []).map(p => `<div><span class="pill ${p.available ? '' : 'amber'}">${p.available ? 'Available' : 'Unavailable'}</span> <strong>${esc(p.label)}</strong><p class="help">${esc(p.reason || '')}</p></div>`).join('') || '<p class="help">No providers reported by the server.</p>'}</div><p class="help section-gap">${s?.active_job ? 'A job is using the studio queue.' : 'No active server job.'} Resource limits are enforced by the server.</p><div class="row">${button('close-modal','Done','primary')}</div>`);}
  async function action(name, target) {
    const id = target.dataset.id;
    switch (name) {
      case 'home': case 'nav': {
        const view = name === 'home' ? 'projects' : target.dataset.view;
        if (view === 'projects') await leaveThen(() => {state.view = 'projects'; render();});
        else {state.view = view; render(); $('#workspace')?.focus({preventScroll:true}); if (['review','characters'].includes(view)) await loadJobs();}
        break;
      }
      case 'reconnect': await connect(); break;
      case 'refresh': {const data = await api('/api/projects'); state.projects = data.projects; render(); break;}
      case 'new-project': await leaveThen(newProjectDialog); break;
      case 'open-project': await leaveThen(() => openProject(id)); break;
      case 'delete-project': {const p = state.projects.find(p => p.id === id); showModal(`<h2 id="modal-title">Delete this project?</h2><p class="muted">${esc(p.name)} and its project assets will be removed. This cannot be undone.</p><div class="row">${button('close-modal','Keep project')}${button('confirm-delete-project','Delete project','danger',`data-id="${esc(id)}"`)}</div>`); break;}
      case 'confirm-delete-project': await api(`/api/projects/${enc(id)}`,{method:'DELETE'}); state.projects = state.projects.filter(p => p.id !== id); if (state.project?.id === id) {state.project = null; state.dirty = false; state.jobs = []; store.set('pond-selected-project','');} state.view = 'projects'; modal.close(); render(); break;
      case 'save': if (await save()) notify('Story saved.'); break;
      case 'close-modal': modal.close(); break;
      case 'backup': downloadBlob(new Blob([JSON.stringify(state.project,null,2)],{type:'application/json'}),'pond-studio-unsaved-project.json'); break;
      case 'reload-project': showModal(`<h2 id="modal-title">Load the server version?</h2><p>Your unsaved edits in this tab will be replaced. Download your edits first if you want to keep a copy.</p><div class="row">${button('backup','Download my edits')}${button('confirm-reload','Replace local edits','primary')}</div>`); break;
      case 'confirm-reload': await openProject(state.project.id); modal.close(); break;
      case 'save-leave': {const next = modal._leave; modal.close(); if (await save()) await next?.(); break;}
      case 'discard-leave': {const next = modal._leave; const latest = await api(path()); state.project = latest; state.dirty = false; state.conflict = false; modal.close(); await next?.(); break;}
      case 'scene': stopPlayback(); state.scene = id; render(); break;
      case 'add-scene': {const p = state.project; if (p.scenes.length >= 24 || total(p) >= 120) throw new Error('This story has reached its scene or duration limit.'); const s = {id:uid('scene'),title:`Scene ${p.scenes.length+1}`,duration:Math.min(5,120-total(p)),narration:'',caption:'',background:'pond',motion:'sway',character_ids:p.characters.slice(0,1).map(c => c.id),camera:'wide'}; p.scenes.push(s); state.scene=s.id; markDirty(); render(); break;}
      case 'remove-scene': if (state.project.scenes.length > 1) {state.project.scenes=state.project.scenes.filter(s => s.id !== state.scene); state.scene=state.project.scenes[0].id; markDirty(); render();} break;
      case 'move-scene': {const scenes=state.project.scenes, i=scenes.findIndex(s => s.id === state.scene), j=i+Number(target.dataset.direction); if (j>=0 && j<scenes.length) {[scenes[i],scenes[j]]=[scenes[j],scenes[i]]; markDirty(); render();} break;}
      case 'play': play(); break;
      case 'new-character': if (state.project.characters.length < 8) characterDialog(); break;
      case 'edit-character': characterDialog(state.project.characters.find(c => c.id === id)); break;
      case 'delete-character': showModal(`<h2 id="modal-title">Remove this character?</h2><p class="muted">They’ll also be removed from every scene in this story.</p><div class="row">${button('close-modal','Keep character')}${button('confirm-delete-character','Remove','danger',`data-id="${esc(id)}"`)}</div>`); break;
      case 'confirm-delete-character': state.project.characters=state.project.characters.filter(c => c.id !== id); state.project.scenes.forEach(s => {s.character_ids=s.character_ids.filter(cid => cid !== id);}); markDirty(); modal.close(); render(); break;
      case 'reset-character': state.project.characters.find(c => c.id === id).asset_id=null; markDirty(); modal.close(); render(); break;
      case 'generate-character': await generateDialog(id); break;
      case 'upload-audio': uploadDialog('audio'); break;
      case 'upload-image': uploadDialog('image',id); break;
      case 'guide-audio': state.project.audio.mode='guide'; state.project.audio.asset_id=null; markDirty(); render(); break;
      case 'plan': planDialog(); break;
      case 'render': await queueJob({kind:'render',format:state.project.format,resolution:state.project.resolution}); break;
      case 'cancel-job': {const job=await api(`/api/jobs/${enc(id)}/cancel`,{method:'POST'}); const i=state.jobs.findIndex(j => j.id===id); if(i>=0)state.jobs[i]=job; updateJobs(); break;}
      case 'download': {const file = target.dataset.file; if (!['download','package','character'].includes(file)) return; const blob = await api(`/api/jobs/${enc(id)}/${file}`,{blob:true}); downloadBlob(blob,`pond-studio-${id}.${file === 'package' ? 'zip' : file === 'character' ? 'png' : 'mp4'}`); break;}
      case 'system': state.system = await api('/api/system'); systemDialog(); break;
      case 'lock': await leaveThen(async () => {await api('/api/session',{method:'DELETE'}); state.token=''; store.set('pond-token','',true); state.connected=false; state.project=null; state.jobs=[]; render(); unlock();}); break;
    }
  }
  document.addEventListener('click', async event => {
    const target = event.target.closest('[data-action]'); if (!target || target.disabled) return;
    event.preventDefault();
    const name=target.dataset.action;
    const remote=['render','generate-character','confirm-delete-project','cancel-job','download','open-project','confirm-reload','system','refresh'];
    if (remote.includes(name) && state.busy) {notify('Please wait for the current operation.'); return;}
    target.disabled=true;
    if(remote.includes(name)) state.busy=true;
    try {await action(name,target);} catch(error) {notify(error.message,true);} finally {target.disabled=false; if(remote.includes(name))state.busy=false;}
  });
  document.addEventListener('change', event => {
    const el=event.target, name=el.name;
    if (!name || el.closest('dialog') || !state.project) return;
    const scene=selectedScene();
    try {
      if (el.validity && !el.validity.valid) {el.reportValidity(); return;}
      if(name==='project-name') {if(!el.value.trim()) throw new Error('Your story needs a name.'); state.project.name=el.value;}
      else if(name==='project-format') state.project.format=el.value;
      else if(name==='project-resolution') state.project.resolution=el.value;
      else if(name==='audio-volume') {state.project.audio.volume=Number(el.value); el.setAttribute('aria-valuetext',`${Math.round(Number(el.value)*100)} percent`);}
      else if(name.startsWith('review-')) state.project.review[name.slice(7)]=el.checked;
      else if(name==='scene-character') scene.character_ids=[...document.querySelectorAll('input[name="scene-character"]:checked')].map(input => input.value);
      else if(name.startsWith('scene-') && scene) {
        const key=name.slice(6);
        if(key==='duration') {const duration=Number(el.value); if(duration<1||duration>15||total(state.project)-scene.duration+duration>120) {el.value=scene.duration; throw new Error('Use 1–15 seconds per scene, up to 120 seconds in total.');} scene.duration=duration;}
        else scene[key]=el.value;
      } else return;
      markDirty();
      // Update art without replacing the focused editor or interrupting typing.
      const preview=$('#stage-preview'); if(preview && scene)preview.innerHTML=`${sceneArt(scene)}<span class="preview-tag">Illustration preview</span><span class="preview-caption">${esc(scene.caption)}</span>`;
      if(name==='scene-duration'||name==='scene-title') {const timeline=$('.timeline'); if(timeline)timeline.outerHTML=timelineView(); const card=[...document.querySelectorAll('.scene-card')].find(c => c.dataset.id===scene.id); if(card){const text=card.lastElementChild; text.innerHTML=`${String(state.project.scenes.indexOf(scene)+1).padStart(2,'0')} · ${esc(scene.title)}<small>${scene.duration} seconds · ${esc(scene.motion)}</small>`;}}
    } catch(error){notify(error.message,true);}
  });
  modal.addEventListener('input', event => {const form=event.target.closest('#character-form'); if(!form)return; const data=new FormData(form); $('#character-live').innerHTML=animal(Object.fromEntries(data));});
  document.addEventListener('submit', async event => {
    event.preventDefault(); const form=event.target; if (!form.checkValidity()) {form.reportValidity(); return;}
    const submit=$('button[type="submit"]',form); if(submit?.disabled)return;
    if(submit)submit.disabled=true;
    const data=new FormData(form);
    try {
      switch(form.id) {
        case 'unlock-form': {
          const token=String(data.get('token')).trim();
          await api('/api/session',{method:'POST',body:{token}});
          state.token=token; store.set('pond-token',token,true); authPending=false; modal.close(); await connect(); break;
        }
        case 'new-project-form': {
          const project=await api('/api/projects',{method:'POST',body:Object.fromEntries(data)}); upsertProject(project); modal.close(); await openProject(project.id); break;
        }
        case 'character-form': {
          const id=form.dataset.id, characters=state.project.characters, existing=characters.find(c => c.id===id);
          const c={...(existing||{id,asset_id:null}),...Object.fromEntries(data)};
          if(!c.name.trim())throw new Error('Your character needs a name.');
          if(existing)characters[characters.indexOf(existing)]=c; else {if(characters.length>=8)throw new Error('A project supports up to 8 characters.'); characters.push(c);}
          markDirty(); modal.close(); render(); break;
        }
        case 'upload-form': {
          if(!await save())return;
          const projectId=state.project.id, kind=form.dataset.kind, characterId=form.dataset.character;
          const payload=new FormData(); payload.set('file',data.get('file')); payload.set('kind',kind); payload.set('rights_confirmed','true');
          const asset=await api(path('/assets'),{method:'POST',body:payload});
          if(state.project.id!==projectId)throw new Error('Upload finished for another project. Reopen that project to continue.');
          if(kind==='audio'){state.project.audio.asset_id=asset.id;state.project.audio.mode='uploaded';}else {const c=state.project.characters.find(c=>c.id===characterId); if(!c)throw new Error('The character no longer exists.'); c.asset_id=asset.id;}
          state.project.review.rights=false; if(kind==='audio')state.project.review.lyrics=false; else state.project.review.visuals=false;
          markDirty(); const saved=await save(); if(saved)modal.close(); render(); notify(saved ? 'Upload saved to your project.' : 'File uploaded. Save your project to keep the selection.'); break;
        }
        case 'generate-form': {if(await queueJob({kind:'character',character_id:form.dataset.id,provider:data.get('provider')}))modal.close(); break;}
        case 'plan-form': {if(!await save())return; state.project=await api(path('/plan'),{method:'POST',body:Object.fromEntries(data)}); upsertProject(state.project); state.scene=state.project.scenes[0]?.id; state.dirty=false; modal.close(); render(); break;}
      }
    } catch(error) {if(form.id==='unlock-form') {const errorEl=$('#unlock-error'); if(errorEl)errorEl.textContent=error.message;} else notify(error.message,true);}
    finally {if(submit?.isConnected)submit.disabled=false;}
  });
  modal.addEventListener('cancel', event => {if(modal.dataset.locked==='true')event.preventDefault();});
  window.addEventListener('beforeunload',event=>{if(state.dirty){event.preventDefault();event.returnValue='';}});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stopPlayback();else schedulePoll();});
  render(); connect();
})();

import { available, escapeHtml as escape, format, extent, seriesPath, score,
  metricValue, axisValue, selectedEpisode, phaseShare } from './model.js';

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const state = {catalog:[], run:null, comparison:null, page:'evolution', metric:'score',
  evaluator:'', axis:'episode', selected:0, phase:'observe', playing:false, speed:1,
  filter:'all', token:'', session:null};
let timer, toastTimer, objectUrl, requestGeneration = 0, refreshing = false;
let draft = null;
const populationCache = new Map();
const clone = (object) => JSON.parse(JSON.stringify(object));
const current = () => selectedEpisode(state.run, state.selected);
const last = () => state.run.episodes.at(-1);
function toast(text, error = false) {
  clearTimeout(toastTimer); $('#toast').textContent = text;
  $('#toast').classList.toggle('toast-error', error); $('#toast').hidden = false;
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 5000);
}
async function api(path, body) {
  const response = await fetch(path, {method:body ? 'POST':'GET',
    headers:{Authorization:`Bearer ${state.token}`, ...(body ? {'Content-Type':'application/json'}:{})},
    ...(body ? {body:JSON.stringify(body)}:{})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
function theme(value) {
  document.documentElement.dataset.theme = value;
  $('#theme-toggle').setAttribute('aria-label', `Switch to ${value === 'light' ? 'dark':'light'} theme`);
  try { localStorage.setItem('proteus-visualize-theme', value); } catch { /* Optional. */ }
}
function page(value) {
  state.page = ['evolution','measurements','configuration','snapshots'].includes(value) ? value:'evolution';
  $$('.page-content').forEach((el) => { el.hidden = el.id !== `page-${state.page}`; });
  $$('[data-page]').forEach((el) => {
    el.classList.toggle('active', el.dataset.page === state.page);
    if (el.dataset.page === state.page) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
  history.replaceState(null, '', `#${state.page}`);
}
function catalog() {
  $('#run-count').textContent = state.catalog.length;
  $('#run-list').innerHTML = state.catalog.map((r) => `<button class="run-button ${r.id === state.run?.id ? 'active':''}" data-run="${escape(r.id)}">${escape(r.name)}<small>${escape(r.harness)} · ${r.episodes_target} episodes planned</small></button>`).join('') || '<p class="micro">No sweeps found.</p>';
  $('#mobile-run').innerHTML = state.catalog.map((r) => `<option value="${escape(r.id)}" ${r.id === state.run?.id ? 'selected':''}>${escape(r.name)}</option>`).join('');
  $('#compare-run').innerHTML = '<option value="">None</option>' + state.catalog.filter((r) => r.id !== state.run?.id).map((r) => `<option value="${escape(r.id)}" ${r.id === state.comparison?.id ? 'selected':''}>${escape(r.name)}</option>`).join('');
}
function heading() {
  const r = state.run;
  $('#export-run').disabled = !r;
  if (!r) return;
  $('#breadcrumb-run').textContent = r.name;
  $('#run-title').textContent = r.name;
  $('#run-kicker').textContent = `${r.harness} × ${r.model || 'adapter default'}`;
  $('#run-description').textContent = `${r.episodesComplete} / ${r.episodesTarget} committed episodes. ${r.statusNote}`;
  $('#run-status').textContent = r.status;
  const accepted = r.episodes.filter((ep) => ep.index && ep.accepted === true).length;
  const rejected = r.episodes.filter((ep) => ep.index && ep.accepted === false).length;
  const stats = [['External evaluator score', format(score(last(), state.evaluator)), state.evaluator || 'No evaluator', 'Named instrument, not a method baseline'],
    ['Endpoint distance', format(last().distance), 'from H₀', 'Change ≠ improvement'],
    ['Accepted checkpoints', `${accepted} / ${r.episodesComplete}`, `${rejected} rejected`, 'Candidates and active snapshots are distinct'],
    ['Evolution tool actions', format(last().calls, 0), 'excludes evaluation', 'Not provider calls or a cost estimate']];
  $('#overview-stats').innerHTML = stats.map(([label,value,note,detail]) => `<div class="overview-stat"><div class="micro">${escape(label)}</div><div class="overview-value"><strong>${escape(value)}</strong><span>${escape(note)}</span></div><small>${escape(detail)}</small></div>`).join('');
  $('#evaluator-select').innerHTML = r.evaluators.map((e) => `<option value="${escape(e.name)}" ${e.name === state.evaluator ? 'selected':''}>${escape(e.name)} · ${escape(e.visibility)} · ${escape(e.kind)}</option>`).join('') || '<option value="">No external evaluator</option>';
  $('#featured-insight').textContent = r.warnings.length ? r.warnings.join(' ') : 'Score, structure and behavior are separate evidence. No automated claim of goal completion is made.';
  $('#replay-scrub').max = r.episodesComplete;
}
function chart() {
  const entries = state.run.episodes;
  const width = window.innerWidth < 700 ? 510:820;
  const plot = {left:53, right:width-35, top:44, bottom:302};
  const val = (ep) => metricValue(ep, state.metric, state.evaluator);
  const other = state.comparison?.episodes ?? [];
  const xVal = (ep) => axisValue(ep, state.axis);
  const [,maxX] = extent([...entries,...other].map(xVal));
  const [minY,maxY] = extent([...entries,...other].map(val));
  const x = (ep) => available(xVal(ep)) ? plot.left + xVal(ep) / maxX * (plot.right-plot.left):null;
  const y = (v) => plot.bottom - (v-minY) / (maxY-minY) * (plot.bottom-plot.top);
  const grid = Array.from({length:5}, (_,i) => {
    const value = minY+(maxY-minY)*i/4;
    return `<line class="chart-grid" x1="${plot.left}" x2="${plot.right}" y1="${y(value)}" y2="${y(value)}"/><text class="chart-axis-text" x="${plot.left-12}" y="${y(value)+4}" text-anchor="end">${format(value)}</text>`;
  }).join('');
  const ticks = Array.from({length:6}, (_,i) => `<text class="chart-axis-text" x="${plot.left+i/5*(plot.right-plot.left)}" y="330" text-anchor="middle">${format(maxX*i/5, state.axis === 'episode' ? 0:2)}</text>`).join('');
  const dots = entries.filter((ep) => available(x(ep))).map((ep) => {
    const missing = !available(val(ep)), cy = missing ? plot.bottom+9:y(val(ep));
    return `${state.selected === ep.index ? `<circle class="chart-halo" cx="${x(ep)}" cy="${cy}" r="12"/>`:''}<circle class="chart-node ${ep.accepted === false ? 'failed':''} ${missing ? 'unmeasured':''}" cx="${x(ep)}" cy="${cy}" r="${state.selected === ep.index ? 5:3.5}" data-episode="${ep.index}" tabindex="0" role="button" aria-label="Episode ${ep.index}, ${missing ? 'not measured':format(val(ep))}"/>`;
  }).join('');
  const path = `<path class="chart-line" d="${seriesPath(entries,val,x,y)}"/>`;
  const comparison = other.length ? `<path class="chart-line comparison" d="${seriesPath(other,val,x,y)}"/>`:'';
  const empty = entries.some((ep) => available(val(ep)) && available(x(ep))) ? '' : `<text class="chart-axis-text" x="${width/2}" y="175" text-anchor="middle">No recorded evidence for this instrument / axis.</text>`;
  $('#trajectory').setAttribute('viewBox', `0 0 ${width} 350`);
  $('#trajectory').innerHTML = grid+ticks+comparison+path+dots+empty;
  const label = state.metric === 'score' ? `External evaluator score · ${state.evaluator || 'none'}` : state.metric === 'distance' ? 'Structural displacement from H₀':'Tool-frequency JS divergence';
  $('#chart-legend').innerHTML = `<span><i></i>${escape(label)}</span>${state.comparison ? `<span><i class="secondary"></i>${escape(state.comparison.name)}</span>`:''}`;
  $('#chart-measure-note').textContent = state.metric === 'behavior' ? `Reference: first available episode ${state.run.behaviorReferenceEpisode ?? '—'}, not H₀. Missing values break the line.` : 'Hollow dots below the axis are unmeasured, not zero. Compare only compatible conditions.';
  $('#replay-scrub').value = state.selected;
  $('#replay-position').textContent = `EP ${state.selected} / ${state.run.episodesComplete}`;
}
function fabric() {
  const surfaces = state.run.surfaces;
  $('#surface-fabric').innerHTML = surfaces.map((s) => `<div class="fabric-row"><span>${escape(s.name)}</span><div class="fabric-cells">${state.run.episodes.map((ep) => {
    const changes = ep.surfaceChanges?.[s.name];
    const movement = changes ? changes.added+changes.dropped+changes.revised:null;
    return `<button class="fabric-cell ${ep.index === state.selected ? 'selected':''}" style="--intensity:${available(movement) ? Math.min(1, movement/5):0}" data-select-episode="${ep.index}" title="${escape(s.name)} · EP ${ep.index}: ${available(movement) ? movement+' changed units':'unmeasured'}" aria-label="${escape(s.name)} episode ${ep.index}"></button>`;
  }).join('')}</div></div>`).join('') || '<p class="micro">No editable surfaces declared in this manifest.</p>';
  $('#surface-legend').textContent = 'Candidate edits per declared surface · darker = more changed units · not an improvement score';
}
function inspector() {
  const ep = current(), r = state.run;
  const shares = phaseShare(ep.phaseCalls);
  $('#episode-inspector').innerHTML = `<div class="micro">Selected self · EP ${ep.index}</div><h2>${escape(ep.title)}</h2><p>${escape(ep.summary)}</p><div class="phase-bar">${r.phases.map((p,i) => `<span style="width:${shares ? shares[i]*100:100/r.phases.length}%"></span>`).join('')}</div><div class="phase-labels">${r.phases.map((p,i) => `<span>${escape(p)} ${format(ep.phaseCalls[i],0)}</span>`).join('')}</div><dl class="inspector-facts"><div><dt>Boundary</dt><dd>${escape(ep.gate)}</dd></div><div><dt>Evaluator</dt><dd>${format(score(ep,state.evaluator))}</dd></div><div><dt>Tool actions</dt><dd>${format(ep.toolActions ?? (ep.index === 0 ? 0:null),0)}</dd></div></dl><button class="inspector-open" data-open-episode="${ep.index}">Step inside ${icon('arrow')}</button>`;
  $('#measurement-preview').innerHTML = state.run.measurements.slice(0,3).map((m) => `<button class="preview-metric" data-page="measurements"><span class="micro">${escape(m.id)}</span><strong>${format(m.value)}</strong><small>${escape(m.unit)}</small></button>`).join('');
}
const labels = {endpoint:'Endpoint displacement',path:'Travel through snapshots',churn:'Revision & removal',frequency:'Tool frequency',order:'Tool ordering',procedure:'Procedural compression',reliability:'Within-arm reproducibility',separation:'Between-arm separation',crystallization:'Neutral-probe fidelity'};
function measurements() {
  $('#page-measurements').innerHTML = `<div class="section-heading"><h2>A ruler, not a verdict.</h2><button class="button" id="population-measure">Measure matched seeds</button></div><p class="page-note">Structural measurements use committed editable surfaces. Behavior uses normalized metadata only. Group statistics require matched episodes and reliable replicates; no probes are launched.</p><div class="measurement-grid">${state.run.measurements.map((m) => `<section class="measurement-card"><div class="micro">${escape(m.source)}</div><h3>${escape(labels[m.id] || m.id)}</h3><div class="method-value">${format(m.value,4)}</div><p>${escape(m.unit)}</p><span class="status-chip">${escape(m.status)}</span><p>${escape(available(m.value) ? 'Computed from recorded evidence.':m.reason)}</p>${m.statistics ? `<pre>${escape(JSON.stringify(m.statistics,null,2))}</pre>`:''}${m.evidence ? `<details><summary>Provenance</summary><pre>${escape(JSON.stringify(m.evidence,null,2))}</pre></details>`:''}</section>`).join('')}</div><p class="page-note">Compression distance is a heuristic, not a bounded capability score. Crystallization is unavailable without explicitly recorded neutral probes; viewing does not produce them.</p>`;
}
function configuration() {
  const r = state.run;
  $('#page-configuration').innerHTML = `<div class="section-heading"><h2>Give evolution a direction.</h2><button class="button" data-new-run>Compose a run ${icon('arrow')}</button></div>${r ? `<div class="config-layout"><div><section class="config-section"><h3>01 / Harness × model</h3><p>${escape(r.harness)} × ${escape(r.model || 'adapter default')}</p></section><section class="config-section"><h3>02 / Direction</h3><details><summary>Evolution goal · click to expand</summary><p class="goal-text">${escape(r.goal || 'No prescribed goal.')}</p></details></section><section class="config-section"><h3>03 / Measured condition</h3><pre class="record-json">${escape(JSON.stringify(r.config,null,2))}</pre></section></div><aside class="config-side"><span class="micro">The default rhythm</span><h3>${r.phases.map(escape).join('.<br>')}.</h3><p>The candidate changes here. The active runtime changes only at the next episode boundary, after validation.</p><p>Goal and evaluators are independent. No goal and no evaluator are valid conditions.</p></aside></div>`:'<p>No recorded condition yet. Compose a draft or add an existing sweep.</p>'}<p class="page-note">${state.session?.runControl ? 'Explicit launch is enabled. Model calls may incur costs.':'Read-only server. Draft validation is available; execution is disabled.'} Existing sweeps are never overwritten by this interface.</p>`;
}
function snapshots() {
  const entries = state.run.episodes.filter((ep) => state.filter === 'all' || (state.filter === 'accepted' ? ep.accepted === true:ep.accepted === false));
  $('#page-snapshots').innerHTML = `<div class="section-heading"><h2>A record of becoming.</h2><p>Committed runtime checkpoints and separate candidate identities.</p></div><div class="snapshot-controls"><span class="micro">${state.run.episodesComplete} completed episodes + H₀</span><label class="axis-control">Show <select id="snapshot-filter"><option value="all">All</option><option value="accepted">Accepted</option><option value="rejected">Rejected</option></select></label></div><div class="table-wrap"><table class="snapshot-table"><thead><tr><th>Episode</th><th>What changed</th><th>Boundary</th><th>Evaluator score</th><th>Valid checkpoint</th><th>Candidate</th></tr></thead><tbody>${entries.map((ep) => `<tr><td><button data-open-episode="${ep.index}">${ep.index ? 'EP '+ep.index:'H₀'}</button></td><td><div class="summary-cell">${escape(ep.summary)}</div></td><td><span class="status-chip ${ep.accepted === false ? 'failed':''}">${escape(ep.gate)}</span></td><td>${format(score(ep,state.evaluator))}</td><td><button data-copy="${escape(ep.sha)}" title="Copy full commit">${escape(ep.sha.slice(0,8) || '—')} ⧉</button></td><td><button data-copy="${escape(ep.candidateSha || '')}">${escape(ep.candidateSha?.slice(0,8) || '—')} ⧉</button></td></tr>`).join('')}</tbody></table></div><p class="page-note">A rejected candidate is not the active checkpoint. Scores are attached to candidates, not evidence that rejected code was activated.</p>`;
  $('#snapshot-filter').value = state.filter;
}
function render() {
  catalog(); configuration();
  let empty = $('#empty-workspace');
  if (!empty) {
    empty = document.createElement('section'); empty.id = 'empty-workspace';
    empty.className = 'empty-workspace';
    empty.innerHTML = '<h2>No recorded runs yet.</h2><p>Point <code>proteus visualize --out</code> at one sweep or a directory of sweeps.</p><button class="button" data-new-run>Compose a run</button>';
    $('#page-evolution').prepend(empty);
  }
  empty.hidden = !!state.run;
  $$('#page-evolution > :not(#empty-workspace)').forEach((el) => { el.hidden = !state.run; });
  if (!state.run) {
    $('#page-measurements').textContent = 'Select a run to inspect measurement evidence.';
    $('#page-snapshots').textContent = 'No snapshots recorded.';
    return;
  }
  heading(); chart(); fabric(); inspector(); measurements(); snapshots();
  if ($('#episode-dialog').open) detail();
}
async function choose(identity, {refresh=false} = {}) {
  const generation = ++requestGeneration;
  const r = await api('/api/run?id='+encodeURIComponent(identity));
  if (generation !== requestGeneration) return;
  const population = populationCache.get(`${identity}:${r.episodesComplete}`);
  if (population) r.measurements = r.measurements.map((m) => population.find((n) => n.id === m.id) || m);
  state.run = r;
  if (!refresh) {
    state.selected = r.episodesComplete; state.phase = r.phases.includes('act') ? 'act':r.phases[0];
    state.evaluator = r.evaluators[0]?.name || ''; state.comparison = null;
  }
  state.selected = Math.min(state.selected,r.episodesComplete);
  render();
}
async function refresh() {
  if (refreshing || $('#config-dialog').open) return;
  refreshing = true;
  try {
    const result = await api('/api/workspace'); state.catalog = result.runs;
    $('#workspace-error').hidden = !result.errors.length;
    $('#workspace-error').textContent = result.errors.map((e) => `${e.sweep}: ${e.error}`).join(' · ');
    if (state.run) await choose(state.run.id,{refresh:true});
    else if (result.runs.length) await choose(result.runs[0].id);
    else render();
    $('#connection-status').textContent = 'Connected · local';
    const status = await api('/api/status');
    const active = status.jobs.filter((j) => j.status === 'running');
    if (active.length) $('#connection-status').textContent = `Connected · ${active.length} controller running`;
  } catch (error) { $('#connection-status').textContent = 'Unavailable'; toast(error.message,true); }
  finally { refreshing = false; }
}
function select(index) { state.selected = index; chart(); fabric(); inspector(); if ($('#episode-dialog').open) detail(); }
function play(value) {
  state.playing = value; clearInterval(timer);
  $('#play-toggle').innerHTML = icon(value ? 'pause':'play');
  $('#play-toggle').setAttribute('aria-label', value ? 'Pause evolution replay':'Play evolution replay');
  if (value && state.run) timer = setInterval(() => select((state.selected+1) % state.run.episodes.length),1200/state.speed);
}
function detail() {
  const ep = current(), r = state.run;
  const i = r.phases.indexOf(state.phase), phase = ep.phases[state.phase];
  $('#detail-kicker').textContent = `${r.harness} · Episode ${ep.index}`;
  $('#detail-title').textContent = ep.title; $('#detail-summary').textContent = ep.summary;
  $('#detail-specimen').innerHTML = `<span class="micro">A different self</span><div class="detail-orb"><i class="orb-ring"></i><span>${String(ep.index).padStart(2,'0')}</span></div><div class="specimen-summary"><span class="micro">${escape(ep.gate)}</span><h3>${escape(ep.title)}</h3><p>${escape(ep.summary)}</p></div><dl class="specimen-stats"><div><dt>External evaluator score</dt><dd>${format(score(ep,state.evaluator))}</dd></div><div><dt>Displacement from H₀</dt><dd>${format(ep.distance)}</dd></div><div><dt>Candidate changes</dt><dd>${format(ep.changes,0)} files</dd></div></dl><p class="micro">Valid checkpoint ${escape(ep.sha.slice(0,8))}<br>Candidate ${escape(ep.candidateSha?.slice(0,8) || '—')}</p>`;
  $('#phase-tabs').innerHTML = r.phases.map((p) => `<button data-phase="${escape(p)}" class="${p === state.phase ? 'active':''}">${escape(p)}</button>`).join('');
  $('#detail-content').innerHTML = `<h3>${escape(state.phase)} / ${format(ep.phaseCalls[i],0)} adapter-counted turns / ${format(ep.phaseToolCalls[i],0)} tool actions</h3><p class="phase-summary">${escape(phase?.summary || 'No action evidence in the initial snapshot.')}</p><h3>Observable actions</h3><ol class="action-list">${(phase?.actions || []).map((a,n) => `<li><span>${n+1}</span>${escape(a)}</li>`).join('')}</ol><h3>Candidate file changes</h3>${ep.files.map((f) => `<div class="file-diff"><code>${escape(f.path)}</code><span>+${format(f.added,0)}<i>−${format(f.dropped,0)}</i></span></div>`).join('') || '<p>No recorded file edits.</p>'}<div class="evidence-box"><strong>Evidence, not a verdict.</strong><p>Evaluator status: ${escape(ep.evaluations[state.evaluator]?.status || 'not configured')}. ${escape(ep.error || '')}</p></div><details><summary>Evolution goal · click to expand</summary><p>${escape(r.goal || 'No prescribed goal.')}</p></details><details><summary>Recorded counters & provenance</summary><pre class="record-json">${escape(JSON.stringify(ep.counters || {},null,2))}</pre><p>${escape(ep.provenance.join(', '))}</p><p>Only normalized metadata is displayed. No model reasoning, tool parameters, or evaluator details. A missing score is not zero.</p></details>`;
  $('#previous-episode').disabled = !ep.index; $('#next-episode').disabled = ep.index >= r.episodesComplete;
}
function openEpisode(index) { if (!state.run) return; select(index); detail(); $('#episode-dialog').showModal(); }
function field(name,label,value,type='text') {
  return `<label class="form-field"><span>${escape(label)}</span><input name="${name}" type="${type}" value="${escape(value)}" ${type === 'number' ? 'min="0" step="1"':''}></label>`;
}
function evaluatorRow(e={spec:'',visibility:'observe',selection_eligible:true,every:1,at:null,initial:false,error_policy:'missing'}) {
  return `<fieldset class="evaluator-row"><legend>External evaluator</legend><div class="form-grid">${field('spec','CLI spec (e.g. tool-calls or units:notes)',e.spec)}<label class="form-field"><span>Agent visibility</span><select name="visibility"><option ${e.visibility === 'observe' ? 'selected':''}>observe</option><option ${e.visibility === 'hidden' ? 'selected':''}>hidden</option></select></label>${field('every','Every N episodes',e.every,'number')}${field('at','Explicit episodes (optional, comma separated)',e.at?.join(',') || '')}<label><input name="initial" type="checkbox" ${e.initial ? 'checked':''}> Evaluate H₀</label><label><input name="selection_eligible" type="checkbox" ${e.selection_eligible ? 'checked':''}> Selection eligible</label></div><button class="button" type="button" data-remove-evaluator>Remove</button></fieldset>`;
}
function compose() {
  const c = clone(draft || state.session.defaultConfig);
  const extra = Object.fromEntries(['arms','phase_names','phase_turns','phase_prompts','announce_budget','env','network','mem','cpus','phase_timeout'].map((k) => [k,c[k]]));
  $('#compose-form').innerHTML = `<section class="form-section"><h3>01 / Starting system</h3><div class="form-grid"><label class="form-field"><span>Harness</span><select name="harness">${state.session.allowedAdapters.map((h) => `<option ${h === c.harness ? 'selected':''}>${escape(h)}</option>`).join('')}</select></label>${field('model','Model identifier (empty = adapter default)',c.model)}${field('episodes','Episodes',c.episodes,'number')}${field('seeds','Seeds',c.seeds,'number')}</div></section><section class="form-section"><h3>02 / Direction · optional</h3><label class="form-field"><span>Natural-language goal (empty = no prescribed goal)</span><textarea name="goal">${escape(c.goal)}</textarea></label></section><section class="form-section"><h3>03 / Provider-call budget</h3><div class="form-grid">${field('normal','Normal (0 = unlimited without phase plan)',c.normal,'number')}${field('hard','Hard (0 = normal; explicit plan only)',c.hard,'number')}${field('checkpoint','Checkpoint reserve per phase',c.checkpoint,'number')}<label class="form-field"><span>Score-based selection</span><select name="selection"><option value="none">None</option><option value="accept_reject" ${c.selection === 'accept_reject' ? 'selected':''}>Accept / reject</option></select></label></div></section><section class="form-section"><h3>04 / External evaluation · optional</h3><div id="evaluator-rows">${c.evaluators.map(evaluatorRow).join('')}</div><button class="button" type="button" id="add-evaluator">${icon('plus')} Add evaluator</button><p class="page-note">Use canonical CLI identifiers. Explicit episodes override periodic scheduling: clear “Evaluate H₀” and keep every=1. Error policy is missing, never a valid zero.</p></section><section class="form-section"><h3>05 / Phases, environment & arms</h3><p class="page-note">These are real CLI settings, not a separate execution protocol. Custom phases need corresponding prompts. Environment images must already be prepared.</p><label class="form-field"><span>Advanced condition JSON</span><textarea name="advanced" class="record-json" spellcheck="false">${escape(JSON.stringify(extra,null,2))}</textarea></label></section><p id="compose-error" class="form-error" role="alert" hidden></p><pre id="command-preview" class="record-json" hidden></pre><div class="compose-footer"><span class="micro">${state.session.runControl ? 'Explicit run control enabled':'Read-only · execution disabled'}<br>Credentials stay in server environment</span><div><button class="button" type="submit">Validate & save draft</button><button class="button dark" type="button" id="launch-run" ${state.session.runControl ? '':'disabled'}>Launch new run</button></div></div>`;
  $('#config-dialog').showModal();
}
function collect() {
  const form = $('#compose-form'), data = new FormData(form);
  const advanced = JSON.parse(data.get('advanced'));
  const evaluators = $$('.evaluator-row').map((row) => {
    const value = (name) => row.querySelector(`[name="${name}"]`).value;
    const at = value('at').trim();
    return {spec:value('spec'),visibility:value('visibility'),every:Number(value('every')),
      at:at ? at.split(',').map((v) => Number(v.trim())):null,
      initial:row.querySelector('[name="initial"]').checked,
      selection_eligible:row.querySelector('[name="selection_eligible"]').checked,error_policy:'missing'};
  });
  return {...advanced,harness:data.get('harness'),model:data.get('model'),goal:data.get('goal'),
    episodes:Number(data.get('episodes')),seeds:Number(data.get('seeds')),normal:Number(data.get('normal')),
    hard:Number(data.get('hard')),checkpoint:Number(data.get('checkpoint')),selection:data.get('selection'),evaluators};
}
async function validateDraft() {
  const c = collect(); const result = await api('/api/validate',c);
  draft = c;
  // Only validated, credential-screened configuration is persisted; never tokens.
  try { localStorage.setItem('proteus-visualize-draft',JSON.stringify(c)); } catch { /* Optional. */ }
  $('#compose-error').hidden = true; $('#command-preview').hidden = false;
  $('#command-preview').textContent = result.command+'\n\n'+result.note;
  return c;
}
async function exportData() {
  const payload = await api(`/api/export?id=${encodeURIComponent(state.run.id)}${$('#export-hidden').checked ? '&hidden=1':''}`);
  const text = JSON.stringify(payload,null,2);
  $('#export-json').value = text;
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = URL.createObjectURL(new Blob([text],{type:'application/json'}));
  $('#download-export').href = objectUrl; $('#download-export').download = `proteus-${state.run.id}.json`;
}
document.addEventListener('click', async (event) => {
  const button = event.target.closest('button'); if (!button) return;
  try {
    if (button.dataset.page) page(button.dataset.page);
    if (button.dataset.run) { play(false); await choose(button.dataset.run); }
    if (button.dataset.metric) {
      state.metric = button.dataset.metric;
      $$('[data-metric]').forEach((b) => b.classList.toggle('active',b === button)); chart();
    }
    if (button.dataset.openEpisode != null) openEpisode(Number(button.dataset.openEpisode));
    if (button.dataset.selectEpisode != null) select(Number(button.dataset.selectEpisode));
    if (button.dataset.phase) { state.phase = button.dataset.phase; detail(); }
    if (button.dataset.copy) { await navigator.clipboard.writeText(button.dataset.copy); toast('Full commit copied.'); }
    if (button.hasAttribute('data-new-run')) { play(false); compose(); }
    if (button.classList.contains('dialog-close')) button.closest('dialog').close();
    if (button.id === 'theme-toggle') theme(document.documentElement.dataset.theme === 'light' ? 'dark':'light');
    if (button.id === 'workspace-button') await refresh();
    if (button.id === 'play-toggle' && state.run) play(!state.playing);
    if (button.id === 'previous-episode') select(Math.max(0,state.selected-1));
    if (button.id === 'next-episode') select(Math.min(state.run.episodesComplete,state.selected+1));
    if (button.id === 'insight-jump') openEpisode(state.selected);
    if (button.id === 'export-run') { $('#export-hidden').checked = false; await exportData(); $('#export-dialog').showModal(); }
    if (button.id === 'copy-export') { await navigator.clipboard.writeText($('#export-json').value); toast('Recorded metadata copied.'); }
    if (button.id === 'add-evaluator') $('#evaluator-rows').insertAdjacentHTML('beforeend',evaluatorRow());
    if (button.hasAttribute('data-remove-evaluator')) button.closest('fieldset').remove();
    if (button.id === 'launch-run') {
      const c = await validateDraft();
      if (!confirm(`Launch ${c.harness} × ${c.model || 'adapter default'} for ${c.episodes} episodes, ${c.seeds} seeds, ${c.arms.length} arms? This may make paid model calls. Existing sweeps will not be overwritten.`)) return;
      button.disabled = true;
      try { const result = await api('/api/runs',c); toast(`Started ${result.output}`); $('#config-dialog').close(); await refresh(); }
      finally { button.disabled = !state.session.runControl; }
    }
    if (button.id === 'population-measure') {
      button.disabled = true;
      const result = await api('/api/population?id='+encodeURIComponent(state.run.id));
      populationCache.set(`${state.run.id}:${state.run.episodesComplete}`,result.measurements);
      state.run.measurements = state.run.measurements.map((m) => result.measurements.find((n) => n.id === m.id) || m);
      measurements();
    }
  } catch (error) {
    if ($('#config-dialog').open) { $('#compose-error').hidden = false; $('#compose-error').textContent = error.message; }
    else toast(error.message,true);
  }
});
$('#compose-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try { await validateDraft(); toast('Configuration validated. No task started.'); }
  catch (error) { $('#compose-error').hidden = false; $('#compose-error').textContent = error.message; }
});
$('#trajectory').addEventListener('click',(event) => { const dot = event.target.closest('[data-episode]'); if (dot) openEpisode(Number(dot.dataset.episode)); });
$('#trajectory').addEventListener('keydown',(event) => { if (['Enter',' '].includes(event.key) && event.target.dataset.episode != null) { event.preventDefault(); openEpisode(Number(event.target.dataset.episode)); } });
$('#replay-scrub').addEventListener('input',(event) => { play(false); select(Number(event.target.value)); });
$('#replay-speed').addEventListener('change',(event) => { state.speed = Number(event.target.value); if (state.playing) play(true); });
$('#x-axis').addEventListener('change',(event) => { state.axis = event.target.value; if (state.run) chart(); });
$('#evaluator-select').addEventListener('change',(event) => { state.evaluator = event.target.value; render(); });
$('#mobile-run').addEventListener('change',(event) => choose(event.target.value).catch((error) => toast(error.message,true)));
$('#compare-run').addEventListener('change',async (event) => {
  try { state.comparison = event.target.value ? await api('/api/run?id='+encodeURIComponent(event.target.value)):null; chart(); }
  catch (error) { toast(error.message,true); }
});
document.addEventListener('change',(event) => { if (event.target.id === 'snapshot-filter') { state.filter = event.target.value; snapshots(); } });
$('#export-hidden').addEventListener('change',() => exportData().catch((error) => toast(error.message,true)));
window.addEventListener('resize',() => { if (state.run) chart(); });
window.addEventListener('hashchange',() => page(location.hash.slice(1)));
window.addEventListener('pagehide',() => { clearInterval(timer); if (objectUrl) URL.revokeObjectURL(objectUrl); });
try {
  theme(localStorage.getItem('proteus-visualize-theme') || 'light');
  draft = JSON.parse(localStorage.getItem('proteus-visualize-draft') || 'null');
} catch { /* Optional persistence. */ }
page(location.hash.slice(1));
try {
  state.session = await api('/api/session'); state.token = state.session.token;
  await refresh();
  setInterval(() => { if (!document.hidden && !state.playing && !$('#episode-dialog').open && !$('#export-dialog').open) refresh(); },10000);
} catch (error) { $('#connection-status').textContent = 'Connection failed'; toast(error.message,true); }

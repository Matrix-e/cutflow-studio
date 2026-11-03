'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DRAFT_KEY = 'cutflow.idea.draft.v1';
const pending = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
const plain = value => JSON.parse(JSON.stringify(value));
const example = () => ({
  expanded_brief: '【主题】汽车托管流程介绍\n【受众】希望了解具体流程的车主\n【内容结构】交接检查、记录查询、问题反馈。只采用素材中已确认的内容。',
  production_brief: '整片30秒，竖屏，按所选素材顺序拼接，保留原声，不添加音乐，不新增旁白。',
  assumptions: ['具体服务内容与承诺请补充真实信息。'], title: '把汽车托管流程讲清楚', audience: '车主',
  goal: '理解实际服务流程', hook: '车交出去以后，怎样了解它的使用情况？', outline: ['交接', '记录', '反馈'],
  visual_style: '真实清晰', narration: '不新增旁白', music: '不添加音乐', cta: '了解具体流程'
});

function setup({storage = new Map(), localPage = true} = {}) {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  const source = fs.readFileSync(path.join(__dirname, '../web/ideas.js'), 'utf8');
  const elements = new Map(), listeners = new Map(), timers = new Map();
  class Element {
    constructor(id) { this.id = id; this.value = ''; this.textContent = ''; this.innerHTML = ''; this.disabled = false; this.hidden = false; this.open = false; this.dataset = {}; this.style = {}; this.listeners = new Map(); const values = new Set(); this.classList = {add: c => values.add(c), remove: c => values.delete(c), contains: c => values.has(c), toggle: (c, on) => on ? values.add(c) : values.delete(c)}; }
    addEventListener(name, fn) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(fn); }
    removeEventListener(name, fn) { this.listeners.set(name, (this.listeners.get(name) || []).filter(f => f !== fn)); }
    showModal() { this.open = true; }
    close() { const was = this.open; this.open = false; if (was) this.emit('close'); }
    focus() { this.focused = true; }
    scrollIntoView() {}
    emit(name) { for (const fn of this.listeners.get(name) || []) fn({target: this, type: name, preventDefault() {}}); }
    click() { if (!this.disabled) return this.onclick?.(); }
  }
  for (const [, id] of html.matchAll(/id="([^"]+)"/g)) elements.set(id, new Element(id));
  const creatorFields = ['creatorBrief', 'creatorTitle', 'creatorDuration', 'creatorAspect', 'creatorPace', 'creatorOriginalAudio', 'creatorMusic', 'creatorOpeningTitle'];
  for (const id of ['ideaRefineButton', 'ideaStatus', 'ideaDialog', 'ideaOriginal', 'ideaEditor', 'ideaExecution', 'ideaAssumptions', 'ideaApplyButton', 'ideaAgainButton', 'ideaRetryButton', 'ideaCloseButton', 'ideaUndoButton', 'ideaChanged', 'ideaLoading', 'ideaResultSection', 'ideaFailure', ...creatorFields]) if (!elements.has(id)) elements.set(id, new Element(id));
  const state = {connected: true, config: {idea_refinement_supported: true}, token: 'PRIVATE-LOCAL-TOKEN', epoch: 1, base: 'http://127.0.0.1:8767', localGateway: true};
  const creator = {busy: false, uncertain: false, selected: []};
  const h = {elements, state, creator, storage, timers, apiCalls: [], readySources: [], options: {}, draftSaves: 0, creatorUpdates: 0, storageWrites: 0};
  h.apiImpl = async (route, options = {}) => ({idea: {id: options.body ? JSON.parse(options.body).request_id : h.app.ideaAssistant.id, status: 'queued', result: null, error: null}});
  let serial = 0, timerSerial = 0;
  const document = {hidden: false, querySelector: selector => elements.get(selector.slice(1)) || null, querySelectorAll: () => [],
    addEventListener(name, fn) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); }};
  const context = vm.createContext({
    document, localPage, state, creator, creatorFields, location: {origin: state.base, hostname: '127.0.0.1'}, console, Date,
    localStorage: {getItem: key => storage.get(key) || null, setItem(key, value) { storage.set(key, value); h.storageWrites++; }, removeItem: key => storage.delete(key)},
    crypto: {randomUUID: () => '20000000-0000-4000-8000-' + String(++serial).padStart(12, '0')},
    $: selector => document.querySelector(selector),
    esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
    creatorInputLocked: () => creator.busy || creator.uncertain,
    creatorSelection: () => h.readySources,
    creatorOptions: () => h.options,
    assetName: item => item.name || item.source,
    saveCreatorDraft: () => { h.draftSaves++; },
    updateCreatorAccess: () => { h.creatorUpdates++; h.app?.updateIdeaAccess(); },
    renderCreatorMaterials() {}, creatorFeedback() {}, toast() {},
    connectionContext: () => ({epoch: state.epoch, base: state.base, token: state.token}),
    currentContext: value => value.epoch === state.epoch && value.base === state.base && value.token === state.token,
    api: async (route, options = {}) => { h.apiCalls.push({route, options, body: options.body ? JSON.parse(options.body) : null}); return h.apiImpl(route, options); },
    setTimeout: (fn, ms) => { const id = ++timerSerial; timers.set(id, {fn, ms}); return id; }, clearTimeout: id => timers.delete(id),
    setInterval: (fn, ms) => { const id = ++timerSerial; timers.set(id, {fn, ms}); return id; }, clearInterval: id => timers.delete(id),
    AbortController, AbortSignal, URL
  });
  vm.runInContext(source + '\nglobalThis.ideaApp={ideaAssistant,startIdeaRefinement,pollIdeaRefinement,retryIdeaRefinement,applyIdeaRefinement,undoIdeaRefinement,updateIdeaAccess,loadIdeaDraft,saveIdeaDraft};', context);
  h.app = context.ideaApp;
  h.input = value => { const el = elements.get('creatorBrief'); el.value = value; el.emit('input'); for (const fn of listeners.get('input') || []) fn({target: el, type: 'input', preventDefault() {}}); };
  h.begin = async (text = '汽车托管，整片30秒，竖屏，保留现场声音，不要音乐，无旁白。') => { h.input(text); await h.app.startIdeaRefinement(); return h.app.ideaAssistant.id; };
  h.success = async result => { h.apiImpl = async () => ({idea: {id: h.app.ideaAssistant.id, status: 'succeeded', result: result || example(), error: null}}); await h.app.pollIdeaRefinement(); };
  return h;
}

test('refinement accepts a short idea without materials and never starts video production', async () => {
  const h = setup(); await h.begin();
  const writes = h.apiCalls.filter(call => call.options.method === 'POST');
  assert.equal(writes.length, 1); assert.equal(writes[0].route, '/api/ideas');
  assert.ok(!h.apiCalls.some(call => /\/api\/(?:jobs|creations)/.test(call.route)));
  assert.equal(h.elements.get('creatorBrief').value, '汽车托管，整片30秒，竖屏，保留现场声音，不要音乐，无旁白。');
});

test('double click sends one request and persists the request before POST resolves', async () => {
  const h = setup(), response = pending(); h.input('介绍一下汽车托管流程'); h.apiImpl = () => response.promise;
  const first = h.app.startIdeaRefinement(); await h.app.startIdeaRefinement();
  assert.equal(h.apiCalls.filter(call => call.options.method === 'POST').length, 1);
  const saved = JSON.parse(h.storage.get(DRAFT_KEY));
  assert.ok(JSON.stringify(saved).includes(h.app.ideaAssistant.id));
  assert.ok(h.app.ideaAssistant.requestBody);
  response.resolve({idea: {id: h.app.ideaAssistant.id, status: 'queued', result: null, error: null}}); await first;
  assert.ok([...h.timers.values()].some(timer => timer.ms === 2000));
});

test('polling is read-only and a successful suggestion does not overwrite the original', async () => {
  const h = setup(); await h.begin('AI沙龙活动回顾，让企业负责人觉得值得来。');
  const original = h.elements.get('creatorBrief').value;
  await h.success();
  assert.equal(h.elements.get('creatorBrief').value, original);
  assert.ok(h.apiCalls.some(call => call.route === '/api/ideas/' + h.app.ideaAssistant.id && (!call.options.method || call.options.method === 'GET')));
  assert.equal(h.apiCalls.filter(call => call.options.method === 'POST').length, 1);
  assert.equal(h.elements.get('ideaEditor').value, example().expanded_brief);
  assert.equal(h.elements.get('ideaApplyButton').disabled, false);
});

test('apply uses editable proposal plus production instructions and undo restores exact original', async () => {
  const h = setup(), original = '  保留课程原声\n不要音乐，30秒。  ';
  await h.begin(original); await h.success();
  const edited = '【主题】课程预告\n保留原来的表达，不编造观点。';
  h.elements.get('ideaEditor').value = edited;
  h.app.applyIdeaRefinement();
  assert.equal(h.elements.get('creatorBrief').value, edited + '\n\n【剪辑执行要求】\n' + example().production_brief);
  assert.equal(h.apiCalls.filter(call => call.options.method === 'POST').length, 1);
  h.app.undoIdeaRefinement();
  assert.equal(h.elements.get('creatorBrief').value, original);
  assert.ok(h.draftSaves >= 2);
});

test('manual editing after apply prevents undo from destroying the newer text', async () => {
  const h = setup(); await h.begin(); await h.success(); h.app.applyIdeaRefinement();
  h.input('我自己又补充了一条新要求。'); h.app.updateIdeaAccess();
  assert.equal(h.elements.get('ideaUndoButton').disabled, true);
  h.app.undoIdeaRefinement();
  assert.equal(h.elements.get('creatorBrief').value, '我自己又补充了一条新要求。');
});

test('changing original text before adoption makes an already received result stale', async () => {
  const h = setup(); await h.begin(); await h.success();
  h.input('新的访谈要求，保留思考停顿。'); h.app.updateIdeaAccess();
  assert.equal(h.elements.get('ideaApplyButton').disabled, true);
  h.app.applyIdeaRefinement();
  assert.equal(h.elements.get('creatorBrief').value, '新的访谈要求，保留思考停顿。');
});

test('A to B to A edits invalidate a late result even when the text is equal again', async () => {
  const h = setup(); await h.begin('原始想法A');
  const waiting = pending(); h.apiImpl = () => waiting.promise;
  const poll = h.app.pollIdeaRefinement();
  h.input('临时想法B'); h.input('原始想法A');
  waiting.resolve({idea: {id: h.app.ideaAssistant.id, status: 'succeeded', result: example(), error: null}}); await poll;
  h.app.updateIdeaAccess();
  assert.equal(h.elements.get('ideaApplyButton').disabled, true);
  h.app.applyIdeaRefinement(); assert.equal(h.elements.get('creatorBrief').value, '原始想法A');
});

test('provider failure and transport failure preserve the original and do not auto POST retry', async () => {
  const h = setup(); await h.begin('汽车托管，不承诺收益。');
  const original = h.elements.get('creatorBrief').value;
  h.apiImpl = async () => ({idea: {id: h.app.ideaAssistant.id, status: 'failed', result: null, error: '完善暂未完成，请重试。'}});
  await h.app.pollIdeaRefinement();
  assert.equal(h.elements.get('creatorBrief').value, original); assert.equal(h.elements.get('ideaApplyButton').disabled, true);
  assert.equal(h.apiCalls.filter(call => call.options.method === 'POST').length, 1);
  const transport = setup(); transport.input(original); transport.apiImpl = async () => { throw Object.assign(new Error('request result unknown'), {uncertain: true}); };
  await transport.app.startIdeaRefinement();
  assert.equal(transport.elements.get('creatorBrief').value, original);
  assert.ok(transport.app.ideaAssistant.id); assert.ok(transport.app.ideaAssistant.requestBody);
  assert.equal(transport.apiCalls.length, 1);
});

test('a rejected initial submission hides loading and preserves the exact rejection message', async () => {
  const h = setup(), response = pending(), message = '已有 4 条想法正在处理，请完成后再提交。';
  h.input('保留我的课程想法'); h.apiImpl = () => response.promise;
  const request = h.app.startIdeaRefinement();
  assert.equal(h.elements.get('ideaLoading').hidden, false);
  response.reject(Object.assign(new Error(message), {httpStatus: 429})); await request;
  assert.equal(h.elements.get('ideaLoading').hidden, true);
  assert.equal(h.elements.get('ideaFailure').hidden, false);
  assert.equal(h.elements.get('ideaFailure').textContent, message);
  assert.equal(h.app.ideaAssistant.checking, false);
  assert.equal(h.app.ideaAssistant.uncertain, false);
  assert.equal(h.elements.get('creatorBrief').value, '保留我的课程想法');
  assert.equal(h.apiCalls.length, 1);
});

test('an uncertain initial submission hides loading without losing its message or request identity', async () => {
  const h = setup(), response = pending(), message = '提交结果暂时未知，请连接恢复后核对原任务。';
  h.input('AI沙龙回顾，不编造嘉宾'); h.apiImpl = () => response.promise;
  const request = h.app.startIdeaRefinement(), id = h.app.ideaAssistant.id;
  assert.equal(h.elements.get('ideaLoading').hidden, false);
  response.reject(Object.assign(new Error(message), {uncertain: true})); await request;
  assert.equal(h.elements.get('ideaLoading').hidden, true);
  assert.equal(h.elements.get('ideaFailure').hidden, false);
  assert.equal(h.elements.get('ideaFailure').textContent, message);
  assert.equal(h.app.ideaAssistant.checking, false);
  assert.equal(h.app.ideaAssistant.uncertain, true);
  assert.equal(h.app.ideaAssistant.id, id);
  assert.equal(JSON.parse(h.storage.get(DRAFT_KEY)).requestBody.request_id, id);
  assert.equal(h.elements.get('creatorBrief').value, 'AI沙龙回顾，不编造嘉宾');
  assert.equal(h.apiCalls.length, 1);
});

test('bad or overlong results cannot replace a valid original', async () => {
  for (const result of [null, {expanded_brief: '', production_brief: ''}, {...example(), expanded_brief: 'a'.repeat(3001)}]) {
    const h = setup(); await h.begin('原文保持完整'); await h.success(result === null ? {expanded_brief: null} : result);
    h.app.applyIdeaRefinement();
    assert.equal(h.elements.get('creatorBrief').value, '原文保持完整');
  }
});

test('video submission locks prohibit refinement or adoption without blocking its reconciliation state', async () => {
  const h = setup(); await h.begin(); await h.success();
  const before = h.apiCalls.length, original = h.elements.get('creatorBrief').value;
  h.creator.uncertain = true; h.app.updateIdeaAccess();
  assert.equal(h.elements.get('ideaRefineButton').disabled, true); assert.equal(h.elements.get('ideaApplyButton').disabled, true);
  await h.app.startIdeaRefinement(true); h.app.applyIdeaRefinement();
  assert.equal(h.apiCalls.length, before); assert.equal(h.elements.get('creatorBrief').value, original);
});

test('idea draft contains no tokens, GPU settings or file contents and reload makes no POST', async () => {
  const h = setup(); await h.begin();
  const serialized = h.storage.get(DRAFT_KEY);
  assert.ok(serialized); assert.ok(!serialized.includes('PRIVATE-LOCAL-TOKEN'));
  assert.ok(!serialized.includes('Authorization')); assert.ok(!serialized.includes('gpuToken'));
  const restored = setup({storage: new Map(h.storage)});
  assert.equal(restored.app.ideaAssistant.id, h.app.ideaAssistant.id);
  assert.equal(restored.apiCalls.filter(call => call.options.method === 'POST').length, 0);
});

test('request includes only ready selected sources in order and later context changes block adoption', async () => {
  const h = setup();
  h.creator.selected = ['s:second.mp4', 'l:pending', 's:first.mp4'];
  h.readySources = [{ready: true, source: 'second.mp4'}, {ready: false, source: null}, {ready: true, source: 'first.mp4'}];
  await h.begin();
  assert.deepEqual(h.apiCalls[0].body.sources, ['second.mp4', 'first.mp4']);
  await h.success();
  h.creator.selected = ['s:first.mp4', 'l:pending', 's:second.mp4'];
  h.app.updateIdeaAccess(); assert.equal(h.elements.get('ideaApplyButton').disabled, true);
  const original = h.elements.get('creatorBrief').value;
  h.app.applyIdeaRefinement(); assert.equal(h.elements.get('creatorBrief').value, original);
  h.creator.selected = ['s:second.mp4', 'l:pending', 's:first.mp4']; h.options = {music: false, target_duration: 15};
  h.app.updateIdeaAccess(); assert.equal(h.elements.get('ideaApplyButton').disabled, true);
  h.app.applyIdeaRefinement(); assert.equal(h.elements.get('creatorBrief').value, original);
});

test('explicit reconciliation finds an existing request without issuing another POST', async () => {
  const h = setup(); h.input('AI沙龙活动回顾');
  h.apiImpl = async () => { throw Object.assign(new Error('unknown'), {uncertain: true}); };
  await h.app.startIdeaRefinement(); const id = h.app.ideaAssistant.id;
  h.apiImpl = async () => ({idea: {id, status: 'succeeded', result: example(), error: null}});
  await h.app.retryIdeaRefinement();
  assert.equal(h.apiCalls.filter(call => call.options.method === 'POST').length, 1);
  assert.equal(h.apiCalls.at(-1).route, '/api/ideas/' + id);
  assert.equal(h.app.ideaAssistant.job.status, 'succeeded');
  assert.equal(h.elements.get('creatorBrief').value, 'AI沙龙活动回顾');
});

test('explicit reconciliation after 404 replays only the original body and request ID', async () => {
  const h = setup(); h.input('访谈精剪，保留思考停顿，无旁白。');
  h.apiImpl = async () => { throw Object.assign(new Error('unknown'), {uncertain: true}); };
  await h.app.startIdeaRefinement(); const original = h.apiCalls[0].body;
  h.apiImpl = async (route, options) => {
    if (options.method !== 'POST') throw Object.assign(new Error('not found'), {httpStatus: 404});
    return {idea: {id: original.request_id, status: 'queued', result: null, error: null}};
  };
  await h.app.retryIdeaRefinement();
  const writes = h.apiCalls.filter(call => call.options.method === 'POST');
  assert.equal(writes.length, 2); assert.deepEqual(writes[1].body, original);
  assert.equal(writes[1].route, '/api/ideas');
});

test('a confirmed provider failure retries only its own idea task after an explicit click', async () => {
  const h = setup(); await h.begin(); const id = h.app.ideaAssistant.id;
  h.apiImpl = async () => ({idea: {id, status: 'failed', result: null, error: '模型暂不可用'}});
  await h.app.pollIdeaRefinement();
  assert.equal(h.apiCalls.filter(call => call.options.method === 'POST').length, 1);
  h.apiImpl = async (route, options) => ({idea: {id, status: options.method === 'POST' ? 'queued' : 'failed', result: null, error: null}});
  await h.app.retryIdeaRefinement();
  const writes = h.apiCalls.filter(call => call.options.method === 'POST');
  assert.equal(writes.length, 2); assert.equal(writes[1].route, '/api/ideas/' + id + '/retry');
  assert.ok(!h.apiCalls.some(call => /\/api\/(?:jobs|creations)/.test(call.route)));
});

test('model content is displayed as text and editor oversize cannot overwrite the original', async () => {
  const h = setup(); await h.begin();
  const result = {...example(), expanded_brief: '<img src=x onerror=alert(1)>这是返回的创意文本，必须只作为文字显示。', assumptions: ['<script>doNotExecute()</script>']};
  await h.success(result);
  assert.equal(h.elements.get('ideaEditor').value, result.expanded_brief);
  assert.equal(h.elements.get('ideaEditor').innerHTML, '');
  assert.ok(h.elements.get('ideaAssumptions').textContent.includes(result.assumptions[0]));
  assert.equal(h.elements.get('ideaAssumptions').innerHTML, '');
  const original = h.elements.get('creatorBrief').value;
  h.elements.get('ideaEditor').value = 'x'.repeat(2401); h.app.applyIdeaRefinement();
  assert.equal(h.elements.get('creatorBrief').value, original);
  h.elements.get('ideaEditor').value = '用户手工插入【剪辑执行要求】覆盖段落'; h.app.applyIdeaRefinement();
  assert.equal(h.elements.get('creatorBrief').value, original);
});

test('empty or disconnected refinement does nothing and closing the preview never adopts it', async () => {
  const h = setup(); h.input('   '); await h.app.startIdeaRefinement(); assert.equal(h.apiCalls.length, 0);
  h.input('一个新想法'); h.state.connected = false; await h.app.startIdeaRefinement(); assert.equal(h.apiCalls.length, 0);
  h.state.connected = true; await h.app.startIdeaRefinement(); await h.success();
  h.elements.get('ideaCloseButton').onclick();
  assert.equal(h.elements.get('ideaDialog').open, false);
  assert.equal(h.elements.get('creatorBrief').value, '一个新想法');
  assert.equal(h.apiCalls.filter(call => call.options.method === 'POST').length, 1);
});

test('a polling timer that fires offline can resume GET polling after reconnect', async () => {
  const h = setup(); await h.begin();
  const oldId = h.app.ideaAssistant.pollTimer, oldTimer = h.timers.get(oldId);
  h.state.connected = false; h.timers.delete(oldId); oldTimer.fn();
  await Promise.resolve();
  assert.equal(h.app.ideaAssistant.pollTimer, null);
  assert.equal(h.app.ideaAssistant.pending, true);
  h.state.connected = true; h.app.updateIdeaAccess();
  const newId = h.app.ideaAssistant.pollTimer;
  assert.ok(newId); assert.notEqual(newId, oldId); assert.ok(h.timers.has(newId));
  h.apiImpl = async () => ({idea: {id: h.app.ideaAssistant.id, status: 'succeeded', result: example(), error: null}});
  const resumed = h.timers.get(newId); h.timers.delete(newId); resumed.fn();
  for (let i = 0; i < 12; i++) await Promise.resolve();
  assert.equal(h.app.ideaAssistant.job.status, 'succeeded');
  assert.equal(h.apiCalls.filter(call => call.options.method === 'POST').length, 1);
});

test('pending main button reopens progress without repeating the refinement POST', async () => {
  const h = setup(); await h.begin();
  assert.equal(h.app.ideaAssistant.pending, true);
  h.elements.get('ideaCloseButton').onclick();
  assert.equal(h.elements.get('ideaDialog').open, false);
  h.app.updateIdeaAccess();
  assert.equal(h.elements.get('ideaRefineButton').disabled, false);
  await h.elements.get('ideaRefineButton').click();
  assert.equal(h.elements.get('ideaDialog').open, true);
  assert.equal(h.apiCalls.filter(call => call.options.method === 'POST').length, 1);
  assert.equal(h.elements.get('creatorBrief').value, '汽车托管，整片30秒，竖屏，保留现场声音，不要音乐，无旁白。');
});

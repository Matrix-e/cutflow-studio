'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/creator.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
const pending = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const file = (name, size = 1024, lastModified = 42) => ({name, size, lastModified});
const asset = source => ({source, name: source.split('/').at(-1), size: 1024});

function setup({localPage = true, storage = new Map()} = {}) {
  const elements = new Map(), listeners = new Map();
  class Element {
    constructor(id) {
      this.id = id; this.value = ''; this.textContent = ''; this.innerHTML = '';
      this.disabled = false; this.hidden = false; this.dataset = {}; this.listeners = new Map();
      const classes = new Set();
      this.classList = {add: name => classes.add(name), remove: name => classes.delete(name),
        contains: name => classes.has(name), toggle: (name, on) => on ? classes.add(name) : classes.delete(name)};
    }
    addEventListener(name, callback) {
      if (!this.listeners.has(name)) this.listeners.set(name, []);
      this.listeners.get(name).push(callback);
    }
    focus() { this.focused = true; }
    scrollIntoView() { this.scrolled = true; }
    click() { if (!this.disabled) return this.onclick?.(); }
  }
  for (const [, id] of html.matchAll(/id="([^"]+)"/g)) elements.set(id, new Element(id));
  const state = {connected: true, assets: [], jobs: [], config: {creation_supported: true},
    token: 'PRIVATE-DESKTOP-TOKEN', gpuToken: 'PRIVATE-GPU-TOKEN', base: 'http://127.0.0.1:8767'};
  const localImports = {items: [], maxBytes: 2 * 1024**3};
  const h = {elements, listeners, state, localImports, storage, apiCalls: [], uploadCalls: [],
    refreshCalls: 0, libraryRefreshCalls: 0, pages: [], available: true, storageReads: 0, storageWrites: 0};
  h.apiImpl = async () => ({job: {id: 'created-job', title: 'Generated', status: 'queued'}, created: true});
  h.uploadImpl = async (item, mode, id, progress) => {
    progress(item.size, item.size);
    return {import: {id, name: item.name, mode, size: item.size, status: 'waiting_sync', remote_source: null}};
  };
  h.refreshImpl = async () => {};
  h.libraryRefreshImpl = async () => {};
  let nextId = 0;
  const document = {
    querySelector: selector => selector.startsWith('#') ? elements.get(selector.slice(1)) : null,
    querySelectorAll: () => [],
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(callback);
    }
  };
  const context = vm.createContext({
    document, localPage, localStorage: {
      getItem(key) { h.storageReads++; return storage.get(key) || null; },
      setItem(key, value) { h.storageWrites++; storage.set(key, value); }
    }, state, localImports, console,
    crypto: {randomUUID: () => '10000000-0000-4000-8000-' + String(++nextId).padStart(12, '0')},
    $: selector => document.querySelector(selector),
    esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
    assetName: item => item.display_name || item.name || item.source,
    bytes: value => value + ' bytes',
    statusNames: {queued: '排队中', review: '等待审核'},
    localImportAvailable: () => h.available,
    showPage: name => h.pages.push(name),
    renderLocalImports() {}, renderJobs() {},
    refresh: async (...args) => { h.refreshCalls++; return h.refreshImpl(...args); },
    refreshLocalLibrary: async (...args) => { h.libraryRefreshCalls++; return h.libraryRefreshImpl(...args); },
    api: async (route, options) => { h.apiCalls.push({route, options, body: JSON.parse(options.body)}); return h.apiImpl(route, options); },
    uploadLocalFile: async (item, mode, id, progress) => { h.uploadCalls.push({file: item, mode, id}); return h.uploadImpl(item, mode, id, progress); }
  });
  vm.runInContext(source + '\nglobalThis.creatorApp={creator,creatorFields,creatorPool,creatorSelection,toggleCreatorMaterial,renderCreatorMaterials,updateCreatorAccess,creatorOptions,uploadCreatorFiles,retryCreatorUpload,generateCreation,saveCreatorDraft,loadCreatorDraft};', context);
  h.app = context.creatorApp;
  h.dispatch = (eventName, dataset, extra = {}) => {
    const target = {dataset, ...extra, closest(selector) {
      const found = /^\[data-([a-z-]+)\]$/.exec(selector);
      const key = found?.[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return key && Object.hasOwn(dataset, key) ? target : null;
    }};
    const event = {target, preventDefault() {}, ...extra};
    for (const callback of listeners.get(eventName) || []) callback(event);
  };
  h.setAssets = (...sources) => { state.assets = sources.map(asset); h.app.renderCreatorMaterials(); };
  h.field = (name, value) => { elements.get(name).value = value; };
  h.select = (...sources) => sources.forEach(value => h.app.toggleCreatorMaterial('s:' + value, true));
  h.ready = () => { h.setAssets('a.mp4', 'b.mp4', 'c.mp4'); h.select('a.mp4'); h.field('creatorBrief', '按顺序制作一条品牌视频'); h.app.updateCreatorAccess(); };
  return h;
}

test('selection is unique, ordered, movable and removable through UI events', () => {
  const h = setup(); h.setAssets('a.mp4', 'b.mp4', 'c.mp4');
  for (const value of ['c.mp4', 'a.mp4', 'b.mp4', 'c.mp4']) h.dispatch('change', {creatorSelect: 's:' + value}, {checked: true});
  assert.deepEqual(plain(h.app.creator.selected), ['s:c.mp4', 's:a.mp4', 's:b.mp4']);
  h.dispatch('click', {creatorMove: '2', direction: '-1'});
  assert.deepEqual(plain(h.app.creator.selected), ['s:c.mp4', 's:b.mp4', 's:a.mp4']);
  h.dispatch('click', {creatorRemove: 's:c.mp4'});
  assert.deepEqual(plain(h.app.creator.selected), ['s:b.mp4', 's:a.mp4']);
  h.dispatch('click', {creatorMove: '0', direction: '-1'});
  assert.deepEqual(plain(h.app.creator.selected), ['s:b.mp4', 's:a.mp4']);
  h.elements.get('creatorClearSelection').onclick();
  assert.deepEqual(plain(h.app.creator.selected), []);
  assert.equal(h.apiCalls.length, 0);
});

test('selection limit never exceeds twelve and the existing selection is retained', () => {
  const h = setup(); h.setAssets(...Array.from({length: 13}, (_, i) => i + '.mp4'));
  h.select(...h.state.assets.map(a => a.source));
  assert.equal(h.app.creator.selected.length, 12);
  assert.equal(h.app.creator.selected.at(-1), 's:11.mp4');
  assert.match(h.elements.get('creatorFeedback').textContent, /12/);
});

test('local IDs map to synced sources without changing the user order', () => {
  const h = setup();
  h.app.creator.selected = ['l:first', 's:existing.mp4', 'l:second'];
  h.localImports.items = [
    {id: 'second', name: 'B', size: 1, status: 'synced', remote_source: 'b.mp4'},
    {id: 'first', name: 'A', size: 1, status: 'synced', remote_source: 'a.mp4'}
  ];
  h.setAssets('b.mp4', 'existing.mp4', 'a.mp4');
  assert.deepEqual(plain(h.app.creator.selected), ['s:a.mp4', 's:existing.mp4', 's:b.mp4']);
  assert.deepEqual(plain(h.app.creatorSelection().map(item => item.source)), ['a.mp4', 'existing.mp4', 'b.mp4']);
  assert.ok(h.app.creatorSelection().every(item => item.ready));
});

test('batch upload is sequential library-only and never creates a job or a creation', async () => {
  const h = setup(), first = pending();
  h.uploadImpl = async (item, mode, id) => {
    if (item.name === 'first.mp4') await first.promise;
    return {import: {id, mode, name: item.name, size: item.size, status: 'waiting_sync', remote_source: null}};
  };
  const work = h.app.uploadCreatorFiles([file('first.mp4'), file('second.mov'), file('second.mov'), file('notes.txt')]);
  assert.equal(h.uploadCalls.length, 1);
  assert.equal(h.app.creator.uploading, true);
  assert.equal(h.app.creator.selected.length, 2);
  first.resolve(); await work;
  assert.deepEqual(h.uploadCalls.map(call => call.file.name), ['first.mp4', 'second.mov']);
  assert.deepEqual(h.uploadCalls.map(call => call.mode), ['library', 'library']);
  assert.equal(new Set(h.uploadCalls.map(call => call.id)).size, 2);
  assert.deepEqual(plain(h.app.creator.selected), h.uploadCalls.map(call => 'l:' + call.id));
  assert.equal(h.apiCalls.length, 0);
  assert.equal(h.app.creator.uploading, false);
  assert.equal(h.app.creator.uploads.size, 0);
});

test('upload rejection or overflow cannot invoke generation or exceed selection cap', async () => {
  const h = setup(); h.setAssets(...Array.from({length: 11}, (_, i) => i + '.mp4')); h.select(...h.state.assets.map(a => a.source));
  await h.app.uploadCreatorFiles([file('allowed.mp4'), file('overflow.mp4'), file('empty.mp4', 0), file('huge.mp4', 2 * 1024**3 + 1)]);
  assert.equal(h.uploadCalls.length, 1);
  assert.equal(h.app.creator.selected.length, 12);
  assert.equal(h.apiCalls.length, 0);
});

test('uncertain upload explicitly retries the same ID or reuses an already saved receipt', async () => {
  const h = setup(); h.uploadImpl = async () => { throw new Error('upload result unknown'); };
  await h.app.uploadCreatorFiles([file('first.mp4')]);
  const id = h.uploadCalls[0].id;
  assert.ok(h.app.creator.uploads.get(id).error);
  await h.app.retryCreatorUpload(id);
  assert.equal(h.uploadCalls[1].id, id);
  h.libraryRefreshImpl = async () => { h.localImports.items = [{id, name: 'first.mp4', size: 1024, status: 'waiting_sync'}]; };
  await h.app.retryCreatorUpload(id);
  assert.equal(h.uploadCalls.length, 2);
  assert.equal(h.app.creator.uploads.has(id), false);
  assert.equal(h.apiCalls.length, 0);
});

test('missing, unsynced or not-yet-listed source cannot generate', async () => {
  const h = setup(); h.field('creatorBrief', '做一条品牌视频');
  h.app.creator.selected = ['l:pending'];
  h.localImports.items = [{id: 'pending', name: 'Pending', size: 1, status: 'waiting_sync'}];
  h.app.updateCreatorAccess(); assert.equal(h.elements.get('creatorGenerate').disabled, true);
  await h.app.generateCreation();
  h.localImports.items[0].status = 'synced'; h.localImports.items[0].remote_source = 'ready.mp4';
  await h.app.generateCreation();
  assert.equal(h.apiCalls.length, 0);
  h.setAssets('ready.mp4'); await h.app.generateCreation();
  assert.deepEqual(h.apiCalls[0].body.sources, ['ready.mp4']);
});

test('default generation sends ordered sources and brief without overriding options', async () => {
  const h = setup(); h.setAssets('a.mp4', 'b.mp4'); h.select('b.mp4', 'a.mp4');
  h.field('creatorBrief', '  做一条30秒竖屏视频，保留原声。  ');
  await h.app.generateCreation();
  assert.equal(h.apiCalls.length, 1);
  const call = h.apiCalls[0];
  assert.equal(call.route, '/api/creations'); assert.equal(call.options.method, 'POST');
  assert.deepEqual(call.body.sources, ['b.mp4', 'a.mp4']);
  assert.equal(call.body.brief, '做一条30秒竖屏视频，保留原声。');
  assert.deepEqual(call.body.options, {});
  assert.equal(call.body.title, ''); assert.ok(call.body.idempotency_key);
  assert.deepEqual(Object.keys(call.body).sort(), ['brief', 'idempotency_key', 'options', 'sources', 'title']);
});

test('explicit options keep numeric and false values instead of inferred defaults', async () => {
  const h = setup(); h.ready();
  const values = {creatorTitle: '  实际作品  ', creatorDuration: '30', creatorAspect: '9:16', creatorPace: 'preserve',
    creatorOriginalAudio: 'false', creatorMusic: 'false', creatorOpeningTitle: '  开头标题  '};
  for (const [id, value] of Object.entries(values)) h.field(id, value);
  await h.app.generateCreation();
  assert.equal(h.apiCalls[0].body.title, '实际作品');
  assert.deepEqual(h.apiCalls[0].body.options, {target_duration: 30, aspect: '9:16', pace: 'preserve',
    keep_original_audio: false, music: false, opening_title: '开头标题'});
});

test('ambiguous API result retains the same request ID and body for explicit retry', async () => {
  const h = setup(); h.ready();
  h.apiImpl = async () => { throw Object.assign(new Error('timeout after submit'), {uncertain: true}); };
  await h.app.generateCreation();
  assert.equal(h.app.creator.uncertain, true);
  assert.equal(h.elements.get('creatorBrief').disabled, true);
  const first = h.apiCalls[0].body;
  h.dispatch('click', {briefExample: 'this must not replace an uncertain submission'});
  assert.equal(h.elements.get('creatorBrief').value, first.brief);
  h.apiImpl = async () => ({created: false, job: {id: 'existing-job', title: 'Existing', status: 'queued'}});
  await h.app.generateCreation();
  assert.deepEqual(h.apiCalls[1].body, first);
  assert.equal(h.app.creator.resultId, 'existing-job');
  assert.equal(h.app.creator.requestId, null);
  assert.equal(h.app.creator.uncertain, false);
  assert.equal(h.elements.get('creatorBrief').disabled, false);
});

test('reload while POST is still pending restores the idempotency key before replay', async () => {
  const h = setup(), waiting = pending(); h.ready();
  h.apiImpl = () => waiting.promise;
  const inflight = h.app.generateCreation();
  assert.equal(h.apiCalls.length, 1);
  const first = h.apiCalls[0].body;
  const restored = setup({storage: new Map(h.storage)});
  assert.equal(restored.app.creator.uncertain, true);
  assert.equal(restored.app.creator.requestId, first.idempotency_key);
  await restored.app.generateCreation();
  assert.deepEqual(restored.apiCalls[0].body, first);
  waiting.resolve({created: true, job: {id: 'old-window-job', title: 'Old window', status: 'queued'}});
  await inflight;
});

test('draft persists only local creation fields and never connection credentials or File bytes', async () => {
  const h = setup(); h.ready();
  h.state.config.webhook = 'PRIVATE-WEBHOOK';
  h.app.creator.uploads.set('secret-file', {file: {name: 'x.mp4', bytes: 'PRIVATE-FILE-CONTENTS'}});
  h.apiImpl = async () => { throw Object.assign(new Error('unknown'), {uncertain: true}); };
  await h.app.generateCreation();
  const raw = [...h.storage.values()][0], draft = JSON.parse(raw);
  for (const secret of ['PRIVATE-DESKTOP-TOKEN', 'PRIVATE-GPU-TOKEN', 'PRIVATE-WEBHOOK', 'PRIVATE-FILE-CONTENTS']) assert.ok(!raw.includes(secret));
  assert.deepEqual(Object.keys(draft).sort(), ['fields', 'requestBody', 'requestId', 'resultId', 'selected', 'uncertain']);
  assert.deepEqual(Object.keys(draft.fields).sort(), plain(h.app.creatorFields).sort());
  const cloud = setup({localPage: false}); cloud.ready(); cloud.app.saveCreatorDraft();
  assert.equal(cloud.storageReads, 0); assert.equal(cloud.storageWrites, 0); assert.equal(cloud.storage.size, 0);
});

test('unavailable service, empty brief and busy submission cannot create duplicate requests', async () => {
  const h = setup(); h.setAssets('a.mp4'); h.select('a.mp4');
  await h.app.generateCreation(); assert.equal(h.apiCalls.length, 0);
  h.field('creatorBrief', 'make video'); h.state.config.creation_supported = false;
  await h.app.generateCreation(); assert.equal(h.apiCalls.length, 0);
  h.state.config.creation_supported = true; h.state.connected = false;
  await h.app.generateCreation(); assert.equal(h.apiCalls.length, 0);
  h.state.connected = true;
  const held = pending(); h.apiImpl = () => held.promise;
  const work = h.app.generateCreation(); await h.app.generateCreation();
  assert.equal(h.apiCalls.length, 1);
  held.resolve({job: {id: 'one', title: 'One', status: 'queued'}, created: true}); await work;
});

test('busy and uncertain submissions freeze selection, order, clearing and all upload entry points', async () => {
  for (const mode of ['busy', 'uncertain']) {
    const h = setup(); h.ready(); h.select('b.mp4');
    h.app.creator.uploads.set('failed-upload', {file: file('failed.mp4'), error: 'unknown', status: '导入需处理'});
    h.app.creator[mode] = true;
    h.app.renderCreatorMaterials();
    assert.equal(h.elements.get('creatorUploadButton').disabled, true, mode);
    assert.equal(h.elements.get('creatorFileInput').disabled, true, mode);
    assert.equal(h.elements.get('creatorClearSelection').disabled, true, mode);
    for (const tag of h.elements.get('creatorAssetGrid').innerHTML.match(/<input\b[^>]*data-creator-select[^>]*>/g)) assert.match(tag, /\bdisabled\b/);
    for (const tag of h.elements.get('creatorSelection').innerHTML.match(/<button\b[^>]*>/g)) assert.match(tag, /\bdisabled\b/);
    assert.match(h.elements.get('creatorAssetGrid').innerHTML, /data-creator-retry="failed-upload" disabled/);
    h.dispatch('change', {creatorSelect: 's:c.mp4'}, {checked: true});
    h.dispatch('click', {creatorRemove: 's:a.mp4'});
    h.dispatch('click', {creatorMove: '1', direction: '-1'});
    h.dispatch('click', {addToCreator: 'c.mp4'});
    h.elements.get('creatorClearSelection').onclick();
    await h.app.uploadCreatorFiles([file('new.mp4')]);
    await h.app.retryCreatorUpload('failed-upload');
    assert.deepEqual(plain(h.app.creator.selected), ['s:a.mp4', 's:b.mp4'], mode);
    assert.equal(h.uploadCalls.length, 0, mode);
    assert.equal(h.libraryRefreshCalls, 0, mode);
    assert.equal(h.app.creator.uploads.has('failed-upload'), true, mode);
    assert.equal(h.app.creator.uploading, false, mode);
  }
});

test('uncertain state still allows reconciliation and unlocks inputs after confirmed recovery', async () => {
  const h = setup(); h.ready();
  h.apiImpl = async () => { throw Object.assign(new Error('result unknown'), {uncertain: true}); };
  await h.app.generateCreation();
  const originalBody = h.apiCalls[0].body;
  assert.equal(h.elements.get('creatorGenerate').disabled, false);
  assert.equal(h.elements.get('creatorUploadButton').disabled, true);
  h.apiImpl = async () => ({created: false, job: {id: 'recovered', title: 'Recovered', status: 'queued'}});
  await h.elements.get('creatorGenerate').onclick();
  assert.deepEqual(h.apiCalls[1].body, originalBody);
  assert.equal(h.elements.get('creatorUploadButton').disabled, false);
  assert.equal(h.elements.get('creatorFileInput').disabled, false);
  assert.equal(h.elements.get('creatorClearSelection').disabled, false);
  h.app.toggleCreatorMaterial('s:b.mp4', true);
  assert.deepEqual(plain(h.app.creator.selected), ['s:a.mp4', 's:b.mp4']);
});

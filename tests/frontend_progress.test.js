'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup() {
  let now = 100000;
  const elements = new Map(['productionProgressBoard', 'jobsProgressBoard', 'reviewProgress'].map(id => [id, {innerHTML: '', hidden: false}]));
  const state = {connected: true, jobs: [], selected: null, filter: 'all'};
  const h = {state, elements, timers: [], clockNodes: [], cards: [], advance: ms => { now += ms; }};
  const context = vm.createContext({state, Date: {now: () => now},
    esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    document: {querySelector: selector => elements.get(selector.slice(1)), querySelectorAll: selector => selector === '[data-progress-clock]' ? h.clockNodes : selector === '[data-progress-job]' ? h.cards : []},
    setInterval: (fn, ms) => {h.timers.push({fn, ms}); return h.timers.length;},
    fetch: () => {throw new Error('Progress UI must never fetch or create jobs');}
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/progress-ui.js'), 'utf8') + '\nglobalThis.progressTest={progressCardHtml,renderProductionProgress,progressClock,progressClockValue,progressElapsedLabel,updateProductionProgressClocks};', context);
  h.app = context.progressTest;
  return h;
}

function job(status = 'rendering', extra = {}) {
  return {id: 'job-one', title: '沙龙宣传片', status, updated_at: '2000-01-01T00:00:00Z',
    progress_detail: {stage: 'compositing', label: '合成视频', detail: '正在合成画面、字幕和声音',
      elapsed_seconds: 70, stage_elapsed_seconds: 10, completed_count: 2, total_count: 4,
      stages: [{id:'validating',label:'检查素材',state:'completed'}, {id:'preparing_voice',label:'生成旁白',state:'completed'}, {id:'compositing',label:'合成视频',state:'active'}, {id:'checking',label:'检查成片',state:'pending'}]}, ...extra};
}

test('real stages, completed count and queue position are shown without percentage or ETA', () => {
  const h = setup(), queued = job('queued');
  queued.progress_detail = {...queued.progress_detail, stage:'queued', label:'排队中', queue_position:3, completed_count:0, stages:queued.progress_detail.stages.map(s => ({...s,state:'pending'}))};
  const html = h.app.progressCardHtml(queued);
  assert.match(html, /队列第 <b>3<\/b> 位/); assert.match(html, /已完成 <b>0 \/ 4<\/b> 步/);
  assert.match(html, /本次已用/); assert.match(html, /已等待/); assert.match(html, /1分10秒/);
  assert.doesNotMatch(html, /%|预计|剩余|role="progressbar"/);
});

test('elapsed starts from response duration, never double-counts old updated_at or advances stages', () => {
  const h = setup(), value = job();
  const initial = h.app.progressCardHtml(value); h.advance(5000);
  const later = h.app.progressCardHtml(value);
  assert.match(initial, /1分10秒/); assert.match(later, /1分15秒/); assert.match(later, />15秒<\/b>/);
  assert.match(later, /已完成 <b>2 \/ 4<\/b>/); assert.match(later, /aria-current="step"/);
});

test('disconnect freezes clock and stale snapshot stays frozen after reconnect until fresh data arrives', () => {
  const h = setup(), value = job(); h.app.progressCardHtml(value); h.advance(4000); h.state.connected = false;
  const stopped = h.app.progressCardHtml(value); assert.match(stopped, /1分14秒/); assert.match(stopped, /data-progress-review disabled/);
  h.advance(90000); assert.match(h.app.progressCardHtml(value), /1分14秒/);
  h.state.connected = true; assert.match(h.app.progressCardHtml(value), /data-progress-review disabled/);
  const fresh = job(); fresh.progress_detail.elapsed_seconds = 170;
  const resumed = h.app.progressCardHtml(fresh); assert.match(resumed, /2分50秒/); assert.doesNotMatch(resumed, /data-progress-review disabled/);
  h.advance(2000); assert.match(h.app.progressCardHtml(fresh), /2分52秒/);
});

test('boards include only active and failed production jobs; review completion is shown separately', () => {
  const h = setup();
  h.state.jobs = [job('failed',{id:'failed'}), job('review',{id:'finished'}), job('rendering',{id:'active'}), job('queued',{id:'test',job_kind:'technical_test'}), job('published',{id:'published'})];
  h.state.selected = h.state.jobs[1]; h.app.renderProductionProgress();
  const board = h.elements.get('productionProgressBoard').innerHTML;
  assert.ok(board.indexOf('data-progress-job="active"') < board.indexOf('data-progress-job="failed"'));
  assert.doesNotMatch(board, /data-progress-job="(?:finished|published|test)"/);
  assert.match(h.elements.get('reviewProgress').innerHTML, /制作用时/);
  h.state.filter = 'technical_test'; h.app.renderProductionProgress();
  assert.equal(h.elements.get('jobsProgressBoard').hidden, true);
});

test('failed and completed elapsed values stop; errors and untrusted titles are escaped', () => {
  const h = setup(), value = job('failed',{title:'<img src=x onerror=alert(1)>',error:'<script>bad()</script>'});
  const first = h.app.progressCardHtml(value); h.advance(100000);
  assert.equal(h.app.progressCardHtml(value), first); assert.match(first, /&lt;img/); assert.match(first, /&lt;script&gt;/); assert.doesNotMatch(first, /<script>|<img/);
  const complete = job('review'); h.app.progressCardHtml(complete,{review:true}); h.advance(30000);
  assert.match(h.app.progressCardHtml(complete,{review:true}), /1分10秒/);
});

test('missing progress detail and malformed duration do not invent timing or stages', () => {
  const h = setup(), value = job('analyzing',{progress_detail:undefined});
  const html = h.app.progressCardHtml(value);
  assert.match(html, /等待制作服务返回步骤/); assert.match(html, /暂无计时/); assert.doesNotMatch(html, /已完成|制作步骤/);
  for (const v of [null, undefined, true, '20', NaN, Infinity]) assert.equal(h.app.progressElapsedLabel(v), '暂无计时');
  assert.equal(h.app.progressElapsedLabel(3601), '1小时0分1秒');
});

test('one-second timer updates clock text and offline notice without rebuilding DOM or making requests', () => {
  const h = setup(), value = job(); h.state.jobs = [value]; h.app.renderProductionProgress();
  const board = h.elements.get('productionProgressBoard'), initial = board.innerHTML;
  const counter = {dataset:{progressId:value.id,progressClock:'elapsed'},textContent:''}, notice = {hidden:true}, button = {disabled:false};
  h.clockNodes.push(counter); h.cards.push({dataset:{progressJob:value.id},querySelector:s => s === '[data-progress-connection]' ? notice : button});
  assert.equal(h.timers.length,1); assert.equal(h.timers[0].ms,1000);
  h.advance(3000); h.timers[0].fn(); assert.equal(counter.textContent,'1分13秒'); assert.equal(board.innerHTML,initial);
  h.state.connected = false; h.timers[0].fn(); assert.equal(notice.hidden,false); assert.equal(button.disabled,true);
  h.advance(10000); h.timers[0].fn(); assert.equal(counter.textContent,'1分13秒');
});

test('empty boards hide and selected technical tests never enter the production panel', () => {
  const h = setup(); h.state.selected = job('review',{job_kind:'technical_test'}); h.app.renderProductionProgress();
  for (const element of h.elements.values()) { assert.equal(element.hidden,true); assert.equal(element.innerHTML,''); }
});

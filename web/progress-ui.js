'use strict';

// A clock may advance between successful snapshots; a stage may never do so.
const productionProgressClocks = new Map();
const productionProgressSnapshots = new WeakMap();
const productionProgressActive = new Set(['queued', 'analyzing', 'rendering']);
const productionProgressDone = new Set(['review', 'approved', 'publishing', 'published']);

function progressElapsedLabel(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '暂无计时';
  const seconds = Math.max(0, Math.floor(value)), minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60);
  return hours ? `${hours}小时${minutes % 60}分${seconds % 60}秒` : minutes ? `${minutes}分${seconds % 60}秒` : `${seconds}秒`;
}

function progressClock(job) {
  const detail = job.progress_detail;
  if (!detail || typeof detail !== 'object') return null;
  let clock = productionProgressSnapshots.get(detail);
  if (!clock) {
    clock = {at: Date.now(), elapsed: detail.elapsed_seconds, stageElapsed: detail.stage_elapsed_seconds,
      running: productionProgressActive.has(job.status), frozen: !state.connected, frozenAt: Date.now()};
    productionProgressSnapshots.set(detail, clock);
  }
  // Reconnecting alone does not make an old snapshot current. A fresh response does.
  if (!state.connected && !clock.frozen) { clock.frozen = true; clock.frozenAt = Date.now(); }
  productionProgressClocks.set(job.id, clock);
  return clock;
}

function progressClockValue(clock, field) {
  if (!clock) return null;
  if (!state.connected && !clock.frozen) { clock.frozen = true; clock.frozenAt = Date.now(); }
  const baseline = field === 'stage' ? clock.stageElapsed : clock.elapsed;
  if (typeof baseline !== 'number' || !Number.isFinite(baseline)) return null;
  const now = clock.frozen ? clock.frozenAt : Date.now();
  return baseline + (clock.running ? Math.max(0, Math.floor((now - clock.at) / 1000)) : 0);
}

function progressCardHtml(job, options = {}) {
  if (!job || job.job_kind === 'technical_test') return '';
  const active = productionProgressActive.has(job.status), failed = job.status === 'failed';
  const done = productionProgressDone.has(job.status);
  if (!active && !failed && !(options.review && done)) return '';
  const detail = job.progress_detail && typeof job.progress_detail === 'object' ? job.progress_detail : {};
  const stages = Array.isArray(detail.stages) ? detail.stages.filter(step => step && typeof step.label === 'string').slice(0, 12) : [];
  const stepStates = new Set(['completed', 'active', 'failed', 'pending']);
  const completed = Number.isInteger(detail.completed_count) ? Math.max(0, Math.min(detail.completed_count, stages.length)) : null;
  const clock = progressClock(job), stale = !state.connected || clock?.frozen;
  const label = detail.label || (failed ? '需要处理' : done ? '制作完成' : job.status === 'queued' ? '排队中' : '制作中');
  const message = detail.detail || (failed ? '本次制作未完成，请查看详情。' : done ? '成片已完成，请试看确认。' : '正在等待制作服务返回步骤。');
  const error = detail.error || (failed ? job.error : null);
  const queue = job.status === 'queued' && Number.isInteger(detail.queue_position) && detail.queue_position > 0 ? detail.queue_position : null;
  const steps = stages.map((step, index) => {
    const stepState = stepStates.has(step.state) ? step.state : 'pending';
    const description = {completed: '已完成', active: '进行中', failed: '已停止', pending: '待进行'}[stepState];
    return `<li class="production-progress-step ${stepState}"${stepState === 'active' ? ' aria-current="step"' : ''}><span class="production-progress-marker" aria-hidden="true">${stepState === 'completed' ? '✓' : stepState === 'failed' ? '!' : index + 1}</span><span>${esc(step.label)}<small>${description}</small></span></li>`;
  }).join('');
  return `<article class="production-progress-card${failed ? ' is-failed' : ''}${done ? ' is-complete' : ''}" data-progress-job="${esc(job.id)}">
    <div class="production-progress-heading"><div><span class="production-progress-state">${esc(label)}</span><h3>${esc(job.title || '视频制作任务')}</h3></div>${options.review ? '' : `<button type="button" class="button subtle" data-review="${esc(job.id)}" data-progress-review${stale ? ' disabled' : ''}>查看任务</button>`}</div>
    <p class="production-progress-description">${esc(message)}</p>
    <div class="production-progress-facts"><span>${done ? '制作用时' : failed ? '停止时已用' : '本次已用'} <b data-progress-clock="elapsed" data-progress-id="${esc(job.id)}">${esc(progressElapsedLabel(progressClockValue(clock, 'elapsed')))}</b></span>${active ? `<span>${job.status === 'queued' ? '已等待' : '本步骤已用'} <b data-progress-clock="stage" data-progress-id="${esc(job.id)}">${esc(progressElapsedLabel(progressClockValue(clock, 'stage')))}</b></span>` : ''}${queue ? `<span>队列第 <b>${queue}</b> 位</span>` : ''}${completed !== null && stages.length ? `<span>已完成 <b>${completed} / ${stages.length}</b> 步</span>` : ''}</div>
    ${steps ? `<ol class="production-progress-steps" aria-label="制作步骤">${steps}</ol>` : ''}
    ${error && !String(message).includes(String(error)) ? `<p class="production-progress-error">${esc(error)}</p>` : ''}
    <p class="production-progress-offline" data-progress-connection${stale ? '' : ' hidden'}>连接中断或待更新，以上是上次读取的状态；计时已暂停。</p>
  </article>`;
}

function updateProductionProgressClocks() {
  document.querySelectorAll('[data-progress-clock]').forEach(node => {
    const clock = productionProgressClocks.get(node.dataset.progressId);
    node.textContent = progressElapsedLabel(progressClockValue(clock, node.dataset.progressClock));
  });
  document.querySelectorAll('[data-progress-job]').forEach(card => {
    const clock = productionProgressClocks.get(card.dataset.progressJob);
    const stale = !state.connected || !!clock?.frozen;
    const notice = card.querySelector('[data-progress-connection]');
    if (notice) notice.hidden = !stale;
    const button = card.querySelector('[data-progress-review]');
    if (button) button.disabled = stale;
  });
}

function renderProductionProgress() {
  const jobs = (Array.isArray(state.jobs) ? state.jobs : []).filter(job => job && job.job_kind !== 'technical_test');
  const showing = jobs.filter(job => productionProgressActive.has(job.status) || job.status === 'failed');
  // Preserve the service's queue ordering; bring work in progress ahead of old errors.
  const ordered = showing.filter(job => productionProgressActive.has(job.status)).concat(showing.filter(job => job.status === 'failed'));
  for (const id of ['productionProgressBoard', 'jobsProgressBoard']) {
    const board = document.querySelector('#' + id);
    if (!board) continue;
    const visible = id === 'jobsProgressBoard' && state.filter === 'technical_test' ? [] : ordered;
    board.innerHTML = visible.map(job => progressCardHtml(job)).join('');
    board.hidden = !visible.length;
  }
  const reviewBoard = document.querySelector('#reviewProgress');
  if (reviewBoard) {
    reviewBoard.innerHTML = state.selected ? progressCardHtml(state.selected, {review: true}) : '';
    reviewBoard.hidden = !reviewBoard.innerHTML;
  }
  const retained = new Set(jobs.map(job => job.id));
  if (state.selected) retained.add(state.selected.id);
  for (const id of productionProgressClocks.keys()) if (!retained.has(id)) productionProgressClocks.delete(id);
  updateProductionProgressClocks();
}

// Only elapsed text changes on this timer: no network calls, phase inference or ETA.
setInterval(updateProductionProgressClocks, 1000);
renderProductionProgress();

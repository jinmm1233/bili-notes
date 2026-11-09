'use strict';

/* ==================== 基础工具 ==================== */
const $ = (id) => document.getElementById(id);

const imgProxy = (url, width) => url
  ? `/api/proxy/image?url=${encodeURIComponent(url)}${width ? `&w=${width}` : ''}`
  : '';

const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

const fmtNum = (n) => {
  const v = Number(n || 0);
  if (v >= 10000) return (v / 10000).toFixed(1).replace(/\.0$/, '') + ' 万';
  return String(v);
};

const fmtDur = (sec) => {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`
           : `${m}:${String(ss).padStart(2, '0')}`;
};

let toastTimer = null;
function toast(msg, bad) {
  const box = $('toast');
  box.textContent = msg;
  box.className = 'toast' + (bad ? ' bad' : '');
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 3200);
}

async function api(path, opts = {}) {
  // 本地应用，正确性优先：默认不让浏览器缓存 GET，
  // 否则刚改过的开关/列表读到的还是上一份（"点了关闭还是开着"就是这么来的）
  const res = await fetch(path, Object.assign({
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
  }, opts));
  let data = {};
  try { data = await res.json(); } catch (_) { /* 空响应 */ }
  if (!res.ok) {
    throw new Error(data.message || data.detail || `请求失败 (${res.status})`);
  }
  return data;
}

/* ==================== 视图切换 ==================== */
const VIEW_LOADERS = {
  dashboard: loadOverview,
  collect: loadAuth,
  queue: loadTasks,
  library: () => {
    currentCollection = ''; currentSeries = ''; currentSeriesTitle = '';
    $('kbSearch').value = '';
    return loadLibrary();
  },
  review: loadReview,
  concepts: loadConcepts,
  quiz: loadQuiz,
  settings: loadSettings,
};
let currentView = 'dashboard';

function switchView(name) {
  if (!VIEW_LOADERS[name]) return;
  currentView = name;
  document.querySelectorAll('.view').forEach((v) => {
    v.classList.toggle('active', v.id === `view-${name}`);
  });
  document.querySelectorAll('.nav-item').forEach((b) => {
    b.classList.toggle('active', b.dataset.view === name);
  });
  if (location.hash.slice(1) !== name) {
    history.replaceState(null, '', '#' + name);
  }
  $('reader').hidden = true;
  VIEW_LOADERS[name]().catch((e) => toast(e.message, true));
}

/* ==================== 总览 ==================== */
async function loadOverview() {
  const o = await api('/api/overview');

  const stats = [
    { num: fmtNum(o.library.notes), lab: '知识库笔记', cls: 'accent' },
    { num: fmtNum(o.library.images), lab: '关键画面', cls: '' },
    { num: fmtNum(o.library.chapters), lab: '章节总数', cls: 'accent2' },
    { num: fmtNum(o.tasks.running), lab: '处理中', cls: '' },
  ];
  $('statGrid').innerHTML = stats.map((s) => `
    <div class="stat ${s.cls}">
      <div class="num">${s.num}</div>
      <div class="lab">${s.lab}</div>
    </div>`).join('');

  const rt = $('recentTasks');
  if (!o.recent_tasks.length) {
    rt.innerHTML = '<p class="empty">暂无任务</p>';
  } else {
    rt.innerHTML = o.recent_tasks.map((t) => `
      <div class="mini-item">
        <span class="t" title="${esc(t.title || t.url)}">${esc(t.title || t.url)}</span>
        <div class="progress"><i style="width:${t.progress || 0}%"></i></div>
        <span class="p">${Math.round(t.progress || 0)}%</span>
      </div>`).join('');
  }

  const rn = $('recentNotes');
  rn.innerHTML = o.recent_notes.length
    ? o.recent_notes.map(noteCard).join('')
    : '<p class="empty">还没有笔记，去采集收藏夹或处理一个视频试试</p>';
  bindNoteCards(rn);
}

function noteCard(n) {
  const cover = n.cover
    ? `<img src="/api/library/${encodeURIComponent(n.id)}/file/${n.cover}" alt="" loading="lazy">`
    : '无封面';
  const tags = [
    n.lecture ? '<span class="tag has-lecture">讲义</span>' : '',
    ...(n.tags || []).slice(0, 3).map((t) => `<span class="tag">${esc(t)}</span>`),
  ].join('');
  return `
    <article class="note-card" data-note="${esc(n.id)}">
      <div class="note-cover">
        ${cover}
        <button class="note-del" data-del-note="${esc(n.id)}" data-title="${esc(n.title)}"
                title="删除这篇笔记">✕</button>
      </div>
      <div class="note-body">
        <h3 class="note-title">${esc(n.title)}</h3>
        <div class="note-meta">
          <span>${esc(n.uploader || '未知')}</span>
          <span>${n.chapters} 章</span>
          <span>${n.images} 图</span>
        </div>
        <div class="note-tags">${tags}</div>
      </div>
    </article>`;
}

function bindNoteCards(root) {
  root.querySelectorAll('[data-note]').forEach((card) => {
    card.onclick = () => {
      // 多选模式下点卡片是勾选，不要打开阅读器
      if (selectMode) { togglePick(card); return; }
      openNote(card.dataset.note);
    };
  });
  root.querySelectorAll('[data-del-note]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();   // 别把"打开笔记"也带出来
      deleteNote(btn.dataset.delNote, btn.dataset.title).catch((e) => toast(e.message, true));
    };
  });
}

async function deleteNote(id, title) {
  const ok = confirm(
    `删除笔记「${title || id}」？\n\n`
    + '笔记连同里面的图片会移到回收站（数据目录的 trash 文件夹，可找回），\n'
    + '之后在「采集收藏」重新导入这个视频，就能再处理一遍。',
  );
  if (!ok) return;
  await api(`/api/library/${encodeURIComponent(id)}`, { method: 'DELETE' });
  if (!$('reader').hidden) $('reader').hidden = true;
  toast('已删除，可在采集收藏里重新处理');
  await loadLibrary($('kbSearch').value.trim() || undefined);
}

/* ==================== 采集：登录 ==================== */
let qrTimer = null;
let qrKey = '';

async function loadAuth() {
  const s = await api('/api/auth/status');
  renderAuth(s);
  if (s.logged_in) loadFolders();
}

function renderAuth(s) {
  $('authLoggedOut').hidden = !!s.logged_in;
  $('authLoggedIn').hidden = !s.logged_in;
  $('favPanel').hidden = !s.logged_in;
  $('btnReloadFav').hidden = !s.logged_in;

  const dot = $('biliDot'), txt = $('biliText');
  if (s.logged_in) {
    dot.className = 'dot on';
    txt.textContent = s.uname || 'B站已登录';
    $('userName').textContent = s.uname || '已登录';
    $('userMid').textContent = s.mid ? `UID ${s.mid}` : '';
    if (s.face) $('userFace').src = imgProxy(s.face, 120);
  } else {
    dot.className = 'dot';
    txt.textContent = 'B站未登录';
  }
}

async function startQr() {
  stopQr();
  $('qrStatus').className = 'qr-status';
  $('qrStatus').textContent = '正在获取二维码…';
  try {
    const d = await api('/api/auth/qrcode', { method: 'POST' });
    qrKey = d.qrcode_key;
    if (d.image) {
      $('qrBox').innerHTML = `<img src="${d.image}" alt="登录二维码">`;
    } else {
      $('qrBox').innerHTML = `<div class="qr-placeholder">二维码生成失败，请重试</div>`;
    }
    $('qrStatus').textContent = '打开 B站手机客户端 → 扫一扫';
    qrTimer = setInterval(pollQr, 2200);
  } catch (e) {
    $('qrStatus').className = 'qr-status bad';
    $('qrStatus').textContent = e.message;
  }
}

async function pollQr() {
  if (!qrKey) return;
  try {
    const d = await api(`/api/auth/qrcode/poll?key=${encodeURIComponent(qrKey)}`);
    if (d.logged_in) {
      stopQr();
      $('qrStatus').className = 'qr-status ok';
      $('qrStatus').textContent = '登录成功';
      const s = await api('/api/auth/status');
      renderAuth(s);
      toast('登录成功');
      loadFolders();
      return;
    }
    $('qrStatus').className = 'qr-status';
    $('qrStatus').textContent = d.message || '等待扫码…';
    if (d.done) stopQr();
  } catch (e) {
    $('qrStatus').className = 'qr-status bad';
    $('qrStatus').textContent = e.message;
    stopQr();
  }
}

function stopQr() {
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
}

async function logout() {
  await api('/api/auth/logout', { method: 'POST' });
  $('favPanel').hidden = true;
  $('qrBox').innerHTML = '<div class="qr-placeholder">点击下方按钮生成二维码</div>';
  renderAuth({ logged_in: false });
  toast('已退出登录');
}

/* ==================== 采集：收藏夹 ==================== */
let folders = [];
let videos = [];
let selected = new Set();
let page = 1;
let hasMore = false;

async function loadFolders() {
  try {
    const d = await api('/api/favorites/folders');
    folders = d.folders || [];
    const sel = $('favSelect');
    if (!folders.length) {
      sel.innerHTML = '<option value="">没有收藏夹</option>';
      return;
    }
    sel.innerHTML = folders.map((f) =>
      `<option value="${f.media_id}">${esc(f.title)}（${f.count}）</option>`).join('');
    page = 1;
    selected.clear();
    await loadVideos();
  } catch (e) {
    toast(e.message, true);
    if (/登录/.test(e.message)) renderAuth({ logged_in: false });
  }
}

async function loadVideos() {
  const mediaId = $('favSelect').value;
  if (!mediaId) return;
  $('videoGrid').innerHTML = '<p class="empty">加载中…</p>';
  try {
    const d = await api(`/api/favorites/items?media_id=${mediaId}&page=${page}`);
    videos = d.items || [];
    hasMore = !!d.has_more;
    renderVideos();
    $('pageInfo').textContent = `第 ${page} 页 · 共 ${d.total} 个`;
    $('btnPrev').disabled = page <= 1;
    $('btnNext').disabled = !hasMore;
  } catch (e) {
    $('videoGrid').innerHTML = `<p class="empty">${esc(e.message)}</p>`;
  }
}

/* 封面图标签：懒加载 + 加载失败自动补一次。
   以前封面是 background-image，一张失败就是永久空白且不会重试，
   收藏夹一页 20+ 张图时特别明显。改成 <img> 才能拿到失败事件。 */
function coverTag(url, width) {
  const src = imgProxy(url, width || 480);
  if (!src) return '';
  return `<img class="thumb-img" loading="lazy" decoding="async" alt="" src="${esc(src)}"
    onerror="if(this.dataset.retry){this.classList.add('dead')}else{this.dataset.retry='1';this.src=this.getAttribute('src')+'&retry=1'}">`;
}

function renderVideos() {
  if (!videos.length) {
    $('videoGrid').innerHTML = '<p class="empty">这个收藏夹还没有视频</p>';
    return;
  }
  $('videoGrid').innerHTML = videos.map((v) => `
    <div class="video-card ${selected.has(v.bvid) ? 'sel' : ''}" data-bvid="${esc(v.bvid)}">
      <div class="thumb">
        ${coverTag(v.cover, 480)}
        <div class="tick">✓</div>
        <span class="dur">${fmtDur(v.duration)}</span>
        <button class="pick-btn" data-pick="${esc(v.bvid)}" title="这个视频属于合集/多P时，可以按集挑选">选集</button>
      </div>
      <div class="vt" title="${esc(v.title)}">${esc(v.title)}</div>
    </div>`).join('');

  $('videoGrid').querySelectorAll('[data-bvid]').forEach((card) => {
    card.onclick = (e) => {
      const pick = e.target.closest('.pick-btn');
      if (pick) { e.stopPropagation(); openPicker(pick.dataset.pick); return; }
      const id = card.dataset.bvid;
      if (selected.has(id)) selected.delete(id); else selected.add(id);
      card.classList.toggle('sel', selected.has(id));
      updateSelCount();
    };
  });
  updateSelCount();
}

function updateSelCount() {
  // 导入过程中按钮文字会被换成"导入中…"，里面的 #selCount 会短暂消失，
  // 这里必须容错——否则任务已经建好了，用户却收到一句报错。
  const num = $('selCount');
  if (num) num.textContent = String(selected.size);
  const btn = $('btnImport');
  if (btn) btn.disabled = selected.size === 0;
}

/** 当前选中的收藏夹名，导入时带给后端，笔记会记下来源便于知识库分组。 */
function currentFolderTitle() {
  const id = Number($('favSelect').value);
  const hit = folders.find((f) => Number(f.media_id) === id);
  return hit ? (hit.title || '') : '';
}

async function doImport() {  if (!selected.size) return;
  const btn = $('btnImport');
  btn.disabled = true;
  btn.textContent = '导入中…';
  try {
    const d = await api('/api/favorites/import', {
      method: 'POST',
      body: JSON.stringify({
        bvids: [...selected],
        skip_done: true,
        expand_series: $('batchExpand').checked,
        series_limit: Number($('batchSeriesLimit').value) || 0,
        lecture: $('batchLecture').checked,
        collection: currentFolderTitle(),
        media_id: Number($('favSelect').value) || null,
      }),
    });
    toast(`已加入队列 ${d.count} 个${d.expanded ? `（含展开 ${d.expanded} 集）` : ''}${d.skipped ? `，跳过 ${d.skipped} 个` : ''}`);
    selected.clear();
    switchView('queue');
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.innerHTML = '导入选中（<span id="selCount">0</span>）';
    updateSelCount();
  }
}

function estimateHuman(totalSeconds) {
  // 经验值：每分钟视频约 36 秒处理时间；队列并发 2，墙钟时间再减半
  const minutes = (Number(totalSeconds) * 0.6) / 60 / 2;
  if (minutes < 1) return "不到 1 分钟";
  if (minutes < 60) return `约 ${Math.round(minutes)} 分钟`;
  return `约 ${(minutes / 60).toFixed(1)} 小时`;
}

async function doBatch() {
  const mediaId = $('favSelect').value;
  if (!mediaId) { toast('请先选择一个收藏夹', true); return; }

  const limit = Math.max(1, Math.min(100, parseInt($('batchLimit').value, 10) || 20));
  const btn = $('btnBatch');
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '提交中…';

  try {
    const expand = $('batchExpand').checked;
    const d = await api('/api/favorites/import-count', {
      method: 'POST',
      body: JSON.stringify({
        media_id: Number(mediaId),
        limit,
        skip_done: true,
        expand_series: expand,
        series_limit: Number($('batchSeriesLimit').value) || 0,
        lecture: $('batchLecture').checked,
        collection: currentFolderTitle(),
      }),
    });
    if (!d.count) {
      toast(d.message || '没有新的视频需要处理（都已产出笔记）', true);
      return;
    }
    const parts = [`已加入 ${d.count} 条`];
    const perSeries = Number($('batchSeriesLimit').value) || 0;
    if (d.expanded) {
      parts.push(`其中展开合集 ${d.expanded} 集` + (perSeries ? `（每合集最多 ${perSeries} 集）` : ''));
    }
    if (d.skipped) parts.push(`跳过 ${d.skipped} 条已处理的`);
    parts.push(`共 ${fmtDur(d.total_duration)} 素材，预计 ${estimateHuman(d.total_duration)} 完成`);
    toast(parts.join('；'));
    switchView('queue');
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

/* ==================== 任务队列 ==================== */
let lastTasksJson = '';

async function loadTasks(force) {
  const d = await api('/api/tasks');
  const items = d.items || [];
  const json = JSON.stringify(items.map((t) => [t.id, t.status, t.progress, t.message]));

  const running = items.filter((t) => t.status === 'running' || t.status === 'queued').length;
  const badge = $('navBadge');
  badge.hidden = running === 0;
  badge.textContent = String(running);

  // 顶部的批量暂停/继续：有在跑的才显示"全部暂停"，有暂停的才显示"全部继续"
  $('btnPauseAll').hidden = running === 0;
  $('btnResumeAll').hidden = !items.some((t) => t.status === 'paused');

  if (!force && json === lastTasksJson) return;
  lastTasksJson = json;

  if (currentView !== 'queue') return;
  const box = $('taskList');
  if (!items.length) {
    box.innerHTML = '<p class="empty">暂无任务</p>';
    return;
  }
  box.innerHTML = items.map(taskRow).join('');
  box.querySelectorAll('[data-act]').forEach((btn) => {
    btn.onclick = () => taskAction(btn.dataset.act, btn.dataset.id);
  });
}

const STATE_LABEL = {
  queued: '排队中', running: '处理中', paused: '已暂停', done: '已完成',
  failed: '失败', cancelled: '已取消',
};

function taskRow(t) {
  const pct = Math.round(t.progress || 0);
  const ops = [];
  if (t.status === 'running' || t.status === 'queued') {
    ops.push(`<button class="btn ghost small" data-act="pause" data-id="${t.id}">暂停</button>`);
    ops.push(`<button class="btn ghost small" data-act="cancel" data-id="${t.id}">取消</button>`);
  }
  if (t.status === 'paused') {
    ops.push(`<button class="btn ghost small" data-act="unpause" data-id="${t.id}">继续</button>`);
    ops.push(`<button class="btn ghost small" data-act="cancel" data-id="${t.id}">取消</button>`);
  }
  if (t.status === 'failed' || t.status === 'cancelled') {
    ops.push(`<button class="btn ghost small" data-act="retry" data-id="${t.id}">重试</button>`);
  }
  if (t.status === 'done') {
    ops.push(`<button class="btn ghost small" data-act="open" data-id="${t.id}">查看</button>`);
  }
  return `
    <div class="task-row">
      <span class="state ${t.status}">${STATE_LABEL[t.status] || t.status}</span>
      <span class="t" title="${esc(t.title || t.url)}">${esc(t.title || t.url)}</span>
      <div class="progress"><i style="width:${pct}%"></i></div>
      <span class="m">${esc(t.error || t.message || '')}</span>
      <div class="ops">${ops.join('')}</div>
    </div>`;
}

async function taskAction(act, id) {
  try {
    if (act === 'cancel') await api(`/api/tasks/${id}/cancel`, { method: 'POST' });
    if (act === 'retry') await api(`/api/tasks/${id}/retry`, { method: 'POST' });
    if (act === 'pause') await api(`/api/tasks/${id}/pause`, { method: 'POST' });
    if (act === 'unpause') await api(`/api/tasks/${id}/unpause`, { method: 'POST' });
    if (act === 'open') {
      const t = await api(`/api/tasks/${id}`);
      if (t.output_dir) {
        const name = t.output_dir.split(/[\\/]/).pop();
        switchView('library');
        setTimeout(() => openNote(name), 260);
        return;
      }
    }
    loadTasks(true);
  } catch (e) {
    toast(e.message, true);
  }
}

async function addTask() {
  const url = $('queueUrl').value.trim();
  if (!url) return;
  try {
    await api('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({ url, frames: true, ocr: true, summary: true }),
    });
    $('queueUrl').value = '';
    toast('已加入队列');
    loadTasks(true);
  } catch (e) {
    toast(e.message, true);
  }
}

/* ==================== 知识库 ==================== */
/* 三级浏览：收藏夹 → 合集（+ 单篇笔记）→ 合集里的全部分集。
   一个课程十几集全铺在收藏夹里会把别的笔记挤没，所以同一合集先折成一张卡，
   点进去才按集数顺序展开。搜索时仍直接给命中的笔记，不做折叠。 */
let currentCollection = '';
let currentSeries = '';
let currentSeriesTitle = '';
let libCollections = [];

/* 列表排序口径，用户自己选，记在本机：
   created = 按笔记生成时间（补结构、重建导图不会把顺序打乱）
   mtime   = 按文件修改时间（"最近动过的"排最前） */
const LIB_SORT_KEY = 'bn.lib.sort';
let libSort = localStorage.getItem(LIB_SORT_KEY) === 'mtime' ? 'mtime' : 'created';

function setLibSort(mode) {
  libSort = mode === 'mtime' ? 'mtime' : 'created';
  try { localStorage.setItem(LIB_SORT_KEY, libSort); } catch (e) { /* 隐私模式存不了，不影响本次 */ }
  const sel = $('libSort');
  if (sel) sel.value = libSort;
  // 只重排当前列表，不回到第一级——正在某个收藏夹里看的时候别把人踢出去
  loadLibrary($('kbSearch').value.trim()).catch((e) => toast(e.message, true));
}

// 多选搬移：只在进了某个收藏夹之后才有意义
let selectMode = false;
const picked = new Set();

function exitSelect() {
  selectMode = false;
  picked.clear();
  const grid = $('kbGrid');
  grid.classList.remove('selecting');
  grid.querySelectorAll('.note-card.picked').forEach((c) => c.classList.remove('picked'));
  syncSelectUI();
}

function syncSelectUI() {
  const n = picked.size;
  $('libPicked').hidden = !selectMode || n === 0;
  $('libPicked').textContent = `已选 ${n} 篇`;
  $('btnMovePicked').hidden = !selectMode || n === 0;
  $('btnSelectMode').textContent = selectMode ? '取消多选' : '多选';
}

function togglePick(card) {
  const id = card.dataset.note;
  if (picked.has(id)) { picked.delete(id); card.classList.remove('picked'); }
  else { picked.add(id); card.classList.add('picked'); }
  syncSelectUI();
}

async function loadLibrary(query) {
  const box = $('kbGrid');
  box.innerHTML = '<p class="empty">加载中…</p>';
  exitSelect();
  try {
    let d;
    if (query) {
      const qs = new URLSearchParams({ q: query, sort: libSort });
      if (currentCollection) qs.set('collection', currentCollection);
      d = await api(`/api/library/search?${qs}`);
    } else {
      const qs = new URLSearchParams({ sort: libSort });
      if (currentCollection) qs.set('collection', currentCollection);
      d = await api(`/api/library?${qs}`);
      libCollections = d.collections || [];
    }
    const items = d.items || [];
    const all = d.stats || {};
    const sortSel = $('libSort');
    if (sortSel) sortSel.value = libSort;

    $('btnClearSearch').hidden = !query;
    $('libCrumb').hidden = !currentCollection;
    $('libCrumbName').textContent = currentSeries
      ? `${currentCollection} › ${currentSeriesTitle}` : currentCollection;
    $('btnLibBack').textContent = currentSeries ? '← 返回收藏夹' : '← 全部收藏夹';
    $('btnSelectMode').hidden = !currentCollection;
    $('btnNewCollection').hidden = !!currentCollection;
    const sortWord = libSort === 'mtime' ? '按最近修改' : '按生成时间';

    if (!query && !currentCollection) {
      // 第一级：收藏夹大类
      $('libSub').textContent = `${libCollections.length} 个收藏夹 · ${all.notes || 0} 篇笔记 · `
        + `${fmtNum(all.words)} 字 · ${all.images || 0} 张画面`;
      if (!libCollections.length) {
        box.innerHTML = '<p class="empty">知识库还是空的</p>';
        return;
      }
      box.className = 'collection-grid';
      box.innerHTML = libCollections.map(collectionCard).join('');
      box.querySelectorAll('[data-collection]').forEach((card) => {
        card.onclick = () => enterCollection(card.dataset.collection);
      });
      box.querySelectorAll('[data-del]').forEach((btn) => {
        btn.onclick = (ev) => {
          ev.stopPropagation();   // 别触发进入收藏夹
          deleteCollection(btn.dataset.del).catch((e) => toast(e.message, true));
        };
      });
      return;
    }

    // 第二级：收藏夹里，同一合集折成一张卡 + 不属于任何合集的散篇
    const groups = (d.series || []).filter((g) => g.count >= 2);
    const inSeries = (n) => groups.some((g) => g.key === n.series_key);
    const third = !query && currentCollection && currentSeries;
    box.className = 'note-grid' + (selectMode ? ' selecting' : '');

    if (!query && currentCollection && !currentSeries) {
      $('libSub').textContent = `「${currentCollection}」共 ${items.length} 篇`
        + (groups.length ? ` · ${groups.length} 个合集已折叠` : '') + ` · ${sortWord}`;
      const singles = items.filter((n) => !inSeries(n));
      if (!groups.length && !singles.length) {
        box.innerHTML = '<p class="empty">这个收藏夹下还没有笔记</p>';
        return;
      }
      box.innerHTML = groups.map(seriesCard).join('') + singles.map(noteCard).join('');
      box.querySelectorAll('[data-series]').forEach((card) => {
        card.onclick = () => {
          if (selectMode) return;      // 合集卡不参与多选搬移
          enterSeries(card.dataset.series, card.dataset.seriesTitle);
        };
      });
      bindNoteCards(box);
      return;
    }

    // 第三级：点开合集后，按集数顺序看全部分集
    if (third) {
      const parts = items.filter((n) => n.series_key === currentSeries)
        .sort((a, b) => (a.series_order || 0) - (b.series_order || 0)
          || String(a.title).localeCompare(String(b.title), 'zh'));
      $('libSub').textContent = `「${currentSeriesTitle}」共 ${parts.length} 集 · 按集数顺序`;
      if (!parts.length) {
        box.innerHTML = '<p class="empty">这个合集里还没有笔记</p>';
        return;
      }
      box.innerHTML = parts.map(noteCard).join('');
      bindNoteCards(box);
      return;
    }

    // 搜索结果：直接给命中的笔记，不做折叠
    $('libSub').textContent = `搜索「${query}」找到 ${items.length} 篇`
      + `${currentCollection ? `（限「${currentCollection}」）` : ''} · ${sortWord}`;
    if (!items.length) {
      box.innerHTML = '<p class="empty">没有匹配的笔记</p>';
      return;
    }
    box.innerHTML = items.map((n) => noteCard(n)).join('');
    bindNoteCards(box);
  } catch (e) {
    box.className = 'note-grid';
    box.innerHTML = `<p class="empty">${esc(e.message)}</p>`;
  }
}

function enterCollection(name) {
  currentCollection = name || '';
  currentSeries = '';
  currentSeriesTitle = '';
  $('kbSearch').value = '';
  loadLibrary();
}

function enterSeries(key, title) {
  currentSeries = key || '';
  currentSeriesTitle = title || '未命名合集';
  $('kbSearch').value = '';
  loadLibrary();
}

function leaveSeries() {
  currentSeries = '';
  currentSeriesTitle = '';
  loadLibrary();
}

/* ---- 自建收藏夹 / 搬移 ---- */
async function newCollection() {
  const name = await askName('新建收藏夹', '比如：编程、旅行、英语学习');
  if (!name) return;
  const d = await api('/api/collections', { method: 'POST', body: JSON.stringify({ name }) });
  toast(`已创建「${d.name}」`);
  await loadLibrary();
}

async function deleteCollection(name) {
  const ok = confirm(`删除收藏夹「${name}」？\n里面的笔记会退回「默认收藏夹」，笔记本身不会被删除。`);
  if (!ok) return;
  const d = await api(`/api/collections?name=${encodeURIComponent(name)}`, { method: 'DELETE' });
  toast(`已删除「${name}」${d.moved ? `，${d.moved} 篇退回默认收藏夹` : ''}`);
  await loadLibrary();
}

let moveIds = [];

function preferredTarget() {
  const names = libCollections.map((c) => c.name);
  return names.find((n) => n !== currentCollection) || currentCollection || '默认收藏夹';
}

function renderMoveList(selected) {
  const names = libCollections.map((c) => c.name);
  if (!names.includes('默认收藏夹')) names.unshift('默认收藏夹');
  const counts = Object.fromEntries(libCollections.map((c) => [c.name, c.count]));
  $('moveList').innerHTML = names.map((n) => `
    <label class="move-item">
      <input type="radio" name="moveTo" value="${esc(n)}" ${n === selected ? 'checked' : ''}>
      <span class="move-name">${esc(n)}</span>
      <span class="move-count">${counts[n] ?? 0} 篇</span>
    </label>`).join('');
}

function openMoveDialog(ids) {
  if (!ids.length) return;
  moveIds = ids;
  $('moveSub').textContent = `把 ${ids.length} 篇笔记移动到：`;
  renderMoveList(preferredTarget());
  $('moveDlg').showModal();
}

async function newCollectionInMove() {
  const name = await askName('新建收藏夹', '名字，比如：编程');
  if (!name) return;
  const d = await api('/api/collections', { method: 'POST', body: JSON.stringify({ name }) });
  const j = await api('/api/library');
  libCollections = j.collections || [];
  renderMoveList(d.name);
  toast(`已创建「${d.name}」，选好就可以移动`);
}

async function doMove() {
  const sel = document.querySelector('input[name="moveTo"]:checked');
  if (!sel) { toast('先选一个收藏夹', true); return; }
  const to = sel.value;
  if (to === currentCollection) { toast('已经在这个收藏夹里了', true); return; }
  const d = await api('/api/collections/move', {
    method: 'POST',
    body: JSON.stringify({ ids: moveIds, to }),
  });
  $('moveDlg').close();
  toast(`已移动 ${d.moved.length} 篇到「${d.to}」${d.failed.length ? `，${d.failed.length} 篇失败` : ''}`);
  await loadLibrary($('kbSearch').value.trim() || undefined);
}

/* 输入名字的通用小弹窗，返回 Promise<string|null> */
function askName(title, placeholder, initial = '') {
  return new Promise((resolve) => {
    const dlg = $('promptDlg');
    const input = $('promptInput');
    $('promptTitle').textContent = title;
    input.placeholder = placeholder;
    input.value = initial;
    $('promptHint').textContent = '';
    let done = false;
    const finish = (val) => { if (!done) { done = true; resolve(val); } };
    dlg.onclose = () => finish(null);
    $('promptOk').onclick = () => { const v = input.value.trim(); if (!v) return; finish(v); dlg.close(); };
    $('promptCancel').onclick = () => { finish(null); dlg.close(); };
    input.onkeydown = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); const v = input.value.trim(); if (!v) return; finish(v); dlg.close(); }
    };
    dlg.showModal();
    setTimeout(() => input.focus(), 30);
  });
}

function collectionCard(c) {
  // 大类和视频卡片用同一套骨架，尺寸自然一致；只多一个"收藏夹"角标做区分
  const cover = (c.cover && c.cover_id)
    ? `<img src="/api/library/${encodeURIComponent(c.cover_id)}/file/${c.cover}" alt="" loading="lazy">`
    : '<span class="collection-empty">📁</span>';
  const when = c.mtime ? new Date(c.mtime * 1000).toLocaleDateString('zh-CN') : '';
  const del = c.is_default ? ''
    : `<button class="coll-del" data-del="${esc(c.name)}" title="删除这个收藏夹">✕</button>`;
  return `
    <article class="note-card collection-card" data-collection="${esc(c.name)}">
      <div class="note-cover">
        ${cover}
        <span class="collection-badge">收藏夹</span>
        ${del}
      </div>
      <div class="note-body">
        <h3 class="note-title">${esc(c.name)}</h3>
        <div class="note-meta">
          <span>${c.count} 篇</span>
          <span>${fmtNum(c.words)} 字</span>
          ${c.series_count ? `<span>${c.series_count} 个合集</span>` : ''}
          ${when ? `<span>${when}</span>` : ''}
        </div>
        <p class="collection-latest">${c.latest ? `最近：${esc(c.latest)}` : '还没有笔记'}</p>
      </div>
    </article>`;
}

/* 合集卡：和收藏夹卡同一套骨架，点进去按集数顺序列出全部分集 */
function seriesCard(g) {
  const cover = (g.cover && g.cover_id)
    ? `<img src="/api/library/${encodeURIComponent(g.cover_id)}/file/${g.cover}" alt="" loading="lazy">`
    : '<span class="collection-empty">📚</span>';
  const range = (g.first_order && g.last_order && g.first_order !== g.last_order)
    ? `<span>第 ${g.first_order}–${g.last_order} 集</span>` : '';
  return `
    <article class="note-card collection-card series-card"
             data-series="${esc(g.key)}" data-series-title="${esc(g.title)}">
      <div class="note-cover">
        ${cover}
        <span class="collection-badge series-badge">合集 · ${g.count} 集</span>
      </div>
      <div class="note-body">
        <h3 class="note-title">${esc(g.title)}</h3>
        <div class="note-meta">
          <span>${g.count} 集</span>
          <span>${g.images} 张画面</span>
          ${range}
        </div>
        <p class="collection-latest">${g.latest ? `最近：${esc(g.latest)}` : '还没有笔记'}</p>
      </div>
    </article>`;
}

/* ==================== 阅读器 ==================== */
let currentNoteId = '';

let currentNote = null;      // { title, markdown, lecture }
let readerTab = 'note';      // note | lecture

async function openNote(id) {
  currentNoteId = id;
  try {
    const d = await api(`/api/library/${encodeURIComponent(id)}`);
    currentNote = d;
    readerTab = 'note';
    $('readerTitle').textContent = d.title;
    renderReaderTab();
    $('reader').hidden = false;
    $('reader').scrollTop = 0;
  } catch (e) {
    toast(e.message, true);
  }
}

function hasLecture() {
  return !!(currentNote && currentNote.lecture && currentNote.lecture.trim());
}

/* 笔记 / 讲义两个标签页共用一块阅读区：讲义是独立文件，切换时重新渲染即可 */
function renderReaderTab() {
  if (!currentNote) return;
  document.querySelectorAll('.reader-tab').forEach((t) => {
    const isLecture = t.dataset.tab === 'lecture';
    t.classList.toggle('active', t.dataset.tab === readerTab);
    t.classList.toggle('has-dot', isLecture && hasLecture());
  });
  const wantLecture = readerTab === 'lecture';
  $('btnMakeLecture').hidden = !(wantLecture && !hasLecture());

  if (wantLecture && !hasLecture()) {
    $('readerBody').innerHTML = `
      <div class="lecture-empty">
        <div class="le-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5A1.5 1.5 0 015.5 4H11v16H5.5A1.5 1.5 0 014 18.5z"/><path d="M20 5.5A1.5 1.5 0 0018.5 4H13v16h5.5a1.5 1.5 0 001.5-1.5z"/></svg></div>
        <h3>这篇笔记还没有讲义</h3>
        <p>讲义会把笔记里的知识重新讲一遍：为什么需要它 → 定义 → 推导 → 例题 → 易错 → 自测。<br>
           只读现有笔记，不重跑视频，大约几十秒，消耗一次文本模型额度。</p>
        <button class="btn primary" id="btnMakeLectureInline">用当前模型生成讲义</button>
      </div>`;
    const inline = $('btnMakeLectureInline');
    if (inline) inline.onclick = makeLecture;
  } else {
    $('readerBody').innerHTML = renderMarkdown(
      wantLecture ? currentNote.lecture : currentNote.markdown, currentNoteId);
    // 生词链接只在笔记正文里有意义（讲义的正文走同一套渲染，但没有词表数据）
    if (!wantLecture) bindWordLinks($('readerBody'));
  }
  $('reader').scrollTop = 0;
}

async function makeLecture() {
  if (!currentNoteId) return;
  const btn = $('btnMakeLecture');
  const inline = $('btnMakeLectureInline');
  const targets = [btn, inline].filter(Boolean);
  const old = btn ? btn.textContent : '';
  targets.forEach((b) => { b.disabled = true; if (b === btn) b.textContent = '生成中…'; });
  if (inline) inline.textContent = '正在生成，请稍候…';
  try {
    const d = await api(`/api/library/${encodeURIComponent(currentNoteId)}/lecture`, { method: 'POST' });
    const fresh = await api(`/api/library/${encodeURIComponent(currentNoteId)}`);
    currentNote = fresh;
    readerTab = 'lecture';
    renderReaderTab();
    toast(`讲义已生成（约 ${d.chars} 字）`);
    loadLibrary().catch(() => {});   // 顺手刷新列表上的讲义角标
  } catch (e) {
    toast(e.message, true);
    if (btn) btn.textContent = old;
    if (inline) inline.textContent = '用当前模型生成讲义';
  } finally {
    targets.forEach((b) => { b.disabled = false; });
    if (btn && !btn.hidden) btn.textContent = '生成讲义';
  }
}

/* ==================== 概念索引 ====================
   一篇笔记只能告诉你"这一集讲了什么"，概念视图回答的是
   "这个概念一共在哪几集讲过、各集是怎么解释的"。 */
let conceptList = [];
let conceptKey = '';
let conceptFilter = 'all';
let conceptGroupMode = 'topic';
try { conceptGroupMode = localStorage.getItem('bn.conceptGroup') || 'topic'; } catch (e) { /* 隐私模式 */ }
let conceptCollapsed = new Set();   // 用户手动收起的组

let conceptHidden = 0;

async function loadConcepts(autoTidy = true) {
  let d = await api('/api/concepts');
  // 上级被重新对齐过（模型写的父节点在列表里不存在）：先把修正写回缓存，
  // 否则每次进这一页都要重算一遍，而且「整理」按钮看到的还是旧层级。
  if (d.stale > 0 && autoTidy) {
    try {
      await api('/api/concepts/tidy', { method: 'POST' });
      d = await api('/api/concepts');
    } catch (e) { /* 整理失败不影响浏览 */ }
  }
  // 第一次进这一页（或有新概念）时，让模型把词条判一遍：景点/地名/鸡汤要挡掉。
  // 结果存在 concept_tags.json 里，之后不再调用；没有 Key 时后端退回本地规则。
  if (d.untagged > 0) {
    $('conceptSub').textContent = `正在识别景点、鸡汤等非知识条目（剩 ${d.untagged} 个）…`;
    try {
      await api('/api/concepts/classify', { method: 'POST' });
      d = await api('/api/concepts');
    } catch (e) { /* 判别失败不影响浏览，继续用已有分类 */ }
  }
  conceptList = d.items || [];
  conceptHidden = d.hidden || 0;
  const cardOnly = conceptList.filter((c) => !c.note_count && c.cards);
  const totalCards = conceptList.reduce((s, c) => s + (c.cards || 0), 0);
  const reviewable = d.reviewable || 0;
  $('conceptSub').textContent = conceptList.length
    ? `共 ${conceptList.length} 个 · 已隐藏 ${conceptHidden} 个非知识条目`
      + `${totalCards ? ` · ${totalCards} 张卡片` : ''}`
      + `${reviewable ? ` · ${reviewable} 条可让 AI 复核` : ''}`
    : '还没有概念：先生成笔记（笔记里会有「关键概念」表）';
  renderConceptList();
  if (conceptList.length) {
    // 默认选第一个有笔记的概念；卡片条目排在后面，别一进来就落在那里
    const first = conceptList.find((c) => c.key === conceptKey)
      || conceptList.find((c) => c.note_count > 0)
      || conceptList[0];
    await selectConcept(first.key);
  } else {
    $('conceptDetail').innerHTML = '<p class="empty">还没有可索引的概念</p>';
  }
}

function conceptItemHtml(c, isChild) {
  // 子项缩进后位置已经很紧，只显示"有几张卡片"，不重复"1 篇"
  const meta = isChild
    ? (c.cards ? `${c.cards} 卡` : '')
    : [c.note_count > 0 ? `${c.note_count} 篇` : '', c.cards ? `${c.cards} 卡` : '']
      .filter(Boolean).join(' · ');
  return `<button class="concept-item${isChild ? ' child' : ''}${c.key === conceptKey ? ' active' : ''}" data-key="${esc(c.key)}">
    <span class="ci-name">${esc(c.name)}</span>
    <span class="ci-meta">${meta}</span>
  </button>`;
}

function byWeight(a, b) {
  return (b.note_count - a.note_count) || (b.cards - a.cards) || a.name.localeCompare(b.name);
}

/* 一个概念往上的祖先链。带环保护——坏数据不能让页面卡死。 */
function ancestorChain(c, byKey) {
  const chain = [c];
  let cur = c;
  let guard = 0;
  while (cur && cur.parent_key && byKey.has(cur.parent_key) && guard++ < 8) {
    const up = byKey.get(cur.parent_key);
    if (chain.includes(up)) break;
    chain.push(up);
    cur = up;
  }
  return chain;
}

/* 分组：按主题时，一条线上所有下级都收进最顶上那个概念；按视频时按来源笔记分。
   只有一条的组直接平铺——"一个标题配一个孩子"纯属噪声。
   （「整数规划」→「整数规划判定问题」→「可满足到可行解」这样两层的，
    在窄侧栏里再往下缩进就没法读了，所以统一挂在最顶层，按层级排序。） */
function groupConcepts(items) {
  if (conceptGroupMode === 'note') {
    const groups = new Map();
    for (const c of items) {
      const name = c.primary_note || '仅来自卡片';
      const key = `n:${name}`;
      if (!groups.has(key)) groups.set(key, { key, name, items: [] });
      groups.get(key).items.push(c);
    }
    const list = [...groups.values()];
    for (const g of list) g.items.sort(byWeight);
    list.sort((a, b) => b.items.length - a.items.length || a.name.localeCompare(b.name));
    return list;
  }

  const byKey = new Map(items.map((c) => [c.key, c]));
  const groups = new Map();
  const depth = new Map();
  for (const c of items) {
    const chain = ancestorChain(c, byKey);
    const root = chain[chain.length - 1];
    depth.set(c.key, chain.length - 1);
    if (!groups.has(root.key)) groups.set(root.key, { key: root.key, name: root.name, items: [] });
    groups.get(root.key).items.push(c);
  }
  const list = [...groups.values()];
  for (const g of list) {
    g.items.sort((a, b) => ((depth.get(a.key) || 0) - (depth.get(b.key) || 0)) || byWeight(a, b));
  }
  list.sort((a, b) => b.items.length - a.items.length || a.name.localeCompare(b.name));
  return list;
}

function renderConceptList() {
  const needle = ($('conceptSearch').value || '').trim().toLowerCase();
  let items = conceptList;
  // 默认挡掉景点/鸡汤/常识操作；取消勾选可看全部（也能在这里发现误判）
  if ($('conceptKnowledgeOnly').checked) {
    items = items.filter((c) => !['place', 'soup', 'trivial', 'step', 'relation', 'artifact'].includes(c.cat));
  }
  if (conceptFilter === 'multi') items = items.filter((c) => c.note_count > 1);
  if (conceptFilter === 'carded') items = items.filter((c) => c.cards > 0);
  // 只来自卡片：笔记没写进「关键概念」表，但生成的卡片标了这个知识点
  if (conceptFilter === 'cardonly') items = items.filter((c) => !c.note_count && c.cards > 0);
  if (needle) items = items.filter((c) => c.name.toLowerCase().includes(needle));
  if (!items.length) {
    $('conceptList').innerHTML = '<p class="empty">没有匹配的概念</p>';
    return;
  }

  // 搜索时平铺，分组反而碍事
  if (needle) {
    $('conceptList').innerHTML = [...items].sort(byWeight)
      .map((c) => conceptItemHtml(c, false)).join('');
    return;
  }

  const groups = groupConcepts(items);
  $('conceptList').innerHTML = groups.map((g) => {
    const parent = g.items.find((c) => c.key === g.key);   // 主题模式下组长本身就是概念
    const children = g.items.filter((c) => c.key !== g.key);
    if (!children.length) return conceptItemHtml(g.items[0], false);

    const open = !conceptCollapsed.has(g.key);
    let gmeta = parent
      ? [parent.cards ? `${parent.cards} 卡` : '', parent.note_count > 1 ? `${parent.note_count} 篇` : '']
        .filter(Boolean).join(' · ')
      : '';
    if (!gmeta) gmeta = `${children.length} 项`;   // 方法本身没有卡片时，退而显示挂了几条
    // 点箭头只收起；点名字看组长详情——组头不再重复列出自己
    return `<div class="cg">
      <div class="cg-head${parent && parent.key === conceptKey ? ' active' : ''}">
        <button class="cg-caret" data-toggle="${esc(g.key)}" title="展开 / 收起">${open ? '▾' : '▸'}</button>
        <button class="cg-name${parent ? '' : ' plain'}"${parent ? ` data-key="${esc(g.key)}"` : ''}>${esc(g.name)}</button>
        <em>${gmeta}</em>
      </div>
      <div class="cg-items"${open ? '' : ' hidden'}>
        ${children.map((c) => conceptItemHtml(c, true)).join('')}
      </div>
    </div>`;
  }).join('');
}

/* 卡片列表里的紧凑预览：矩阵这类多行公式在窄行里会被裁断，统一压成「公式」占位。
   想看完整题目去「复习」页。 */
function plainPreview(text) {
  return String(text || '')
    // 块级公式（矩阵那种）单独占一行，说"（公式）"；行内公式夹在句子里，
    // 换成省略号比"（公式）"更像话："对矩阵 ⋯，第一列下方化为 0 后…"
    .replace(/\$\$[\s\S]*?\$\$/g, '（公式）')
    .replace(/\$[^$\n]+?\$/g, '⋯')
    .replace(/\*\*/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/* 手动纠正分类：以后自动判别不会再覆盖它（后端记 src=manual） */
async function retagConcept(name, cat) {
  try {
    await api('/api/concepts/tag', {
      method: 'POST',
      body: JSON.stringify({ name, cat }),
    });
    toast(cat === 'knowledge' ? '已标记为知识点' : '已标记为非知识点');
    await loadConcepts();
    await selectConcept(conceptKey);
  } catch (e) {
    toast(e.message, true);
  }
}

/* 整理：把「XX 的优势」这类派生说法并回主体、层级重挂到真实存在的概念上。
   规则认名字形态，认不出的（"变换顺序"这种换角度命名的）再让模型复核一遍——
   模型只能"并进已有概念 / 保持独立"，不能发明概念、也不能删东西。 */
async function tidyConcepts() {
  try {
    const r = await api('/api/concepts/tidy?ai=1', { method: 'POST' });
    const parts = [];
    if ((r.merged || []).length) parts.push(`并回 ${r.merged.length} 个派生说法`);
    if (r.reparented) parts.push(`重挂 ${r.reparented} 条层级`);
    if ((r.hidden || []).length) parts.push(`隐藏 ${r.hidden.length} 个空泛词`);
    const ai = r.ai || {};
    if (ai.reason === 'no_key') {
      parts.push('未配置模型，只做了规则整理');
    } else if ((ai.merged || []).length) {
      const head = ai.merged.slice(0, 2).map((m) => `${m[0]}→${m[1]}`).join('、');
      parts.push(`AI 复核并回 ${ai.merged.length} 条（${head}${ai.merged.length > 2 ? '…' : ''}）`);
    } else if (ai.reason === 'ok' || ai.reason === 'nothing') {
      parts.push(ai.checked ? `AI 复核了 ${ai.checked} 条，没发现该合并的` : 'AI 复核：没有可疑条目');
    }
    toast(parts.length ? parts.join('，') : '已经很整齐了');
    conceptCollapsed = new Set();
    await loadConcepts(false);
    if (conceptKey) await selectConcept(conceptKey);
  } catch (e) {
    toast(e.message, true);
  }
}

function switchGroupMode(mode) {
  conceptGroupMode = mode;
  try { localStorage.setItem('bn.conceptGroup', mode); } catch (e) { /* 隐私模式 */ }
  conceptCollapsed = new Set();
  if (mode === 'note') {
    // 按视频分组默认收起，否则一屏几十条
    for (const c of conceptList) conceptCollapsed.add(`n:${c.primary_note || '仅来自卡片'}`);
    const active = conceptList.find((c) => c.key === conceptKey);
    if (active) conceptCollapsed.delete(`n:${active.primary_note || '仅来自卡片'}`);
  }
  renderConceptList();
}

/* 选中一条时，把它所在的那一组展开。
   组是按"最顶层概念"分的，所以这里要沿着祖先链找到同一个组键。 */
function rootKeyOf(item) {
  const byKey = new Map(conceptList.map((c) => [c.key, c]));
  const chain = ancestorChain(item, byKey);
  return chain[chain.length - 1].key;
}

async function selectConcept(key) {
  conceptKey = key;
  const item = conceptList.find((c) => c.key === key);
  if (item) {
    if (conceptGroupMode === 'topic') conceptCollapsed.delete(rootKeyOf(item));
    if (conceptGroupMode === 'note') {
      conceptCollapsed.delete(`n:${item.primary_note || '仅来自卡片'}`);
    }
  }
  renderConceptList();
  const box = $('conceptDetail');
  box.innerHTML = '<p class="empty">加载中…</p>';
  try {
    const d = await api(`/api/concepts/detail?name=${encodeURIComponent(key)}`);
    const notes = d.notes || [];
    const cards = d.cards || [];
    const cat = d.cat || 'other';
    const catText = {
      knowledge: '知识点', place: '景点/地点', soup: '鸡汤/口号',
      trivial: '常识操作', step: '实例步骤', relation: '关系判断',
      artifact: '变量/占位符', other: '未归类',
    }[cat] || '未归类';
    const hide = ['place', 'soup', 'trivial', 'step', 'relation', 'artifact'].includes(cat);
    const fixCat = hide ? 'knowledge' : 'trivial';
    box.innerHTML = `
      <div class="cd-head">
        <h2 class="cd-name">${esc(d.name)}</h2>
        <div class="cd-meta">
          出现在 ${notes.length} 篇笔记${cards.length ? ` · ${cards.length} 张卡片` : ''}
          ${d.parent ? ` · 属于「${esc(d.parent)}」` : ''}
          ${(d.aliases || []).length ? ` · 也叫「${esc(d.aliases.join('、'))}」` : ''}
          <span class="cd-cat cat-${cat}">${catText}</span>
          <button class="cd-fix" data-retag="${esc(d.name)}" data-cat="${fixCat}">
            ${hide ? '其实是知识点' : '不是知识点'}</button>
        </div>
      </div>
      <div class="cd-section">
        <div class="cd-label">各集怎么讲</div>
        ${notes.map((n) => `
          <div class="cd-note" data-note="${esc(n.id)}" title="打开这篇笔记">
            <div class="cd-note-title">${esc(n.title)}</div>
            <div class="cd-note-desc">${
              n.explanation ? renderMarkdown(n.explanation, n.id) : '（这篇笔记没有写解释）'}</div>
            <div class="cd-note-foot">${esc(n.uploader || '')}${n.collection ? ` · ${esc(n.collection)}` : ''}</div>
          </div>`).join('') || `<p class="hint">这个概念还没出现在任何笔记的「关键概念」表里，
            只来自生成的卡片。卡片把它标成了同一个知识点，所以这里也能看到。</p>`}
      </div>
      ${cards.length ? `
      <div class="cd-section">
        <div class="cd-label">相关卡片（${cards.length}）</div>
        ${cards.slice(0, 6).map((c) => `
          <div class="cd-card">
            <span class="cd-card-kind">${esc(c.kind || '概念')}</span>
            <span class="cd-card-front">${esc(plainPreview(c.front))}</span>
          </div>`).join('')}
        ${cards.length > 6 ? `<div class="hint">还有 ${cards.length - 6} 张，去「复习」里看</div>` : ''}
      </div>` : ''}`;
    box.querySelectorAll('[data-note]').forEach((el) => {
      el.onclick = () => openNote(el.dataset.note);
    });
    box.querySelectorAll('[data-retag]').forEach((btn) => {
      btn.onclick = () => retagConcept(btn.dataset.retag, btn.dataset.cat);
    });
  } catch (e) {
    box.innerHTML = `<p class="empty">${esc(e.message)}</p>`;
  }
}


/* ==================== 自测卷 ====================
   出题 → 做题 → 交卷判分 → 错题变卡片。答题状态全在前端，交卷才落一次库。
   手机上只点选项，所以单选题做成整块可点的大按钮。 */
let quizRun = null;          // {quiz, idx, answers}
let quizLastResult = null;   // 最近一次判分结果，供"错题变卡片"用

async function loadQuiz() {
  const [lib, d] = await Promise.all([api('/api/library'), api('/api/quiz')]);
  renderQuizScope(lib);
  renderQuizList(d.items || []);
}

function renderQuizScope(lib) {
  const sel = $('quizScope');
  const keep = sel.value;
  const notes = (lib.items || []).slice(0, 300);
  const cols = (lib.collections || []).filter((c) => c.name && c.count);
  const groups = [];
  if (cols.length) {
    groups.push(`<optgroup label="整个收藏夹">${cols.map((c) =>
      `<option value="c:${esc(c.name)}">${esc(c.name)} · ${c.count} 篇</option>`).join('')}</optgroup>`);
  }
  groups.push(`<optgroup label="单篇笔记">${notes.map((n) =>
    `<option value="n:${esc(n.id)}">${esc(n.title || n.id)}</option>`).join('')}</optgroup>`);
  sel.innerHTML = groups.join('');
  if (keep) sel.value = keep;
}

function renderQuizList(items) {
  const box = $('quizList');
  if (!items.length) {
    box.innerHTML = '<p class="empty">还没有卷子，上面选个范围出一套</p>';
    return;
  }
  box.innerHTML = items.map((q) => {
    const last = q.last && q.last.total ? `${q.last.score}/${q.last.total}` : '未做';
    return `<div class="quiz-row" data-quiz="${esc(q.id)}">
      <div class="qr-main">
        <div class="qr-title">${esc(q.title || '自测卷')}</div>
        <div class="qr-meta">${q.count} 题 · ${(q.kinds || []).map((k) => ({
          single: '单选', multi: '多选', judge: '判断', blank: '填空', short: '简答',
        }[k] || k)).join('/')} · 上次 ${esc(last)}</div>
      </div>
      <button class="btn ghost small" data-act="open">做题</button>
      <button class="btn ghost small" data-act="pdf" title="导成 PDF 打印，答案附在最后">PDF</button>
      <button class="btn ghost small" data-act="docx" title="导成 Word，方便改题或加批注">Word</button>
      <button class="btn ghost small" data-act="del" title="删除这份卷子">删除</button>
    </div>`;
  }).join('');
  box.querySelectorAll('.quiz-row').forEach((row) => {
    const id = row.dataset.quiz;
    row.querySelector('[data-act="open"]').onclick = () => openQuiz(id);
    row.querySelector('[data-act="pdf"]').onclick = (e) => exportQuiz(id, 'pdf', e.currentTarget);
    row.querySelector('[data-act="docx"]').onclick = (e) => exportQuiz(id, 'docx', e.currentTarget);
    row.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm('删掉这份卷子？（已转成卡片的错题不受影响）')) return;
      await api(`/api/quiz/${encodeURIComponent(id)}`, { method: 'DELETE' });
      loadQuiz();
    };
  });
}

/* 卷子导出：PDF 走浏览器无头打印（公式由 KaTeX 渲染），Word 走后端排版。
   PDF 要启一次浏览器，5~15 秒，所以按钮上给个"生成中"。 */
async function exportQuiz(id, fmt, btn) {
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  try {
    const resp = await fetch(`/api/quiz/${encodeURIComponent(id)}/export?fmt=${fmt}`);
    if (!resp.ok) {
      let detail = `导出失败（${resp.status}）`;
      try { detail = (await resp.json()).detail || detail; } catch (e) { /* 非 JSON 用默认文案 */ }
      throw new Error(detail);
    }
    const blob = await resp.blob();
    const disp = resp.headers.get('Content-Disposition') || '';
    const star = /filename\*=UTF-8''([^;]+)/i.exec(disp);
    const plain = /filename="?([^";]+)"?/i.exec(disp);
    const name = star ? decodeURIComponent(star[1]) : (plain ? plain[1] : `自测卷.${fmt}`);
    const href = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = href;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 4000);
    toast(`已导出 ${name}（${(blob.size / 1024 / 1024).toFixed(2)} MB）`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

async function makeQuiz() {
  const value = $('quizScope').value;
  const kinds = [...document.querySelectorAll('#quizKinds input:checked')].map((x) => x.value);
  if (!kinds.length) { toast('至少选一种题型', true); return; }
  if (!value) { toast('先选范围', true); return; }
  const body = { kinds, count: Number($('quizCount').value) };
  if (value.startsWith('c:')) body.collection = value.slice(2);
  else body.note_ids = [value.slice(2)];

  const btn = $('btnQuizGen');
  btn.disabled = true;
  $('quizGenState').textContent = '出题中…模型要把笔记读一遍，十几秒';
  try {
    const quiz = await api('/api/quiz/generate', { method: 'POST', body: JSON.stringify(body) });
    $('quizGenState').textContent = `出了 ${quiz.count} 题`;
    startQuiz(quiz);
  } catch (e) {
    $('quizGenState').textContent = '';
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function openQuiz(id) {
  try {
    const quiz = await api(`/api/quiz/${encodeURIComponent(id)}`);
    startQuiz(quiz);
  } catch (e) { toast(e.message, true); }
}

function startQuiz(quiz) {
  quizRun = { quiz, idx: 0, answers: {} };
  quizLastResult = null;
  $('quizGenPanel').hidden = true;
  $('quizListPanel').hidden = true;
  $('quizResultPanel').hidden = true;
  $('quizRunPanel').hidden = false;
  $('quizRunTitle').textContent = quiz.title || '';
  renderQuestion();
  $('quizRunPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function quitQuiz() {
  quizRun = null;
  $('quizRunPanel').hidden = true;
  $('quizResultPanel').hidden = true;
  $('quizGenPanel').hidden = false;
  $('quizListPanel').hidden = false;
  loadQuiz().catch(() => {});
}

const QUIZ_KIND_LABEL = { single: '单选', multi: '多选', judge: '判断', blank: '填空', short: '简答' };

/* 选项/答案这种"行内"文本：走一遍渲染拿公式，但把外层 <p> 摘掉，
   否则 <p> 塞进 span 里会把一行撑成好几行。 */
function quizInline(text) {
  return renderMarkdown(String(text || ''))
    .replace(/^<p>/, '').replace(/<\/p>\s*$/, '');
}

function renderQuestion() {
  if (!quizRun) return;
  const { quiz, idx } = quizRun;
  const q = quiz.questions[idx];
  const total = quiz.questions.length;
  $('quizProgress').textContent = `${idx + 1} / ${total}`;
  $('quizBar').style.width = `${((idx + 1) / total) * 100}%`;
  $('quizStem').innerHTML = `<span class="quiz-kind">${QUIZ_KIND_LABEL[q.kind] || ''}</span>
    <div class="quiz-stem-text">${renderMarkdown(q.stem)}</div>`;
  const box = $('quizOpts');
  const given = quizRun.answers[q.id];

  if (q.kind === 'single' || q.kind === 'multi') {
    box.innerHTML = (q.options || []).map((opt, i) => {
      const sel = Array.isArray(given) && given.includes(i);
      return `<button class="quiz-opt${sel ? ' sel' : ''}" data-i="${i}">
        <span class="quiz-opt-k">${'ABCDEF'[i] || i + 1}</span>
        <span class="quiz-opt-t">${quizInline(opt)}</span></button>`;
    }).join('');
    box.querySelectorAll('.quiz-opt').forEach((btn) => {
      btn.onclick = () => {
        const i = Number(btn.dataset.i);
        const cur = Array.isArray(quizRun.answers[q.id]) ? [...quizRun.answers[q.id]] : [];
        if (q.kind === 'single') quizRun.answers[q.id] = [i];
        else if (cur.includes(i)) quizRun.answers[q.id] = cur.filter((x) => x !== i);
        else quizRun.answers[q.id] = [...cur, i];
        renderQuestion();
      };
    });
  } else if (q.kind === 'judge') {
    box.innerHTML = [true, false].map((v) => `
      <button class="quiz-opt${given === v ? ' sel' : ''}" data-v="${v}">
        <span class="quiz-opt-k">${v ? '✓' : '✕'}</span>
        <span class="quiz-opt-t">${v ? '正确' : '错误'}</span></button>`).join('');
    box.querySelectorAll('.quiz-opt').forEach((btn) => {
      btn.onclick = () => { quizRun.answers[q.id] = btn.dataset.v === 'true'; renderQuestion(); };
    });
  } else if (q.kind === 'short') {
    // 简答自评：先写几句自己的版本（可选），点开参考答案后自己判"答上/没答上"。
    // 判分只认自评结果（true/false），写的文字只是草稿，帮自己组织语言。
    const draft = (quizRun.drafts && quizRun.drafts[q.id]) || '';
    const shown = !!(quizRun.shown && quizRun.shown[q.id]) || given === true || given === false;
    box.innerHTML = `
      <textarea class="quiz-short" placeholder="先用自己的话写几句（可不写，直接自评）">${esc(draft)}</textarea>
      ${shown ? `
        <div class="quiz-ref">
          <em>参考答案</em>
          <div class="quiz-ref-text">${renderMarkdown(q.ref || '')}</div>
          <div class="quiz-self">
            <button class="btn small${given === true ? ' primary' : ''}" data-self="1">答上了</button>
            <button class="btn small${given === false ? ' primary' : ''}" data-self="0">没答上</button>
            ${given === true || given === false
              ? `<span class="quiz-self-note">已自评：${given ? '答上了' : '没答上'}（可改）</span>`
              : '<span class="quiz-self-note">对照完再点，点了就记分</span>'}
          </div>
        </div>` : `
        <div class="row" style="gap:8px">
          <button class="btn small quiz-reveal">看参考答案，自评</button>
          <span class="quiz-self-note">建议先在上面写一遍再对照</span>
        </div>`}`;
    const ta = box.querySelector('.quiz-short');
    ta.value = draft;
    ta.oninput = () => {
      quizRun.drafts = quizRun.drafts || {};
      quizRun.drafts[q.id] = ta.value;
    };
    const reveal = box.querySelector('.quiz-reveal');
    if (reveal) reveal.onclick = () => {
      quizRun.shown = quizRun.shown || {};
      quizRun.shown[q.id] = true;
      renderQuestion();
    };
    box.querySelectorAll('[data-self]').forEach((btn) => {
      btn.onclick = () => { quizRun.answers[q.id] = btn.dataset.self === '1'; renderQuestion(); };
    });
  } else {
    box.innerHTML = `<input type="text" class="quiz-blank" id="quizBlank"
      placeholder="填答案（1~8 个字）" value="${esc(typeof given === 'string' ? given : '')}">`;
    $('quizBlank').oninput = (e) => { quizRun.answers[q.id] = e.target.value; };
    $('quizBlank').onkeydown = (e) => { if (e.key === 'Enter') gotoQuestion(idx + 1); };
  }

  $('btnQuizPrev').disabled = idx === 0;
  $('btnQuizNext').disabled = idx >= total - 1;
  $('btnQuizSubmit').hidden = idx < total - 1;
}

function gotoQuestion(next) {
  if (!quizRun) return;
  const total = quizRun.quiz.questions.length;
  quizRun.idx = Math.max(0, Math.min(next, total - 1));
  renderQuestion();
}

async function submitQuiz() {
  if (!quizRun) return;
  const answered = Object.keys(quizRun.answers).length;
  const total = quizRun.quiz.questions.length;
  if (answered < total && !confirm(`还有 ${total - answered} 题没答，直接交卷？`)) return;
  try {
    const result = await api(`/api/quiz/${encodeURIComponent(quizRun.quiz.id)}/submit`, {
      method: 'POST', body: JSON.stringify({ answers: quizRun.answers }),
    });
    quizLastResult = result;
    showQuizResult(result);
  } catch (e) { toast(e.message, true); }
}

function showQuizResult(result) {
  const { score, total } = result;
  const pct = total ? Math.round((score / total) * 100) : 0;
  const word = pct >= 90 ? '稳了' : pct >= 70 ? '基本掌握' : pct >= 50 ? '还得再看看' : '回去过一遍笔记';
  $('quizScore').innerHTML = `<b>${score}</b> / ${total}
    <span class="quiz-score-note">${pct}% · ${word}</span>`;
  const rv = result.review || { questions: [] };
  $('quizResultList').innerHTML = rv.questions.map((q, i) => {
    const item = (result.items || []).find((x) => x.id === q.id) || {};
    return `<div class="quiz-res ${item.correct ? 'ok' : 'bad'}">
      <div class="quiz-res-head">
        <span class="quiz-res-no">${i + 1}</span>
        <span class="quiz-res-kind">${QUIZ_KIND_LABEL[q.kind] || ''}</span>
        <span class="quiz-res-mark">${item.correct ? '答对' : '答错'}</span>
        ${item.correct ? '' : `<label class="chip quiz-pick"><input type="checkbox" data-wrong="${esc(q.id)}" checked><span>收进卡片</span></label>`}
      </div>
      <div class="quiz-res-stem">${renderMarkdown(q.stem)}</div>
      <div class="quiz-res-line"><em>你的答案</em>${quizAnswerText(q, q.given)}</div>
      <div class="quiz-res-line"><em>正确答案</em>${quizInline(q.answer_text || '')}</div>
      ${q.explain ? `<div class="quiz-res-why">${renderMarkdown(q.explain)}</div>` : ''}
    </div>`;
  }).join('');
  const wrong = (result.items || []).filter((x) => !x.correct).length;
  $('btnQuizCards').disabled = wrong === 0;
  $('btnQuizCards').textContent = wrong ? `错题变卡片（${wrong}）` : '全对，没有错题';
  $('quizRunPanel').hidden = true;
  $('quizResultPanel').hidden = false;
  $('quizResultPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function quizAnswerText(q, given) {
  if (q.kind === 'single' || q.kind === 'multi') {
    const arr = Array.isArray(given) ? given : [];
    if (!arr.length) return '<i class="quiz-none">没答</i>';
    return arr.map((i) => `${'ABCDEF'[i] || i + 1}. ${quizInline((q.options || [])[i] || '')}`).join('；');
  }
  if (q.kind === 'judge') {
    if (given === true) return '正确';
    if (given === false) return '错误';
    return '<i class="quiz-none">没答</i>';
  }
  if (q.kind === 'short') {
    if (given === true) return '自评：答上了';
    if (given === false) return '自评：没答上';
    return '<i class="quiz-none">没自评</i>';
  }
  return given ? esc(String(given)) : '<i class="quiz-none">没答</i>';
}

async function wrongToCards() {
  if (!quizRun || !quizLastResult) return;
  const ids = [...document.querySelectorAll('#quizResultList [data-wrong]')]
    .filter((x) => x.checked).map((x) => x.dataset.wrong);
  if (!ids.length) { toast('没勾选错题', true); return; }
  try {
    const r = await api(`/api/quiz/${encodeURIComponent(quizRun.quiz.id)}/cards`, {
      method: 'POST', body: JSON.stringify({ ids }),
    });
    toast(r.added ? `${r.added} 张卡片已进复习队列${r.dup ? `（${r.dup} 张重复已跳过）` : ''}` : '这些题已经有卡片了');
  } catch (e) { toast(e.message, true); }
}

/* ==================== 卷面（打印 / 导出 PDF 用） ====================
   后端导出 PDF 时让浏览器无头打开 /?printquiz=<id>，画的就是这里。
   和笔记打印不同：卷子要题号连续、选项对齐、答案统一放最后——先做题再对答案。 */
const QUIZ_SHEET_ORDER = ['single', 'multi', 'judge', 'blank', 'short'];
const QUIZ_SHEET_CN = {
  single: '单项选择题', multi: '多项选择题', judge: '判断题', blank: '填空题', short: '简答题',
};

async function renderQuizSheet(id) {
  const sheet = await api(`/api/quiz/${encodeURIComponent(id)}/sheet`);
  const qs = sheet.questions || [];
  let no = 0;
  const groups = [];
  for (const kind of QUIZ_SHEET_ORDER) {
    const bucket = qs.filter((q) => q.kind === kind);
    if (bucket.length) groups.push({ kind, items: bucket.map((q) => ({ no: ++no, q })) });
  }
  const rest = qs.filter((q) => !QUIZ_SHEET_ORDER.includes(q.kind));
  if (rest.length) groups.push({ kind: '', items: rest.map((q) => ({ no: ++no, q })) });
  const flat = groups.flatMap((g) => g.items);

  const meta = [`共 ${qs.length} 题`]
    .concat(groups.map((g) => `${QUIZ_SHEET_CN[g.kind] || '其他'} ${g.items.length}`));
  if (sheet.created) {
    const d = new Date(sheet.created * 1000);
    if (!Number.isNaN(d.getTime())) meta.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} 出题`);
  }

  const questionHtml = groups.map((g) => `
    <h2 class="sheet-h2">${esc(QUIZ_SHEET_CN[g.kind] || '其他题目')}</h2>
    ${g.items.map(({ no: n, q }) => `
      <div class="sheet-q">
        <div class="sheet-stem"><span class="sheet-no">${n}.</span>${quizInline(q.stem)}${q.kind === 'judge' ? '<span class="sheet-paren">（　　）</span>' : ''}</div>
        ${(q.kind === 'single' || q.kind === 'multi')
    ? `<div class="sheet-opts">${(q.options || []).map((o, i) => `
            <div class="sheet-opt"><em>${'ABCDEF'[i] || i + 1}.</em><span>${quizInline(o)}</span></div>`).join('')}
          </div>` : ''}
        ${q.kind === 'short' ? '<div class="sheet-write"><i></i><i></i><i></i></div>' : ''}
      </div>`).join('')}
  `).join('');

  const answerHtml = flat.map(({ no: n, q }) => `
    <div class="sheet-ans">
      <b>${n}.</b> <span>${quizInline(q.answer_text || '（未给出）')}</span>
      ${q.explain ? `<div class="sheet-why">${renderMarkdown(q.explain)}</div>` : ''}
    </div>`).join('');

  let host = document.getElementById('quizSheet');
  if (!host) {
    host = document.createElement('div');
    host.id = 'quizSheet';
    host.className = 'quiz-sheet';
    document.body.appendChild(host);
  }
  host.innerHTML = `
    <div class="sheet-head">
      <h1>${esc(sheet.title || '自测卷')}</h1>
      <div class="sheet-meta">${meta.map(esc).join(' · ')}</div>
      <div class="sheet-tip">先自己做，做完再翻到最后一页对答案</div>
    </div>
    ${questionHtml}
    <h2 class="sheet-h2 sheet-answers-h">参考答案</h2>
    ${answerHtml}
    <div class="sheet-foot">BiliNotes · 导出时间 ${new Date().toLocaleString('zh-CN')}</div>`;
  document.body.classList.add('sheet-mode');
}

function retryQuiz() {
  if (!quizRun) return;
  quizRun.idx = 0;
  quizRun.answers = {};
  quizRun.drafts = {};
  quizRun.shown = {};
  quizLastResult = null;
  $('quizResultPanel').hidden = true;
  $('quizRunPanel').hidden = false;
  renderQuestion();
}

/* ==================== 知识卡片 / 复习 ====================
   笔记是"读"的，卡片是"回忆"的。这一页做三件事：
   1) 按范围推送到期卡片（全部 / 某个收藏夹 / 某几篇笔记，范围记在本机）；
   2) 四档评分排期（SM-2 简化版，规则见右上角「排期规则」）；
   3) 按收藏夹 → 笔记浏览全部卡片，随时生成 / 删除 / 现在复习。
   卡片不存收藏夹（笔记搬家后会不准），分组每次按笔记当前归属现算。 */

const SCOPE_KEY = 'bn.reviewScope';
const REVIEW_KIND_KEY = 'bn.reviewKind';
let reviewScope = loadScope();
let reviewKind = (function () {
  const v = localStorage.getItem(REVIEW_KIND_KEY);
  return v === 'knowledge' || v === 'word' ? v : 'all';
})();
let reviewCram = false;          // true = 不看到期时间，把范围内全部过一遍
let reviewQueue = [];
let reviewIdx = 0;
let revealed = false;
let reviewStats = { total: 0, due: 0 };

let cardLibGroups = [];          // [{name, cards, due, notes:[{..., cards:[{card}]}]}]
let cardLibAll = [];             // 全部卡片，一次拉完（几百张也很快）
let scopeDraft = new Set();

function nowSec() { return Date.now() / 1000; }

function loadScope() {
  try {
    const raw = JSON.parse(localStorage.getItem(SCOPE_KEY) || 'null');
    if (raw && ['all', 'collection', 'notes'].includes(raw.mode)) {
      return {
        mode: raw.mode,
        collection: raw.collection || '',
        noteIds: Array.isArray(raw.noteIds) ? raw.noteIds : [],
      };
    }
  } catch (e) { /* 存的东西坏了就当没设过 */ }
  return { mode: 'all', collection: '', noteIds: [] };
}

function saveScope() {
  try { localStorage.setItem(SCOPE_KEY, JSON.stringify(reviewScope)); } catch (e) { /* 隐身模式 */ }
}

function scopeQuery() {
  if (reviewScope.mode === 'collection' && reviewScope.collection) {
    return `collection=${encodeURIComponent(reviewScope.collection)}`;
  }
  if (reviewScope.mode === 'notes' && reviewScope.noteIds.length) {
    return `note_ids=${reviewScope.noteIds.map(encodeURIComponent).join(',')}`;
  }
  return '';
}

function findNote(id) {
  for (const g of cardLibGroups) {
    for (const n of g.notes) if (n.id === id) return n;
  }
  return null;
}

function scopeLabel() {
  if (reviewScope.mode === 'collection') return reviewScope.collection || '全部笔记';
  if (reviewScope.mode === 'notes') {
    const n = reviewScope.noteIds.length;
    if (!n) return '全部笔记';
    if (n === 1) {
      const hit = findNote(reviewScope.noteIds[0]);
      return hit ? hit.title : '1 篇笔记';
    }
    return `自定义 ${n} 篇`;
  }
  return '全部笔记';
}

/* 到期时间说人话。后端已经给了评分预览，这里只用于卡片库列表 */
function dueText(ts) {
  const gap = Number(ts || 0) - nowSec();
  if (gap <= 0) return '待复习';
  if (gap < 3600) return `${Math.max(1, Math.round(gap / 60))} 分钟后`;
  if (gap < 86400) return `${Math.round(gap / 3600)} 小时后`;
  return `${Math.round(gap / 86400)} 天后`;
}

async function updateDueBadge() {
  try {
    const s = await api('/api/cards/stats');
    const badge = $('navDueBadge');
    badge.hidden = !s.due;
    badge.textContent = s.due > 99 ? '99+' : String(s.due);
  } catch (e) { /* 徽标失败不影响主流程 */ }
}

/* ---------------------------- 范围 ---------------------------- */
function renderScopeSelect() {
  const sel = $('scopeSelect');
  if (!sel) return;
  const parts = ['<option value="all">全部笔记</option>'];
  const withCards = cardLibGroups.filter((g) => g.cards > 0);
  if (withCards.length) {
    parts.push('<optgroup label="按收藏夹">');
    for (const g of withCards) {
      parts.push(`<option value="col:${esc(g.name)}">${esc(g.name)} · ${g.due} 待复习</option>`);
    }
    parts.push('</optgroup><optgroup label="按笔记">');
    for (const g of withCards) {
      for (const n of g.notes) {
        if (!n.cards.length) continue;
        const due = n.cards.filter((c) => Number(c.due || 0) <= nowSec()).length;
        parts.push(`<option value="note:${esc(n.id)}">${esc(n.title)} · ${due} 待复习</option>`);
      }
    }
    parts.push('</optgroup>');
  }

  let value = 'all';
  if (reviewScope.mode === 'collection' && reviewScope.collection) {
    value = `col:${reviewScope.collection}`;
  } else if (reviewScope.mode === 'notes') {
    if (reviewScope.noteIds.length === 1) {
      value = `note:${reviewScope.noteIds[0]}`;
    } else if (reviewScope.noteIds.length > 1) {
      parts.push(`<option value="custom">自定义（${reviewScope.noteIds.length} 篇）</option>`);
      value = 'custom';
    }
  }
  sel.innerHTML = parts.join('');
  sel.value = value;
  if (!sel.value) sel.value = 'all';      // 选中的笔记被删了之类，退回全部
}

function onScopeChange() {
  const v = $('scopeSelect').value;
  if (v === 'all') {
    reviewScope = { mode: 'all', collection: '', noteIds: [] };
  } else if (v.startsWith('col:')) {
    reviewScope = { mode: 'collection', collection: v.slice(4), noteIds: [] };
  } else if (v.startsWith('note:')) {
    reviewScope = { mode: 'notes', collection: '', noteIds: [v.slice(5)] };
  } else if (v === 'custom') {
    openScopeDlg();
    return;
  }
  reviewCram = false;
  saveScope();
  loadReview().catch((e) => toast(e.message, true));
}

/* 自定义范围：跨收藏夹勾选多篇笔记 */
function openScopeDlg() {
  scopeDraft = new Set(reviewScope.mode === 'notes' ? reviewScope.noteIds : []);
  $('scopeSearch').value = '';
  $('scopeState').textContent = '';
  renderScopeDlg();
  $('scopeDlg').showModal();
}

function renderScopeDlg() {
  const needle = ($('scopeSearch').value || '').trim().toLowerCase();
  const groups = cardLibGroups.filter((g) => g.cards > 0);
  if (!groups.length) {
    $('scopeGroups').innerHTML = '<p class="empty">还没有卡片，先在下面的列表里生成</p>';
    return;
  }
  const html = groups.map((g) => {
    const notes = g.notes.filter((n) => n.cards.length
      && (!needle || n.title.toLowerCase().includes(needle)));
    if (!notes.length) return '';
    const allOn = notes.every((n) => scopeDraft.has(n.id));
    return `<div class="sg">
      <label class="sg-head">
        <input type="checkbox" data-group="${esc(g.name)}"${allOn ? ' checked' : ''}>
        <span>${esc(g.name)}</span><em>${notes.length} 篇</em>
      </label>
      <div class="sg-notes">
        ${notes.map((n) => `
          <label class="sg-note">
            <input type="checkbox" data-note="${esc(n.id)}"${scopeDraft.has(n.id) ? ' checked' : ''}>
            <span class="sg-title">${esc(n.title)}</span>
            <em>${n.cards.length} 张${n.due ? ` · ${n.due} 待复习` : ''}</em>
          </label>`).join('')}
      </div>
    </div>`;
  }).join('');
  $('scopeGroups').innerHTML = html || '<p class="empty">没有匹配的笔记</p>';
  syncGroupBoxes();
  updateScopeDlgState();
}

/* 分组勾选框：全选打勾，选了一部分显示横线（半选）。
   勾单篇后也要重算，否则分组框会一直显示未选。 */
function syncGroupBoxes() {
  $('scopeGroups').querySelectorAll('input[data-group]').forEach((box) => {
    const g = cardLibGroups.find((x) => x.name === box.dataset.group);
    if (!g) return;
    const notes = g.notes.filter((n) => n.cards.length);
    const on = notes.filter((n) => scopeDraft.has(n.id)).length;
    box.checked = notes.length > 0 && on === notes.length;
    box.indeterminate = on > 0 && on < notes.length;
  });
}

function updateScopeDlgState() {
  let cards = 0;
  for (const g of cardLibGroups) {
    for (const n of g.notes) if (scopeDraft.has(n.id)) cards += n.cards.length;
  }
  $('scopeState').textContent = scopeDraft.size
    ? `已选 ${scopeDraft.size} 篇，共 ${cards} 张卡片`
    : '还没选笔记';
  $('scopeOk').disabled = scopeDraft.size === 0;
}

function toggleGroup(name, on) {
  const g = cardLibGroups.find((x) => x.name === name);
  if (!g) return;
  for (const n of g.notes) {
    if (!n.cards.length) continue;
    if (on) scopeDraft.add(n.id); else scopeDraft.delete(n.id);
  }
  renderScopeDlg();
}

function applyScopeDlg() {
  if (!scopeDraft.size) return;
  reviewScope = { mode: 'notes', collection: '', noteIds: [...scopeDraft] };
  reviewCram = false;
  saveScope();
  $('scopeDlg').close();
  loadReview().catch((e) => toast(e.message, true));
}

/* ---------------------------- 复习卡 ---------------------------- */
const isWordCard = (c) => String(c.note_id || '').startsWith('word:');
const filterQueueByKind = (cards) => (reviewKind === 'word'
  ? cards.filter(isWordCard)
  : reviewKind === 'knowledge' ? cards.filter((c) => !isWordCard(c)) : cards);

async function loadReview() {
  // 每次进复习页都刷一次卡片库：在别处删过笔记/生成过卡片，这里才不会是旧数据
  await loadCardLib().catch(() => {});
  renderScopeSelect();
  renderReviewKindSeg();
  const q = scopeQuery();
  const extra = `${q ? `&${q}` : ''}${reviewCram ? '&mode=all' : ''}`;
  // 整轮模式要一次取完，否则超过 30 张的那部分永远轮不到
  const limit = reviewCram ? 200 : 30;
  const d = await api(`/api/cards/due?limit=${limit}${extra}`);
  const allDue = d.cards || [];
  // 知识卡 / 单词卡分段：单词卡是 note_id 以 word: 开头的虚拟笔记
  const wordDue = allDue.filter(isWordCard);
  const knowDue = allDue.filter((c) => !isWordCard(c));
  reviewQueue = filterQueueByKind(allDue);
  reviewIdx = 0;
  revealed = false;
  const wordAll = cardLibAll.filter(isWordCard).length;
  const knowAll = cardLibAll.length - wordAll;
  reviewStats = {
    total: reviewKind === 'word' ? wordAll : reviewKind === 'knowledge' ? knowAll : (d.total || 0),
    due: reviewKind === 'word' ? wordDue.length : reviewKind === 'knowledge' ? knowDue.length : (d.total_due || 0),
  };
  const kindName = reviewKind === 'word' ? '单词卡' : reviewKind === 'knowledge' ? '知识卡' : '';
  const scopeText = scopeLabel();
  if (reviewStats.total === 0) {
    $('reviewSub').textContent = reviewKind === 'word'
      ? '还没有单词卡：在设置页「英语词库」点「生词变卡片」'
      : (scopeIsAll() && reviewKind === 'all'
        ? '把笔记里的知识拆成卡片，按遗忘曲线安排复习'
        : `${scopeText}：还没有卡片`);
  } else {
    $('reviewSub').textContent = kindName
      ? `${kindName} · ${scopeText}：共 ${reviewStats.total} 张，${reviewStats.due} 张待复习`
      : `${scopeText}：共 ${reviewStats.total} 张，${reviewStats.due} 张待复习`;
  }
  $('scopeHint').textContent = reviewCram ? '正在整轮复习（不看到期时间）' : '';
  renderReviewCard();
  updateDueBadge();
}

function renderReviewKindSeg() {
  const seg = $('reviewKindSeg');
  if (!seg) return;
  seg.querySelectorAll('.seg-item').forEach((b) => {
    b.classList.toggle('active', b.dataset.kind === reviewKind);
  });
}

function setReviewKind(kind) {
  if (kind === reviewKind) return;
  reviewKind = kind;
  try { localStorage.setItem(REVIEW_KIND_KEY, kind); } catch (e) { /* 隐身模式 */ }
  renderReviewKindSeg();
  loadReview().catch((e) => toast(e.message, true));
}

function scopeIsAll() { return reviewScope.mode === 'all'; }

function renderReviewCard() {
  const card = reviewQueue[reviewIdx];
  const has = !!card;
  $('flashcard').hidden = !has;
  $('flashcard').classList.remove('flipped');
  $('reviewActions').hidden = true;
  $('reviewEmpty').hidden = has;
  $('reviewProgress').textContent = has
    ? `第 ${reviewIdx + 1} / ${reviewQueue.length} 张` : '—';
  if (!has) {
    $('reviewSrc').textContent = '';
    renderReviewEmpty();
    return;
  }
  revealed = false;
  $('fcKind').textContent = card.kind || '概念';
  // 正面也要走 markdown 渲染，否则 $[A \mid E]$ 会原样露出来
  $('fcFront').innerHTML = renderMarkdown(card.front, card.note_id || '');
  $('fcBack').innerHTML = renderMarkdown(card.back, card.note_id || '');
  $('fcBack').hidden = true;
  $('fcHint').hidden = false;
  const preview = card.preview || {};
  document.querySelectorAll('.g-next').forEach((el) => {
    el.textContent = preview[el.dataset.next] || '';
  });
  $('reviewSrc').textContent = card.concept
    ? `${card.note_title || ''} · ${card.concept}` : (card.note_title || '');
}

function renderReviewEmpty() {
  const { total } = reviewStats;
  if (!total) {
    $('reTitle').textContent = '这个范围还没有卡片';
    $('reHint').textContent = '在下面「所有卡片」里找到笔记，点「生成卡片」。只读笔记，不重跑视频。';
    $('btnCram').hidden = true;
  } else if (reviewCram) {
    $('reTitle').textContent = '这一轮过完了';
    $('reHint').textContent = `范围内共 ${total} 张，刚才都过了一遍。`;
    $('btnCram').hidden = true;
  } else {
    $('reTitle').textContent = '这个范围现在没有到期的卡片';
    $('reHint').textContent = `范围内共 ${total} 张，都已排到以后。想现在练也可以整体过一遍。`;
    $('btnCram').hidden = false;
    $('btnCram').textContent = `把这 ${total} 张都过一遍`;
  }
}

function flipCard() {
  if (!reviewQueue[reviewIdx] || revealed) return;
  revealed = true;
  $('fcBack').hidden = false;
  $('fcHint').hidden = true;
  $('reviewActions').hidden = false;
  $('flashcard').classList.add('flipped');
}

async function gradeCard(grade) {
  const card = reviewQueue[reviewIdx];
  if (!card || !revealed) return;
  const btns = document.querySelectorAll('.review-actions .btn');
  btns.forEach((b) => { b.disabled = true; });
  try {
    await api(`/api/cards/${encodeURIComponent(card.id)}/review`, {
      method: 'POST',
      body: JSON.stringify({ grade }),
    });
  } catch (e) {
    toast(e.message, true);
  } finally {
    btns.forEach((b) => { b.disabled = false; });
  }

  reviewIdx += 1;
  if (reviewIdx >= reviewQueue.length) {
    if (reviewCram) {
      // 整轮模式不能重新拉：mode=all 会把刚评过的又取回来，变成死循环
      reviewQueue = [];
      reviewIdx = 0;
      renderReviewCard();
      toast('这一轮过完了');
    } else {
      const q = scopeQuery();
      const d = await api(`/api/cards/due?limit=30${q ? `&${q}` : ''}`)
        .catch(() => ({ cards: [], total_due: 0, total: reviewStats.total }));
      reviewQueue = filterQueueByKind(d.cards || []);
      reviewIdx = 0;
      reviewStats = { total: d.total || 0, due: d.total_due || 0 };
      if (!reviewQueue.length) toast('今天的卡片都过了一遍');
      renderReviewCard();
    }
  } else {
    renderReviewCard();
  }
  updateDueBadge();
}

/* ---------------------------- 所有卡片 ---------------------------- */
async function loadCardLib() {
  const [src, all] = await Promise.all([
    api('/api/cards/sources'),
    api('/api/cards/list?limit=1000'),
  ]);
  cardLibAll = all.cards || [];
  const byNote = {};
  for (const c of cardLibAll) {
    const key = c.note_id || '';
    (byNote[key] = byNote[key] || []).push(c);
  }

  const groups = {};
  const known = new Set();
  for (const n of (src.notes || [])) {
    known.add(n.id);
    const name = n.collection || '默认收藏夹';
    if (!groups[name]) groups[name] = { name, notes: [] };
    groups[name].notes.push({ ...n, cards: byNote[n.id] || [] });
  }
  // 笔记删了但卡片还在（历史数据）：单独一组，别让这些题凭空消失
  const orphans = Object.keys(byNote).filter((id) => id && !known.has(id));
  if (orphans.length) {
    groups['已删除的笔记'] = {
      name: '已删除的笔记',
      notes: orphans.map((id) => ({
        id, title: byNote[id][0].note_title || id, uploader: '',
        collection: '已删除的笔记', due: 0, mtime: 0, cards: byNote[id],
      })),
    };
  }

  cardLibGroups = Object.values(groups).map((g) => {
    g.cards = g.notes.reduce((s, n) => s + n.cards.length, 0);
    g.due = g.notes.reduce(
      (s, n) => s + n.cards.filter((c) => Number(c.due || 0) <= nowSec()).length, 0);
    g.notes.sort((a, b) => (b.due - a.due) || (b.cards.length - a.cards.length)
      || (b.mtime - a.mtime));
    return g;
  });
  // 有卡片的收藏夹在前，按卡片数降序
  cardLibGroups.sort((a, b) => (b.cards - a.cards) || a.name.localeCompare(b.name));

  $('libStat').textContent = cardLibAll.length
    ? `共 ${cardLibAll.length} 张，分布在 ${cardLibGroups.filter((g) => g.cards).length} 个收藏夹`
    : '';
  renderCardLib();
  renderScopeSelect();
}

function renderCardLib() {
  const needle = ($('cardLibSearch').value || '').trim().toLowerCase();
  const groups = cardLibGroups;
  if (!groups.length) {
    $('cardLib').innerHTML = '<p class="empty">还没有笔记，先去「采集收藏」导入一些视频</p>';
    return;
  }

  const html = groups.map((g) => {
    const notes = g.notes.filter((n) => {
      if (!needle) return true;
      if (n.title.toLowerCase().includes(needle)) return true;
      return n.cards.some((c) => plainPreview(c.front).toLowerCase().includes(needle));
    });
    if (!notes.length) return '';
    return `<details class="cl-group"${g.cards && !needle ? ' open' : ''}>
      <summary>
        <span class="cl-gname">${esc(g.name)}</span>
        <span class="cl-gmeta">${g.cards ? `${g.cards} 张 · ${g.due} 待复习` : '还没生成卡片'}</span>
      </summary>
      <div class="cl-notes">
        ${notes.map((n) => noteRow(n, needle)).join('')}
      </div>
    </details>`;
  }).join('');
  $('cardLib').innerHTML = html || '<p class="empty">没有匹配的卡片</p>';
}

function noteRow(n, needle) {
  const has = n.cards.length > 0;
  const due = n.cards.filter((c) => Number(c.due || 0) <= nowSec()).length;
  const hitCard = !!needle && !n.title.toLowerCase().includes(needle);
  return `<details class="cl-note"${hitCard ? ' open' : ''}>
    <summary>
      <span class="cl-title" title="${esc(n.title)}">${esc(n.title)}</span>
      <span class="cl-meta">${has ? `${n.cards.length} 张${due ? ` · ${due} 待复习` : ''}` : '还没生成卡片'}</span>
      <span class="cl-acts">
        ${has ? `<button class="btn ghost small" data-review="${esc(n.id)}">复习本篇</button>` : ''}
        <button class="btn ghost small cl-gen" data-gen="${esc(n.id)}">
          ${has ? '补充卡片' : '生成卡片'}</button>
      </span>
    </summary>
    ${has ? `<div class="cl-cards">
      ${n.cards.map((c) => `
        <div class="cl-card">
          <span class="cl-kind">${esc(c.kind || '概念')}</span>
          <span class="cl-front">${esc(plainPreview(c.front))}</span>
          <span class="cl-when">${dueText(c.due)}</span>
          <button class="cl-mini" data-now="${esc(c.id)}" title="把这张卡提到现在复习">现在复习</button>
          <button class="cl-mini danger" data-del="${esc(c.id)}" title="删除这张卡">删</button>
        </div>`).join('')}
    </div>` : ''}
  </details>`;
}

async function generateCards(btn, noteId) {
  if (!noteId) return;
  const old = btn.textContent.trim();
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    const d = await api('/api/cards/generate', {
      method: 'POST',
      body: JSON.stringify({ note_id: noteId }),
    });
    toast(`已生成 ${d.created} 张卡片${d.duplicate ? `，${d.duplicate} 张重复已跳过` : ''}`);
    await loadReview();          // 里面会一起刷新卡片库
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
    btn.textContent = old;
  }
}

function reviewNote(noteId) {
  reviewScope = { mode: 'notes', collection: '', noteIds: [noteId] };
  reviewCram = false;
  saveScope();
  window.scrollTo({ top: 0, behavior: 'smooth' });
  loadReview().catch((e) => toast(e.message, true));
}

async function reviewCardNow(cardId) {
  try {
    await api(`/api/cards/${encodeURIComponent(cardId)}/reset`, { method: 'POST' });
    const card = cardLibAll.find((c) => c.id === cardId);
    if (card && card.note_id) {
      reviewScope = { mode: 'notes', collection: '', noteIds: [card.note_id] };
      saveScope();
    }
    reviewCram = false;
    window.scrollTo({ top: 0, behavior: 'smooth' });
    await loadReview();
    toast('已加入本轮复习');
  } catch (e) {
    toast(e.message, true);
  }
}

async function deleteCard(cardId) {
  if (!confirm('删掉这张卡片？它不会再出现在复习里（笔记本身不受影响）。')) return;
  try {
    await api(`/api/cards/${encodeURIComponent(cardId)}`, { method: 'DELETE' });
    toast('已删除');
    await loadReview();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ==================== 合集选集 ====================
   「第 3 集，第 5~7 集，第 11~20 集」这种需求靠一个个点太累，给一个范围输入框。
   写法：逗号/顿号/分号/空格分隔；单集写 3，区间写 5-7（也认 5~7、5—7）。
   超出集数或写错会直接提示，不会静默少勾。 */
let picker = { bvid: '', items: [], picked: new Set() };

function parseRange(text, max) {
  const raw = String(text || '').trim();
  if (!raw) return { nums: [], error: '' };
  const nums = new Set();
  const parts = raw.replace(/[，、;；\s]+/g, ',').split(',').filter(Boolean);
  for (const part of parts) {
    const span = /^(\d+)\s*[-~—至]\s*(\d+)$/.exec(part);
    if (span) {
      let a = Number(span[1]);
      let b = Number(span[2]);
      if (a > b) { const t = a; a = b; b = t; }
      if (a < 1 || b > max) return { nums: [], error: `「${part}」超出范围（共 ${max} 集）` };
      for (let i = a; i <= b; i += 1) nums.add(i);
      continue;
    }
    if (/^\d+$/.test(part)) {
      const n = Number(part);
      if (n < 1 || n > max) return { nums: [], error: `第 ${n} 集不存在（共 ${max} 集）` };
      nums.add(n);
      continue;
    }
    return { nums: [], error: `看不懂「${part}」，请用 3 或 5-7 这样写` };
  }
  return { nums: [...nums].sort((a, b) => a - b), error: '' };
}

function renderPicker() {
  $('epList').innerHTML = picker.items.map((it, i) => {
    const on = picker.picked.has(i);
    return `<label class="ep-item${it.done ? ' is-done' : ''}${on ? ' on' : ''}">
      <input type="checkbox" data-ep="${i}"${on ? ' checked' : ''}>
      <span class="ep-no">第 ${i + 1} 集</span>
      <span class="ep-t" title="${esc(it.title)}">${esc(it.title)}</span>
      <span class="ep-d">${fmtDur(it.duration)}</span>
      ${it.done ? '<span class="ep-done">已做</span>' : ''}
    </label>`;
  }).join('');
  refreshPickCount();
}

async function openPicker(bvid) {
  picker = { bvid, items: [], picked: new Set() };
  $('pickTitle').textContent = '正在读取合集…';
  $('pickSub').textContent = '';
  $('pickRange').value = '';
  $('pickRedo').checked = false;
  $('pickState').textContent = '';
  $('pickState').className = 'hint';
  $('epList').innerHTML = '<p class="empty">加载中…</p>';
  $('pickDlg').showModal();
  try {
    const d = await api(`/api/videos/${encodeURIComponent(bvid)}/series`);
    picker.items = d.items || [];
    const kind = d.kind === 'season' ? '合集' : (d.kind === 'pages' ? '多P视频' : '单独视频');
    $('pickTitle').textContent = `${d.title || '这个视频'} · 共 ${d.total} 集`;
    $('pickSub').textContent = `识别为${kind}` +
      (d.done_count ? `；其中 ${d.done_count} 集已经有笔记（灰显，默认不勾）` : '') +
      '。在下面输入序号可以批量勾选。';
    if (!picker.items.length) {
      $('epList').innerHTML = '<p class="empty">没有读到分集，可能这个视频就是单集</p>';
      $('pickOk').disabled = true;
      return;
    }
    renderPicker();
  } catch (e) {
    $('pickTitle').textContent = '读取失败';
    $('epList').innerHTML = `<p class="empty">${esc(e.message)}</p>`;
  }
}

/* 计数条：除了已选多少集，还告诉你里面有几句是"已经做过"的，避免误以为会重做 */
function refreshPickCount() {
  const total = picker.items.length;
  const donePicked = [...picker.picked].filter((i) => picker.items[i] && picker.items[i].done).length;
  const redo = $('pickRedo').checked;
  let text = `已选 ${picker.picked.size} / ${total} 集`;
  if (donePicked) text += redo ? `（含 ${donePicked} 集重做）` : `（${donePicked} 集做过，将跳过）`;
  $('pickCount').textContent = text;
  $('pickOk').disabled = picker.picked.size === 0;
}

function setPicked(indexes) {
  picker.picked = new Set(indexes);
  renderPicker();
}

async function submitPicks() {
  const idx = [...picker.picked].sort((a, b) => a - b);
  if (!idx.length) return;
  const picks = idx.map((i) => ({
    bvid: picker.items[i].bvid,
    page: Number(picker.items[i].page) || 1,
  }));
  const btn = $('pickOk');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '加入中…';
  try {
    const d = await api('/api/favorites/import', {
      method: 'POST',
      body: JSON.stringify({
        episodes: picks,
        skip_done: !$('pickRedo').checked,
        lecture: $('batchLecture').checked,
        collection: currentFolderTitle(),
        media_id: Number($('favSelect').value) || null,
      }),
    });
    $('pickDlg').close();
    toast(`已加入 ${d.count} 集${d.skipped ? `，跳过 ${d.skipped} 集已做过的` : ''}`);
    switchView('queue');
  } catch (e) {
    $('pickState').textContent = e.message;
    $('pickState').className = 'hint bad';
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

/* ==================== 导出 / 打印 ====================
   PDF 走后端无头浏览器打印（保真）；Word 走后端 python-docx；
   「直接打印」用当前页面 + @media print 样式，适合临时打一张。
   打印专用页面是 ?print=<笔记id>，后端生成的 PDF 就从这里来。 */
function setExportState(text, bad) {
  const el = $('exportState');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'hint' + (bad ? ' bad' : '');
}

async function downloadExport(fmt) {
  if (!currentNoteId) return;
  const withTranscript = $('expTranscript').checked;
  const items = document.querySelectorAll('.export-item');
  items.forEach((b) => { b.disabled = true; });
  setExportState(fmt === 'docx' ? '正在生成 Word，请稍候…' : '正在生成 PDF（要启动浏览器排版，约 5~15 秒）…');
  try {
    const url = `/api/library/${encodeURIComponent(currentNoteId)}/export`
      + `?fmt=${fmt}&transcript=${withTranscript ? 1 : 0}`;
    const resp = await fetch(url);
    if (!resp.ok) {
      let detail = `导出失败（${resp.status}）`;
      try {
        const j = await resp.json();
        detail = j.detail || detail;
      } catch (e) { /* 不是 JSON 就用默认文案 */ }
      throw new Error(detail);
    }
    const blob = await resp.blob();
    const disp = resp.headers.get('Content-Disposition') || '';
    let name = `${currentNote ? currentNote.title : 'note'}.${fmt}`;
    const star = /filename\*=UTF-8''([^;]+)/i.exec(disp);
    const plain = /filename="?([^";]+)"?/i.exec(disp);
    if (star) name = decodeURIComponent(star[1]);
    else if (plain) name = plain[1];
    const href = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = href;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 4000);
    setExportState(`已导出：${name}（${(blob.size / 1024 / 1024).toFixed(2)} MB，在浏览器下载栏里）`);
    toast('已导出 ' + (fmt === 'pdf' ? 'PDF' : 'Word'));
  } catch (e) {
    setExportState(e.message, true);
    toast(e.message, true);
  } finally {
    items.forEach((b) => { b.disabled = false; });
  }
}

/* 打印前把公式、图片都等出来，否则会打出半截 */
function waitAssets() {
  const imgs = [...document.images].filter((i) => !i.complete);
  const loaded = Promise.all(imgs.map((i) => new Promise((res) => {
    i.addEventListener('load', res, { once: true });
    i.addEventListener('error', res, { once: true });
    setTimeout(res, 4000);
  })));
  const fonts = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
  return Promise.all([loaded, fonts.catch(() => {})]);
}

/* ---- 数学公式：LaTeX → KaTeX ----
   笔记里的矩阵 / 行列式 / 分式由模型写成 $...$ 或 $$...$$（Obsidian 也认这套）。
   自研渲染器不懂数学，交给随包分发的 KaTeX；没加载成功或语法写错时
   退回等宽原文，至少不把 \begin{bmatrix} 这种源码糊在正文里。 */
const MATH_RE = /\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g;

/** 是不是公式？"$50" 这种价格要放过，但 $[A|E]$、$A$ 这种短式子要认。 */
function isMath(s) {
  const t = String(s ?? '').trim();
  if (!t || t.length > 300) return false;
  if (/[\u4e00-\u9fff]/.test(t)) return false;          // 夹中文的一般不是公式
  if (/\\[a-zA-Z]+/.test(t)) return true;               // \frac \begin 等 LaTeX 命令
  if (/[_^{}]/.test(t)) return true;                    // 上下标 / 分组
  if (/^[A-Za-z]\d?$/.test(t)) return true;             // 单个变量：A、x、n、x1
  if (/^[+-]?\d[\d.,]*$/.test(t)) return true;          // 纯数字（含负数）：$-1$、$2$ 是数学。
                                                        // 价格误判不在这里挡——"$50 和 $60" 中间夹中文/字母会被上面的规则拦下
  return /[A-Za-z]/.test(t) && /[[\]|+\-*/=<>∧·]/.test(t); // [A|E]、a+b、x≤y
}

function renderMath(tex, display) {
  const src = String(tex ?? '').trim();
  if (window.katex && typeof window.katex.renderToString === 'function') {
    try {
      return window.katex.renderToString(src, {
        displayMode: !!display, throwOnError: false, strict: 'ignore', output: 'html',
      });
    } catch (e) { /* 落到下面兜底 */ }
  }
  return `<code class="tex">${esc(display ? `$$${src}$$` : `$${src}$`)}</code>`;
}

/** 抽公式换占位符 —— 必须在 esc() 之前跑，否则 LaTeX 里的 < > & 会被转义。
 *  行内公式用 \u0000 占位（跟正文同一段），块级 $$...$$ 用 \u0001 占位（独占一段）：
 *  还原时行内的不能包 <p>，否则一句话会被公式截成好几行。 */
function holdMath(text, hold) {
  return text.replace(MATH_RE, (m, block, inline) => {
    const tex = block !== undefined ? block : inline;
    if (!isMath(tex)) return m;
    hold.push(renderMath(tex, block !== undefined));
    return block !== undefined ? `\u0001${hold.length - 1}\u0001` : `\u0000${hold.length - 1}\u0000`;
  });
}

/* ==================== 生词：悬停释义 + 锚点跳转 ====================
   笔记里命中的词被渲染成 <a href="#w-xxx">，下方「生词」段每条有对应锚点。
   桌面悬停出气泡（原形 + 词性 + 释义），手机点一下跳到词条——手机上本来
   就没有 hover，跳过去看释义更直接。 */
let wordTipEl = null;
const CAN_HOVER = !!(window.matchMedia && window.matchMedia('(hover: hover)').matches);

function wordSlug(text) {
  return String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'w';
}

function wordMapFromNote(note) {
  const map = {};
  ((note && note.words && note.words.words) || []).forEach((w) => {
    map[`w-${wordSlug(w.w)}`] = w;
  });
  return map;
}

function hideWordTip() {
  if (wordTipEl) wordTipEl.hidden = true;
}

function showWordTip(anchor, map) {
  const id = (anchor.getAttribute('href') || '').slice(1);
  const info = map[id];
  if (!info) return;
  if (!wordTipEl) {
    wordTipEl = document.createElement('div');
    wordTipEl.className = 'word-tip';
    wordTipEl.hidden = true;
    document.body.appendChild(wordTipEl);
  }
  const form = anchor.textContent.trim();
  const head = form.toLowerCase() === String(info.w).toLowerCase()
    ? `<b>${esc(info.w)}</b>`
    : `<b>${esc(info.w)}</b><span class="wt-form">${esc(form)}</span>`;
  wordTipEl.innerHTML = `<div class="wt-head">${head}</div>
    <div class="wt-cn">${info.pos ? `<em>${esc(info.pos)}</em> ` : ''}${esc(info.cn || '—')}</div>`;
  wordTipEl.hidden = false;

  const r = anchor.getBoundingClientRect();
  const tw = wordTipEl.offsetWidth;
  const th = wordTipEl.offsetHeight;
  let left = r.left + r.width / 2 - tw / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
  let top = r.top - th - 8;
  if (top < 8) top = r.bottom + 8;
  wordTipEl.style.left = `${left}px`;
  wordTipEl.style.top = `${top}px`;
}

function bindWordLinks(root) {
  if (!root || !currentNote) return;
  const links = root.querySelectorAll('a.wa-link');
  if (!links.length) return;
  const map = wordMapFromNote(currentNote);
  links.forEach((a) => {
    a.onclick = (ev) => {
      const id = (a.getAttribute('href') || '').slice(1);
      const target = root.querySelector(`[id="${id}"]`);
      if (!target) return;                       // 找不到就交给浏览器
      ev.preventDefault();
      hideWordTip();
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      const item = target.closest('li') || target.parentElement;
      if (item) {
        item.classList.add('word-flash');
        setTimeout(() => item.classList.remove('word-flash'), 1400);
      }
    };
    if (CAN_HOVER) {
      a.onmouseenter = () => showWordTip(a, map);
      a.onmouseleave = () => setTimeout(hideWordTip, 130);
    }
  });
}


function renderMarkdown(md, noteId) {
  const base = `/api/library/${encodeURIComponent(noteId)}/file/`;
  const hold = [];

  let text = String(md || '').replace(/\r\n?/g, '\n')   // 老笔记是 CRLF，先归一
    .replace(/^---\n[\s\S]*?\n---\n/, '');

  // 0. 折叠块（完整转录在用）。之前没人处理它，<details> 标签直接裸奔成文字。
  //    内部内容递归走一遍 markdown 渲染。
  text = text.replace(/<details><summary>([\s\S]*?)<\/summary>([\s\S]*?)<\/details>/g,
    (_m, sum, inner) => {
      const body = renderMarkdown(inner, noteId);
      hold.push(`<details class="fold"><summary>${esc(sum)}</summary>`
        + `<div class="fold-body">${body}</div></details>`);
      return `\u0000${hold.length - 1}\u0000`;
    });

  // 1. 代码块（先抽出来，避免被后续规则破坏）
  //    知识地图单独渲染成图——否则就是一坨源码，图完全没有。
  //    旧笔记是 mermaid mindmap（放射状），新笔记是 flowchart TD（纵向流程）。
  text = text.replace(/```(\w*)\r?\n?([\s\S]*?)```/g, (_m, _lang, code) => {
    let mm = null;
    if (_lang === 'mermaid') {
      if (/^\s*mindmap\b/.test(code)) mm = renderMindmapTree(code);
      else if (/^\s*(?:flowchart|graph)\s+(?:TD|TB|LR|RL|BT)\b/i.test(code)) mm = renderFlowTree(code);
    }
    hold.push(mm || `<pre class="code"><code>${esc(code)}</code></pre>`);
    return `\u0000${hold.length - 1}\u0000`;
  });

  // 1.5 数学公式（转义前抽走：$...$ / $$...$$）
  text = holdMath(text, hold);

  // 1.6 HTML 注释不该显示出来（生词段的 <!-- words:start/end --> 就是靠这一步吃掉）。
  //     markdown 本来就把注释当不可见内容，之前没处理，页面上会露出一行标记。
  text = text.replace(/<!--[\s\S]*?-->/g, '');

  // 1.8 生词锚点 <a id="w-xxx"></a>：先抽出来，否则会被下面一步转义成文字。
  //     这些锚点是"原文生词 -> 下方词条"的跳转目标。
  text = text.replace(/<a id="([^"]+)"><\/a>/g, (_m, id) => {
    hold.push(`<a id="${esc(id)}" class="wa"></a>`);
    return `\u0000${hold.length - 1}\u0000`;
  });

  // 2. 转义
  text = esc(text);

  // 3. 表格（也要抽出来，否则列表规则会把 | 行吃掉）
  text = text.replace(/(?:^\|.*\|[ \t]*\n?)+/gm, (block) => {
    const rows = block.trim().split('\n').filter((r) => r.trim());
    if (rows.length < 2) return block;
    const split = (r) => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
    const isDivider = (r) => /^[\s|:-]+$/.test(r) && r.includes('-');
    const head = split(rows[0]);
    const bodyRows = rows.slice(isDivider(rows[1]) ? 2 : 1);
    const th = head.map((c) => `<th>${c}</th>`).join('');
    const tb = bodyRows.map((r) => `<tr>${split(r).map((c) => `<td>${c}</td>`).join('')}</tr>`).join('');
    hold.push(`<table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table>`);
    return `\u0000${hold.length - 1}\u0000`;
  });

  // 4. 图片 / 链接
  text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, alt, src) => {
    const url = /^(https?:|data:|\/)/.test(src) ? src : base + src;
    return `<img alt="${alt}" src="${url}" loading="lazy">`;
  });
  // 4. 链接。除 http(s) 和站内路径外，还要认 #锚点（生词跳转用），
  //    锚点不加 target=_blank，否则会新开一个空白页。
  text = text.replace(/\[([^\]]+)\]\(((?:https?:|\/|#)[^)]+)\)/g, (_m, label, href) =>
    href.startsWith('#')
      ? `<a href="${href}" class="wa-link">${label}</a>`
      : `<a href="${href}" target="_blank" rel="noopener">${label}</a>`);

  // 5. 标题
  text = text.replace(/^###### (.*)$/gm, '<h6>$1</h6>')
             .replace(/^##### (.*)$/gm, '<h5>$1</h5>')
             .replace(/^#### (.*)$/gm, '<h4>$1</h4>')
             .replace(/^### (.*)$/gm, '<h3>$1</h3>')
             .replace(/^## (.*)$/gm, '<h2>$1</h2>')
             .replace(/^# (.*)$/gm, '<h1>$1</h1>');

  // 6. 引用（连续多行合并为一个块）
  text = text.replace(/(?:^&gt; ?.*(?:\n|$))+/gm, (block) => {
    const lines = block.trim().split('\n').map((l) => l.replace(/^&gt; ?/, ''));
    return `<blockquote>${lines.join('<br>')}</blockquote>\n`;
  });

  // 7. 列表：按行扫描，同类标记合并成一个列表块。
  //    之前用「连续 <li> 正则」一次包 <ul>，遇到列表后紧跟空行 + 标题时会错位，
  //    产出 <ul>…</ul> 与 <p></ul></p> 这种残渣；逐行处理就没有这类问题。
  {
    const rows = text.split('\n');
    const out = [];
    let mode = null;
    let items = [];
    // 整个列表拼成一个字符串：中间不能留换行，否则第 9 步会把换行转成 <br>，
    // 出现 <ul><br><li> 和 <p></ul></p> 这种残渣。
    const flush = () => {
      if (mode) { out.push(`<${mode}>${items.join('')}</${mode}>`); mode = null; items = []; }
    };
    for (const row of rows) {
      const mu = row.match(/^\s*[-*+] (.*)$/);
      const mo = mu ? null : row.match(/^\s*\d+[.)] (.*)$/);
      if (mu || mo) {
        const kind = mu ? 'ul' : 'ol';
        if (mode !== kind) { flush(); mode = kind; }
        items.push(mu ? `<li>${mu[1]}</li>` : `<li class="oli">${mo[1]}</li>`);
      } else {
        if (row.trim()) flush();       // 空行不打断列表，正文行才结束
        out.push(row);
      }
    }
    flush();
    text = out.join('\n');
  }

  // 8. 行内样式
  // 黄色荧光笔：==重点==（与 Obsidian 的 highlight 语法一致）
  text = text.replace(/==([^=\n]+)==/g, '<mark class="hl">$1</mark>');
  text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  // <sub> 是我们自己写进笔记的标签：esc 之后已变成实体，先还原，
  // 否则时间码小字会原样显示成 &lt;sub&gt;。
  text = text.replace(/&lt;sub&gt;([\s\S]*?)&lt;\/sub&gt;/g, '<sub>$1</sub>');
  text = text.replace(/^\s*<sub>(.*)<\/sub>\s*$/gm, '<sub>$1</sub>');

  // 9. 段落
  const BLOCK = /^\s*<(h[1-6]|ul|ol|li|pre|blockquote|table|div|img|p)\b/;
  // 块级标签出现在段落中间时（标记符和正文之间只隔一个空行），也不能再套 <p>：
  // 否则会产出 <p><ul>…</ul><h2>…</h2></p> 这种非法嵌套，列表和标题一起垮掉。
  const HAS_BLOCK = /<(?:h[1-6]|ul|ol|pre|blockquote|table|div|details)\b/;
  text = text.split(/\n{2,}/).map((chunk) => {
    const body = chunk.replace(/\n/g, '<br>').trim();
    if (!body) return '';
    return BLOCK.test(body) || HAS_BLOCK.test(body) ? body : `<p>${body}</p>`;
  }).join('\n');

  // 10. 还原占位块。块级的（\u0001：独立公式/图表/折叠块）自占一段；
  //     行内的（\u0000：行内公式）原样放回，包 <p> 会把一句话截断成多行。
  // 表格是先抽成 HTML 再替换的，单元格里的行内公式会留下占位符：
  // 先让 hold 内部互相同步一遍，再往正文里还原。
  for (let i = 0; i < hold.length; i++) {
    hold[i] = hold[i].replace(/\u0001(\d+)\u0001|\u0000(\d+)\u0000/g, (mm, a, b) => {
      const k = Number(a !== undefined ? a : b);
      return k !== i && hold[k] !== undefined ? hold[k] : mm;
    });
  }

  // 占位块本身就是块级元素（公式、表格、代码、折叠块），段落已在第 9 步包好，
  // 这里只做替换，不能再包 <p>——否则会出现 <p><p><details>… 这种嵌套。
  text = text.replace(/\u0001(\d+)\u0001/g, (_m, i) => hold[Number(i)]);
  text = text.replace(/\u0000(\d+)\u0000/g, (_m, i) => hold[Number(i)]);
  return text;
}

/* ---- mermaid mindmap → 可视化树（阅读器内） ----
   mermaid 块此前被当普通代码块原样展示，"知识地图"一节就是一坨源码。
   这里把缩进层级解析成嵌套列表，用 CSS 画连线；节点可点击折叠。 */
function renderMindmapTree(src) {
  const lines = String(src || '').split('\n')
    .map((l) => l.replace(/\t/g, '  '))
    .filter((l) => l.trim() && !/^%%/.test(l.trim()));
  if (lines.length < 2) return null;

  let rootText = '思维导图';
  const body = [];
  for (const line of lines) {
    const t = line.trim();
    const root = t.match(/^root\(\((.*)\)\)$/) || t.match(/^root\[(.*)\]$/)
      || t.match(/^root\(\[?(.*?)\]?\)?$/);
    if (root) { rootText = root[1]; continue; }
    if (/^mindmap$/.test(t)) continue;
    body.push(line);
  }
  if (!body.length) return null;

  const stack = [{ depth: -1, el: null, children: [] }];
  for (const line of body) {
    const depth = Math.floor((line.length - line.replace(/^ +/, '').length) / 2);
    const label = line.trim();
    while (stack.length > 1 && stack[stack.length - 1].depth >= depth) stack.pop();
    const parent = stack[stack.length - 1];
    parent.children.push({ depth, label, children: [], el: null });
    stack.push(parent.children[parent.children.length - 1]);
  }
  const root = stack[0].children[0];
  if (!root) return null;

  const esc2 = (s) => esc(s);
  const build = (node) => {
    const kids = node.children.map(build).join('');
    const hasKids = node.children.length > 0;
    return `<li class="${hasKids ? 'has-kids' : ''}">`
      + `<span class="mm-label">${esc2(node.label)}</span>`
      + (hasKids ? `<ul>${kids}</ul>` : '')
      + '</li>';
  };
  return `<div class="mmap"><div class="mm-root">${esc2(rootText)}</div><ul>${build(root)}</ul></div>`;
}

/* ---- mermaid flowchart TD → 规整的纵向流程树（阅读器内） ----
   放射状的 mindmap 会把"先…再…最后…"这类步骤甩成八爪鱼，顺序也看不出来。
   这里自己算纵向布局：同层并排、父子上下、单链自然排成一列，箭头指明先后。 */
function parseFlowchart(src) {
  const nodes = new Map();
  const order = [];
  let dir = 'TD';
  const DECL = /^([A-Za-z0-9_]+)\s*(?:\(\s*\[\s*"([\s\S]*)"\s*\]\s*\)|\[\s*"([\s\S]*)"\s*\])\s*$/;
  const EDGE = /^([A-Za-z0-9_]+)\s*-->\s*([A-Za-z0-9_]+)\s*$/;
  const get = (id) => {
    if (!nodes.has(id)) {
      nodes.set(id, { id, label: id, kids: [], parent: null });
      order.push(id);
    }
    return nodes.get(id);
  };
  for (const raw of String(src || '').split('\n')) {
    const line = raw.trim();
    if (!line || /^%%/.test(line)) continue;
    const head = line.match(/^(?:flowchart|graph)\b\s*([A-Za-z]{2})?/i);
    if (head) { dir = (head[1] || 'TD').toUpperCase(); continue; }
    const d = line.match(DECL);
    if (d) { get(d[1]).label = (d[2] !== undefined ? d[2] : d[3]) || ''; continue; }
    const e = line.match(EDGE);
    if (e) {
      const a = get(e[1]);
      const b = get(e[2]);
      b.parent = a.id;
      a.kids.push(b.id);
      continue;
    }
    return null;   // 认不出的语法就老实显示源码，别瞎画
  }
  const root = order.find((id) => !nodes.get(id).parent);
  return root ? { root, nodes, dir } : null;
}

function renderFlowTree(src) {
  const g = parseFlowchart(src);
  if (!g) return null;
  const vertical = g.dir !== 'LR' && g.dir !== 'RL';

  const FONT = 13, LINE_H = 19, PAD_X = 13, PAD_Y = 9;
  const GAP = 14;                       // 同层节点之间的空隙
  const LAYER_GAP = vertical ? 30 : 46; // 层级之间的空隙
  // 分支特别多的图（如旅行攻略 7-8 个主题并排）把节点收窄，否则整张图宽到没法看
  const MAX_W = g.nodes.get(g.root).kids.length >= 5 ? 200 : 320;
  const charW = (ch) => (ch.codePointAt(0) > 0x2e80 ? FONT : FONT * 0.56);
  const textW = (s) => Array.from(s).reduce((n, ch) => n + charW(ch), 0);

  const wrap = (label) => {
    const out = [];
    for (const part of String(label).split(/<br\s*\/?>/i)) {
      let cur = '', w = 0;
      for (const ch of Array.from(part)) {
        const c = charW(ch);
        if (w + c > MAX_W && cur) { out.push(cur); cur = ch; w = c; }
        else { cur += ch; w += c; }
      }
      if (cur) out.push(cur);
    }
    return out.length ? out : [''];
  };

  const depthOf = new Map();
  const layerMax = [];   // 每层的主轴尺寸（竖向布局=行高，横向布局=列宽）
  (function measure(id, d) {
    const n = g.nodes.get(id);
    n.lines = wrap(n.label);
    n.w = Math.round(Math.max(...n.lines.map(textW)) + PAD_X * 2);
    n.h = n.lines.length * LINE_H + PAD_Y * 2;
    depthOf.set(id, d);
    layerMax[d] = Math.max(layerMax[d] || 0, vertical ? n.h : n.w);
    n.kids.forEach((k) => measure(k, d + 1));
  })(g.root, 0);

  const layerPos = [];
  let cursor = 0;
  layerMax.forEach((size, d) => { layerPos[d] = cursor + size / 2; cursor += size + LAYER_GAP; });

  // 次轴（兄弟方向）跨度：单链的每个节点独占一层，天然排成一列
  const crossOf = (n) => (vertical ? n.w : n.h);
  const spanOf = (id) => {
    const n = g.nodes.get(id);
    if (!n.kids.length) return (n.span = crossOf(n));
    const total = n.kids.reduce((s, k) => s + spanOf(k), 0) + GAP * (n.kids.length - 1);
    return (n.span = Math.max(crossOf(n), total));
  };
  spanOf(g.root);

  // 以中线定位：单链的父子天然对齐成一列；多个子节点则在父节点下居中铺开
  const rootSpan = g.nodes.get(g.root).span;
  (function place(id, center) {
    const n = g.nodes.get(id);
    if (vertical) { n.x = center; n.y = layerPos[depthOf.get(id)]; }
    else { n.y = center; n.x = layerPos[depthOf.get(id)]; }
    if (!n.kids.length) return;
    const total = n.kids.reduce((s, k) => s + g.nodes.get(k).span, 0) + GAP * (n.kids.length - 1);
    let cur = center - total / 2;
    n.kids.forEach((k) => {
      const kn = g.nodes.get(k);
      place(k, cur + kn.span / 2);
      cur += kn.span + GAP;
    });
  })(g.root, rootSpan / 2);

  const W = Math.ceil(vertical
    ? rootSpan
    : Math.max(...[...g.nodes.values()].map((n) => n.x + n.w / 2)));
  const H = Math.ceil(vertical
    ? Math.max(...[...g.nodes.values()].map((n) => n.y + n.h / 2))
    : rootSpan);

  const fx = (v) => v.toFixed(1);
  const parts = [];
  for (const n of g.nodes.values()) {
    if (!n.parent) continue;
    const p = g.nodes.get(n.parent);
    if (vertical) {
      const x1 = p.x, y1 = p.y + p.h / 2, x2 = n.x, y2 = n.y - n.h / 2 - 1;
      const mid = (y1 + y2) / 2;
      parts.push(`<path class="fl" d="M${fx(x1)},${fx(y1)} C${fx(x1)},${fx(mid)} ${fx(x2)},${fx(mid)} ${fx(x2)},${fx(y2)}"/>`);
      parts.push(`<path class="arw" d="M${fx(x2 - 4)},${fx(y2 - 7)} L${fx(x2 + 4)},${fx(y2 - 7)} L${fx(x2)},${fx(y2)} Z"/>`);
    } else {
      const x1 = p.x + p.w / 2, y1 = p.y, x2 = n.x - n.w / 2 - 1, y2 = n.y;
      const mid = (x1 + x2) / 2;
      parts.push(`<path class="fl" d="M${fx(x1)},${fx(y1)} C${fx(mid)},${fx(y1)} ${fx(mid)},${fx(y2)} ${fx(x2)},${fx(y2)}"/>`);
      parts.push(`<path class="arw" d="M${fx(x2 + 7)},${fx(y2 - 4)} L${fx(x2 + 7)},${fx(y2 + 4)} L${fx(x2)},${fx(y2)} Z"/>`);
    }
  }
  for (const n of g.nodes.values()) {
    const d = Math.min(depthOf.get(n.id), 3);
    const left = n.x - n.w / 2, top = n.y - n.h / 2;
    parts.push(`<g class="fn d${d}"><title>${esc(n.label)}</title>`);
    parts.push(`<rect x="${fx(left)}" y="${fx(top)}" width="${n.w}" height="${n.h}" rx="8"/>`);
    n.lines.forEach((line, i) => {
      const ty = n.y - (n.lines.length - 1) * LINE_H / 2 + i * LINE_H + FONT * 0.35;
      parts.push(`<text x="${fx(n.x)}" y="${fx(ty)}" text-anchor="middle">${esc(line)}</text>`);
    });
    parts.push('</g>');
  }
  return `<div class="mmflow"><svg viewBox="0 0 ${W + 2} ${H + 2}" width="${W + 2}" height="${H + 2}" preserveAspectRatio="xMidYMin meet" xmlns="http://www.w3.org/2000/svg">${parts.join('')}</svg></div>`;
}

/* ==================== 设置 ==================== */
let providers = [];
let visionModelProvider = null;   // 上次渲染时"看图模型"对应的供应商，用于判断是否要重置模型名

/* 笔记详细度：三档同时决定「候选抽多密」和「每段留几张」。
   只放配额不改密度的话，详细档也只是从同一批候选里多挑几张，照样会漏。 */
/* 三档判定标准完全一样（都等画面停住、内容放全才截），
   唯一差别是「每段最多留几张」。全片不设总配额。 */
const DETAIL_META = {
  brief: {
    perChunk: 5,
    hint: '简洁：每段最多留 5 张。画面判定和另外两档完全一致，只是同段里挑最关键的 5 张。',
  },
  standard: {
    perChunk: 8,
    hint: '适中：每段最多留 8 张。画面判定和另外两档完全一致。',
  },
  detailed: {
    perChunk: 0,
    hint: '详细：每段不设上限，画面全部保留，要点也逐条写全、不合并。'
      + '三档都只截「内容放全、画面停住」的那一帧，不会抓 PPT 逐条动画的中间态。'
      + '代价是长视频明显变慢（每帧都要 OCR，30 分钟以上可能要十几分钟）。',
  },
};

function renderDetail() {
  const meta = DETAIL_META[$('setDetail').value] || DETAIL_META.standard;
  $('detailHint').textContent = meta.hint;
  $('setFrames').value = meta.perChunk ? `${meta.perChunk} 张` : '不限';
}

async function loadSettings() {
  const pv = await api('/api/providers');
  providers = pv.providers || [];
  renderProviderOptions();

  const s = await api('/api/settings');
  $('setProvider').value = s.llm_provider;
  $('setAsr').value = s.asr_model;
  $('setAsrLang').value = s.asr_language || 'auto';
  renderFfmpeg(s.ffmpeg);
  $('setDetail').value = s.note_detail || 'standard';
  renderDetail();
  $('setHd').checked = s.hd_frames !== false;
  $('setParallel').checked = s.parallel_pipeline !== false;
  $('setPolish').checked = s.polish_punctuation !== false;
  $('setBilingual').value = s.bilingual_transcript === false ? 'source' : 'bilingual';
  $('setCaption').checked = s.frame_captions !== false;
  $('setItinerary').checked = s.travel_itinerary !== false;
  renderVision(s);
  $('setCachePolicy').value = s.cache_policy || 'media';
  renderCache(s.cache);
  $('setOutput').value = s.output_dir;
  onProviderChange();
  updateLlmDot(s);
  loadLan().catch(() => { /* 双端状态拿不到不影响其它设置 */ });
  loadWordbooks().catch(() => { /* 词库同理 */ });
  loadUpdate().catch(() => { /* 更新状态拿不到不影响其它设置 */ });
}

/* ==================== 软件更新 ====================
   检查/下载都走后端：国内连不上 GitHub 是常态，所以失败只显示一行提示，
   不弹错误框、不阻塞启动；下载在后端线程跑，这里轮询状态。 */
let updReleaseUrl = '';
let updPoll = null;

function updState(msg, bad) {
  const box = $('updState');
  box.className = 'status' + (bad ? ' bad' : '');
  box.textContent = msg;
}

async function loadUpdate() {
  renderUpdate(await api('/api/update/status'));
}

function renderUpdProgress(dl) {
  const box = $('updProgress');
  const st = (dl && dl.state) || 'idle';
  if (st === 'running') {
    const mb = (Number(dl.received) || 0) / 1048576;
    const total = Number(dl.total) || 0;
    box.className = 'status';
    box.textContent = `下载中 ${Number(dl.progress) || 0}% · ${mb.toFixed(1)} MB`
      + (total ? ` / ${(total / 1048576).toFixed(1)} MB` : '');
  } else if (st === 'done') {
    box.className = 'status ok';
    box.textContent = (dl.verified ? '下载完成 · SHA256 校验通过' : '下载完成')
      + (dl.message ? ` · ${dl.message}` : '');
  } else if (st === 'error') {
    box.className = 'status bad';
    box.textContent = dl.message || '下载失败，可重试';
  } else {
    box.className = 'status';
    box.textContent = '';
  }
}

function renderUpdate(d) {
  $('updVersion').textContent = `v${d.version || '?'}${d.frozen ? '' : '（源码运行）'}`;
  if (document.activeElement !== $('updRepo')) $('updRepo').value = d.repo || '';
  if (document.activeElement !== $('updMirror')) $('updMirror').value = d.mirror || '';
  // API 检查失败时也要留一条手动下载的路：直接指到仓库的 Releases 列表
  updReleaseUrl = d.release_url || (d.repo ? `https://github.com/${d.repo}/releases` : '');
  $('btnUpdRelease').hidden = !updReleaseUrl;

  const dl = d.download || {};
  if (d.has_update) {
    $('updNew').hidden = false;
    $('updNotes').textContent = d.notes || '（该版本没有填写更新说明）';
    updState(`发现新版本 v${d.latest}`, false);
    $('btnUpdDownload').hidden = dl.state === 'running' || dl.state === 'done';
    $('btnUpdDownload').disabled = false;
    $('btnUpdInstall').hidden = dl.state !== 'done';
    renderUpdProgress(dl);
    if (dl.state === 'running') startUpdPoll();
    else stopUpdPoll();
  } else {
    $('updNew').hidden = true;
    stopUpdPoll();
    if (!d.configured) updState('未配置仓库，不会联网检查', false);
    else if (d.last_checked) updState(`已是最新版本 v${d.version}`, false);
    else updState('尚未检查', false);
  }
}

function startUpdPoll() {
  if (updPoll) return;
  updPoll = setInterval(async () => {
    try {
      const d = await api('/api/update/status');
      renderUpdate(d);
    } catch (_) { stopUpdPoll(); }
  }, 1200);
}

function stopUpdPoll() {
  if (updPoll) { clearInterval(updPoll); updPoll = null; }
}

async function checkUpdate() {
  const btn = $('btnUpdCheck');
  btn.disabled = true;
  btn.textContent = '检查中…';
  updState('正在连接 GitHub…', false);
  try {
    const r = await api('/api/update/check', {
      method: 'POST', body: JSON.stringify({ force: true }),
    });
    if (!r.ok) {
      updState(r.message || '检查失败', r.reason !== 'network');
      return;
    }
    renderUpdate(await api('/api/update/status'));
  } catch (e) {
    updState(`检查失败：${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '检查更新';
  }
}

async function saveUpdateRepo() {
  try {
    const r = await api('/api/update/config', {
      method: 'POST', body: JSON.stringify({
        repo: $('updRepo').value.trim(),
        mirror: $('updMirror').value.trim(),
      }),
    });
    if (!r.ok) { toast(r.message, true); return; }
    toast('更新源已保存');
    await checkUpdate();
  } catch (e) { toast(e.message, true); }
}

async function downloadUpdate() {
  const btn = $('btnUpdDownload');
  btn.disabled = true;
  try {
    const r = await api('/api/update/download', { method: 'POST' });
    if (!r.ok) { toast(r.message, true); btn.disabled = false; return; }
    updState('正在下载安装包…', false);
    btn.hidden = true;
    renderUpdProgress({ state: 'running', progress: 0, received: 0, total: 0 });
    startUpdPoll();
  } catch (e) { toast(e.message, true); btn.disabled = false; }
}

async function installUpdate() {
  if (!window.confirm('即将关闭 BiliNotes 并启动安装程序。\n会弹出系统权限确认，笔记和配置都会保留。\n\n现在继续吗？')) return;
  try {
    const r = await api('/api/update/install', { method: 'POST' });
    if (!r.ok) { toast(r.message, true); return; }
    stopUpdPoll();
    updState(r.message, false);
  } catch (e) { toast(e.message, true); }
}

/* ---------- 手机访问（双端） ----------
   开关只改配置，监听地址要重启才变；口令是每次请求现查的，换了立刻生效。 */
function renderLan(d) {
  $('setLan').checked = !!d.enabled;
  // 这里给的是完整界面地址；真正脱机（电脑关机也能看）走上面的「导出学习包」
  $('lanUrl').value = d.url || '';
  const on = !!d.enabled;
  const bound = !!d.port_open;        // 端口是否还在对外监听（与开关无关）
  let state;
  if (on && d.reachable) state = '已生效：手机连同一 WiFi 就能打开';
  else if (on) state = '已保存，重启软件后生效';
  else if (bound) state = '已关闭：手机已经打不开（端口要下次启动才彻底释放）';
  else state = '未开启：只有这台电脑能打开';
  // 地址换过：手机上的书签/截图二维码全指向旧地址，必须先说清楚，
  // 否则用户只会觉得"又断了"，去翻防火墙。
  if (d.ip_changed && d.prev_ip) {
    state += `　⚠ 电脑地址变了（${d.prev_ip} → ${d.ip || '未知'}），手机上的旧链接已失效，请重新扫码`;
  }
  $('lanState').textContent = state;
  $('lanUrl').disabled = !on;
  $('btnCopyLan').disabled = !on;
  $('btnRotateLan').disabled = !on;

  // 二维码只在"真的已经绑到局域网"时给——没重启就扫，手机只会看到连接失败
  const show = on && !!d.reachable && !!d.url;
  $('lanQrBox').hidden = !show;
  if (!show) {
    $('lanQr').removeAttribute('src');
    return;
  }
  // 优先用接口内联的 data-uri：省掉一次可能被 403 的图片请求
  if (d.qr) $('lanQr').src = d.qr;
}

/* ---------- 英语词库 ---------- */
async function loadWordbooks() {
  renderWordbooks(await api('/api/wordbooks'));
}

function renderWordbooks(d) {
  const box = $('wbList');
  const books = (d && d.books) || [];
  if (!books.length) {
    box.innerHTML = '<p class="empty">还没有导入词库</p>';
    return;
  }
  box.innerHTML = books.map((b) => {
    const when = b.imported_at
      ? new Date(b.imported_at * 1000).toLocaleDateString('zh-CN')
      : '';
    return `<div class="wb-row">
      <span class="wb-name">${esc(b.name)}</span>
      <span class="wb-meta">${fmtNum(b.count)} 词${when ? ` · ${when}` : ''}</span>
      <button class="btn ghost small" data-del-book="${esc(b.id)}">删除</button>
    </div>`;
  }).join('');
  box.querySelectorAll('[data-del-book]').forEach((btn) => {
    btn.onclick = () => deleteWordbook(btn.dataset.delBook);
  });
}

async function importWordbook(file) {
  const state = $('wbState');
  state.textContent = '解析中…';
  const form = new FormData();
  form.append('file', file);
  if ($('wbKeepMissing') && $('wbKeepMissing').checked) form.append('keep_missing', '1');
  // 这里不能走 api()：它固定 Content-Type: application/json，FormData 要浏览器自己带边界
  const res = await fetch('/api/wordbooks', { method: 'POST', body: form });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    state.textContent = '';
    toast(data.detail || `导入失败（${res.status}）`, true);
    return;
  }
  const parsed = data.parsed || {};
  const parts = [`导入 ${fmtNum(parsed.count || 0)} 个词`];
  if (parsed.missing) parts.push(`其中 ${fmtNum(parsed.missing)} 个待 AI 补释义`);
  if (parsed.skipped) parts.push(`跳过 ${parsed.skipped} 行`);
  state.textContent = parts.join('，');
  renderWordbooks(data.summary);
  const firstError = (parsed.errors || [])[0];
  if (firstError) {
    // 不静默吞坏行：告诉人第几行、为什么，改完重新导入即可
    toast(`第 ${firstError.line} 行${firstError.why}：${firstError.text}`, true);
  } else {
    toast(`「${data.book.name}」已导入 ${fmtNum(parsed.count || 0)} 个词`);
  }
}

/* 生词变卡片：只收笔记里真的遇到过的词，整本词库倒进来多少张都不算数 */
async function wordsToCards() {
  const state = $('wbState');
  state.textContent = '正在生成…';
  try {
    const d = await api('/api/wordbooks/to-cards', { method: 'POST' });
    if (d.hint) {
      state.textContent = '';
      toast(d.hint, true);
      return;
    }
    state.textContent = d.added
      ? `新增 ${fmtNum(d.added)} 张单词卡${d.dup ? `（${fmtNum(d.dup)} 张已存在）` : ''}`
      : '遇到的生词都已经有卡片了';
    toast(d.added ? `${d.added} 张单词卡已进复习队列` : '没有需要新增的单词卡');
  } catch (e) {
    state.textContent = '';
    toast(e.message, true);
  }
}

/* AI 补释义：一次最多补 360 个词（12 批），没补完会报剩余，
   再点一次继续。补完自动重算生词段，笔记里的「词库无释义」占位随之消失。 */
async function fillWordDefinitions() {
  const state = $('wbState');
  const btn = $('btnFillWords');
  btn.disabled = true;
  state.textContent = '正在补释义…（一批 30 个词，最多 12 批）';
  try {
    const d = await api('/api/wordbooks/fill', { method: 'POST', body: JSON.stringify({}) });
    if (!d.remaining && !d.filled) {
      state.textContent = '词库里的词都有释义';
      toast('没有需要补释义的词');
    } else if (d.remaining) {
      state.textContent = `已补 ${fmtNum(d.filled)} 个，还剩 ${fmtNum(d.remaining)} 个——再点一次继续`;
      toast(`补上 ${fmtNum(d.filled)} 个释义${d.failed ? `，${d.failed} 个没认出来` : ''}`);
    } else {
      state.textContent = `补齐 ${fmtNum(d.filled)} 个释义，笔记已重算`;
      toast(`补齐 ${fmtNum(d.filled)} 个释义${d.failed ? `（${d.failed} 个没认出来）` : ''}`);
    }
  } catch (e) {
    state.textContent = '';
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function deleteWordbook(bookId) {
  if (!confirm('删掉这个词库？（已生成的笔记里的生词段不会自动消失）')) return;
  try {
    const d = await api(`/api/wordbooks/${encodeURIComponent(bookId)}`, { method: 'DELETE' });
    renderWordbooks(d.summary);
    toast('已删除');
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadLan() {
  renderLan(await api('/api/lan?qr=1'));
}

async function toggleLan(enabled) {
  try {
    const d = await api('/api/lan', { method: 'POST', body: JSON.stringify({ enabled }) });
    await loadLan();
    if (enabled) {
      toast(d.reachable ? '已打开，手机现在就能连' : '已打开，重启软件后手机就能连');
    } else {
      toast(d.port_open
        ? '已关闭：手机已经打不开（端口要下次启动才彻底释放）'
        : '已关闭');
    }
  } catch (e) {
    $('setLan').checked = !enabled;
    toast(e.message, true);
  }
}

async function rotateLan() {
  try {
    await api('/api/lan', { method: 'POST', body: JSON.stringify({ rotate: true }) });
    await loadLan();
    toast('已换新口令，旧链接立刻失效（手机需要重新输一次）');
  } catch (e) {
    toast(e.message, true);
  }
}

async function copyLanUrl() {
  const text = $('lanUrl').value.trim();
  if (!text) { toast('先打开开关', true); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast('地址已复制，发给自己手机打开');
  } catch (e) {
    $('lanUrl').select();                    // 非 https 下 clipboard 不可用时退回手选
    toast('浏览器不让自动复制，已帮你选中，按 Ctrl+C');
  }
}

/* 手机连不上时按下按钮：逐项查「软件→开关→端口→地址→网络」。
   程序修不了防火墙和 AP 隔离，但能告诉人卡在哪一环、下一步做什么。 */
async function runLanDoctor() {
  const box = $('lanDoctor');
  box.hidden = false;
  box.innerHTML = '<p class="hint" style="margin:0">正在逐项检查…</p>';
  const d = await api('/api/lan/doctor');
  const rows = (d.checks || []).map((c) => `<div class="d-row">
      <span class="d-dot ${c.ok ? 'd-ok' : 'd-bad'}">${c.ok ? '✓' : '✗'}</span>
      <span class="d-label">${esc(c.label)}</span>
      <span class="d-detail">${esc(c.detail)}</span>
    </div>`).join('');
  const advice = (d.advice || [])
    .map((a) => `<li>${esc(a).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')}</li>`)
    .join('');
  const net = [d.ssid ? `WiFi「${esc(d.ssid)}」` : '没连 WiFi']
    .concat(d.port ? [`端口 ${d.port}`] : [])
    .join(' · ');
  box.innerHTML = `${rows}
    <div class="d-row" style="border-top:1px solid var(--line);margin-top:6px;padding-top:6px">
      <span class="d-dot"></span><span class="d-detail">${net}</span>
    </div>
    ${advice ? `<ol>${advice}</ol>` : ''}`;
  // 按钮文案换成"重新检查"，顺手让结果别一直挂着
  $('btnLanDoctor').textContent = '重新检查';
}

function renderProviderOptions() {
  const cur = $('setProvider').value;
  $('setProvider').innerHTML = providers.map((p) =>
    `<option value="${p.id}">${esc(p.label)}${p.has_key ? ' · 已配置' : ''}</option>`).join('');
  if (cur) $('setProvider').value = cur;
}

/* ---- 看图模型：让 AI 真正"看"关键帧来提炼画面要点 ---- */
const VISION_PROVIDERS = {
  zhipu: '智谱 GLM（glm-4.6v-flashx · 实测可用）',
  qwen: '通义千问（qwen-vl-plus）',
  moonshot: 'Moonshot（视觉版）',
  openai: 'OpenAI（gpt-4o-mini）',
};

function renderVision(s) {
  const opts = ['<option value="">不使用（用画面文字合成要点）</option>']
    .concat(providers.filter((p) => p.id === 'custom' || p.id === 'ollama')
      .map((p) => `<option value="${p.id}">${esc(p.label)}</option>`));
  for (const [id, label] of Object.entries(VISION_PROVIDERS)) {
    opts.push(`<option value="${id}">${esc(label)}</option>`);
  }
  $('setVision').innerHTML = opts.join('');
  $('setVision').value = s.vision_provider || '';
  visionModelProvider = s.vision_provider || '';
  const prov = providers.find((p) => p.id === visionModelProvider) || {};
  $('setVisionModel').value = s.vision_model || prov.vision_model || '';
  onVisionChange();
}

/** 看图模型可能会换一家服务商，所以它有自己独立的 Key 输入行。 */
function onVisionChange() {
  const v = $('setVision').value;
  const textProvider = $('setProvider').value;
  const prov = providers.find((p) => p.id === v) || {};
  const custom = v === 'custom';
  const local = v === 'ollama';
  const sameAsText = !!v && v === textProvider && !custom;

  $('visionCustomRow').hidden = !custom;
  $('visionModelRow').hidden = !v;

  // 换了服务商就把模型名换成这家的默认值（旧模型名对不上新服务商会 404）
  if (visionModelProvider !== v) {
    visionModelProvider = v;
    $('setVisionModel').value = prov.vision_model || '';
  }
  const list = $('visionModelList');
  list.innerHTML = (prov.vision_options || []).map((m) => `<option value="${esc(m)}"></option>`).join('');

  // 同一家服务、文字模型已经存过 Key → 直接复用，不用再填一遍
  const reuse = sameAsText && prov.has_key;
  const showKey = !!v && !local && !reuse;
  $('visionKeyRow').hidden = !showKey;
  if (showKey) {
    $('setVisionKey').value = '';
    $('setVisionKey').placeholder = custom ? '该服务的 Key（本地服务可留空）' : '粘贴看图模型的 Key';
    $('visionKeyState').textContent = prov.has_key
      ? `当前：${prov.masked}（这家服务已存过 Key，可直接用）`
      : '这家还没有 Key，填完点保存';
  }

  const hint = $('visionHint');
  if (!v) {
    hint.textContent = '不配置也能出笔记，只是画面要点由画面文字合成。'
      + '配置后 AI 会直接看关键帧图片来写（智谱 glm-4.6v-flashx 实测最稳，也很便宜）。';
  } else if (custom) {
    hint.textContent = '填入任何支持图片输入的 OpenAI 兼容服务地址与模型名。';
  } else if (local) {
    hint.textContent = '用本地 Ollama 看图，需要先执行 ollama pull qwen2.5vl:7b，无需 Key。';
  } else if (reuse) {
    hint.textContent = `文字模型就是同一家（${prov.label}），看图直接复用上面填的 Key，不用重复填。`;
  } else if (prov.has_key) {
    hint.textContent = '已就绪：画面要点会把关键帧图片发给这个模型看过再提炼。';
  } else {
    hint.textContent = `文字模型是另一家（${(providers.find((p) => p.id === textProvider) || {}).label || textProvider}），`
      + '看图要单独填这家的 Key——在下面填，不影响上面的 Key。';
  }
}

async function clearCurrentKey() {
  const id = $('setProvider').value;
  const p = providers.find((x) => x.id === id);
  if (!p || !p.has_key) return;
  if (!confirm(`确定清除 ${p.label || id} 的 API Key？清除后要重新填写才能用这家模型。`)) return;
  await api(`/api/settings/key/${encodeURIComponent(id)}`, { method: 'DELETE' });
  const pv = await api('/api/providers');
  providers = pv.providers || [];
  renderProviderOptions();
  onProviderChange();
  toast('已清除该供应商的 Key');
}

function onProviderChange() {
  const p = providers.find((x) => x.id === $('setProvider').value);
  if (!p) return;
  $('providerHint').textContent = `${p.hint}${p.base_url ? ' · ' + p.base_url : ''}`;
  $('keyState').textContent = p.needs_key
    ? (p.has_key ? `当前：${p.masked}` : '该供应商需要 API Key')
    : '本地服务无需 Key';
  // 已经存过 Key 才给「清除」入口：没存过时这个按钮没意义
  if ($('clearKeyRow')) $('clearKeyRow').hidden = !p.has_key;
  $('setKey').value = '';
  $('setKey').placeholder = p.needs_key ? '粘贴 API Key' : '无需填写';
  onVisionChange();
}

function updateLlmDot(s) {
  const dot = $('llmDot'), txt = $('llmText');
  if (s.ready) {
    dot.className = 'dot on';
    txt.textContent = `模型：${s.llm_provider}`;
  } else {
    dot.className = 'dot warn';
    txt.textContent = '模型未配置';
  }
}

async function saveKey() {
  const provider = $('setProvider').value;
  const key = $('setKey').value.trim();
  if (!key) { toast('请先填写 Key', true); return; }
  await api('/api/settings/key', { method: 'POST', body: JSON.stringify({ provider, key }) });
  toast('已加密保存');
  await loadSettings();
}

/** 看图三项单独持久化：改完下拉就能生效，不用再点顶部的"保存设置"。 */
async function persistVision() {
  const patch = {
    vision_provider: $('setVision').value,
    vision_model: $('setVisionModel').value.trim(),
    vision_base_url: $('setVisionUrl').value.trim(),
  };
  await api('/api/settings', { method: 'POST', body: JSON.stringify(patch) });
}

/** 看图 Key 存到"看图服务商"自己的槽位，不会碰到文字模型的 Key。 */
async function saveVisionKey() {
  const provider = $('setVision').value;
  const key = $('setVisionKey').value.trim();
  if (!provider) { toast('请先选择看图模型', true); return; }
  if (!key) { toast('请先填写看图 Key', true); return; }
  await api('/api/settings/key', { method: 'POST', body: JSON.stringify({ provider, key }) });
  // 填了 Key 就是想用它看图，顺手把选择和模型名一起存下来
  await persistVision();
  $('setVisionKey').value = '';
  const pv = await api('/api/providers');
  providers = pv.providers || [];
  renderProviderOptions();
  onVisionChange();
  toast('看图 Key 已加密保存');
}

async function testConn() {
  const provider = $('setProvider').value;
  const key = $('setKey').value.trim();
  const box = $('testMsg');
  box.className = 'status';
  box.textContent = '测试中…';
  try {
    const r = await api('/api/settings/test', { method: 'POST', body: JSON.stringify({ provider, key }) });
    box.className = 'status ' + (r.ok ? 'ok' : 'bad');
    box.textContent = r.ok ? `正常：${r.message}` : r.message;
  } catch (e) {
    box.className = 'status bad';
    box.textContent = e.message;
  }
}

function renderFfmpeg(ff) {
  const box = $('ffState'), btn = $('btnFf');
  if (!ff) { box.textContent = '未知'; return; }
  const label = { system: '系统已安装', bundled: '随程序分发', downloaded: '已下载到用户目录' };
  if (ff.ready) {
    box.className = 'status ok';
    box.textContent = `就绪 · ${label[ff.source] || '可用'}`;
    btn.hidden = true;
  } else {
    box.className = 'status bad';
    box.textContent = '未找到，画面提取与转写不可用';
    btn.hidden = false;
  }
}

async function installFfmpeg() {
  const btn = $('btnFf'), box = $('ffState');
  btn.disabled = true;
  btn.textContent = '下载中…（约 92MB，可断点续传）';
  box.className = 'status';
  box.textContent = '正在下载并解压，请稍候…';
  try {
    const r = await api('/api/system/ffmpeg/install', { method: 'POST' });
    renderFfmpeg(r.ffmpeg);
    toast('ffmpeg 已就绪');
  } catch (e) {
    box.className = 'status bad';
    box.textContent = e.message;
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '一键下载（约 92MB）';
  }
}

function renderCache(c) {
  const box = $('cacheState'), btn = $('btnClean');
  if (!c) { box.textContent = '未知'; return; }
  if (!c.bytes) {
    box.className = 'status ok';
    box.textContent = '干净，没有缓存文件';
    btn.disabled = true;
    return;
  }
  box.className = 'status';
  box.textContent = `${c.human} · ${c.tasks} 个任务`;
  btn.disabled = false;
}

async function cleanCache() {
  const btn = $('btnClean'), box = $('cacheState');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '清理中…';
  box.textContent = '正在删除缓存的视频与音频…';
  try {
    const r = await api('/api/system/cache/clean?mode=all', { method: 'POST' });
    renderCache(r.cache);
    toast(r.freed ? `已释放 ${r.human}` : '没有可清理的文件');
  } catch (e) {
    box.textContent = e.message;
    toast(e.message, true);
  } finally {
    btn.textContent = old;
    btn.disabled = false;
  }
}

async function saveAll() {
  const body = {
    llm_provider: $('setProvider').value,
    asr_model: $('setAsr').value,
    asr_language: $('setAsrLang').value || 'auto',
    note_detail: $('setDetail').value,
    hd_frames: $('setHd').checked,
    parallel_pipeline: $('setParallel').checked,
    polish_punctuation: $('setPolish').checked,
    bilingual_transcript: $('setBilingual').value !== 'source',
    frame_captions: $('setCaption').checked,
    travel_itinerary: $('setItinerary').checked,
    vision_provider: $('setVision').value,
    vision_base_url: $('setVisionUrl').value.trim(),
    vision_model: $('setVisionModel').value.trim(),
    cache_policy: $('setCachePolicy').value,
  };
  const out = $('setOutput').value.trim();
  if (out) body.output_dir = out;
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify(body) });
    toast('设置已保存');
    await loadSettings();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ==================== 事件绑定 ==================== */
function bindEvents() {
  document.querySelectorAll('.nav-item').forEach((b) => {
    b.onclick = () => switchView(b.dataset.view);
  });

  $('setDetail').onchange = renderDetail;

  $('btnUpdSave').onclick = () => saveUpdateRepo();
  $('btnUpdCheck').onclick = () => checkUpdate();
  $('btnUpdDownload').onclick = () => downloadUpdate();
  $('btnUpdInstall').onclick = () => installUpdate();
  $('btnUpdRelease').onclick = () => {
    if (updReleaseUrl) window.open(updReleaseUrl, '_blank', 'noopener');
  };
  $('updRepo').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') $('btnUpdSave').click();
  });

  $('btnRefreshDash').onclick = () => loadOverview().catch((e) => toast(e.message, true));
  $('quickStart').onclick = async () => {
    const url = $('quickUrl').value.trim();
    if (!url) { toast('请先粘贴链接', true); return; }
    try {
      await api('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({
          url,
          frames: $('qFrames').checked,
          ocr: $('qOcr').checked,
          summary: $('qSummary').checked,
          lecture: $('qLecture').checked,
        }),
      });
      $('quickUrl').value = '';
      toast('已加入队列');
      switchView('queue');
    } catch (e) {
      toast(e.message, true);
    }
  };
  $('quickUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('quickStart').click(); });

  $('btnQr').onclick = startQr;
  $('btnLogout').onclick = logout;
  $('btnReloadFav').onclick = () => loadFolders();
  $('favSelect').onchange = () => { page = 1; selected.clear(); loadVideos(); };
  $('btnPrev').onclick = () => { if (page > 1) { page -= 1; loadVideos(); } };
  $('btnNext').onclick = () => { if (hasMore) { page += 1; loadVideos(); } };
  $('btnSelectAll').onclick = () => { videos.forEach((v) => selected.add(v.bvid)); renderVideos(); };
  $('btnSelectNone').onclick = () => { selected.clear(); renderVideos(); };
  $('btnImport').onclick = doImport;
  const syncSeriesLimit = () => {
    const on = $('batchExpand').checked;
    $('batchSeriesLimit').disabled = !on;
    $('batchSeriesLimit').title = on ? '' : '先勾选「展开合集/分P」';
  };
  $('batchExpand').addEventListener('change', syncSeriesLimit);
  syncSeriesLimit();
  $('btnBatch').onclick = doBatch;
  $('batchLimit').addEventListener('keydown', (e) => { if (e.key === 'Enter') doBatch(); });

  $('btnRefreshTasks').onclick = () => loadTasks(true).catch((e) => toast(e.message, true));
  $('btnPauseAll').onclick = async () => {
    try { await api('/api/tasks/pause-all', { method: 'POST' }); loadTasks(true); }
    catch (e) { toast(e.message, true); }
  };
  $('btnResumeAll').onclick = async () => {
    try { await api('/api/tasks/resume-all', { method: 'POST' }); loadTasks(true); }
    catch (e) { toast(e.message, true); }
  };
  $('queueAdd').onclick = addTask;
  $('queueUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') addTask(); });

  $('btnSearch').onclick = () => loadLibrary($('kbSearch').value.trim());
  $('kbSearch').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') loadLibrary($('kbSearch').value.trim());
  });
  $('btnClearSearch').onclick = () => { $('kbSearch').value = ''; loadLibrary(); };
  $('btnLibBack').onclick = () => (currentSeries ? leaveSeries() : enterCollection(''));
  $('btnNewCollection').onclick = () => newCollection().catch((e) => toast(e.message, true));
  $('libSort').onchange = (e) => setLibSort(e.target.value);
  $('libSort').value = libSort;          // 记住上次选的口径
  $('btnSelectMode').onclick = () => {
    if (selectMode) { exitSelect(); return; }
    selectMode = true;
    $('kbGrid').classList.add('selecting');
    syncSelectUI();
  };
  $('btnMovePicked').onclick = () => openMoveDialog([...picked]);
  $('moveOk').onclick = () => doMove().catch((e) => toast(e.message, true));
  $('moveCancel').onclick = () => $('moveDlg').close();
  $('btnMoveNew').onclick = () => newCollectionInMove().catch((e) => toast(e.message, true));

  $('readerBack').onclick = () => { $('reader').hidden = true; };
  $('readerTabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.reader-tab');
    if (!tab || tab.dataset.tab === readerTab) return;
    readerTab = tab.dataset.tab;
    renderReaderTab();
  });
  $('btnMakeLecture').onclick = makeLecture;
  // 合集选集：勾选、快捷操作、按序号套用
  $('epList').addEventListener('change', (e) => {
    const box = e.target.closest('input[data-ep]');
    if (!box) return;
    const i = Number(box.dataset.ep);
    if (box.checked) picker.picked.add(i); else picker.picked.delete(i);
    const row = box.closest('.ep-item');
    if (row) row.classList.toggle('on', box.checked);
    refreshPickCount();
  });
  document.querySelectorAll('[data-pick-act]').forEach((btn) => {
    btn.onclick = () => {
      const n = picker.items.length;
      if (!n) return;
      const all = [...Array(n).keys()];
      const act = btn.dataset.pickAct;
      if (act === 'all') setPicked(all);
      else if (act === 'none') setPicked([]);
      else if (act === 'invert') setPicked(all.filter((i) => !picker.picked.has(i)));
      else if (act === 'undone') setPicked(all.filter((i) => !picker.items[i].done));
      $('pickState').textContent = '';
      $('pickState').className = 'hint';
    };
  });
  const applyRange = () => {
    const n = picker.items.length;
    if (!n) return;
    const r = parseRange($('pickRange').value, n);
    if (r.error) {
      $('pickState').textContent = r.error;
      $('pickState').className = 'hint bad';
      return;
    }
    if (!r.nums.length) return;
    setPicked(r.nums.map((x) => x - 1));
    $('pickState').textContent = `已按序号勾选 ${r.nums.length} 集`;
    $('pickState').className = 'hint';
  };
  $('pickApply').onclick = applyRange;
  $('pickRange').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); applyRange(); }
  });
  $('pickRedo').addEventListener('change', refreshPickCount);
  $('pickCancel').onclick = () => $('pickDlg').close();
  $('pickOk').onclick = submitPicks;

  $('conceptFilter').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-item');
    if (!btn) return;
    conceptFilter = btn.dataset.cf;
    document.querySelectorAll('#conceptFilter .seg-item').forEach((b) => {
      b.classList.toggle('active', b === btn);
    });
    renderConceptList();
  });
  $('btnQuizGen').onclick = () => makeQuiz().catch((e) => toast(e.message, true));
  $('btnQuizRefresh').onclick = () => loadQuiz().catch((e) => toast(e.message, true));
  $('btnQuizPrev').onclick = () => gotoQuestion((quizRun ? quizRun.idx : 0) - 1);
  $('btnQuizNext').onclick = () => gotoQuestion((quizRun ? quizRun.idx : 0) + 1);
  $('btnQuizSubmit').onclick = submitQuiz;
  $('btnQuizQuit').onclick = quitQuiz;
  $('btnQuizAgain').onclick = retryQuiz;
  $('btnQuizCards').onclick = wrongToCards;

  $('conceptSearch').addEventListener('input', renderConceptList);
  $('conceptKnowledgeOnly').addEventListener('change', renderConceptList);
  $('btnTidyConcepts').onclick = tidyConcepts;

  // 概念列表：分组展开/收起 + 选中（列表是动态渲染的，用委托）
  $('conceptList').addEventListener('click', (e) => {
    const head = e.target.closest('[data-toggle]');
    if (head) {
      const key = head.dataset.toggle;
      if (conceptCollapsed.has(key)) conceptCollapsed.delete(key);
      else conceptCollapsed.add(key);
      renderConceptList();
      return;
    }
    const item = e.target.closest('[data-key]');
    if (item) selectConcept(item.dataset.key);
  });

  document.querySelectorAll('#conceptGroupMode .seg-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.gm === conceptGroupMode);
    btn.onclick = () => {
      if (conceptGroupMode === btn.dataset.gm) return;
      document.querySelectorAll('#conceptGroupMode .seg-item').forEach((b) => {
        b.classList.toggle('active', b === btn);
      });
      switchGroupMode(btn.dataset.gm);
    };
  });
  // 刷新：先用模型重新判一遍（规则判过的会升级成模型结论），再载入列表
  $('btnRefreshConcepts').onclick = async () => {
    try {
      await api('/api/concepts/classify', { method: 'POST' });
    } catch (e) { /* 没 Key 就只用已有分类 */ }
    loadConcepts().catch((e) => toast(e.message, true));
  };

  $('flashcard').onclick = flipCard;
  document.querySelectorAll('.review-actions .btn').forEach((b) => {
    b.onclick = () => gradeCard(b.dataset.grade);
  });
/* 查重：旧版「补充卡片」不看已有题目，堆了一批换个说法问同一件事的卡。
   先只报告（dry），让人看一眼再决定——删卡片不可逆。 */
async function dedupeCards() {
  try {
    const r = await api('/api/cards/dedupe?dry=1', { method: 'POST' });
    const groups = r.groups || [];
    if (!groups.length) {
      toast('没有发现重复卡片');
      return;
    }
    const head = groups.slice(0, 3)
      .map((g) => `· ${plainPreview(g.keep.front).slice(0, 30)}（另有 ${g.drop.length} 张同义）`)
      .join('\n');
    const more = groups.length > 3 ? `\n… 还有 ${groups.length - 3} 组` : '';
    const ok = confirm(
      `发现 ${groups.length} 组重复，将删除 ${r.duplicates} 张：\n\n${head}${more}\n\n`
      + '每组保留复习次数多的那一张，其余删除（笔记本身不受影响）。继续？',
    );
    if (!ok) return;
    const done = await api('/api/cards/dedupe?dry=0', { method: 'POST' });
    toast(`已删除 ${done.removed} 张重复卡片`);
    cardLibGroups = [];
    await loadReview();
  } catch (e) {
    toast(e.message, true);
  }
}

/* 例题卡：答案是「第一行乘 1/3、第二行乘 -1」这种某道题的具体系数。
   复习卡该考"换个例子还成立"的知识，具体题目属于自测卷。 */
async function cleanExampleCards() {
  try {
    const r = await api('/api/cards/clean-examples?dry=1', { method: 'POST' });
    const cards = r.cards || [];
    if (!cards.length) {
      toast('没有例题卡');
      return;
    }
    const head = cards.slice(0, 3)
      .map((c) => `· ${plainPreview(c.front).slice(0, 34)}`)
      .join('\n');
    const more = cards.length > 3 ? `\n… 还有 ${cards.length - 3} 张` : '';
    const ok = confirm(
      `发现 ${cards.length} 张例题卡：\n\n${head}${more}\n\n`
      + '这些卡的答案在复述某道题的具体系数，换个例子就不成立，复习时记不住也搬不动。\n'
      + '删除它们？（笔记本身不受影响，需要做题可以用「自测」）',
    );
    if (!ok) return;
    const done = await api('/api/cards/clean-examples?dry=0', { method: 'POST' });
    toast(`已删除 ${done.removed} 张例题卡`);
    cardLibGroups = [];
    await loadReview();
  } catch (e) {
    toast(e.message, true);
  }
}

  $('btnRefreshCards').onclick = () => {
    cardLibGroups = [];
    loadReview().catch((e) => toast(e.message, true));
  };
  $('btnDedupeCards').onclick = dedupeCards;
  $('btnCleanExamples').onclick = cleanExampleCards;
  $('btnReviewRules').onclick = () => $('rulesDlg').showModal();
  $('rulesClose').onclick = () => $('rulesDlg').close();

  // 复习范围：下拉切换 + 自定义对话框
  $('scopeSelect').addEventListener('change', onScopeChange);
  $('btnScopeCustom').onclick = openScopeDlg;
  document.querySelectorAll('#reviewKindSeg .seg-item').forEach((b) => {
    b.onclick = () => setReviewKind(b.dataset.kind);
  });
  $('btnScopeWiden').onclick = openScopeDlg;
  $('btnCram').onclick = () => {
    reviewCram = true;
    loadReview().catch((e) => toast(e.message, true));
  };
  $('cardLibSearch').addEventListener('input', renderCardLib);
  $('scopeCancel').onclick = () => { $('scopeDlg').close(); renderScopeSelect(); };
  $('scopeClear').onclick = () => { scopeDraft.clear(); renderScopeDlg(); };
  $('scopeOk').onclick = applyScopeDlg;
  $('scopeSearch').addEventListener('input', renderScopeDlg);
  $('scopeUndue').onclick = () => {
    scopeDraft.clear();
    for (const g of cardLibGroups) {
      for (const n of g.notes) {
        if (n.cards.some((c) => Number(c.due || 0) <= nowSec())) scopeDraft.add(n.id);
      }
    }
    renderScopeDlg();
  };
  $('scopeGroups').addEventListener('change', (e) => {
    const box = e.target;
    if (box.dataset.group) { toggleGroup(box.dataset.group, box.checked); return; }
    if (!box.dataset.note) return;
    if (box.checked) scopeDraft.add(box.dataset.note);
    else scopeDraft.delete(box.dataset.note);
    syncGroupBoxes();
    updateScopeDlgState();
  });

  // 卡片库：生成 / 复习本篇 / 现在复习 / 删除（动态渲染，用委托）
  $('cardLib').addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    e.preventDefault();          // 别让 <summary> 里的按钮顺带折叠/展开
    e.stopPropagation();
    if (btn.dataset.gen) return generateCards(btn, btn.dataset.gen);
    if (btn.dataset.review) return reviewNote(btn.dataset.review);
    if (btn.dataset.now) return reviewCardNow(btn.dataset.now);
    if (btn.dataset.del) return deleteCard(btn.dataset.del);
  });

  document.addEventListener('keydown', (e) => {
    if (currentView !== 'review') return;
    if (e.target.closest && e.target.closest('input, textarea, select')) return;
    if (e.code === 'Space') { e.preventDefault(); flipCard(); return; }
    const map = { 1: 'again', 2: 'hard', 3: 'good', 4: 'easy' };
    if (map[e.key] && revealed) gradeCard(map[e.key]);
  });

  $('btnExport').onclick = () => { setExportState(''); $('exportDlg').showModal(); };
  $('exportCancel').onclick = () => $('exportDlg').close();
  document.querySelectorAll('.export-item').forEach((btn) => {
    btn.onclick = () => {
      const fmt = btn.dataset.fmt;
      if (fmt !== 'print') { downloadExport(fmt); return; }
      $('exportDlg').close();
      // 关键帧图和大图要先解码，否则打印预览里会缺图
      waitAssets().then(() => setTimeout(() => window.print(), 120));
    };
  });
  $('readerReveal').onclick = () => {
    if (currentNoteId) api(`/api/library/${encodeURIComponent(currentNoteId)}/reveal`, { method: 'POST' })
      .catch((e) => toast(e.message, true));
  };
  $('readerZip').onclick = () => {
    if (currentNoteId) window.location = `/api/library/${encodeURIComponent(currentNoteId)}/download`;
  };
  $('readerDel').onclick = () => {
    if (!currentNoteId) return;
    deleteNote(currentNoteId, $('readerTitle').textContent).catch((e) => toast(e.message, true));
  };

  $('setProvider').onchange = onProviderChange;
  $('setVision').onchange = () => { onVisionChange(); persistVision().catch((e) => toast(e.message, true)); };
  $('setVisionModel').onchange = () => persistVision().catch((e) => toast(e.message, true));
  $('setVisionUrl').onchange = () => persistVision().catch((e) => toast(e.message, true));
  $('btnSaveKey').onclick = () => saveKey().catch((e) => toast(e.message, true));
  $('btnClearKey').onclick = () => clearCurrentKey().catch((e) => toast(e.message, true));
  $('btnSaveVisionKey').onclick = () => saveVisionKey().catch((e) => toast(e.message, true));
  $('btnTest').onclick = testConn;
  $('btnSaveAll').onclick = saveAll;
  $('setLan').onchange = (e) => toggleLan(e.target.checked);
  $('btnImportWords').onclick = () => $('wbFile').click();
  $('btnFillWords').onclick = fillWordDefinitions;
  $('btnWordsToCards').onclick = wordsToCards;
  $('btnRescanWords').onclick = async () => {
    const state = $('wbState');
    state.textContent = '正在重算…';
    try {
      const d = await api('/api/wordbooks/rescan', { method: 'POST' });
      state.textContent = d.changed ? `更新 ${d.changed} 篇` : '没有需要更新的笔记';
      toast(d.changed
        ? `${d.changed} 篇笔记已标出生词（共 ${d.words} 个词）`
        : '现有笔记都不需要更新（或不是英语内容）');
    } catch (e) {
      state.textContent = '';
      toast(e.message, true);
    }
  };
  $('wbFile').onchange = async (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';                       // 允许重复选同一个文件
    if (!file) return;
    try {
      await importWordbook(file);
    } catch (err) {
      $('wbState').textContent = '';
      toast(err.message, true);
    }
  };
  $('btnRotateLan').onclick = rotateLan;
  $('btnCopyLan').onclick = copyLanUrl;
  $('btnLanDoctor').onclick = () => {
    runLanDoctor().catch((e) => toast(e.message, true));
  };
  $('btnExportPack').onclick = () => {
    $('exportState').textContent = '正在打包（图片要压一遍）…';
    setTimeout(() => { $('exportState').textContent = '下载完直接发到手机'; }, 2500);
  };
  $('btnFf').onclick = installFfmpeg;
  $('btnClean').onclick = cleanCache;

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('reader').hidden) $('reader').hidden = true;
  });
}

/* ==================== 启动 ==================== */
async function init() {
  bindEvents();
  // 知识地图节点折叠：点标签收起/展开它的下级
  $('readerBody').addEventListener('click', (e) => {
    const label = e.target.closest('.mm-label');
    if (!label) return;
    const li = label.closest('li');
    if (li && li.classList.contains('has-kids')) li.classList.toggle('mm-closed');
  });
  const initial = location.hash.slice(1);
  const start = VIEW_LOADERS[initial] ? initial : 'dashboard';
  try {
    await VIEW_LOADERS[start]();
    if (start !== 'dashboard') switchView(start);
  } catch (e) {
    toast('初始化失败：' + e.message, true);
  }

  // 打印专用入口：/?print=<笔记id>&t=1|0 —— 后端导出 PDF 时用浏览器打开的就是这个地址
  const printTarget = new URLSearchParams(location.search).get('print');
  if (printTarget) {
    document.body.classList.add('print-mode');
    try {
      await openNote(printTarget);
      if (new URLSearchParams(location.search).get('t') !== '1') {
        document.querySelectorAll('#readerBody details.fold').forEach((el) => el.remove());
      }
      await waitAssets();
      document.documentElement.setAttribute('data-print-ready', '1');
    } catch (e) {
      document.documentElement.setAttribute('data-print-ready', 'error');
    }
  }

  // 卷面入口：/?printquiz=<id> —— 后端导出 PDF 时打开的就是这个地址
  const printQuiz = new URLSearchParams(location.search).get('printquiz');
  if (printQuiz) {
    try {
      await renderQuizSheet(printQuiz);
      await waitAssets();
      document.documentElement.setAttribute('data-print-ready', '1');
    } catch (e) {
      document.documentElement.setAttribute('data-print-ready', 'error');
    }
  }

  api('/api/auth/status').then((s) => {
    renderAuth(s);
    if (!s.logged_in) {
      $('biliDot').className = 'dot';
      $('biliText').textContent = 'B站未登录';
    }
  }).catch(() => {});
  api('/api/settings').then(updateLlmDot).catch(() => {});
  // 静默查更新：后端自己限频（12 小时），连不上也不报错、不阻塞首屏
  api('/api/update/auto', { method: 'POST' }).catch(() => {});
  updateDueBadge();

  window.addEventListener('hashchange', () => {
    const name = location.hash.slice(1);
    if (VIEW_LOADERS[name] && name !== currentView) switchView(name);
  });

  setInterval(() => { loadTasks().catch(() => {}); }, 3000);
}

/* ================= 界面主题（极光 / 极简 / 科技 / 纸感） ================= */
const THEME_KEY = 'bn.theme';
const THEME_NAME = { aurora: '极光', minimal: '极简', tech: '科技', paper: '纸感' };
const THEME_IDS = Object.keys(THEME_NAME);

function applyTheme(id, opts) {
  const opt = opts || {};
  if (THEME_IDS.indexOf(id) < 0) id = 'aurora';
  document.documentElement.setAttribute('data-theme', id);
  try { localStorage.setItem(THEME_KEY, id); } catch (e) {}
  document.querySelectorAll('[data-theme-pick]').forEach((el) => {
    el.classList.toggle('active', el.getAttribute('data-theme-pick') === id);
  });
  if (opt.announce) {
    const t = document.getElementById('toast');
    if (t) toast('已切换到「' + THEME_NAME[id] + '」主题');
  }
}

// 点击任意换肤按钮（侧栏色点 / 设置页预览卡）都走这里
document.addEventListener('click', (e) => {
  const el = e.target.closest ? e.target.closest('[data-theme-pick]') : null;
  if (!el) return;
  const id = el.getAttribute('data-theme-pick');
  if (id === document.documentElement.getAttribute('data-theme')) return;
  applyTheme(id, { announce: true });
});

// 首次进入：回显已保存的主题（head 里的内联脚本已先落定，这里同步按钮状态）
applyTheme(document.documentElement.getAttribute('data-theme') || 'aurora');

init();

/* ================= 界面动效（聚光灯 / 磁吸按钮 / 涟漪 / 卡片微倾斜） =================
   全部用事件委托挂在 document 上，动态渲染出来的卡片同样生效。
   用户系统开了"减少动态效果"时整体跳过。 */
(() => {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const FINE = matchMedia('(pointer: fine)').matches; // 触屏设备不做跟随类动效
  const SPOT_SEL = '.panel, .stat, .note-card, .video-card';
  const TILT_MAX = 2.2; // 笔记卡片最大倾斜角（度），克制一点

  if (FINE) {
    // 聚光灯：把鼠标坐标写进 CSS 变量，光斑位置由 --mx/--my 决定
    document.addEventListener('mousemove', (e) => {
      const el = e.target.closest ? e.target.closest(SPOT_SEL) : null;
      if (!el) return;
      const r = el.getBoundingClientRect();
      el.style.setProperty('--mx', (e.clientX - r.left) + 'px');
      el.style.setProperty('--my', (e.clientY - r.top) + 'px');
    }, { passive: true });

    // 磁吸按钮：主按钮在鼠标靠近时轻微被"吸"过来
    document.addEventListener('mousemove', (e) => {
      const btn = e.target.closest ? e.target.closest('.btn.primary:not(:disabled)') : null;
      document.querySelectorAll('.btn.primary.magnetic').forEach((b) => {
        if (b !== btn) { b.style.transform = ''; b.classList.remove('magnetic'); }
      });
      if (!btn) return;
      const r = btn.getBoundingClientRect();
      const dx = e.clientX - (r.left + r.width / 2);
      const dy = e.clientY - (r.top + r.height / 2);
      if (Math.abs(dx) < r.width / 2 + 18 && Math.abs(dy) < r.height / 2 + 14) {
        btn.classList.add('magnetic');
        btn.style.transform = `translate(${dx * 0.12}px, ${dy * 0.18}px)`;
      }
    }, { passive: true });

    // 笔记卡片微三维倾斜：只在卡片本体上生效，多选模式下不歪
    document.addEventListener('mousemove', (e) => {
      const card = e.target.closest ? e.target.closest('.note-card') : null;
      document.querySelectorAll('.note-card.tilting').forEach((c) => {
        if (c !== card) { c.style.transform = ''; c.classList.remove('tilting'); }
      });
      if (!card || card.closest('.selecting')) return;
      const r = card.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width - 0.5;
      const py = (e.clientY - r.top) / r.height - 0.5;
      card.classList.add('tilting');
      card.style.transform =
        `perspective(700px) rotateY(${(px * TILT_MAX).toFixed(2)}deg) rotateX(${(-py * TILT_MAX).toFixed(2)}deg) translateY(-2px)`;
    }, { passive: true });

    document.addEventListener('mouseout', (e) => {
      const card = e.target.closest ? e.target.closest('.note-card.tilting') : null;
      if (card && !card.contains(e.relatedTarget)) {
        card.style.transform = ''; card.classList.remove('tilting');
      }
    }, { passive: true });
  }

  // 点击涟漪：按下的位置炸开一圈柔光
  document.addEventListener('pointerdown', (e) => {
    const btn = e.target.closest ? e.target.closest('.btn') : null;
    if (!btn || btn.disabled) return;
    const r = btn.getBoundingClientRect();
    const d = Math.max(r.width, r.height);
    const s = document.createElement('span');
    s.className = 'ripple';
    s.style.width = s.style.height = d + 'px';
    s.style.left = (e.clientX - r.left - d / 2) + 'px';
    s.style.top = (e.clientY - r.top - d / 2) + 'px';
    btn.appendChild(s);
    s.addEventListener('animationend', () => s.remove(), { once: true });
  }, { passive: true });
})();


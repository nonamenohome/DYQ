// B站 UP 主视频看板 —— 原生 JS，无依赖
'use strict';

var POLL_MS = 4000;               // 轮询间隔 4 秒
var DEBOUNCE_MS = 300;            // 搜索防抖

var $ = function (id) { return document.getElementById(id); };
var listEl = $('list'), emptyEl = $('empty'), errEl = $('errBox');

var state = {
  q: '',
  status: 'all',
  order: 'pubdate_desc',
  timer: null,
  lastSig: '',      // 数据未变则跳过重渲染，避免封面图反复加载闪烁
  downloading: 0
};

// ---------- 工具 ----------

// unix 秒 → YYYY-MM-DD HH:mm；无效值返回 '-'
function fmtTime(ts) {
  var n = Number(ts);
  if (!isFinite(n) || n <= 0) return '-';
  var d = new Date(n * 1000);
  if (isNaN(d.getTime())) return '-';
  var p = function (x) { return String(x).padStart(2, '0'); };
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
         ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

// 秒 → mm:ss 或 h:mm:ss；无效值返回 '-'
function fmtDuration(s) {
  var n = Number(s);
  if (!isFinite(n) || n < 0) return '-';
  n = Math.floor(n);
  var h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), sec = n % 60;
  var p = function (x) { return String(x).padStart(2, '0'); };
  return h > 0 ? h + ':' + p(m) + ':' + p(sec) : p(m) + ':' + p(sec);
}

// 数字格式化，避免 NaN / undefined 泄漏到界面
function fmtNum(v, fallback) {
  var n = Number(v);
  return isFinite(n) ? String(n) : (fallback || '0');
}

var STATUS_TEXT = {
  pending: '待下载', downloading: '下载中', done: '已完成', failed: '失败'
};

function statusText(s) { return STATUS_TEXT[s] || '未知'; }
function statusClass(s) { return STATUS_TEXT[s] ? s : 'unknown'; }

// ---------- 请求 ----------

function api(path, opts) {
  return fetch(path, opts).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  });
}

function showError(msg) {
  errEl.textContent = '出错了：' + msg;
  errEl.hidden = false;
}

function clearError() { errEl.hidden = true; }

// ---------- 顶部状态条 ----------

function renderStatus(st) {
  if (!st || typeof st !== 'object') return;
  if (st.up_name) $('upName').textContent = st.up_name + ' · 视频看板';
  $('statCount').textContent = fmtNum(st.video_count);
  $('statLast').textContent = st.last_check ? fmtTime(Date.parse(st.last_check) / 1000) : '尚未检查';
  $('statNext').textContent = st.next_check ? fmtTime(Date.parse(st.next_check) / 1000) : '-';
  var dl = Number(st.downloading);
  state.downloading = isFinite(dl) ? dl : 0;
  $('statDl').textContent = state.downloading;
  $('statPoller').textContent = st.poller_enabled ? '运行中' : '已停止';
}

// ---------- 单个视频卡片 ----------

function buildCard(v) {
  var bvid = v.bvid || '';
  var card = document.createElement('div');
  card.className = 'card';
  card.dataset.bvid = bvid;

  // 封面：loading=lazy 懒加载；referrerpolicy=no-referrer 绕过 B 站图床防盗链(否则 403 裂图)
  var img = document.createElement('img');
  img.className = 'cover';
  img.loading = 'lazy';
  img.referrerPolicy = 'no-referrer';
  img.alt = v.title || '';
  if (v.cover) img.src = v.cover;
  card.appendChild(img);

  var body = document.createElement('div');
  body.className = 'card-body';

  var title = document.createElement('div');
  title.className = 'title';
  title.textContent = v.title || '(无标题)';
  body.appendChild(title);

  var meta = document.createElement('div');
  meta.className = 'meta';
  var t1 = document.createElement('span');
  t1.textContent = fmtTime(v.pubdate);
  var t2 = document.createElement('span');
  t2.textContent = '时长 ' + fmtDuration(v.duration);
  meta.appendChild(t1);
  meta.appendChild(t2);
  body.appendChild(meta);

  var foot = document.createElement('div');
  foot.className = 'card-foot';

  var badge = document.createElement('span');
  badge.className = 'badge ' + statusClass(v.status);
  badge.textContent = statusText(v.status);
  foot.appendChild(badge);

  var st = v.status;
  if (st === 'downloading') {
    // 进度条
    var pct = Number(v.percent);
    if (!isFinite(pct)) pct = 0;
    pct = Math.max(0, Math.min(100, pct));
    var prog = document.createElement('div');
    prog.className = 'progress';
    var bar = document.createElement('div');
    bar.className = 'bar';
    var fill = document.createElement('div');
    fill.className = 'fill';
    fill.style.width = pct + '%';
    bar.appendChild(fill);
    var txt = document.createElement('div');
    txt.className = 'txt';
    txt.textContent = pct.toFixed(1) + '%' + (v.speed ? ' · ' + v.speed : '') + (v.eta ? ' · 剩余 ' + v.eta : '');
    prog.appendChild(bar);
    prog.appendChild(txt);
    foot.appendChild(prog);
  } else if (st === 'done') {
    // 纯本地服务，直接展示路径文本
    if (v.file_path) {
      var p = document.createElement('div');
      p.className = 'file-path';
      p.textContent = v.file_path;
      p.title = v.file_path;
      foot.appendChild(p);
    } else {
      var d = document.createElement('span');
      d.className = 'meta';
      d.textContent = '已完成';
      foot.appendChild(d);
    }
  } else {
    // pending / failed / 未知 → 下载按钮
    var btn = document.createElement('button');
    btn.className = 'btn sm';
    btn.textContent = st === 'failed' ? '重试' : '下载';
    btn.addEventListener('click', function () { enqueue(bvid, btn); });
    foot.appendChild(btn);
  }

  body.appendChild(foot);
  card.appendChild(body);
  return card;
}

// 手动入队下载
function enqueue(bvid, btn) {
  if (!bvid) return;
  btn.disabled = true;
  var old = btn.textContent;
  btn.textContent = '…';
  api('/api/download/' + encodeURIComponent(bvid), { method: 'POST' })
    .then(function (res) {
      if (res && res.ok === false) throw new Error(res.error || '下载入队失败');
      clearError();
      return tick();
    })
    .catch(function (e) {
      showError('下载入队失败（' + (e && e.message ? e.message : e) + '）');
    })
    .finally(function () {
      btn.disabled = false;
      btn.textContent = old;
    });
}

// ---------- 列表渲染 ----------

function renderVideos(videos) {
  listEl.textContent = '';
  if (!videos || !videos.length) {
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;
  var frag = document.createDocumentFragment();
  for (var i = 0; i < videos.length; i++) frag.appendChild(buildCard(videos[i]));
  listEl.appendChild(frag);
}

// ---------- 主循环 ----------

function queryString() {
  var parts = [];
  if (state.q) parts.push('q=' + encodeURIComponent(state.q));
  parts.push('status=' + encodeURIComponent(state.status));
  parts.push('order=' + encodeURIComponent(state.order));
  return '?' + parts.join('&');
}

// 有下载中的任务才拉 /api/progress，按 bvid 合并 percent/speed/eta
function loadProgress() {
  if (state.downloading <= 0) return Promise.resolve({});
  return api('/api/progress').then(function (rows) {
    var map = {};
    if (Array.isArray(rows)) {
      rows.forEach(function (r) { if (r && r.bvid) map[r.bvid] = r; });
    }
    return map;
  }).catch(function () { return {}; });
}

function tick() {
  return Promise.all([
    api('/api/status').catch(function () { return null; }),
    api('/api/videos' + queryString()).catch(function (e) { showError('加载视频列表失败（' + e.message + '）'); return null; })
  ]).then(function (res) {
    renderStatus(res[0]);
    return loadProgress().then(function (pmap) {
      var videos = res[1];
      if (videos) {
        if (pmap) {
          videos.forEach(function (v) {
            var p = pmap[v.bvid];
            if (p) { v.percent = p.percent; v.speed = p.speed; v.eta = p.eta; }
          });
        }
        if (!Array.isArray(videos)) videos = [];
        // 数据没变就不重渲染，省得封面图反复重载
        var sig = JSON.stringify(videos);
        if (sig !== state.lastSig) {
          state.lastSig = sig;
          renderVideos(videos);
        }
        if (res[0]) clearError();
      }
    });
  });
}

function startPolling() {
  if (state.timer) return;
  state.timer = setInterval(tick, POLL_MS);
}

function stopPolling() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

// ---------- 事件绑定 ----------

var debounceTimer = null;
$('fSearch').addEventListener('input', function (e) {
  var val = e.target.value;
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(function () {
    state.q = val.trim();
    state.lastSig = '';   // 条件变了，强制重渲染
    tick();
  }, DEBOUNCE_MS);
});

$('fStatus').addEventListener('change', function (e) {
  state.status = e.target.value || 'all';
  state.lastSig = '';
  tick();
});

$('fOrder').addEventListener('change', function (e) {
  state.order = e.target.value || 'pubdate_desc';
  state.lastSig = '';
  tick();
});

$('btnRefresh').addEventListener('click', function (e) {
  var btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = '检查中…';
  api('/api/refresh', { method: 'POST' })
    .then(function () { clearError(); return tick(); })
    .catch(function (err) { showError('触发检查失败（' + (err && err.message ? err.message : err) + '）'); })
    .finally(function () {
      btn.disabled = false;
      btn.textContent = '立即检查新投稿';
    });
});

// 页面不可见时暂停轮询，回到前台立即刷新一次
document.addEventListener('visibilitychange', function () {
  if (document.visibilityState === 'hidden') {
    stopPolling();
  } else {
    state.lastSig = '';
    tick();
    startPolling();
  }
});

// 启动
tick();
startPolling();

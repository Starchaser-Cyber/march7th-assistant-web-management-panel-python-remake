/* ===== 主题（默认星铁粉蓝 三月七）===== */
var THEMES = [
  { name: 'march7', icon: '🎀', label: '星铁粉蓝' },
  { name: 'light',  icon: '☀️', label: '亮色' },
  { name: 'dark',   icon: '🌙', label: '深色' },
  { name: 'auto',   icon: '🖥️', label: '跟随系统' }
];
var _themeMq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
function themeIdx(name) {
  for (var i = 0; i < THEMES.length; i++) if (THEMES[i].name === name) return i;
  return 0;
}
function themeResolved(name) {
  if (name !== 'auto') return name;
  return (_themeMq && _themeMq.matches) ? 'dark' : 'march7';
}
function applyTheme(name) {
  var idx = themeIdx(name), d = document.documentElement, btn = document.getElementById('themeBtn');
  d.setAttribute('data-theme', themeResolved(THEMES[idx].name));
  if (btn) {
    btn.textContent = THEMES[idx].icon;
    btn.title = '切换主题：' + THEMES[idx].label + ' → ' + THEMES[(idx + 1) % THEMES.length].label;
  }
  var btnM = document.getElementById('themeBtnM');
  if (btnM) btnM.textContent = THEMES[idx].icon;
  try { localStorage.setItem('m7a_theme', THEMES[idx].name); } catch(e) {}
}
function toggleTheme() {
  var cur = null;
  try { cur = localStorage.getItem('m7a_theme'); } catch(e) {}
  if (!cur || themeIdx(cur) < 0 || (cur !== 'auto' && themeIdx(cur) === 0 && cur !== 'march7')) {
    cur = document.documentElement.getAttribute('data-theme') || 'march7';
  }
  if (['march7','light','dark','auto'].indexOf(cur) < 0) cur = 'march7';
  applyTheme(THEMES[(themeIdx(cur) + 1) % THEMES.length].name);
}
(function() {
  var t = null;
  try { t = localStorage.getItem('m7a_theme'); } catch(e) {}
  if (['march7','light','dark','auto'].indexOf(t) < 0) t = 'march7';
  applyTheme(t);
  if (_themeMq) {
    var onMq = function() {
      var s = null;
      try { s = localStorage.getItem('m7a_theme'); } catch(e) {}
      if (s === 'auto') applyTheme('auto');
    };
    if (_themeMq.addEventListener) _themeMq.addEventListener('change', onMq);
    else if (_themeMq.addListener) _themeMq.addListener(onMq);
  }
})();

/* ===== 侧边栏（移动端抽屉） ===== */
function openSidebar() {
  var s = document.getElementById('sidebar'), o = document.getElementById('sidebarOverlay');
  if (s) s.classList.add('open');
  if (o) o.classList.add('show');
}
function closeSidebar() {
  var s = document.getElementById('sidebar'), o = document.getElementById('sidebarOverlay');
  if (s) s.classList.remove('open');
  if (o) o.classList.remove('show');
}

/* ===== 页面切换 ===== */
function switchTab(name) {
  document.querySelectorAll('.page').forEach(function(el) { el.classList.remove('active'); });
  document.querySelectorAll('.nav-item, .mobile-tabbar .tab-item').forEach(function(el) { el.classList.toggle('active', el.dataset.tab === name); });
  var panel = document.getElementById('panel-' + name);
  if (panel) panel.classList.add('active');
  try { localStorage.setItem('m7a_tab', name); } catch(e) {}
  if (name === 'log') refreshLog();
  if (name === 'tasks') loadHistory();
  if (name === 'overview') refreshStatus();
  closeSidebar();
}
// Restore tab
(function() {
  try {
    var t = localStorage.getItem('m7a_tab');
    if (t) switchTab(t);
  } catch(e) {}
})();

// 操作结果通知 5 秒后自动消失（表单 POST 返回的 .msg 通知条）
setTimeout(function(){
  var msgs = document.querySelectorAll('.msg');
  for (var i = 0; i < msgs.length; i++) {
    (function(el){
      el.style.transition = 'opacity .6s ease';
      setTimeout(function(){ el.style.opacity = '0'; }, 5000);
      setTimeout(function(){ el.style.display = 'none'; }, 5600);
    })(msgs[i]);
  }
}, 300);

// 延迟检查更新（等页面渲染完）；手动更新模式下不自动检查
if (PANEL_UPDATE_MODE === 'auto') { setTimeout(function(){ checkUpdate(false); }, 1500); }
// 加载时展示镜像状态（读缓存，不强制联网）
setTimeout(function(){ checkImage(false); }, 2200);

/* ===== 配置子页切换 ===== */
function switchCfgTab(name) {
  document.querySelectorAll('.cfg-panel').forEach(function(el) { el.classList.remove('active'); });
  document.querySelectorAll('.cfg-tab').forEach(function(el) { el.classList.remove('active'); });
  document.getElementById('cfgPanel-' + name).classList.add('active');
  event.target.classList.add('active');
}

/* ===== v1.15+：折叠分组展开 / 收起 ===== */
function toggleCfgGroup(key) {
  var el = document.getElementById('cfgGroup-' + key);
  if (el) el.classList.toggle('collapsed');
}

/* ===== v1.15+：复制文本（clipboard 优先，失败回退 execCommand） ===== */
function copyText(inputId, btn) {
  var el = document.getElementById(inputId);
  if (!el) return;
  var text = (el.innerText !== undefined && el.innerText !== null && el.innerText !== '') ? el.innerText : el.textContent;
  text = (text || '').trim();
  var old = btn ? btn.textContent : '';
  var done = function(ok) {
    if (!btn) return;
    btn.textContent = ok ? '✅ 已复制' : '❌ 复制失败';
    /* v1.15+：追加视觉反馈（变绿 + 轻微弹一下），原有行为不变 */
    btn.classList.remove('copy-ok', 'copy-fail');
    btn.classList.add(ok ? 'copy-ok' : 'copy-fail');
    setTimeout(function() { btn.classList.remove('copy-ok', 'copy-fail'); }, 1200);
    setTimeout(function() { btn.textContent = old; }, 1800);
  };
  var fallback = function() {
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      done(ok);
    } catch (e) { done(false); }
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function(){ done(true); }).catch(fallback);
  } else {
    fallback();
  }
}

/* ===== 自动更新检查 ===== */
var _updIgnoredV = '';
try { _updIgnoredV = localStorage.getItem('m7a_upd_ignore_v') || ''; } catch(e) {}

function checkUpdate(force) {
  if (!force && _updIgnoredV) return; // 忽略过该版本则不提示；手动检查可绕过
  fetch('?ajax=check_update').then(function(r){ return r.json(); }).then(function(d){
    if (d && d.ok && d.has_update) {
      if (!force && d.latest === _updIgnoredV) return;
      document.getElementById('updLatest').textContent = d.latest;
      document.getElementById('updCurrent').textContent = d.current;
      var note = d.note || '';
      if (note.length > 300) note = note.substring(0, 300) + '…';
      document.getElementById('updNote').textContent = note;
      document.getElementById('updNote').style.display = note ? '' : 'none';
      document.getElementById('updateBanner').style.display = '';
    }
  }).catch(function(){});
}

function doUpdate() {
  var btn = document.getElementById('updBtn');
  if (btn.disabled) return;
  btn.disabled = true; btn.textContent = '更新中…';
  var fd = new FormData();
  fd.append('action', 'do_update');
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) fd.append('csrf', csrf.value);
  fetch('action', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d && d.ok) {
        alert('✅ ' + d.msg);
        /* v1.20：整包更新后面板约 2 秒自动重启，稍等再刷新避免撞上重启窗口 */
        setTimeout(function(){ location.reload(); }, 4500);
      } else {
        alert('❌ ' + (d && d.msg ? d.msg : '更新失败'));
        btn.disabled = false; btn.textContent = '一键更新';
      }
    })
    .catch(function(){
      alert('❌ 网络错误，更新未完成');
      btn.disabled = false; btn.textContent = '一键更新';
    });
}

function testUpdateSource() {
  var apiEl = document.getElementById('srcApiStatus');
  var rawEl = document.getElementById('srcRawStatus');
  var mirEl = document.getElementById('srcMirrorStatus');
  apiEl.textContent = '测试中…'; rawEl.textContent = '测试中…'; mirEl.textContent = '测试中…';
  fetch('?ajax=test_update_source').then(function(r){ return r.json(); }).then(function(d){
    if (!d) { apiEl.textContent = '失败'; rawEl.textContent = '失败'; mirEl.textContent = '失败'; return; }
    if (d.api && d.api.state === 'ok_release')      apiEl.textContent = '✅ 已发版（HTTP ' + d.api.code + '）';
    else if (d.api && d.api.state === 'ok_no_release') apiEl.textContent = '✅ 连通，尚未发版（HTTP ' + d.api.code + '）';
    else apiEl.textContent = '❌ HTTP ' + (d.api ? d.api.code : '失败') + '（检查仓库名/发版）';
    rawEl.textContent = (d.raw && d.raw.ok) ? '✅ 可下载（HTTP ' + d.raw.code + '）' : '❌ HTTP ' + (d.raw ? d.raw.code : '失败') + '（官方源不通，将尝试镜像）';
    if (d.mirrors && d.mirrors.length > 1) {
      var okN = 0, list = [];
      d.mirrors.forEach(function(m, i){
        if (i === 0) return;
        list.push(m.name + (m.ok ? '✅' : '❌' + m.code));
        if (m.ok) okN++;
      });
      mirEl.textContent = okN > 0 ? '✅ ' + okN + ' 个可用（' + list.join(' ') + '）' : '❌ 全部不可用';
    } else {
      mirEl.textContent = '无';
    }
  }).catch(function(){ apiEl.textContent = '网络错误'; rawEl.textContent = '网络错误'; mirEl.textContent = '网络错误'; });
}

function hideUpdate() {
  document.getElementById('updateBanner').style.display = 'none';
  try { localStorage.setItem('m7a_upd_hide', Date.now()); } catch(e) {}
}

function ignoreVersion() {
  var v = document.getElementById('updLatest').textContent;
  if (!v) return;
  try { localStorage.setItem('m7a_upd_ignore_v', v); } catch(e) {}
  document.getElementById('updateBanner').style.display = 'none';
}

function updateAssistantImage() {
  if (!confirm('确定更新三月七小助手镜像？将依次尝试官方源与加速镜像（南大/DaoCloud/dockerproxy），可能需要几分钟。')) return;
  var btn = event.target;
  var oldTxt = btn.textContent;
  btn.disabled = true; btn.textContent = '更新中…';
  var fd = new FormData();
  fd.append('action', 'update_image');
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) fd.append('csrf', csrf.value);
  fetch('action', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d && d.ok) {
        alert('✅ ' + d.msg);
        checkImage(true); // 更新后强制刷新镜像状态
      } else {
        alert('❌ ' + (d && d.err ? d.err : '更新失败'));
      }
      btn.disabled = false; btn.textContent = oldTxt;
    })
    .catch(function(){ alert('❌ 网络错误'); btn.disabled = false; btn.textContent = oldTxt; });
}

function checkImage(force) {
  var el = document.getElementById('imgStatus');
  if (!el) return;
  el.textContent = '检测中…';
  fetch('?ajax=image_check' + (force ? '&force=1' : '')).then(function(r){ return r.json(); }).then(function(d){
    if (!d) { el.textContent = '无响应'; return; }
    if (!d.ok) { el.textContent = '❌ ' + (d.err || '检测失败'); return; }
    if (d.has_update) {
      el.textContent = '⚠️ 镜像较旧，建议更新';
    } else if (d.remote_unknown) {
      el.textContent = '⚠️ 无法确认远程版本（GHCR 查询失败），本地：' + (d.local || '未知');
    } else if (d.local) {
      el.textContent = '✅ 已是最新镜像';
    } else {
      el.textContent = d.err || '检测完成';
    }
  }).catch(function(){ el.textContent = '网络错误'; });
}

function setUpdateMode(mode) {
  var fd = new FormData();
  fd.append('action', 'set_update_mode');
  fd.append('mode', mode);
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) fd.append('csrf', csrf.value);
  fetch('action', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d && d.ok) {
        alert('✅ ' + d.msg);
        if (mode === 'manual') document.getElementById('updateBanner').style.display = 'none';
      } else {
        alert('❌ ' + (d && d.msg ? d.msg : '切换失败'));
      }
    }).catch(function(){ alert('❌ 网络错误'); });
}

function setAfterFinish(v) {
  var fd = new FormData();
  fd.append('action', 'set_after_finish');
  fd.append('value', v);
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) fd.append('csrf', csrf.value);
  fetch('action', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d && d.ok) {
        alert('✅ ' + d.msg);
        var valEl = document.getElementById('afterFinishVal');
        var badge = document.getElementById('afterFinishBadge');
        if (v === 'Exit') {
          if (valEl) valEl.textContent = '跑完自动退出游戏';
          if (badge) { badge.textContent = '自动退出'; badge.style.background = 'var(--green,#22c55e)'; badge.style.color = '#fff'; }
        } else {
          if (valEl) valEl.textContent = '跑完保持界面';
          if (badge) { badge.textContent = '保持界面'; badge.style.background = 'var(--gray,#9ca3af)'; badge.style.color = '#fff'; }
        }
      } else {
        alert('❌ ' + (d && d.msg ? d.msg : '切换失败'));
      }
    }).catch(function(){ alert('❌ 网络错误'); });
}

/* ===== 版本备份 / 回滚（v1.14+） ===== */
function rollbackPanel(file) {
  if (!file) return;
  if (!confirm('确定回滚到备份 ' + file + '？\n当前版本会先自动备份一次，回滚后面板会刷新。')) return;
  var fd = new FormData();
  fd.append('action', 'backup_rollback');
  fd.append('file', file);
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) fd.append('csrf', csrf.value);
  fetch('action', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d && d.ok) {
        alert('✅ ' + d.msg);
        location.reload();
      } else {
        alert('❌ ' + (d && d.msg ? d.msg : '回滚失败'));
      }
    }).catch(function(){ alert('❌ 网络错误，回滚未完成'); });
}

/* ===== 任务执行历史（v1.14+） ===== */
function histViewLog() {
  switchTab('log');
  refreshLog();
}
function renderHistoryRows(items) {
  var tb = document.getElementById('histBody');
  if (!tb) return;
  if (!items || !items.length) {
    tb.innerHTML = '<tr><td colspan="5" class="hist-empty">暂无任务执行记录，点击上方任务按钮即可开始记录</td></tr>';
    return;
  }
  var html = '';
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    html += '<tr>'
      + '<td data-label="任务">' + escapeHtml(it.task_label || '') + '</td>'
      + '<td data-label="开始时间">' + escapeHtml(it.start_str || '') + '</td>'
      + '<td data-label="耗时">' + escapeHtml(it.duration_str || '') + '</td>'
      + '<td data-label="状态"><span class="hist-badge" style="background:' + escapeHtml(it.status_color || '') + ';">' + escapeHtml(it.status_label || '') + '</span></td>'
      + '<td data-label="操作" style="text-align:right;"><button type="button" class="btn small gray" onclick="histViewLog()">📝 查看日志</button></td>'
      + '</tr>';
  }
  tb.innerHTML = html;
}
function loadHistory() {
  fetch('?ajax=history').then(function(r){ return r.json(); }).then(function(d){
    if (!d || !d.ok) return;
    var st = document.getElementById('histStat');
    if (st) st.textContent = '今日执行 ' + d.today_count + ' 次 · 成功 ' + d.today_ok + ' 次';
    renderHistoryRows(d.items);
    if (typeof renderWeek === 'function') renderWeek(d.week);
  }).catch(function(){});
}
function clearHistory() {
  if (!confirm('确定清空全部任务执行历史？此操作不可恢复。')) return;
  var fd = new FormData();
  fd.append('action', 'history_clear');
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) fd.append('csrf', csrf.value);
  fetch('action', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d && d.ok) {
        alert('✅ ' + d.msg);
        loadHistory();
      } else {
        alert('❌ ' + (d && d.msg ? d.msg : '清空失败'));
      }
    }).catch(function(){ alert('❌ 网络错误'); });
}

/* ===== 资源监控（v1.13+） ===== */
var _monChart = null;
var _monTimer = null;
var _monRange = '1h';
function loadECharts(cb) {
  if (window.echarts) { if (cb) cb(); return; }
  var urls = [
    'https://cdn.bootcdn.net/ajax/libs/echarts/5.4.3/echarts.min.js',
    'https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js'
  ];
  var i = 0;
  function tryNext() {
    if (i >= urls.length) {
      var box = document.getElementById('monChart');
      if (box) box.innerHTML = '<div style="text-align:center;color:var(--muted);padding-top:110px;font-size:13px;">⚠️ 图表库加载失败（需外网 CDN），上方数字指标仍可用</div>';
      return;
    }
    var s = document.createElement('script');
    s.src = urls[i++];
    s.onload = function(){ if (cb) cb(); };
    s.onerror = tryNext;
    document.head.appendChild(s);
  }
  tryNext();
}
function fmtBytes(b) {
  if (b >= 1024*1024*1024) return (b/1024/1024/1024).toFixed(1) + 'G';
  if (b >= 1024*1024) return (b/1024/1024).toFixed(1) + 'M';
  if (b >= 1024) return (b/1024).toFixed(1) + 'K';
  return b.toFixed(0) + 'B';
}
function fmtSpeed(bps) { return fmtBytes(bps) + '/s'; }
function fmtUptime(sec) {
  if (!sec || sec <= 0) return '--';
  var d = Math.floor(sec/86400), h = Math.floor(sec%86400/3600), m = Math.floor(sec%3600/60);
  if (d > 0) return d + '天' + h + '小时';
  if (h > 0) return h + '小时' + m + '分';
  return m + '分钟';
}
/* v1.16+：数字滚动（requestAnimationFrame 300ms；解析目标文本中的数字段滚动，
   支持小数与 %/GB 等前后缀；当前为 -- 等无数字占位、或含多个数字的复合文本时直接落值不滚动） */
function tweenNum(el, target) {
  if (!el) return;
  var to = String(target);
  var cur = el.textContent || '';
  var mTo = to.match(/^(\D*?)(-?\d+(?:\.\d+)?)(\D*)$/);
  var mFrom = /^(\D*?)(-?\d+(?:\.\d+)?)(\D*)$/.exec(cur);
  if (!mTo || !mFrom) { el.textContent = to; return; }
  var from = parseFloat(mFrom[2]);
  var toV = parseFloat(mTo[2]);
  if (isNaN(from) || isNaN(toV)) { el.textContent = to; return; }
  var dec = (mTo[2].indexOf('.') >= 0) ? mTo[2].split('.')[1].length : 0;
  var token = (el._tweenTok = (el._tweenTok || 0) + 1);
  var t0 = null;
  function step(ts) {
    if (el._tweenTok !== token) return;
    if (t0 === null) t0 = ts;
    var p = Math.min(1, (ts - t0) / 300);
    var e = 1 - Math.pow(1 - p, 3);
    el.textContent = mTo[1] + (from + (toV - from) * e).toFixed(dec) + mTo[3];
    if (p < 1) window.requestAnimationFrame(step);
  }
  window.requestAnimationFrame(step);
}
function setMonStat(id, val, pct, cls) {
  var el = document.getElementById(id);
  if (!el) return;
  el.className = 'mon-stat' + (cls ? ' ' + cls : '');
  var v = el.querySelector('.m-value'); if (v) tweenNum(v, val);
  var bar = el.querySelector('.m-bar > i'); if (bar) bar.style.width = (pct || 0) + '%';
}
function renderMonitor(d) {
  var pts = d.points || [];
  var last = pts.length ? pts[pts.length-1] : null;
  if (last) {
    var cpuCls = last.cpu > 80 ? 'danger' : (last.cpu > 60 ? 'warn' : '');
    var memCls = last.mem > 80 ? 'danger' : (last.mem > 60 ? 'warn' : '');
    var diskCls = last.disk > 80 ? 'danger' : (last.disk > 60 ? 'warn' : '');
    setMonStat('monCpu', (last.cpu||0).toFixed(1) + '%', last.cpu, cpuCls);
    setMonStat('monMem', (last.mem||0).toFixed(1) + '%', last.mem, memCls);
    setMonStat('monDisk', (last.disk||0).toFixed(1) + '%', last.disk, diskCls);
    tweenNum(document.querySelector('#monLoad .m-value'), (last.load||0).toFixed(2));
    tweenNum(document.querySelector('#monNet .m-value'), '↓' + fmtSpeed(last.netIn||0));
    document.getElementById('monNetSub').textContent = '↑' + fmtSpeed(last.netOut||0);
    tweenNum(document.querySelector('#monUp .m-value'), fmtUptime(last.uptime||0));
    document.getElementById('monUpSub').textContent = d.running ? '容器：运行中' : '容器：已停止';
  }
  var h = d.host || {};
  var hostEl = document.getElementById('monHost');
  if (hostEl && (h.name || h.os)) {
    hostEl.innerHTML =
      '<span class="mon-host-row">💻 <b>' + escapeHtml(h.name || '') + '</b></span>' +
      '<span class="mon-host-row">系统 <b>' + escapeHtml(h.os || '--') + '</b></span>' +
      '<span class="mon-host-row">Docker <b>' + escapeHtml(h.docker || '--') + '</b></span>' +
      '<span class="mon-host-row">核心 <b>' + (h.cores || '--') + '</b></span>' +
      '<span class="mon-host-row">内存 <b>' + fmtBytes(h.memTotal||0) + '</b></span>' +
      '<span class="mon-host-row">磁盘 <b>' + fmtBytes(h.diskUsed||0) + ' / ' + fmtBytes(h.diskTotal||0) + '</b></span>';
  }
  if (window.echarts && _monRange !== 'custom') {
    if (!_monChart) {
      var box = document.getElementById('monChart');
      if (box) { box.innerHTML = ''; _monChart = echarts.init(box); }
    }
    if (_monChart) {
      var now = Math.floor(Date.now()/1000);
      var pts2 = pts, timeFmt = {hour12:false};
      if (_monRange === '1m') { pts2 = pts.filter(function(p){ return now - p.t <= 60; }); }
      else if (_monRange === '1h') { pts2 = pts.filter(function(p){ return now - p.t <= 3600; }); }
      else if (_monRange === '1d') { pts2 = d.minutes || []; timeFmt = {hour12:false, hour:'2-digit', minute:'2-digit'}; }
      var times = pts2.map(function(p){ return new Date(p.t*1000).toLocaleTimeString('zh-CN', timeFmt); });
      _monChart.setOption({
        tooltip: { trigger: 'axis', confine: true },
        animationDuration: 800, animationEasing: 'cubicOut',
        legend: { data: ['CPU','内存','磁盘'], textStyle:{color:'#999'}, top:0 },
        grid: { left:42, right:16, top:34, bottom:26 },
        xAxis: { type:'category', data:times, boundaryGap:false, axisLine:{lineStyle:{color:'#999'}}, axisLabel:{color:'#999', fontSize:10} },
        yAxis: { type:'value', max:100, axisLabel:{formatter:'{value}%', color:'#999', fontSize:10}, splitLine:{lineStyle:{color:'rgba(128,128,128,.15)'}} },
        series: [
          { name:'CPU', type:'line', smooth:true, showSymbol:false, data:pts2.map(function(p){return +(p.cpu||0).toFixed(1);}), lineStyle:{width:2,color:'#ec4899'}, itemStyle:{color:'#ec4899'}, areaStyle:{opacity:.08} },
          { name:'内存', type:'line', smooth:true, showSymbol:false, data:pts2.map(function(p){return +(p.mem||0).toFixed(1);}), lineStyle:{width:2,color:'#38bdf8'}, itemStyle:{color:'#38bdf8'}, areaStyle:{opacity:.08} },
          { name:'磁盘', type:'line', smooth:true, showSymbol:false, data:pts2.map(function(p){return +(p.disk||0).toFixed(1);}), lineStyle:{width:2,color:'#f59e0b'}, itemStyle:{color:'#f59e0b'}, areaStyle:{opacity:.08} }
        ]
      });
    }
  }
}
function setMonRange(r) {
  _monRange = r;
  var btns = document.querySelectorAll('.mon-range .btn');
  for (var i = 0; i < btns.length; i++) {
    btns[i].className = 'btn small' + (btns[i].getAttribute('data-range') === r ? ' active' : '');
  }
  var cbox = document.getElementById('monCustomBox');
  if (cbox) cbox.style.display = (r === 'custom') ? '' : 'none';
  var note = document.getElementById('monSrcNote');
  if (r === 'custom') { monCustomInit(); return; }
  if (note) note.style.display = 'none';
  loadMonitor(true);
}
function loadMonitor() {
  var iv = parseInt(document.getElementById('monInterval').value) || 1;
  fetch('?ajax=monitor&iv=' + iv)
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (!d || !d.ok) return;
      renderMonitor(d);
      var badge = document.getElementById('monLiveBadge');
      if (badge) {
        badge.textContent = d.running ? '实时' : '容器已停止';
        badge.style.background = d.running ? 'var(--green,#22c55e)' : 'var(--gray,#9ca3af)';
      }
    }).catch(function(){});
}
function startMonitor() {
  stopMonitor();
  loadMonitor();
  if (_evUp) return; /* M5-C：事件流连通时由 WS 驱动，不设轮询定时器 */
  var iv = parseInt(document.getElementById('monInterval').value) || 1;
  /* v1.16+：手机端且用户未手动设置过刷新间隔时，默认放宽到 3 秒；手动选过则尊重用户选择 */
  if (!window._monIvTouched && window.innerWidth <= 640) iv = 3;
  _monTimer = setInterval(loadMonitor, Math.max(1, iv) * 1000);
}
function stopMonitor() {
  if (_monTimer) { clearInterval(_monTimer); _monTimer = null; }
}
function setMonitorInterval(iv) {
  window._monIvTouched = true; /* v1.16+：标记用户已手动设置过刷新间隔 */
  var fd = new FormData();
  fd.append('action', 'set_monitor_interval');
  fd.append('interval', iv);
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) fd.append('csrf', csrf.value);
  fetch('action', { method:'POST', body: fd })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (d && d.ok) {
        alert('✅ ' + d.msg);
        var t = document.getElementById('monIvText');
        if (t) t.textContent = iv;
        startMonitor();
      } else {
        alert('❌ ' + (d && d.msg ? d.msg : '设置失败'));
      }
    }).catch(function(){ alert('❌ 网络错误'); });
}

/* ===== v1.22：监控「自定义」时段查询（?ajax=monitor_range，复用同一张图表） =====
   仅新增档位，不改变近1分钟/1小时/1天三档行为。custom 档选中后由 queryMonRange 渲染，
   轮询/WS 触发的 renderMonitor 在该档下会跳过图表重绘，避免覆盖查询结果。 */
function monLocalInput(ts) {
  var d = new Date((ts || 0) * 1000);
  function p(n) { return (n < 10 ? '0' : '') + n; }
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
    + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function monTsText(ts) {
  return new Date((ts || 0) * 1000).toLocaleString('zh-CN',
    { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}
function monCustomInit() {
  var f = document.getElementById('monFrom'), t = document.getElementById('monTo');
  if (!f || !t) return;
  if (!f.value) f.value = monLocalInput(Math.floor(Date.now() / 1000) - 3600);
  if (!t.value) t.value = monLocalInput(Math.floor(Date.now() / 1000));
  queryMonRange();
}
function queryMonRange() {
  var f = document.getElementById('monFrom'), t = document.getElementById('monTo');
  var note = document.getElementById('monSrcNote');
  if (!f || !t || !f.value || !t.value) { miniToast('请先选择开始与结束时间'); return; }
  var from = Math.floor(new Date(f.value).getTime() / 1000);
  var to = Math.floor(new Date(t.value).getTime() / 1000);
  if (isNaN(from) || isNaN(to) || to <= from) { miniToast('结束时间需晚于开始时间'); return; }
  if (note) { note.style.display = ''; note.textContent = '查询中…'; }
  fetch('?ajax=monitor_range&from=' + from + '&to=' + to)
    .then(function(r){ return r.json(); })
    .then(function(d){ renderMonRange(d, from, to); })
    .catch(function(){ if (note) { note.style.display = ''; note.textContent = '网络错误，请重试'; } });
}
function renderMonRange(d, from, to) {
  var note = document.getElementById('monSrcNote');
  if (!d || !d.ok) { if (note) { note.style.display = ''; note.textContent = '查询失败，请重试'; } return; }
  var pts = d.points || [];
  var srcTxt = (d.bucket === 'minute') ? '聚合点（每分钟 1 点）' : '原始采样点';
  var head = '数据来源：<b>' + srcTxt + '</b> · 共 '
    + (d.count != null ? d.count : pts.length) + ' 点 · '
    + monTsText(d.from != null ? d.from : from) + ' ~ ' + monTsText(d.to != null ? d.to : to);
  if (note) { note.style.display = ''; note.innerHTML = head; }
  if (!pts.length) {
    if (_monChart && _monChart.clear) { try { _monChart.clear(); } catch (e) {} }
    if (note) note.innerHTML = head + ' · <span style="color:var(--muted);">该时段暂无采样数据</span>';
    return;
  }
  if (!window.echarts) { if (note) note.innerHTML = head + ' · <span style="color:var(--muted);">图表组件未就绪</span>'; return; }
  if (!_monChart) {
    var box = document.getElementById('monChart');
    if (!box) return;
    box.innerHTML = '';
    _monChart = echarts.init(box);
  }
  var times = pts.map(function(p){ return monTsText(p.t); });
  _monChart.setOption({
    tooltip: { trigger: 'axis', confine: true },
    animationDuration: 500, animationEasing: 'cubicOut',
    legend: { data: ['CPU','内存','磁盘'], textStyle:{color:'#999'}, top:0 },
    grid: { left:42, right:16, top:34, bottom:44 },
    xAxis: { type:'category', data:times, boundaryGap:false, axisLine:{lineStyle:{color:'#999'}}, axisLabel:{color:'#999', fontSize:10, rotate:30} },
    yAxis: { type:'value', max:100, axisLabel:{formatter:'{value}%', color:'#999', fontSize:10}, splitLine:{lineStyle:{color:'rgba(128,128,128,.15)'}} },
    series: [
      { name:'CPU', type:'line', smooth:true, showSymbol:false, data:pts.map(function(p){return +(p.cpu||0).toFixed(1);}), lineStyle:{width:2,color:'#ec4899'}, itemStyle:{color:'#ec4899'}, areaStyle:{opacity:.08} },
      { name:'内存', type:'line', smooth:true, showSymbol:false, data:pts.map(function(p){return +(p.mem||0).toFixed(1);}), lineStyle:{width:2,color:'#38bdf8'}, itemStyle:{color:'#38bdf8'}, areaStyle:{opacity:.08} },
      { name:'磁盘', type:'line', smooth:true, showSymbol:false, data:pts.map(function(p){return +(p.disk||0).toFixed(1);}), lineStyle:{width:2,color:'#f59e0b'}, itemStyle:{color:'#f59e0b'}, areaStyle:{opacity:.08} }
    ]
  }, true);
}

var _logTimer = null;
var _statusTimer = null;
var _logKeywordTimer = null;

function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
function renderLogLine(line, kw) {
  var esc = escapeHtml(line);
  if (kw) {
    var re = new RegExp(escapeRegExp(escapeHtml(kw)), 'gi');
    esc = esc.replace(re, '<span class="hl">$&</span>');
  }
  return esc;
}
function logKeywordKeyup(e) {
  if (e.key === 'Enter') { refreshLog(); return; }
  clearTimeout(_logKeywordTimer);
  _logKeywordTimer = setTimeout(refreshLog, 300);
}
function logFilterParams() {
  var kw = document.getElementById('logKeyword') ? document.getElementById('logKeyword').value.trim() : '';
  var lv = document.getElementById('logLevel') ? document.getElementById('logLevel').value : '';
  var hh = document.getElementById('logHours') ? document.getElementById('logHours').value : '0';
  var ff = document.getElementById('logFile') ? document.getElementById('logFile').value : '';
  return 'keyword=' + encodeURIComponent(kw) + '&level=' + encodeURIComponent(lv) + '&hours=' + encodeURIComponent(hh) + '&file=' + encodeURIComponent(ff);
}
function fillLogFiles(files, current) {
  var sel = document.getElementById('logFile');
  if (!sel) return;
  var html = '';
  for (var i = 0; i < files.length; i++) {
    html += '<option value="' + escapeHtml(files[i]) + '"' + (files[i] === current ? ' selected' : '') + '>' + escapeHtml(files[i]) + '</option>';
  }
  sel.innerHTML = html || '<option value="">（无日志文件）</option>';
}
function refreshLog() {
  fetch('?ajax=log&' + logFilterParams()).then(function(r) { return r.json(); }).then(function(d) {
    if (!d || !d.ok) return;
    fillLogFiles(d.files || [], d.file);
    var box = document.getElementById('logBox');
    var countEl = document.getElementById('logCount');
    if (typeof d.lines === 'string') {
      if (box) box.innerHTML = '<div style="color:var(--muted);">' + escapeHtml(d.lines) + '</div>';
      if (countEl) countEl.textContent = '';
      return;
    }
    var kw = document.getElementById('logKeyword') ? document.getElementById('logKeyword').value.trim() : '';
    var hasFilter = kw !== '' || (document.getElementById('logLevel') && document.getElementById('logLevel').value !== '') || (document.getElementById('logHours') && document.getElementById('logHours').value !== '0');
    var html = d.lines.map(function(line) { return renderLogLine(line, kw); }).join('\n');
    if (box) {
      var atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
      box.innerHTML = html;
      var follow = !document.getElementById('logFollow') || document.getElementById('logFollow').checked;
      if (!hasFilter && (follow || atBottom)) box.scrollTop = box.scrollHeight;
    }
    if (countEl) countEl.textContent = '命中 ' + d.total + ' 条';
  }).catch(function() {});
}
/* ===== v1.17：日志工具 / 回到顶部 / 中央快捷键 ===== */
function logJumpBottom() {
  var box = document.getElementById('logBox');
  if (box) box.scrollTop = box.scrollHeight;
}
function copyLogText() {
  var box = document.getElementById('logBox');
  if (!box) return;
  var txt = box.innerText || '';
  var done = function(ok) { miniToast(ok ? '已复制到剪贴板' : '复制失败，请长按选择复制'); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(function() { done(true); }, function() { done(false); });
  } else {
    var ta = document.createElement('textarea');
    ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    var ok = false; try { ok = document.execCommand('copy'); } catch(e) {}
    document.body.removeChild(ta); done(ok);
  }
}
function miniToast(msg) {
  var el = document.getElementById('miniToast');
  if (!el) {
    el = document.createElement('div'); el.id = 'miniToast';
    el.style.cssText = 'position:fixed;left:50%;bottom:calc(92px + env(safe-area-inset-bottom));transform:translateX(-50%);z-index:500;background:rgba(24,10,34,.92);color:#fff;font-size:13px;padding:9px 16px;border-radius:12px;pointer-events:none;transition:opacity .25s;opacity:0;max-width:80%;';
    document.body.appendChild(el);
  }
  el.textContent = msg; el.style.opacity = '1';
  clearTimeout(el._timer); el._timer = setTimeout(function() { el.style.opacity = '0'; }, 1600);
}
var _backTopVisible = false;
window.addEventListener('scroll', function() {
  var y = window.scrollY || document.documentElement.scrollTop || 0;
  var show = y > 420;
  if (show !== _backTopVisible) {
    _backTopVisible = show;
    var b = document.getElementById('backTop');
    if (b) b.classList.toggle('show', show);
  }
}, { passive: true });
function backToTop() { try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch(e) { window.scrollTo(0, 0); } }
/* --- 中央快捷键：点按=启停，长按=快捷菜单 --- */
var _fabRunning = null, _fabTimer = null, _fabSuppressClick = false;
function fabSetState(running) {
  _fabRunning = running;
  var icon = document.getElementById('fabIcon');
  if (icon) icon.textContent = running === null ? '⏳' : (running ? '🏃' : '🎯');
  var fab = document.getElementById('tabFab');
  if (fab) fab.title = '点按=快速跑任务，长按=容器操作';
}
function fabTap() {
  if (_fabSuppressClick) { _fabSuppressClick = false; return; }
  if (_fabRunning === null) { miniToast('容器状态检测中，稍候再试'); return; }
  openTaskSheet();
}
function openTaskSheet() { var o = document.getElementById('taskSheetOverlay'); if (o) o.classList.add('show'); }
function closeTaskSheet() { var o = document.getElementById('taskSheetOverlay'); if (o) o.classList.remove('show'); }
function quickTask(key, label) {
  closeTaskSheet();
  if (_fabRunning === false) {
    if (!confirm('小助手当前未运行，将自动启动并执行「' + label + '」，继续？')) return;
    submitPanelAction(key);
    return;
  }
  if (typeof _rbTask !== 'undefined' && _rbTask && _rbTask !== label) {
    if (!confirm('已有任务「' + _rbTask + '」运行中，强制切换为「' + label + '」？（原任务会被中断）')) return;
  }
  if (!confirm('执行「' + label + '」？')) return;
  submitPanelAction(key);
}
function fabLongPressStart() {
  if (_fabTimer) clearTimeout(_fabTimer);
  _fabSuppressClick = false;
  _fabTimer = setTimeout(function() {
    _fabTimer = null; _fabSuppressClick = true;
    try { if (navigator.vibrate) navigator.vibrate(15); } catch(e) {}
    openFabSheet();
  }, 480);
}
function fabLongPressCancel() { if (_fabTimer) { clearTimeout(_fabTimer); _fabTimer = null; } }
function openFabSheet() { var o = document.getElementById('fabSheetOverlay'); if (o) o.classList.add('show'); }
function closeFabSheet() { var o = document.getElementById('fabSheetOverlay'); if (o) o.classList.remove('show'); }
function fabAct(act) {
  closeFabSheet();
  var tips = {
    restart: '确定重启容器？',
    stop: '确定停止容器？任务将全部中断，之后点中央按钮即可恢复。'
  };
  if (tips[act] && !confirm(tips[act])) return;
  submitPanelAction(act);
}
function submitPanelAction(action) {
  var csrf = document.querySelector('input[name="csrf"]');
  var f = document.createElement('form');
  f.method = 'POST'; f.action = 'action'; f.style.display = 'none';
  if (csrf) {
    var c = document.createElement('input');
    c.type = 'hidden'; c.name = 'csrf'; c.value = csrf.value; f.appendChild(c);
  }
  var a = document.createElement('input');
  a.type = 'hidden'; a.name = 'action'; a.value = action; f.appendChild(a);
  document.body.appendChild(f); f.submit();
}
fabSetState(null);

function resetLogFilter() {
  if (document.getElementById('logKeyword')) document.getElementById('logKeyword').value = '';
  if (document.getElementById('logLevel')) document.getElementById('logLevel').value = '';
  if (document.getElementById('logHours')) document.getElementById('logHours').value = '0';
  refreshLog();
}
function initLogExport() {
  var btn = document.getElementById('exportLogBtn');
  if (btn) btn.onclick = function() {
    var url = '?export_log=1&' + logFilterParams();
    window.open(url, '_blank');
  };
}

/* ===== v1.19 游戏画面实时预览 ===== */
var pv = {ws:null, on:false, paused:false, res:'720p', enc:'jpeg', fps:'15', retry:0, timer:null, lastFrame:0, mse:null, sb:null, pq:[], rmWait:false};
function pvBase() {
  var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  var base = location.pathname.replace(/\/[^\/]*$/, '/');
  return proto + location.host + base + 'm7a-preview/ws';
}
function pvToggle() { pv.on ? pvStop() : pvStart(); }
function pvStart() {
  pv.on = true; pv.paused = false; pv.retry = 0;
  document.getElementById('pvToggle').textContent = '停止预览';
  document.getElementById('pvBox').style.display = 'block';
  document.getElementById('pvRes').style.display = '';
  document.getElementById('pvFull').style.display = '';
  document.getElementById('pvEnc').style.display = '';
  pvEncUi();
  pvSurface();
  pvConnect();
  pv.timer = setInterval(pvWatch, 3000);
}
function pvStop() {
  pv.on = false; pv.paused = true;
  if (pv.timer) { clearInterval(pv.timer); pv.timer = null; }
  if (pv.ws) { try { pv.ws.close(); } catch(e) {} pv.ws = null; }
  pvMseTeardown();
  document.getElementById('pvToggle').textContent = '开启预览';
  document.getElementById('pvBadge').textContent = '未开启';
  document.getElementById('pvBadge').style.background = 'var(--muted,#8a8f98)';
  document.getElementById('pvBox').style.display = 'none';
  document.getElementById('pvRes').style.display = 'none';
  document.getElementById('pvFps').style.display = 'none';
  document.getElementById('pvFull').style.display = 'none';
  document.getElementById('pvEnc').style.display = 'none';
}
function pvUrl(token) {
  var u = pvBase() + '?token=' + token + '&res=' + pv.res;
  if (pv.enc === 'h264') u = pvBase().replace(/\/ws$/, '/ws264') + '?token=' + token + '&res=' + pv.res + '&fps=' + pv.fps;
  return u;
}
function pvSurface() {
  var img = document.getElementById('pvImg'), vid = document.getElementById('pvVideo');
  var h264 = pv.enc === 'h264';
  if (img) img.style.display = h264 ? 'none' : '';
  if (vid) vid.style.display = h264 ? '' : 'none';
  if (!h264) pvMseTeardown();
}
function pvToggleEnc() {
  pv.enc = (pv.enc === 'h264') ? 'jpeg' : 'h264';
  pvEncUi();
  pvSurface();
  if (pv.on) {
    if (pv.ws) { try { pv.ws.close(); } catch(e) {} pv.ws = null; }
    pvConnect();
  } else {
    miniToast(pv.enc === 'h264' ? '已选 H.264 极致档，开启预览后生效' : '已切回 JPEG 标准档');
  }
}
function pvEncUi() {
  var b = document.getElementById('pvEnc');
  if (!b) return;
  var on = pv.enc === 'h264';
  b.textContent = on ? '⚡H.264·开' : '⚡H.264';
  b.style.background = on ? 'var(--green,#22c55e)' : '';
  b.style.color = on ? '#fff' : '';
  /* v1.20：帧率选择只在 H.264 档且预览开启时可见 */
  var fp = document.getElementById('pvFps');
  if (fp) fp.style.display = (on && pv.on) ? '' : 'none';
}
function pvMseTeardown() {
  var vid = document.getElementById('pvVideo');
  pv.sb = null; pv.pq = []; pv.rmWait = false;
  if (pv.mse) { try { pv.mse = null; } catch(e) {} }
  if (vid) {
    try { vid.pause(); vid.removeAttribute('src'); vid.load(); } catch(e) {}
    if (vid.src && vid.src.indexOf('blob:') === 0) { try { URL.revokeObjectURL(vid.src); } catch(e) {} }
  }
}
function pvMseSetup() {
  var mime = 'video/mp4; codecs="avc1.42E01E"';
  if (!window.MediaSource || !MediaSource.isTypeSupported(mime)) return false;
  pvMseTeardown();
  var vid = document.getElementById('pvVideo');
  pv.mse = new MediaSource();
  vid.src = URL.createObjectURL(pv.mse);
  pv.mse.addEventListener('sourceopen', function() {
    try {
      pv.sb = pv.mse.addSourceBuffer(mime);
      pv.sb.addEventListener('updateend', pvMseUpdate);
      pv.pq = []; pv.rmWait = false;
      pvMseUpdate();
    } catch(e) {}
  }, { once: true });
  var play = function() { try { vid.play(); } catch(e) {} };
  play();
  return true;
}
function pvMseUpdate() {
  if (!pv.sb || pv.sb.updating) return;
  try {
    var vid = document.getElementById('pvVideo');
    var b = pv.sb.buffered;
    if (!pv.rmWait && b.length && vid && vid.currentTime > 0 && b.start(0) < vid.currentTime - 60) {
      pv.rmWait = true;
      pv.sb.remove(0, vid.currentTime - 60);
      return; /* updateend 后清位标志再续 */
    }
    pv.rmWait = false;
  } catch(e) {}
  if (pv.pq.length) { try { pv.sb.appendBuffer(pv.pq.shift()); } catch(e) {} }
}
function pvMsePush(data) {
  if (!pv.sb) return;
  pv.pq.push(data);
  pvMseUpdate();
}
function pvConnect() {
  if (!pv.on) return;
  var msg = document.getElementById('pvMsg');
  msg.style.display = 'flex';
  msg.textContent = '正在连接…';
  fetch('?ajax=preview_token').then(function(r){ return r.json(); }).then(function(d) {
    if (!d.ok) { msg.textContent = d.msg || '获取预览令牌失败'; return; }
    if (pv.enc === 'h264' && !pvMseSetup()) {
      msg.textContent = '当前浏览器不支持 H.264（MSE）播放，请切回 JPEG 档';
      pvBadge('不支持', 'var(--red,#ef4444)');
      return;
    }
    var ws = new WebSocket(pvUrl(d.token));
    pv.ws = ws;
    ws.binaryType = 'arraybuffer';
    ws.onopen = function() {
      pv.retry = 0; pv.lastFrame = Date.now();
      pvBadge('已连接', pv.enc === 'h264' ? 'var(--purple,#8b5cf6)' : 'var(--amber,#f59e0b)');
    };
    ws.onmessage = function(ev) {
      if (pv.enc === 'h264') {
        pvMsePush(ev.data);
      } else {
        var img = document.getElementById('pvImg');
        var old = img.src;
        img.src = URL.createObjectURL(new Blob([ev.data], {type:'image/jpeg'}));
        if (old && old.indexOf('blob:') === 0) URL.revokeObjectURL(old);
      }
      pv.lastFrame = Date.now();
      document.getElementById('pvMsg').style.display = 'none';
      pvBadge('实时', pv.enc === 'h264' ? 'var(--purple,#8b5cf6)' : 'var(--green,#22c55e)');
    };
    ws.onclose = function(ev) {
      if (pv.ws !== ws) return;
      pv.ws = null;
      if (ev && (ev.code === 1013 || ev.code === 1008)) {
        /* 服务端明确拒绝（未装 ffmpeg / 上游不可用 / 鉴权失败）：展示原因，不盲目重连 */
        pvBadge('不可用', 'var(--red,#ef4444)');
        msg.style.display = 'flex';
        msg.textContent = ev.reason || (ev.code === 1013 ? 'H.264 档暂不可用' : '预览鉴权失败');
        return;
      }
      if (!pv.on || pv.paused) return;
      pvBadge('重连中', 'var(--amber,#f59e0b)');
      var delay = Math.min(10000, 1000 * Math.pow(2, pv.retry++));
      setTimeout(function() { if (pv.on && !pv.paused && !pv.ws) pvConnect(); }, delay);
    };
    ws.onerror = function() { try { ws.close(); } catch(e) {} };
  }).catch(function(e) { msg.textContent = '连接失败：' + e; });
}
function pvBadge(text, bg) {
  var b = document.getElementById('pvBadge');
  b.textContent = text;
  b.style.background = bg;
}
function pvWatch() {
  if (!pv.on || !pv.ws || pv.ws.readyState !== 1) return;
  if (pv.lastFrame && Date.now() - pv.lastFrame > 8000) {
    var m = document.getElementById('pvMsg');
    m.style.display = 'flex';
    m.textContent = '画面静止或云游戏未运行（任务运行期间自动恢复）';
    pvBadge('无画面', 'var(--amber,#f59e0b)');
  }
}
function pvSetRes(v) {
  pv.res = v;
  if (pv.ws) { try { pv.ws.close(); } catch(e) {} pv.ws = null; }
  if (pv.on) pvConnect();
}
function pvSetFps(v) {
  pv.fps = (String(v) === '30') ? '30' : '15';
  if (pv.ws) { try { pv.ws.close(); } catch(e) {} pv.ws = null; }
  if (pv.on && pv.enc === 'h264') pvConnect();
}
function pvFullscreen() {
  var el = document.getElementById('pvBox');
  if (document.fullscreenElement) { document.exitFullscreen(); return; }
  if (el.requestFullscreen) el.requestFullscreen();
}
document.addEventListener('visibilitychange', function() {
  if (document.hidden) {
    pv.paused = true;
    if (pv.ws) { try { pv.ws.close(); } catch(e) {} pv.ws = null; }
  } else {
    pv.paused = false;
    if (pv.on && !pv.ws) pvConnect();
  }
});
function refreshStatus() {
  fetch('?ajax=status').then(function(r) { return r.text(); }).then(function(t) {
    var box = document.getElementById('statusBox');
    if (box) box.textContent = t;
  }).catch(function() {});
  fetch('?ajax=running').then(function(r) { return r.json(); }).then(function(d) {
    document.querySelectorAll('.status-badge').forEach(function(badge) {
      badge.className = 'status-badge ' + (d.running ? 'running' : 'stopped');
      var txt = badge.querySelector('.status-text');
      if (txt) txt.textContent = d.running ? '运行中' : '已停止';
    });
    fabSetState(!!d.running);
    runbarUpdate(d && d.task && d.running ? d : null);
  }).catch(function() {});
}

function toggleAutoRefresh() {
  var on = document.getElementById('autoRefresh').checked;
  if (on) startAutoRefresh(); else stopAutoRefresh();
}
function updateRefreshInterval() {
  stopAutoRefresh();
  if (document.getElementById('autoRefresh').checked) startAutoRefresh();
}
function startAutoRefresh() {
  var ms = parseInt(document.getElementById('refreshInterval').value) || 5000;
  stopAutoRefresh();
  if (_evUp) _evPause.log = true; /* M5-C：WS 接管日志刷新，断开时按此恢复 */
  else _logTimer = setInterval(refreshLog, ms);
  _statusTimer = setInterval(refreshStatus, 10000);
}
function stopAutoRefresh() {
  if (_logTimer) { clearInterval(_logTimer); _logTimer = null; }
  if (_statusTimer) { clearInterval(_statusTimer); _statusTimer = null; }
  _evPause.log = false; /* M5-C：用户主动关闭，取消 WS 断开后的恢复标记 */
}

/* ===== v1.18：运行中任务悬浮条 ===== */
var _rbTask = null, _rbStart = 0, _rbNow = 0, _rbLocal = 0, _rbTick = null;
function runbarUpdate(d) {
  var bar = document.getElementById('runBar');
  if (!bar) return;
  if (_rbTick) { clearInterval(_rbTick); _rbTick = null; }
  if (!d || !d.task) {
    bar.style.display = 'none';
    document.body.classList.remove('has-runbar');
    _rbTask = null;
    var qbOff = document.getElementById('quickStopTask');
    if (qbOff) qbOff.style.opacity = '0.45';
    return;
  }
  _rbTask = d.task;
  _rbStart = parseInt(d.start, 10) || 0;
  _rbNow = parseInt(d.now, 10) || 0;
  _rbLocal = Math.floor(Date.now() / 1000);
  var lbl = document.getElementById('rbLabel');
  if (lbl) lbl.textContent = d.task;
  bar.style.display = 'flex';
  document.body.classList.add('has-runbar');
  var qbOn = document.getElementById('quickStopTask');
  if (qbOn) qbOn.style.opacity = '1';
  runbarTick();
  _rbTick = setInterval(runbarTick, 1000);
}
function runbarTick() {
  var el = document.getElementById('rbTime');
  if (!el || !_rbStart) return;
  var nowS = _rbNow + (Math.floor(Date.now() / 1000) - _rbLocal);
  var s = Math.max(0, nowS - _rbStart);
  var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  var pad = function(n) { return (n < 10 ? '0' : '') + n; };
  el.textContent = (h ? h + ':' + pad(m) : pad(m)) + ':' + pad(sec);
}
function runbarStop() {
  if (!confirm('停止当前任务？日志可能不完整。')) return;
  submitPanelAction('stop_task');
}
/* ===== v1.21：随时停止（概览页「快捷操作」卡常驻入口） ===== */
function stopCurrentTask() {
  if (!confirm('停止当前任务？（容器将重启，日志可能不完整；若当前没有任务在跑则相当于重启容器）')) return;
  submitPanelAction('stop_task');
}
function stopAssistant() {
  if (!confirm('停止小助手？任务将全部中断；之后点「重启容器」或任意快捷操作即可恢复运行。')) return;
  submitPanelAction('stop');
}
/* ===== v1.18：历史近7天统计图 ===== */
function renderWeek(week) {
  var box = document.getElementById('histWeek');
  if (!box) return;
  if (!week || !week.length) { box.style.display = 'none'; return; }
  var max = 1;
  week.forEach(function(d) { if (d.count > max) max = d.count; });
  box.style.display = 'flex';
  box.innerHTML = week.map(function(d) {
    var failN = d.count - d.ok, bars = '';
    if (d.count === 0) {
      bars = '<div class="wk-bar wk-zero" style="height:4px"></div>';
    } else {
      var okH = Math.max(4, Math.round(d.ok / max * 52));
      var failH = Math.max(4, Math.round(failN / max * 52));
      if (d.ok > 0) bars += '<div class="wk-bar wk-ok" style="height:' + okH + 'px"></div>';
      if (failN > 0) bars += '<div class="wk-bar wk-fail" style="height:' + failH + 'px"></div>';
    }
    var tip = d.date + '：共 ' + d.count + ' 次，成功 ' + d.ok + ' 次' + (failN > 0 ? '，失败/中断 ' + failN + ' 次' : '');
    return '<div class="wk-col' + (d.today ? ' today' : '') + '" title="' + tip + '">' +
      '<span class="wk-num">' + (d.count || '') + '</span>' +
      '<div class="wk-bars">' + bars + '</div>' +
      '<span class="wk-day' + (d.today ? ' today' : '') + '">' + d.wd + '</span></div>';
  }).join('');
}
/* ===== v1.18：一键体检 ===== */
function runDoctor() {
  var box = document.getElementById('doctorBox'), btn = document.getElementById('doctorBtn');
  if (!box) return;
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 检测中…'; }
  box.style.display = 'flex';
  box.innerHTML = '<div class="doctor-item"><b>⏳</b><span>正在体检，请稍候…</span></div>';
  var done = function() { if (btn) { btn.disabled = false; btn.textContent = '🩺 开始体检'; } };
  fetch('?ajax=doctor').then(function(r) { return r.json(); }).then(function(d) {
    done();
    if (!d || !d.ok) { box.innerHTML = '<div class="doctor-item"><b>❌</b><span>体检接口异常</span></div>'; return; }
    var ico = { ok: '✅', warn: '⚠️', err: '❌', info: 'ℹ️' };
    box.innerHTML = d.items.map(function(it) {
      return '<div class="doctor-item"><b>' + (ico[it.level] || 'ℹ️') + '</b><span><strong>' + it.name + '</strong>：' + it.msg + '</span></div>';
    }).join('') + '<div class="doctor-meta">共 ' + d.items.length + ' 项 · 耗时 ' + d.took + 's</div>';
  }).catch(function() {
    done();
    box.innerHTML = '<div class="doctor-item"><b>❌</b><span>网络错误，体检失败</span></div>';
  });
}
/* ===== v1.18：告警卡交互 ===== */
/* ===== M5-C：告警历史（?ajax=alert_history，SQLite 事件留档） ===== */
function toggleAlertHist() {
  var b = document.getElementById('alertHistBox');
  if (!b) return;
  if (b.style.display === 'none' || !b.style.display) { b.style.display = ''; loadAlertHist(); }
  else { b.style.display = 'none'; }
}
function loadAlertHist() {
  var list = alertTimelineShell();
  if (!list) return;
  list.innerHTML = '<div class="alert-tl-empty">加载中…</div>';
  fetch('?ajax=alert_history&limit=200' + alertFilterQS()).then(function(r){ return r.json(); }).then(function(d){
    if (!d || !d.ok) { list.innerHTML = '<div class="alert-tl-empty">读取失败，请刷新重试</div>'; return; }
    renderAlertTimeline(d.items || []);
  }).catch(function(){ list.innerHTML = '<div class="alert-tl-empty">网络错误，请重试</div>'; });
}

/* ===== v1.22：告警历史可视化（时间轴 / kind 配色 / 推送徽章 / 筛选 / 清空 / 实时插入） ===== */
/* 写操作端点常量：与既有各写操作请求等价，集中一处便于维护；不散落字面量。 */
var _PANEL_ACTION = 'action';
var _alertFilter = { kind: '', from: '', to: '' };
var ALERT_KIND = {
  down:      { cls: 'down',      icon: '🔴', label: '异常' },
  recovered: { cls: 'recovered', icon: '🟢', label: '恢复' },
  aborted:   { cls: 'aborted',   icon: '🟠', label: '中断' }
};
function alertKindMeta(k) { return ALERT_KIND[k] || { cls: 'other', icon: '⚪', label: (k || '事件') }; }
function alertDayKey(ts) {
  var d = new Date((ts || 0) * 1000);
  function p(n) { return (n < 10 ? '0' : '') + n; }
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}
function alertElVal(id) { var e = document.getElementById(id); return e ? e.value : ''; }
function alertLocalToTs(v) {
  if (!v) return '';
  var ms = new Date(v).getTime();
  return isNaN(ms) ? '' : Math.floor(ms / 1000);
}
function alertFilterQS() {
  var qs = '';
  if (_alertFilter.kind) qs += '&kind=' + encodeURIComponent(_alertFilter.kind);
  if (_alertFilter.from !== '') qs += '&from=' + _alertFilter.from;
  if (_alertFilter.to !== '') qs += '&to=' + _alertFilter.to;
  return qs;
}
function alertPushBadge(p) {
  if (p && p.pushed) return '<span class="alert-tl-badge ok">已推送</span>';
  var rawRc = (p && (p.retry_count != null ? p.retry_count : p.retryCount));
  var rc = parseInt(rawRc || 0, 10) || 0;
  var st = String((p && p.status) || '');
  var rawOff = (p && (p.push_fail_streak != null ? p.push_fail_streak : p.fail_streak));
  var off = parseInt(rawOff || 0, 10) || 0;
  if (st === 'pending' || rc > 0) return '<span class="alert-tl-badge retry">重试中（已重试 ' + rc + ' 次）</span>';
  if (st === 'fail' || st === 'failed' || off > 0) return '<span class="alert-tl-badge fail">失败</span>';
  return '<span class="alert-tl-badge none">未推送</span>';
}
function alertItemHtml(it, highlight) {
  var p = (it && it.payload) || it || {};
  var meta = alertKindMeta(p.kind || '');
  var title = p.title || meta.label || '告警';
  var body = p.body != null ? p.body : (p.msg || '');
  var ts = (it && it.ts) || 0;
  return '<div class="alert-tl-item' + (highlight ? ' alert-tl-new' : '') + '">'
    + '<span class="alert-tl-dot ' + meta.cls + '"></span>'
    + '<div class="alert-tl-main">'
    +   '<div class="alert-tl-row">'
    +     '<span class="alert-tl-time">' + escapeHtml(new Date(ts * 1000).toLocaleTimeString('zh-CN', {hour12:false})) + '</span>'
    +     '<span class="alert-tl-title">' + meta.icon + ' ' + escapeHtml(title) + '</span>'
    +     '<span class="alert-tl-kind ' + meta.cls + '">' + escapeHtml(meta.label) + '</span>'
    +     '<span class="alert-tl-push">' + alertPushBadge(p) + '</span>'
    +   '</div>'
    +   (body ? '<div class="alert-tl-body">' + escapeHtml(String(body).replace(/\s*\n\s*/g, ' / ')) + '</div>' : '')
    + '</div></div>';
}
function alertTimelineShell() {
  var box = document.getElementById('alertHistBox');
  if (!box) return null;
  if (!document.getElementById('alertTlList')) {
    box.innerHTML =
      '<div class="alert-tl-bar">'
      + '<select id="alertTlKind" class="alert-tl-sel" onchange="alertTlApply()">'
      +   '<option value="">全部类型</option>'
      +   '<option value="down">异常（down）</option>'
      +   '<option value="recovered">恢复（recovered）</option>'
      +   '<option value="aborted">中断（aborted）</option>'
      + '</select>'
      + '<input type="datetime-local" id="alertTlFrom" class="alert-tl-dt" onchange="alertTlApply()">'
      + '<span class="alert-tl-sep">→</span>'
      + '<input type="datetime-local" id="alertTlTo" class="alert-tl-dt" onchange="alertTlApply()">'
      + '<button type="button" class="btn small gray" onclick="alertTlReset()">重置</button>'
      + '<button type="button" class="btn small red" style="margin-left:auto;" onclick="alertClearAll()">🗑 清空历史</button>'
      + '</div>'
      + '<div class="alert-tl" id="alertTlList"></div>';
    var ks = document.getElementById('alertTlKind');
    if (ks) ks.value = _alertFilter.kind;
  }
  return document.getElementById('alertTlList');
}
function alertTlApply() {
  _alertFilter.kind = alertElVal('alertTlKind');
  _alertFilter.from = alertLocalToTs(alertElVal('alertTlFrom'));
  _alertFilter.to = alertLocalToTs(alertElVal('alertTlTo'));
  loadAlertHist();
}
function alertTlReset() {
  _alertFilter = { kind: '', from: '', to: '' };
  var ks = document.getElementById('alertTlKind'); if (ks) ks.value = '';
  var f = document.getElementById('alertTlFrom'); if (f) f.value = '';
  var t = document.getElementById('alertTlTo'); if (t) t.value = '';
  loadAlertHist();
}
function renderAlertTimeline(items) {
  var list = document.getElementById('alertTlList');
  if (!list) return;
  if (!items || !items.length) {
    list.innerHTML = '<div class="alert-tl-empty">'
      + (alertFilterQS() ? '当前筛选条件下暂无告警记录' : '暂无告警记录') + '</div>';
    return;
  }
  var html = '', lastDay = '';
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    var day = alertDayKey(it.ts || 0);
    if (day !== lastDay) {
      if (lastDay !== '') html += '</div>';
      html += '<div class="alert-tl-group" data-day="' + day + '"><div class="alert-tl-day">' + escapeHtml(day) + '</div>';
      lastDay = day;
    }
    html += alertItemHtml(it, false);
  }
  if (lastDay !== '') html += '</div>';
  list.innerHTML = html;
}
function alertTlPrepend(payload, ts) {
  var box = document.getElementById('alertHistBox');
  var list = document.getElementById('alertTlList');
  if (!box || !list || box.style.display === 'none') return;
  var it = { ts: ts || Math.floor(Date.now() / 1000), payload: payload || {} };
  var html = alertItemHtml(it, true);
  var day = alertDayKey(it.ts);
  if (list.querySelector('.alert-tl-empty')) list.innerHTML = '';
  var top = list.firstElementChild;
  if (top && top.classList && top.classList.contains('alert-tl-group') && top.getAttribute('data-day') === day) {
    var hdr = top.querySelector('.alert-tl-day');
    if (hdr) hdr.insertAdjacentHTML('afterend', html);
    else top.insertAdjacentHTML('afterbegin', html);
  } else {
    list.insertAdjacentHTML('afterbegin',
      '<div class="alert-tl-group" data-day="' + day + '"><div class="alert-tl-day">' + escapeHtml(day) + '</div>' + html + '</div>');
  }
}
function alertClearAll() {
  if (!confirm('确定要清空全部告警历史记录吗？此操作不可撤销。')) return;
  var fd = new FormData();
  fd.append('action', 'alert_clear');
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) fd.append('csrf', csrf.value);
  fetch(_PANEL_ACTION, { method: 'POST', body: fd }).then(function(r){ return r.json(); }).then(function(d){
    if (d && d.ok) { miniToast('✅ ' + (d.msg || '告警历史已清空')); loadAlertHist(); }
    else { miniToast('❌ ' + (d && d.msg ? d.msg : '清空失败')); }
  }).catch(function(){ miniToast('❌ 清空失败：网络错误'); });
}
function saveAlert() {
  var btn = document.getElementById('alertSaveBtn');
  if (btn) btn.textContent = '⏳ 保存中…';
  var fd = new FormData();
  fd.append('action', 'alert_save');
  fd.append('alert_enable', document.getElementById('alertEnable').checked ? '1' : '0');
  fd.append('alert_channel', document.getElementById('alertChannel').value);
  fd.append('alert_target', document.getElementById('alertTarget').value.trim());
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) fd.append('csrf', csrf.value);
  fetch('action', { method: 'POST', body: fd }).then(function(r) { return r.json(); }).then(function(d) {
    if (btn) btn.textContent = '💾 保存设置';
    var badge = document.getElementById('alertBadge');
    if (badge && d && d.ok) badge.textContent = d.enable ? '已开启' : '未开启';
    miniToast(d && d.ok ? '告警配置已保存' : ('保存失败：' + (d && d.msg ? d.msg : '未知错误')));
  }).catch(function() {
    if (btn) btn.textContent = '💾 保存设置';
    miniToast('保存失败：网络错误');
  });
}
function testAlert() {
  var btn = document.getElementById('alertTestBtn');
  if (btn) btn.textContent = '⏳ 发送中…';
  var fd = new FormData();
  fd.append('action', 'alert_test');
  fd.append('alert_channel', document.getElementById('alertChannel').value);
  fd.append('alert_target', document.getElementById('alertTarget').value.trim());
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) fd.append('csrf', csrf.value);
  fetch('action', { method: 'POST', body: fd }).then(function(r) { return r.json(); }).then(function(d) {
    if (btn) btn.textContent = '📤 发送测试';
    miniToast(d && d.ok ? '测试消息已发出，去手机上看一眼' : ('发送失败：' + (d && d.msg ? d.msg : '请检查配置')));
  }).catch(function() {
    if (btn) btn.textContent = '📤 发送测试';
    miniToast('发送失败：网络错误');
  });
}
/* ===== v1.18：全局搜索 ===== */
var _searchHits = [], _searchIdx = [];
function openSearch() {
  buildSearchIndex();
  var o = document.getElementById('searchOverlay');
  if (o) o.classList.add('show');
  var inp = document.getElementById('searchInput');
  if (inp) {
    inp.value = '';
    searchRender('');
    setTimeout(function() { inp.focus(); }, 60);
  }
}
function closeSearch() {
  var o = document.getElementById('searchOverlay');
  if (o) o.classList.remove('show');
}
function buildSearchIndex() {
  var idx = [];
  idx.push({ type: 'page', ico: '🏠', main: '概览页', sub: '页面', kw: '概览 首页 主页 状态 overview', go: 'overview' });
  idx.push({ type: 'page', ico: '📋', main: '任务页', sub: '页面', kw: '任务 执行 历史 tasks', go: 'tasks' });
  idx.push({ type: 'page', ico: '📜', main: '日志页', sub: '页面', kw: '日志 输出 排错 log', go: 'log' });
  idx.push({ type: 'page', ico: '⚙️', main: '配置页', sub: '页面', kw: '配置 设置 参数 config', go: 'config' });
  Object.keys(window.TASK_LIST || {}).forEach(function(k) {
    var t = TASK_LIST[k];
    if (!t) return;
    idx.push({ type: 'task', ico: t.icon || '\U0001f3af', main: t.label, sub: '任务', kw: (t.label + ' ' + (t.desc || '') + ' ' + k).toLowerCase(), go: k });
  });
  document.querySelectorAll('.cfg-row').forEach(function(row) {
    var txt = '';
    var lab = row.querySelector('.cfg-label');
    if (lab) {
      for (var i = 0; i < lab.childNodes.length; i++) {
        var n = lab.childNodes[i];
        if (n.nodeType === 3 && n.textContent.trim()) { txt = n.textContent; break; }
      }
      if (!txt) txt = lab.textContent;
    }
    if (!txt) txt = row.textContent || '';
    txt = txt.replace(/\s+/g, ' ').trim();
    if (!txt || txt.length > 46) txt = txt.slice(0, 46);
    if (!txt) return;
    var grp = row.closest ? row.closest('.cfg-group') : null;
    var gname = '';
    if (grp) {
      var h3 = grp.querySelector('h3');
      if (h3) gname = (h3.textContent || '').replace(/\s+/g, ' ').trim();
    }
    idx.push({ type: 'cfg', ico: '🔧', main: txt, sub: gname || '配置', kw: (txt + ' ' + gname).toLowerCase(), ref: row });
  });
  idx.push({ type: 'act', ico: '\U0001f504', main: '重启容器', sub: '操作', kw: '重启 容器 restart', run: function() { fabAct('restart'); } });
  idx.push({ type: 'act', ico: '⏸️', main: '停止容器', sub: '操作', kw: '停止 容器 stop', run: function() { fabAct('stop'); } });
  idx.push({ type: 'act', ico: '⬆️', main: '更新镜像', sub: '操作', kw: '更新 镜像 升级 update', run: function() { if (confirm('更新面板镜像并重建容器？')) doUpdate(); } });
  idx.push({ type: 'act', ico: '\U0001f5d1️', main: '清空任务历史', sub: '操作', kw: '清空 历史 记录 clear', run: function() { clearHistory(); } });
  idx.push({ type: 'act', ico: '⬆️', main: '回到顶部', sub: '操作', kw: '顶部 回顶 backtop', run: function() { backToTop(); } });
  idx.push({ type: 'act', ico: '\U0001f3a8', main: '切换主题', sub: '操作', kw: '主题 颜色 深色 浅色 跟随系统 theme', run: function() { toggleTheme(); } });
  _searchIdx = idx;
}
function searchRender(q) {
  var box = document.getElementById('searchResults');
  if (!box) return;
  q = (q || '').trim().toLowerCase();
  _searchHits = [];
  if (!q) {
    box.innerHTML = '<div class="search-hint">输入关键词：任务名 / 页面 / 配置项 / 操作<br>回车跳第一项 · Esc 关闭 · Ctrl+K 随时唤起</div>';
    return;
  }
  _searchHits = _searchIdx.filter(function(it) {
    return (it.main + ' ' + (it.sub || '') + ' ' + (it.kw || '')).toLowerCase().indexOf(q) >= 0;
  }).slice(0, 20);
  if (!_searchHits.length) {
    box.innerHTML = '<div class="search-hint">没有找到「' + q + '」相关内容</div>';
    return;
  }
  box.innerHTML = _searchHits.map(function(it, i) {
    return '<button type="button" class="search-item' + (i === 0 ? ' sel' : '') + '" onclick="searchGo(' + i + ')">' +
      '<span class="si-ico">' + it.ico + '</span><span class="si-main">' + it.main + '</span><span class="si-sub">' + (it.sub || '') + '</span></button>';
  }).join('');
}
function searchGo(i) {
  var it = _searchHits[i];
  if (!it) return;
  closeSearch();
  if (it.type === 'page') { switchTab(it.go); return; }
  if (it.type === 'task') { switchTab('tasks'); setTimeout(function() { flashFind(it.main); }, 240); return; }
  if (it.type === 'cfg') { switchTab('config'); setTimeout(function() { goCfg(it.ref); }, 240); return; }
  if (it.type === 'act') { setTimeout(function() { it.run(); }, 150); return; }
}
function flashEl(el) {
  if (!el) return;
  try { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch(e) { el.scrollIntoView(); }
  el.classList.remove('search-hit');
  void el.offsetWidth;
  el.classList.add('search-hit');
  setTimeout(function() { el.classList.remove('search-hit'); }, 2300);
}
function flashFind(text) {
  var scope = document.querySelector('.page.active') || document;
  var cands = scope.querySelectorAll('button, .task-card, .cfg-row');
  for (var i = 0; i < cands.length; i++) {
    var tx = cands[i].textContent || '';
    if (tx.indexOf(text) >= 0) { flashEl(cands[i]); return; }
  }
  miniToast('已跳到任务页');
}
function goCfg(ref) {
  if (!ref || !ref.isConnected) {
    buildSearchIndex();
    miniToast('页面已刷新，请重新搜索');
    return;
  }
  var grp = ref.closest ? ref.closest('.cfg-group') : null;
  if (grp) {
    if (grp.classList.contains('collapsed')) toggleCfgGroup(grp.id.replace('cfgGroup-', ''));
    var body = grp.querySelector('.cfg-body');
    if (body) { body.style.maxHeight = 'none'; body.style.opacity = '1'; }
  }
  flashEl(ref);
}
/* ===== v1.18：边缘左右滑切页（≤640px） ===== */
(function() {
  var sx = 0, sy = 0, st = 0, tracking = false;
  var TABS = ['overview', 'tasks', 'log', 'config'];
  document.addEventListener('touchstart', function(e) {
    if (window.innerWidth > 640 || !e.touches || !e.touches.length) { tracking = false; return; }
    if (document.querySelector('.sheet-overlay.show, .search-overlay.show, .sidebar.open, .sidebar.open')) { tracking = false; return; }
    var ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT' || ae.isContentEditable)) { tracking = false; return; }
    var t = e.touches[0];
    if (t.clientX <= 28 || t.clientX >= window.innerWidth - 28) {
      sx = t.clientX; sy = t.clientY; st = Date.now(); tracking = true;
    } else {
      tracking = false;
    }
  }, { passive: true });
  document.addEventListener('touchend', function(e) {
    if (!tracking) return;
    tracking = false;
    if (!e.changedTouches || !e.changedTouches.length) return;
    var t = e.changedTouches[0];
    var dx = t.clientX - sx, dy = t.clientY - sy, dt = Date.now() - st;
    if (Math.abs(dx) < 60 || Math.abs(dy) >= 50 || dt > 700) return;
    var active = document.querySelector('.page.active');
    var cur = active && active.dataset ? active.dataset.tab : 'overview';
    var i = TABS.indexOf(cur);
    if (i < 0) i = 0;
    var nextI = (sx <= 28) ? i - 1 : i + 1;
    if (nextI < 0 || nextI >= TABS.length) return;
    try { if (navigator.vibrate) navigator.vibrate(8); } catch(e2) {}
    switchTab(TABS[nextI]);
  }, { passive: true });
})();

/* ===== M5-C：事件流 WS（/m7a-events）——monitor/log/history/alert 事件驱动，
   连通时挂起对应轮询、断开退避重连并恢复；status/running 保持 10s 轮询。 ===== */
var _evWs = null, _evRetry = 0, _evUp = false, _evLastSeq = 0, _evTimers = {};
var _evPause = { log: false };
function evUrl() {
  var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  var base = location.pathname.replace(/\/[^\/]*$/, '/');
  var since = 0;
  try { since = parseInt(localStorage.getItem('m7a_ev_seq') || '0', 10) || 0; } catch (e) {}
  return proto + location.host + base + 'm7a-events' + (since > 0 ? '?since=' + since : '');
}
function evRemember(seq) {
  seq = parseInt(seq, 10) || 0;
  if (seq <= _evLastSeq) return;
  _evLastSeq = seq;
  try { localStorage.setItem('m7a_ev_seq', String(seq)); } catch (e) {}
}
function evThrottle(key, fn, ms) {
  if (_evTimers[key]) return;
  _evTimers[key] = setTimeout(function() { _evTimers[key] = null; fn(); }, ms);
}
function evCurTab() {
  var a = document.querySelector('.page.active');
  return (a && a.dataset) ? a.dataset.tab : 'overview';
}
function evHandle(m) {
  if (!m || !m.type) return;
  if (m.type === 'hello') {
    /* hello 是权威锚点：服务端重启后 seq 会归零，强制以服务端值为准，避免旧锚点导致每次连接都 resync */
    _evLastSeq = parseInt(m.seq, 10) || 0;
    try { localStorage.setItem('m7a_ev_seq', String(_evLastSeq)); } catch (e) {}
    return;
  }
  if (m.type === 'ping') { evRemember(m.seq); return; }
  if (m.type === 'resync') {
    evRemember(m.seq);
    loadMonitor(); /* 有序缺口兜底：全量刷新一轮 */
    if (evCurTab() === 'log') refreshLog();
    if (evCurTab() === 'tasks') loadHistory();
    return;
  }
  evRemember(m.seq);
  var p = m.payload || {};
  if (m.type === 'monitor') { evThrottle('mon', loadMonitor, 2000); return; }
  if (m.type === 'log') { if (evCurTab() === 'log') evThrottle('log', refreshLog, 400); return; }
  if (m.type === 'history') { if (evCurTab() === 'tasks') evThrottle('hist', loadHistory, 600); return; }
  if (m.type === 'alert') {
    miniToast('⚠️ ' + (p.title || '收到告警'));
    alertTlPrepend(p, m.ts);  /* v1.22：历史面板打开时实时插入时间轴顶部并高亮 */
    return;
  }
}
function evConnect() {
  if (_evUp && _evWs) return;
  var ws = null;
  try { ws = new WebSocket(evUrl()); } catch (e) { evDown(); return; }
  _evWs = ws;
  ws.onopen = function() {
    if (_evWs !== ws) return;
    _evRetry = 0;
    _evUp = true;
    evPausePolling();
  };
  ws.onmessage = function(ev) {
    var m = null;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    evHandle(m);
  };
  ws.onclose = function() {
    if (_evWs !== ws) return;
    evDown();
  };
  ws.onerror = function() { try { ws.close(); } catch (e) {} };
}
function evDown() {
  var was = _evUp;
  _evWs = null;
  _evUp = false;
  if (was) evResumePolling();
  var delay = Math.min(30000, 2000 * Math.pow(2, _evRetry++));
  setTimeout(function() { if (!_evUp) evConnect(); }, delay);
}
function evPausePolling() {
  stopMonitor();
  if (_logTimer) { clearInterval(_logTimer); _logTimer = null; _evPause.log = true; }
}
function evResumePolling() {
  if (_evPause.log && !_logTimer) {
    var ms = parseInt(document.getElementById('refreshInterval').value) || 5000;
    _logTimer = setInterval(refreshLog, ms);
  }
  _evPause.log = false;
  startMonitor();
}

// Init
refreshStatus();
refreshLog();
startAutoRefresh();
initLogExport();
loadECharts(function(){ loadMonitor(); });
startMonitor();
/* v1.18：搜索输入绑定 + 全局快捷键 + 告警巡检心跳 */
(function() {
  var inp = document.getElementById('searchInput');
  if (inp) {
    inp.addEventListener('input', function() { searchRender(inp.value); });
    inp.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') { e.preventDefault(); searchGo(0); }
    });
  }
  document.addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      openSearch();
    }
    if (e.key === 'Escape') { closeSearch(); closeTaskSheet(); }
  });
  setInterval(function() { fetch('?ajax=alert_check').catch(function() {}); }, 60000);
  evConnect(); /* M5-C：事件流接入（monitor/log/history/alert 推送 + 轮询合并） */
})();

/* ===== v1.22：计划任务 表格 / 日历（周视图）切换 =====
   纯前端渲染：数据取自页面内 <script id="schedDataJson">（服务端渲染的计划任务列表）。
   日历按「星期 + 时间」把任务摆到对应格子；停用灰显；点方块切回表格并定位到该行。
   切换不改变表格本身及其原有的启用/停用、删除、立即运行等入口。 */
var SCHED_WEEK = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
function schedTasks() {
  var el = document.getElementById('schedDataJson');
  if (!el) return [];
  try { var v = JSON.parse(el.textContent || '[]'); return (v && v.length) ? v : []; } catch (e) { return []; }
}
function schedTaskDays(t) {
  var d = (t && t._days && t._days.length) ? t._days : ((t && t.days) ? t.days : []);
  if (!d || !d.length) return [1, 2, 3, 4, 5, 6, 7];  /* 空 = 每天 */
  return d;
}
function schedTaskOn(t) {
  var e = t && t.enabled;
  return e === true || e === 1 || e === '1' || e === 'true';
}
function setSchedView(v) {
  var tbl = document.querySelector('table.sched-table');
  var wrap = tbl ? tbl.closest('.hist-table-wrap') : null;
  var cal = document.getElementById('schedCalWrap');
  if (!wrap || !cal) return;
  var isCal = (v === 'cal');
  wrap.style.display = isCal ? 'none' : '';
  cal.style.display = isCal ? '' : 'none';
  var bt = document.getElementById('schedViewTable'), bc = document.getElementById('schedViewCal');
  if (bt) bt.className = 'sched-cal-tab' + (isCal ? '' : ' active');
  if (bc) bc.className = 'sched-cal-tab' + (isCal ? ' active' : '');
  if (isCal && !cal.getAttribute('data-rendered')) { renderSchedCal(); cal.setAttribute('data-rendered', '1'); }
}
function renderSchedCal() {
  var cal = document.getElementById('schedCalWrap');
  if (!cal) return;
  var tasks = schedTasks();
  if (!tasks.length) {
    cal.innerHTML = '<div class="sched-cal-empty">还没有计划任务。切回「表格」用下方表单添加后，这里会按每天/星期显示周视图。</div>';
    return;
  }
  var buckets = {};
  for (var i = 0; i < tasks.length; i++) {
    var t = tasks[i];
    var hm = String(t.time || '00:00').split(':');
    var hh = parseInt(hm[0], 10); if (isNaN(hh) || hh < 0 || hh > 23) hh = 0;
    var days = schedTaskDays(t);
    for (var d = 0; d < days.length; d++) {
      var wd = parseInt(days[d], 10) - 1;
      if (isNaN(wd) || wd < 0 || wd > 6) continue;
      var key = hh + '_' + wd;
      (buckets[key] = buckets[key] || []).push(t);
    }
  }
  var html = '<div class="sched-cal-scroll"><table class="sched-cal"><thead><tr>'
    + '<th class="sched-cal-corner">时间</th>';
  for (var c = 0; c < 7; c++) html += '<th class="sched-cal-th">' + SCHED_WEEK[c] + '</th>';
  html += '</tr></thead><tbody>';
  for (var h = 0; h < 24; h++) {
    html += '<tr><th class="sched-cal-hour">' + ((h < 10 ? '0' : '') + h) + ':00</th>';
    for (var c2 = 0; c2 < 7; c2++) {
      var list = buckets[h + '_' + c2] || [];
      html += '<td class="sched-cal-cell">';
      for (var k = 0; k < list.length; k++) {
        var tk = list[k];
        var on = schedTaskOn(tk);
        var nm = tk.name || tk.id || '任务';
        var lb = tk._label || tk._daytext || tk.args || '';
        html += '<button type="button" class="sched-cal-ev' + (on ? '' : ' off') + '"'
          + ' data-id="' + escapeHtml(String(tk.id || '')) + '"'
          + ' title="' + escapeHtml(nm + ' · ' + (tk.time || '') + ' · ' + lb + (on ? '' : '（已停用）')) + '">'
          + '<span class="sched-cal-ev-time">' + escapeHtml(String(tk.time || '')) + '</span>'
          + '<span class="sched-cal-ev-name">' + escapeHtml(nm) + '</span>'
          + '</button>';
      }
      html += '</td>';
    }
    html += '</tr>';
  }
  html += '</tbody></table></div>'
    + '<p class="sched-cal-hint">按每个任务的「星期 + 时间」摆到对应格子；不勾选星期 = 每天执行（周一至周日都出现）；<span class="sched-cal-off">灰显</span> = 已停用。点某个任务方块会切回表格并高亮该行，可在那里启用/停用、删除，或用下方表单「立即运行一次」。</p>';
  cal.innerHTML = html;
  var evs = cal.querySelectorAll('.sched-cal-ev');
  for (var e = 0; e < evs.length; e++) {
    evs[e].addEventListener('click', function () { schedCalGo(this.getAttribute('data-id')); });
  }
}
function schedCalGo(id) {
  setSchedView('table');
  if (!id) return;
  var rows = document.querySelectorAll('table.sched-table tbody tr');
  for (var i = 0; i < rows.length; i++) {
    var inp = rows[i].querySelector('input[name="sched_id"]');
    if (inp && inp.value === id) {
      var row = rows[i];
      row.classList.remove('sched-cal-hl');
      void row.offsetWidth;  /* 重排以重启动画 */
      row.classList.add('sched-cal-hl');
      try { row.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (e2) { row.scrollIntoView(); }
      setTimeout(function () { row.classList.remove('sched-cal-hl'); }, 2200);
      return;
    }
  }
}

/* ===== 保存并重启 ===== */
function restartAfterSave() {
  var form = document.getElementById('configForm');
  var input = document.createElement('input');
  input.type = 'hidden';
  input.name = 'then_restart';
  input.value = '1';
  form.appendChild(input);
  form.submit();
}

function reloadYaml() {
  fetch('?ajax=config_raw').then(function(r) { return r.text(); }).then(function(t) {
    document.getElementById('yamlEditor').value = t;
  }).catch(function() {});
}

/* ===== 实例管理 ===== */
function openInstModal() {
  document.getElementById('instModal').style.display = 'flex';
}
function closeInstModal() {
  document.getElementById('instModal').style.display = 'none';
}
function newInst() {
  document.getElementById('inst_id').value = '';
  document.getElementById('inst_name').value = '';
  document.getElementById('inst_container').value = '';
  document.getElementById('inst_dir').value = '';
  document.getElementById('inst_default').checked = false;
  document.getElementById('instFormWrap').style.display = '';
}
function hideInstForm() {
  document.getElementById('instFormWrap').style.display = 'none';
}
function editInst(it) {
  document.getElementById('inst_id').value = it.id || '';
  document.getElementById('inst_name').value = it.name || '';
  document.getElementById('inst_container').value = it.container || '';
  document.getElementById('inst_dir').value = it.dir || '';
  document.getElementById('inst_default').checked = !!(it.default);
  document.getElementById('instFormWrap').style.display = '';
}
function askDeleteInst(it) {
  var name = it.name || '';
  var typed = prompt('删除实例「' + name + '」？\n此操作不可撤销，且会从面板移除该实例配置。\n请输入实例名称以确认：', '');
  if (typed === null) return;
  if (typed.trim() !== name) { alert('确认失败：输入的名称与实例名不一致'); return; }
  var form = document.createElement('form');
  form.method = 'post';
  var csrf = document.querySelector('input[name="csrf"]');
  if (csrf) { var c = document.createElement('input'); c.type = 'hidden'; c.name = 'csrf'; c.value = csrf.value; form.appendChild(c); }
  var a = document.createElement('input'); a.type = 'hidden'; a.name = 'action'; a.value = 'instance_delete'; form.appendChild(a);
  var b = document.createElement('input'); b.type = 'hidden'; b.name = 'inst_id'; b.value = it.id || ''; form.appendChild(b);
  var d = document.createElement('input'); d.type = 'hidden'; d.name = 'inst_confirm'; d.value = name; form.appendChild(d);
  document.body.appendChild(form); form.submit();
}

/* ===== v1.15+：折叠分组的高度过渡（独立视觉增强，不改变 toggleCfgGroup 行为） =====
   纯 CSS 已能用 max-height 过渡；这里在能测到真实高度时用实测值，窗口缩放时交回 CSS，避免内容被裁切。 */
(function initCfgCollapseMotion() {
  var bodies = document.querySelectorAll('.cfg-group .cfg-body');
  if (!bodies.length) return;
  var findGroup = function(el) {
    var g = el.parentNode;
    while (g && g.nodeType === 1 && !(g.classList && g.classList.contains('cfg-group'))) g = g.parentNode;
    return (g && g.nodeType === 1) ? g : null;
  };
  var sync = function(body, group) {
    if (group.classList.contains('collapsed')) { body.style.maxHeight = '0px'; return; }
    if (body.scrollHeight > 0) { body.style.maxHeight = (body.scrollHeight + 24) + 'px'; }
    else { body.style.maxHeight = ''; }
  };
  Array.prototype.forEach.call(bodies, function(body) {
    var group = findGroup(body);
    if (!group) return;
    sync(body, group);
    var head = group.querySelector('h3.cfg-toggle');
    if (!head) return;
    head.addEventListener('click', function() {
      window.requestAnimationFrame(function() { sync(body, group); });
    });
  });
  window.addEventListener('resize', function() {
    Array.prototype.forEach.call(bodies, function(body) {
      var group = findGroup(body);
      if (!group) return;
      if (group.classList.contains('collapsed')) body.style.maxHeight = '0px';
      else body.style.maxHeight = '';
    });
  });
})();

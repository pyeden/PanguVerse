/* PanguVerse Web 控制台 — 原生 JS 单页应用
 *
 * 约束：无构建步骤、无 npm、无 CDN、无外部字体与图片，离线可用。
 * 数据：只消费 /api 契约（见 tasks/web-app-mvp-spec.md §3），不伪造数据。
 * 失败：任何非 2xx / 网络错误都在页面上渲染可读原因，绝不静默。
 */
(function () {
  'use strict';

  /* ================================================================== */
  /* 1. 常量与状态                                                       */
  /* ================================================================== */
  var PHASES = [
    { key: 'plan', label: '规划' },
    { key: 'validate', label: '校验' },
    { key: 'execute', label: '执行' },
    { key: 'heal', label: '自愈' },
    { key: 'report', label: '报告' },
    { key: 'evolve', label: '沉淀' },
    { key: 'regression', label: '回归' }
  ];

  var MODE_LABEL = { demo: '演示', scenario: '场景', intent: '意图', regression: '回归' };

  var JOB_STATUS = {
    queued: { label: '排队中', cls: 'dim' },
    running: { label: '运行中', cls: 'run' },
    done: { label: '已完成', cls: 'ok' },
    failed: { label: '失败', cls: 'bad' }
  };

  var STATUS = {
    passed: { label: '通过', glyph: '✓', cls: 'ok' },
    failed: { label: '失败', glyph: '✕', cls: 'bad' },
    error: { label: '异常', glyph: '!', cls: 'bad' },
    skipped: { label: '跳过', glyph: '–', cls: 'dim' },
    ok: { label: '正常', glyph: '✓', cls: 'ok' },
    failed_phase: { label: '失败', glyph: '✕', cls: 'bad' },
    running: { label: '进行中', glyph: '⏵', cls: 'run' },
    pending: { label: '待开始', glyph: '·', cls: 'dim' }
  };

  var ROUTE_TITLE = {
    overview: '概览',
    run: '运行',
    history: '运行历史',
    'run-detail': '运行详情',
    atoms: '原子库',
    packs: '领域包',
    notfound: '页面不存在'
  };

  var APP = {
    seq: 0,
    status: null,
    atomsAll: null,
    atomFilters: { domain: '', tag: '', category: '' },
    job: { id: null, mode: '', data: null, error: null, lost: false, startedLocal: 0, poll: null, tick: null, dismiss: false },
    controller: null,
    lastRoute: null,
    bannerRoute: null
  };

  /* ================================================================== */
  /* 2. 基础工具                                                         */
  /* ================================================================== */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function esc(value) {
    if (value === undefined || value === null) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function hasVal(value) {
    if (value === undefined || value === null) return false;
    if (typeof value === 'string' && value.trim() === '') return false;
    return true;
  }

  function fmtNum(n) {
    if (typeof n !== 'number' || !isFinite(n)) return '—';
    try { return n.toLocaleString('zh-CN'); } catch (e) { return String(n); }
  }

  function fmtMs(ms) {
    if (typeof ms !== 'number' || !isFinite(ms)) return '—';
    if (ms < 1) return ms.toFixed(2) + ' ms';
    if (ms < 1000) return Math.round(ms) + ' ms';
    if (ms < 60000) return (ms / 1000).toFixed(2) + ' s';
    var m = Math.floor(ms / 60000);
    var s = Math.round((ms % 60000) / 1000);
    return m + ' 分 ' + s + ' 秒';
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  /* epoch 秒（后端时间戳）→ 本地时间字符串 */
  function fmtTs(ts) {
    if (typeof ts !== 'number' || !isFinite(ts) || ts <= 0) return '—';
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '—';
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function passStats(summary) {
    var total = summary && typeof summary.total === 'number' ? summary.total : 0;
    var passed = summary && typeof summary.passed === 'number' ? summary.passed : 0;
    var rate = total > 0 ? Math.round((passed / total) * 1000) / 10 : 0;
    var tone = total === 0 ? 'low' : (passed === total ? 'perfect' : (rate >= 80 ? 'mid' : 'low'));
    return { total: total, passed: passed, rate: rate, tone: tone };
  }

  function rateHtml(summary) {
    var s = passStats(summary);
    var text = s.total > 0 ? s.passed + '/' + s.total : '—';
    var bar = '<div class="pass-bar ' + (s.tone === 'perfect' ? '' : s.tone) + '"><span style="width:' + Math.max(0, Math.min(100, s.rate)) + '%"></span></div>';
    return '<div><span class="pass-rate ' + s.tone + '">' + esc(text) +
      (s.total > 0 ? ' · ' + esc(String(s.rate)) + '%' : '') + '</span>' + bar + '</div>';
  }

  function badge(status) {
    var meta = STATUS[status] || { label: status || '未知', glyph: '?', cls: 'dim' };
    return '<span class="badge ' + meta.cls + '"><span class="glyph" aria-hidden="true">' +
      meta.glyph + '</span>' + esc(meta.label) + '</span>';
  }

  function jobBadge(status) {
    var meta = JOB_STATUS[status] || { label: status || '未知', cls: 'dim' };
    return '<span class="badge ' + meta.cls + '">' + esc(meta.label) + '</span>';
  }

  function valOrMissing(value) {
    if (value === undefined || value === null || value === '') {
      return '<span class="missing">未提供</span>';
    }
    if (typeof value === 'object') {
      return '<pre class="json-block">' + esc(JSON.stringify(value, null, 2)) + '</pre>';
    }
    return esc(String(value));
  }

  function prettyJson(value) {
    try { return JSON.stringify(value, null, 2); } catch (e) { return String(value); }
  }

  /* ================================================================== */
  /* 3. 网络请求（统一错误模型）                                          */
  /* ================================================================== */
  function ApiError(message, status, code) {
    this.name = 'ApiError';
    this.message = message;
    this.status = status || 0;
    this.code = code || '';
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function request(path, options) {
    options = options || {};
    var init = {
      method: options.method || 'GET',
      headers: { 'Accept': 'application/json' },
      cache: 'no-store'
    };
    if (options.body !== undefined) {
      init.headers['Content-Type'] = 'application/json; charset=utf-8';
      init.body = JSON.stringify(options.body);
    }

    return fetch(path, init).then(function (res) {
      var ctype = (res.headers.get('content-type') || '').toLowerCase();
      var isJson = ctype.indexOf('json') >= 0;
      var read = isJson
        ? res.json().catch(function () { return null; })
        : res.text().catch(function () { return ''; });

      return read.then(function (payload) {
        if (!res.ok) {
          var msg = '';
          var code = '';
          if (payload && typeof payload === 'object' && payload.error) {
            msg = payload.error.message || '';
            code = payload.error.code || '';
          } else if (typeof payload === 'string' && payload.trim()) {
            msg = payload.trim().slice(0, 300);
          }
          throw new ApiError(msg || ('请求失败（HTTP ' + res.status + '）'), res.status, code);
        }
        if (isJson && payload === null) {
          throw new ApiError('服务端返回的响应不是合法 JSON', res.status, 'bad_json');
        }
        return payload;
      });
    }).catch(function (err) {
      if (err instanceof ApiError) throw err;
      var raw = err && err.message ? err.message : '未知错误';
      throw new ApiError('网络请求失败：' + raw + '。请确认 panguverse serve 仍在运行。', 0, 'network');
    });
  }

  function apiGet(path) { return request(path); }
  function apiPost(path, body) { return request(path, { method: 'POST', body: body }); }

  function errorHint(err) {
    if (!err) return '';
    if (err.code === 'network' || err.status === 0) {
      return '提示：后端未启动或端口不通。用 panguverse serve --port 7788 --no-open 启动后重试。';
    }
    if (err.status === 404) return '提示：资源不存在。检查 run_id / 包名，或确认后端版本包含该接口。';
    if (err.status === 400) return '提示：请求参数被拒绝。请检查填写内容（例如场景是否为空、意图是否为空）。';
    if (err.status === 409) return '提示：已有同名任务在运行，等它结束后再试。';
    if (err.status >= 500) return '提示：服务端内部错误，详细原因见服务端日志。';
    return '';
  }

  function errorHtml(err, title) {
    var msg = err && err.message ? err.message : '未知错误';
    var code = err && err.code ? err.code : (err && err.status ? 'http_' + err.status : 'error');
    var hint = errorHint(err);
    return '' +
      '<div class="error-block" role="alert">' +
        '<div class="error-title"><span aria-hidden="true">⚠</span>' + esc(title || '请求失败') +
          '<span class="error-code">' + esc(code) + '</span>' +
        '</div>' +
        '<div class="error-msg">' + esc(msg) + '</div>' +
        (hint ? '<div class="error-hint">' + esc(hint) + '</div>' : '') +
        '<div class="error-actions">' +
          '<button type="button" class="btn btn-sm" data-retry>重试</button>' +
        '</div>' +
      '</div>';
  }

  function renderError(host, err, onRetry, title) {
    if (!host) return;
    host.innerHTML = errorHtml(err, title);
    var btn = $('[data-retry]', host);
    if (btn && typeof onRetry === 'function') {
      btn.addEventListener('click', function () { onRetry(); });
    }
  }

  /* ================================================================== */
  /* 4. 横幅 / Toast / 抽屉                                              */
  /* ================================================================== */
  function showBanner(kind, html, ttl) {
    var banner = $('#globalBanner');
    if (!banner) return;
    // 横幅只在「产生它的视图」内有效：路由一变就收，避免带着上一页的成功提示
    APP.bannerRoute = APP.lastRoute || null;
    banner.dataset.kind = kind || 'info';
    banner.innerHTML = '<div class="banner-body">' + html + '</div>' +
      '<button type="button" class="banner-close" data-banner-close aria-label="关闭提示">✕</button>';
    banner.hidden = false;
    clearTimeout(showBanner._t);
    if (ttl) {
      showBanner._t = setTimeout(hideBanner, ttl);
    }
  }

  function hideBanner() {
    var banner = $('#globalBanner');
    APP.bannerRoute = null;
    clearTimeout(showBanner._t);
    if (banner) { banner.hidden = true; banner.innerHTML = ''; }
  }

  function toast(message, kind, ttl) {
    var host = $('#toasts');
    if (!host) return;
    var node = document.createElement('div');
    node.className = 'toast';
    node.dataset.kind = kind || 'info';
    node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    node.textContent = message;
    host.appendChild(node);
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, ttl || (kind === 'error' ? 6000 : 3200));
  }

  var drawerState = { opener: null };

  function openDrawer(titleHtml, bodyHtml, opener) {
    var root = $('#drawerRoot');
    if (!root) return;
    drawerState.opener = opener || document.activeElement;
    root.hidden = false;
    root.innerHTML = '' +
      '<button type="button" class="drawer-backdrop" data-drawer-close tabindex="-1" aria-label="关闭侧栏"></button>' +
      '<aside class="drawer" role="dialog" aria-modal="true" aria-label="详情面板">' +
        '<div class="drawer-head">' +
          '<h2>' + titleHtml + '</h2>' +
          '<button type="button" class="btn btn-icon drawer-close" data-drawer-close aria-label="关闭详情面板">✕</button>' +
        '</div>' +
        '<div class="drawer-body">' + bodyHtml + '</div>' +
      '</aside>';
    var closeBtn = $('.drawer-close', root);
    if (closeBtn) closeBtn.focus();
    document.body.style.overflow = 'hidden';
  }

  function setDrawerBody(html) {
    var body = $('#drawerRoot .drawer-body');
    if (body) body.innerHTML = html;
  }

  function closeDrawer() {
    var root = $('#drawerRoot');
    if (!root || root.hidden) return;
    root.hidden = true;
    root.innerHTML = '';
    document.body.style.overflow = '';
    if (drawerState.opener && typeof drawerState.opener.focus === 'function') {
      try { drawerState.opener.focus(); } catch (e) { /* noop */ }
    }
    drawerState.opener = null;
  }

  /* ================================================================== */
  /* 5. 任务面板与轮询                                                   */
  /* ================================================================== */
  function phaseMap(data) {
    var map = {};
    var list = (data && data.phases) || [];
    list.forEach(function (p) { if (p && p.name) map[p.name] = p; });
    return map;
  }

  function currentPhaseKey(map, data) {
    if (!data || (data.status !== 'queued' && data.status !== 'running')) return null;
    for (var i = 0; i < PHASES.length; i++) {
      if (!map[PHASES[i].key]) return PHASES[i].key;
    }
    return null;
  }

  function phaseStatus(key, map, data) {
    var rec = map[key];
    if (rec) {
      if (rec.status === 'failed' || rec.status === 'error') return 'failed';
      if (rec.status === 'running') return 'active';
      if (rec.status === 'healed') return 'healed';
      if (rec.status === 'skipped') return 'skipped';
      return 'ok';
    }
    if (key === currentPhaseKey(map, data)) return 'active';
    return 'pending';
  }

  function completedPhases(map) {
    var n = 0;
    PHASES.forEach(function (p) {
      var rec = map[p.key];
      if (rec && rec.status !== 'running') n++;
    });
    return n;
  }

  function elapsedSeconds() {
    var j = APP.job;
    var d = j.data;
    if (d && typeof d.elapsed_ms === 'number' && d.status && d.status !== 'queued') {
      return Math.max(0, d.elapsed_ms / 1000);
    }
    var base = d && typeof d.started_at === 'number' ? d.started_at * 1000 : j.startedLocal;
    var end = d && typeof d.finished_at === 'number' ? d.finished_at * 1000 : Date.now();
    return Math.max(0, (end - base) / 1000);
  }

  function elapsedText() {
    var sec = elapsedSeconds();
    if (sec < 60) return sec.toFixed(1) + ' 秒';
    return Math.floor(sec / 60) + ' 分 ' + Math.round(sec % 60) + ' 秒';
  }

  function jobCardHtml() {
    var j = APP.job;
    var data = j.data;
    var status = j.lost ? 'failed' : (data ? data.status : 'queued');
    var map = phaseMap(data);
    var done = data && (data.status === 'done' || data.status === 'failed');
    // 后端可能把「跑完但没通过」记为 done；此时按失败呈现，避免绿色「已完成」误导
    var runFailed = !!(data && data.conclusion && data.conclusion.success === false);
    var badgeHtml = jobBadge(status);
    if (data && data.status === 'done' && runFailed) {
      badgeHtml = '<span class="badge bad"><span class="glyph" aria-hidden="true">✕</span>未通过</span>';
    }

    var pct = done ? 100 : Math.round((completedPhases(map) / PHASES.length) * 100);
    if (!done && pct < 4) pct = 4;

    var phasesHtml = PHASES.map(function (p) {
      var st = phaseStatus(p.key, map, data);
      return '<span class="phase" data-s="' + st + '"><span class="p-dot" aria-hidden="true"></span>' + p.label + '</span>';
    }).join('');

    var resultHtml = '';
    if (j.lost) {
      resultHtml = '<div class="error-msg">任务状态已丢失（服务可能已重启）。内存态任务不会恢复，请到运行历史查看已落盘的结果。</div>';
    } else if (j.error) {
      resultHtml = '<div class="error-msg">读取任务状态失败：' + esc(j.error.message) + '</div>';
    } else if (data && data.status === 'failed') {
      resultHtml = '<div class="error-msg">任务失败：' + esc(data.error || '未提供失败原因（请查看服务端日志）') + '</div>';
    } else if (data && data.summary) {
      var s = data.summary;
      var verdict = data.conclusion && data.conclusion.verdict;
      resultHtml = '<div class="job-result">' +
        '<span>通过 <b>' + esc(String(s.passed)) + '/' + esc(String(s.total)) + '</b></span>' +
        '<span>失败 <b>' + esc(String(s.failed)) + '</b></span>' +
        '<span>异常 <b>' + esc(String(s.errored)) + '</b></span>' +
        '<span>自愈 <b>' + esc(String(s.healed_cases)) + '</b></span>' +
        '<span>耗时 <b>' + esc(fmtMs(s.duration_ms)) + '</b></span>' +
        '</div>' +
        (verdict ? '<div class="job-result"><span>结论 <b>' + esc(verdict) + '</b></span></div>' : '');
    }

    var actions = '';
    if (data && data.run_id && data.status === 'done') {
      actions = '<div class="error-actions" style="display:flex;gap:8px;flex-wrap:wrap">' +
        '<button type="button" class="btn btn-sm btn-primary" data-act="job-detail" data-run="' + esc(data.run_id) + '">查看运行详情</button>' +
        '<a class="btn btn-sm" href="#/history">运行历史</a></div>';
    }

    return '' +
      '<div class="job-card" data-status="' + esc(status) + '">' +
        '<div class="job-head">' +
          '<span class="job-title">任务 · ' + esc(MODE_LABEL[j.mode] || j.mode || '未知模式') + '</span>' +
          badgeHtml +
          '<span class="job-meta">' + esc(j.id || '') + '</span>' +
          '<span class="job-spacer"></span>' +
          '<span class="job-meta">已运行 <span data-elapsed="1">' + esc(elapsedText()) + '</span></span>' +
          '<button type="button" class="btn btn-icon" data-act="job-dismiss" aria-label="收起任务面板">✕</button>' +
        '</div>' +
        '<div class="phases" aria-label="阶段进度">' + phasesHtml + '</div>' +
        '<div class="progress-track"><div class="progress-fill ' + (status === 'failed' || runFailed ? 'failed' : (status === 'done' ? 'done' : '')) +
          '" style="width:' + pct + '%"></div></div>' +
        resultHtml +
        actions +
      '</div>';
  }

  function renderJobPanels() {
    var dock = $('#jobDock');
    if (dock) {
      if (APP.job.id && !APP.job.dismiss) {
        dock.hidden = false;
        dock.innerHTML = jobCardHtml();
      } else {
        dock.hidden = true;
        dock.innerHTML = '';
      }
    }
    $$('[data-job-inline]').forEach(function (host) {
      host.innerHTML = APP.job.id ? jobCardHtml() : '';
    });
    syncRunFormState();
  }

  function tickElapsed() {
    if (!APP.job.id) return;
    $$('[data-elapsed]').forEach(function (node) {
      node.textContent = elapsedText();
    });
  }

  function stopJobTimers() {
    if (APP.job.poll) { clearInterval(APP.job.poll); APP.job.poll = null; }
    if (APP.job.tick) { clearInterval(APP.job.tick); APP.job.tick = null; }
  }

  function pollJob() {
    var j = APP.job;
    if (!j.id) return;
    var id = j.id;
    request('/api/jobs/' + encodeURIComponent(id)).then(function (data) {
      if (APP.job.id !== id) return;
      APP.job.data = data;
      APP.job.error = null;
      APP.job.lost = false;
      if (data && (data.status === 'done' || data.status === 'failed')) {
        stopJobTimers();
        try { localStorage.removeItem('pv.activeJob'); } catch (e) { /* noop */ }
        if (APP.job.notified !== id) {
          APP.job.notified = id;
          if (data.status === 'done' && data.conclusion && data.conclusion.success === false) {
            toast('任务未通过：' + (data.summary ? data.summary.passed + '/' + data.summary.total + ' 通过' : '存在失败用例'), 'error');
          } else if (data.status === 'done') {
            toast('任务完成：' + (data.summary ? data.summary.passed + '/' + data.summary.total + ' 通过' : '已生成报告'), 'ok');
          } else {
            toast('任务失败：' + (data.error || '未提供原因'), 'error');
          }
        }
        refreshStatus().then(function (st) {
          if (st && parseRoute().name === 'overview') patchOverviewStats(st);
        });
      }
      renderJobPanels();
    }).catch(function (err) {
      if (APP.job.id !== id) return;
      if (err.status === 404) {
        APP.job.lost = true;
        stopJobTimers();
        try { localStorage.removeItem('pv.activeJob'); } catch (e) { /* noop */ }
      } else {
        APP.job.error = err;
      }
      renderJobPanels();
    });
  }

  function attachJob(id, mode, startedAtLocal) {
    stopJobTimers();
    var j = APP.job;
    j.id = id;
    j.mode = mode || '';
    j.data = null;
    j.error = null;
    j.lost = false;
    j.dismiss = false;
    j.notified = null;
    j.startedLocal = startedAtLocal || Date.now();
    try {
      localStorage.setItem('pv.activeJob', JSON.stringify({ id: id, mode: j.mode, at: j.startedLocal }));
    } catch (e) { /* noop */ }
    j.poll = setInterval(pollJob, 900);
    j.tick = setInterval(tickElapsed, 1000);
    renderJobPanels();
    pollJob();
  }

  function restoreJob() {
    var raw = null;
    try { raw = localStorage.getItem('pv.activeJob'); } catch (e) { return; }
    if (!raw) return;
    var obj = null;
    try { obj = JSON.parse(raw); } catch (e) { obj = null; }
    if (!obj || !obj.id) {
      try { localStorage.removeItem('pv.activeJob'); } catch (e2) { /* noop */ }
      return;
    }
    var age = Date.now() - (obj.at || 0);
    if (age > 6 * 3600 * 1000) {
      try { localStorage.removeItem('pv.activeJob'); } catch (e3) { /* noop */ }
      return;
    }
    attachJob(obj.id, obj.mode, obj.at);
  }

  function jobIsActive() {
    var d = APP.job.data;
    return !!(APP.job.id && !APP.job.lost && (!d || d.status === 'queued' || d.status === 'running'));
  }

  function startRun(payload) {
    if (jobIsActive()) {
      showBanner('warn', '<strong>已有任务在运行。</strong> 前端一次只提交一个任务，请等待当前任务结束。', 8000);
      return Promise.reject(new ApiError('已有任务在运行', 409, 'busy'));
    }
    return apiPost('/api/runs', payload).then(function (res) {
      if (!res || !res.job_id) {
        throw new ApiError('服务端未返回 job_id，无法跟踪任务状态', 500, 'bad_response');
      }
      attachJob(res.job_id, payload.mode);
      toast('任务已提交：' + res.job_id, 'ok');
      return res;
    });
  }

  /* ================================================================== */
  /* 6. 平台状态（侧栏 + 连接指示）                                       */
  /* ================================================================== */
  function updateConn(ok, err) {
    var node = $('#connState');
    if (!node) return;
    node.dataset.state = ok ? 'ok' : 'bad';
    node.innerHTML = '<span class="conn-dot" aria-hidden="true"></span>' + (ok ? '已连接' : '未连接');
    node.title = ok ? '' : ((err && err.message) || '无法连接服务端');
  }

  function applyStatus(st) {
    APP.status = st || null;
    updateConn(!!st);
    var badgeEl = $('#llmBadge');
    var textEl = $('#llmText');
    if (st) {
      var llm = !!st.llm_enabled;
      if (badgeEl) badgeEl.dataset.mode = llm ? 'ai' : 'offline';
      if (textEl) textEl.textContent = '规划方式：' + (llm ? 'AI 规划' : '离线规则规划');
      if (badgeEl) {
        badgeEl.title = llm ? '已配置模型密钥，规划可调用 LLM' : '未配置模型密钥，使用离线规则兜底（不会调用 LLM）';
      }
      var ws = $('#factWorkspace');
      if (ws) { ws.textContent = st.workspace || '—'; ws.title = st.workspace || ''; }
      var ver = $('#factVersion');
      if (ver) ver.textContent = st.version || '—';
    } else if (badgeEl) {
      badgeEl.dataset.mode = 'error';
      if (textEl) textEl.textContent = '规划方式：未知（未连接）';
    }
  }

  function refreshStatus() {
    return apiGet('/api/status').then(function (st) {
      applyStatus(st);
      return st;
    }).catch(function (err) {
      updateConn(false, err);
      return null;
    });
  }

  /* ================================================================== */
  /* 7. 概览                                                             */
  /* ================================================================== */
  function statCard(spec) {
    return '' +
      '<div class="stat-card"' + (spec.tone ? ' data-tone="' + spec.tone + '"' : '') + '>' +
        (spec.action ? '<div class="stat-action">' + spec.action + '</div>' : '') +
        '<div class="stat-label">' + esc(spec.label) + '</div>' +
        '<div class="stat-value' + (spec.small ? ' small' : '') + '">' + spec.value + '</div>' +
        '<div class="stat-note">' + spec.note + '</div>' +
      '</div>';
  }

  function statGridHtml(st) {
    var llm = !!st.llm_enabled;
    return '' +
      statCard({
        label: '原子数',
        value: esc(fmtNum(st.atoms)),
        note: '可编排的原子动作'
      }) +
      statCard({
        label: '领域包',
        value: esc(fmtNum(st.packs)),
        note: '领域包边界与语义'
      }) +
      statCard({
        label: '回归库',
        value: esc(fmtNum(st.regression_count)),
        note: '已沉淀用例，可直接重跑',
        action: '<button type="button" class="btn btn-sm" data-act="run-regression">跑一次</button>'
      }) +
      statCard({
        label: '规划方式',
        small: true,
        tone: llm ? 'accent' : 'warn',
        value: esc(llm ? 'AI 规划' : '离线规则规划'),
        note: llm
          ? '已配置模型密钥'
          : '未配置模型密钥，规则兜底（不会调用 LLM）'
      });
  }

  function overviewHtml(st) {
    var facts = '' +
      '<div class="kv-grid">' +
        '<dt>工作区</dt><dd class="mono">' + (hasVal(st.workspace) ? esc(st.workspace) : '<span class="missing">未提供</span>') + '</dd>' +
        '<dt>HTTP 后端</dt><dd class="mono">' + esc(st.http_backend || '未提供') + '</dd>' +
        '<dt>场景</dt><dd>' + esc(fmtNum(st.scenarios)) + ' 个</dd>' +
        '<dt>经验</dt><dd>' + esc(fmtNum(st.lessons_count)) + ' 条</dd>' +
        '<dt>版本</dt><dd class="mono">' + esc(st.version || '未提供') + '</dd>' +
      '</div>';

    return '' +
      '<div class="page-head">' +
        '<div>' +
          '<h1>概览</h1>' +
          '<div class="sub">本机控制台 · 规划 → 校验 → 执行 → 自愈 → 报告 → 沉淀 → 回归</div>' +
        '</div>' +
        '<div class="actions">' +
          '<button type="button" class="btn btn-primary" data-act="run-demo">运行内置演示</button>' +
          '<a class="btn" href="#/run">自定义运行</a>' +
        '</div>' +
      '</div>' +
      '<div class="stat-grid">' + statGridHtml(st) + '</div>' +
      '<div class="section">' +
        '<div class="section-head"><h2>环境</h2><span class="hint">数据来自 GET /api/status</span></div>' +
        '<div class="panel">' + facts + '</div>' +
      '</div>' +
      '<div class="section">' +
        '<div class="section-head">' +
          '<h2>最近运行</h2>' +
          '<span class="hint"><a href="#/history">查看全部历史</a></span>' +
        '</div>' +
        '<div id="recentRuns"><div class="loading"><span class="spinner"></span>正在读取运行历史…</div></div>' +
      '</div>';
  }

  function renderRecentRuns(host, ctx) {
    if (!host) return;
    apiGet('/api/runs?limit=4').then(function (runs) {
      if (!ctx.alive() || !document.contains(host)) return;
      if (!Array.isArray(runs) || runs.length === 0) {
        host.innerHTML = '<div class="empty"><strong>还没有运行记录</strong>' +
          '<span>点击「运行内置演示」跑通一次闭环，结果会写入工作区 runs/ 目录。</span></div>';
        return;
      }
      var rows = runs.map(function (r) {
        var s = r.summary || {};
        var verdict = (r.conclusion && r.conclusion.verdict) || (s.success ? '通过' : '存在失败');
        return '<tr class="is-clickable" data-href="#/runs/' + encodeURIComponent(r.run_id) + '">' +
          '<td class="tight"><a class="row-link" href="#/runs/' + encodeURIComponent(r.run_id) + '">' + esc(r.run_id) + '</a></td>' +
          '<td class="tight">' + esc(fmtTs(r.started_at)) + '</td>' +
          '<td>' + rateHtml(s) + '</td>' +
          '<td class="num">' + esc(fmtMs(r.duration_ms)) + '</td>' +
          '<td>' + esc(verdict) + '</td>' +
        '</tr>';
      }).join('');
      host.innerHTML = '<div class="table-wrap"><table class="data">' +
        '<thead><tr><th>run</th><th>开始时间</th><th>通过率</th><th class="num">耗时</th><th>结论</th></tr></thead>' +
        '<tbody>' + rows + '</tbody></table></div>';
    }).catch(function (err) {
      if (!ctx.alive() || !document.contains(host)) return;
      host.innerHTML = '<div class="empty"><strong>无法读取运行历史</strong><span>' +
        esc(err.message) + '</span><a href="#/history">打开运行历史页重试</a></div>';
    });
  }

  function patchOverviewStats(st) {
    var grid = $('#view .stat-grid');
    if (!grid || !st) return;
    grid.innerHTML = statGridHtml(st);
  }

  function renderOverview(host, ctx) {
    host.innerHTML = '<div class="loading"><span class="spinner"></span>正在读取平台状态…</div>';
    return apiGet('/api/status').then(function (st) {
      if (!ctx.alive()) return;
      applyStatus(st);
      host.innerHTML = overviewHtml(st);
      renderRecentRuns($('#recentRuns', host), ctx);
    }).catch(function (err) {
      if (!ctx.alive()) return;
      updateConn(false, err);
      applyStatus(null);
      renderError(host, err, function () { render(); }, '无法读取平台状态');
    });
  }

  /* ================================================================== */
  /* 8. 运行页                                                           */
  /* ================================================================== */
  var MODE_HINTS = {
    demo: '运行内置闭环演示（7 个用例）：规划 → 校验 → 执行 → 自愈 → 报告 → 沉淀 → 回归复用。',
    scenario: '从工作区或内置 scenarios/ 目录选择 YAML 场景文件直接执行，不需要重新描述意图。',
    intent: '用一句话描述目标，由规划层生成用例；未配置模型密钥时使用离线规则规划。',
    regression: '直接复用已沉淀的回归库重新执行，不重新规划。'
  };

  function getMode(form) {
    var checked = $('input[name="mode"]:checked', form);
    return checked ? checked.value : 'demo';
  }

  function addEnvRow(container, key, value) {
    if (!container) return;
    var row = document.createElement('div');
    row.className = 'env-row';
    row.innerHTML = '' +
      '<input class="input" data-env-key aria-label="环境变量名" placeholder="BASE_URL" value="' + esc(key || '') + '" autocomplete="off" spellcheck="false">' +
      '<input class="input" data-env-val aria-label="环境变量值" placeholder="http://127.0.0.1:8899" value="' + esc(value || '') + '" autocomplete="off" spellcheck="false">' +
      '<button type="button" class="btn btn-icon btn-danger-ghost" data-remove-env aria-label="删除该环境变量">✕</button>';
    container.appendChild(row);
  }

  function runFormHtml() {
    return '' +
      '<div class="page-head">' +
        '<div>' +
          '<h1>运行</h1>' +
          '<div class="sub">选择一种方式生成计划并执行；结果写入工作区 runs/ 目录，可在运行历史里回看。</div>' +
        '</div>' +
        '<div class="actions"><a class="btn" href="#/history">运行历史</a></div>' +
      '</div>' +
      '<form class="form-grid" id="runForm" novalidate>' +
        '<div class="field">' +
          '<span class="field-label" id="modeLabel">运行模式</span>' +
          '<div class="segmented" role="radiogroup" aria-labelledby="modeLabel">' +
            '<label><input type="radio" name="mode" value="demo" checked><span>演示</span></label>' +
            '<label><input type="radio" name="mode" value="scenario"><span>场景</span></label>' +
            '<label><input type="radio" name="mode" value="intent"><span>意图</span></label>' +
            '<label><input type="radio" name="mode" value="regression"><span>回归</span></label>' +
          '</div>' +
          '<div class="field-hint" id="modeHint"></div>' +
        '</div>' +

        '<div class="field" data-when="scenario" hidden>' +
          '<label for="scenarioSelect">场景文件</label>' +
          '<select class="select" id="scenarioSelect">' +
            '<option value="">加载中…</option>' +
          '</select>' +
          '<div class="field-hint" id="scenarioNote">正在读取场景列表…</div>' +
        '</div>' +

        '<div class="field" data-when="intent" hidden>' +
          '<label for="intentInput">意图描述</label>' +
          '<textarea class="textarea" id="intentInput" placeholder="例如：检查健康状态并验证登录下单" autocomplete="off"></textarea>' +
          '<div class="field-hint">示例：检查健康状态并验证登录下单。无模型密钥时由离线规则生成计划。</div>' +
        '</div>' +

        '<div class="field" data-when="demo scenario intent" hidden>' +
          '<span class="field-label">环境变量</span>' +
          '<div class="env-rows" id="envRows"></div>' +
          '<div><button type="button" class="btn btn-sm" id="addEnv">+ 添加环境变量</button></div>' +
          '<div class="field-hint">键值对会作为运行时环境传入（如 BASE_URL、API_USERNAME）。留空的行会被忽略。</div>' +
        '</div>' +

        '<div class="field" data-when="demo scenario intent" hidden>' +
          '<label class="checkbox">' +
            '<input type="checkbox" id="noLlm">' +
            '<span>强制离线规则规划（no_llm）</span>' +
          '</label>' +
          '<div class="field-hint">勾选后即使配置了模型密钥也不调用 LLM，全部走规则兜底。</div>' +
        '</div>' +

        '<div class="field" data-when="regression" hidden>' +
          '<div class="panel">' +
            '<div class="kv-grid">' +
              '<dt>来源</dt><dd>已沉淀的回归库（复用上次执行结构，不重新规划）</dd>' +
              '<dt>条件</dt><dd>回归库为空时任务会失败并给出原因；先跑一次演示或场景即可沉淀。</dd>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<div class="form-actions">' +
          '<button type="submit" class="btn btn-primary" id="submitRun">开始运行</button>' +
          '<span class="form-note" id="runNote">任务在后台线程执行，本页会轮询进度。</span>' +
        '</div>' +
        '<div id="runFormError" aria-live="assertive"></div>' +
        '<div data-job-inline></div>' +
      '</form>';
  }

  function setFormError(form, message, focusEl) {
    var host = $('#runFormError', form);
    if (host) {
      host.innerHTML = message ? '<div class="error-block" role="alert">' +
        '<div class="error-title"><span aria-hidden="true">⚠</span>无法提交</div>' +
        '<div class="error-msg">' + esc(message) + '</div>' +
        '</div>' : '';
    }
    if (focusEl && typeof focusEl.focus === 'function') focusEl.focus();
  }

  function syncRunFormState() {
    var form = $('#runForm');
    if (!form) return;
    var submit = $('#submitRun', form);
    var note = $('#runNote', form);
    if (!submit) return;
    var busy = jobIsActive();
    if (submit.dataset.busy !== '1') {
      submit.disabled = busy;
    }
    if (note && busy) {
      note.textContent = '已有任务在运行，完成前不能提交新任务。';
    } else if (note && !busy) {
      note.textContent = '任务在后台线程执行，本页会轮询进度。';
    }
  }

  function collectEnv(form) {
    var env = {};
    var errors = [];
    $$('.env-row', form).forEach(function (row, idx) {
      var keyEl = $('[data-env-key]', row);
      var valEl = $('[data-env-val]', row);
      var key = keyEl ? keyEl.value.trim() : '';
      var val = valEl ? valEl.value : '';
      if (!key && String(val).trim() === '') return;
      if (!key) {
        errors.push('第 ' + (idx + 1) + ' 行：填写了值但缺少变量名');
        return;
      }
      if (Object.prototype.hasOwnProperty.call(env, key)) {
        errors.push('环境变量名重复：' + key);
        return;
      }
      env[key] = val;
    });
    return { env: env, errors: errors };
  }

  function submitRunForm(form) {
    if (!form) return;
    setFormError(form, '');
    var mode = getMode(form);
    var payload = { mode: mode };

    var scenarioEl = $('#scenarioSelect', form);
    var intentEl = $('#intentInput', form);
    var noLlmEl = $('#noLlm', form);

    if (mode === 'scenario') {
      var scenario = scenarioEl ? scenarioEl.value : '';
      if (!scenario) {
        setFormError(form, '请选择场景文件（列表为空或全部解析失败时无法运行场景模式）。', scenarioEl);
        return;
      }
      payload.scenario = scenario;
    }
    if (mode === 'intent') {
      var intent = intentEl ? intentEl.value.trim() : '';
      if (!intent) {
        setFormError(form, '意图不能为空，请填写一句话目标。', intentEl);
        return;
      }
      payload.intent = intent;
    }
    if (mode !== 'regression') {
      var envResult = collectEnv(form);
      if (envResult.errors.length) {
        setFormError(form, envResult.errors.join('；'), $('[data-env-key]', form));
        return;
      }
      if (Object.keys(envResult.env).length > 0) payload.env = envResult.env;
      if (noLlmEl && noLlmEl.checked) payload.no_llm = true;
    }

    if (jobIsActive()) {
      setFormError(form, '已有任务在运行，请等待其完成后再提交。');
      return;
    }

    var submit = $('#submitRun', form);
    if (submit) {
      submit.dataset.busy = '1';
      submit.disabled = true;
      submit.textContent = '提交中…';
    }

    startRun(payload).then(function () {
      setFormError(form, '');
    }).catch(function (err) {
      if (err && err.code === 'busy') {
        setFormError(form, '已有任务在运行，请等待其完成后再提交。');
      } else {
        var host = $('#runFormError', form);
        if (host) {
          renderError(host, err, function () { submitRunForm(form); }, '任务提交失败');
        }
      }
    }).then(function () {
      if (submit) {
        submit.dataset.busy = '';
        submit.disabled = jobIsActive();
        submit.textContent = getMode(form) === 'demo' ? '运行内置演示' : '开始运行';
      }
      syncRunFormState();
    });
  }

  function loadScenarios(select, note, ctx) {
    if (!select) return;
    apiGet('/api/scenarios').then(function (list) {
      if (!ctx.alive() || !select.isConnected) return;
      if (!Array.isArray(list) || list.length === 0) {
        select.innerHTML = '<option value="">没有可用场景</option>';
        select.disabled = true;
        if (note) note.textContent = '未发现场景文件：把 YAML 放到 <workspace>/scenarios/ 下，或使用内置 scenarios/ 目录。';
        return;
      }
      var bad = [];
      var firstOk = null;
      var options = list.map(function (item) {
        var isBad = !!item.error;
        var name = item.name || item.id || '(未命名)';
        var sourceLabel = item.source === 'workspace' ? '工作区' : '内置';
        var label = isBad
          ? name + ' · 解析失败 · ' + sourceLabel
          : name + ' · ' + (item.cases || 0) + ' 用例 · ' + sourceLabel;
        if (isBad) {
          bad.push({ name: name, error: item.error });
        } else if (!firstOk) {
          firstOk = item;
        }
        return '<option value="' + esc(item.id) + '"' + (isBad ? ' disabled' : '') + '>' + esc(label) + '</option>';
      });
      select.innerHTML = options.join('');
      select.disabled = false;
      if (firstOk) select.value = firstOk.id;
      if (note) {
        if (bad.length) {
          note.innerHTML = bad.length + ' 个场景解析失败（已置灰不可选）：' +
            esc(bad[0].name) + '：' + esc(bad[0].error || '未提供原因') +
            (bad.length > 1 ? '（另有 ' + (bad.length - 1) + ' 个）' : '');
        } else {
          note.textContent = '共 ' + list.length + ' 个场景可选，工作区场景优先。';
        }
      }
    }).catch(function (err) {
      if (!ctx.alive() || !select.isConnected) return;
      select.innerHTML = '<option value="">场景加载失败</option>';
      select.disabled = true;
      if (note) {
        note.innerHTML = '<span class="field-error">场景列表加载失败：' + esc(err.message) + '</span>' +
          ' <button type="button" class="btn btn-sm" data-scenario-retry>重试</button>';
        var btn = $('[data-scenario-retry]', note);
        if (btn) {
          btn.addEventListener('click', function () {
            note.textContent = '正在读取场景列表…';
            loadScenarios(select, note, ctx);
          });
        }
      }
    });
  }

  function renderRun(host, ctx) {
    host.innerHTML = runFormHtml();
    var form = $('#runForm', host);
    if (!form) return;

    var envRows = $('#envRows', form);
    addEnvRow(envRows);

    var addBtn = $('#addEnv', form);
    if (addBtn) {
      addBtn.addEventListener('click', function () {
        addEnvRow(envRows);
        var inputs = $$('[data-env-key]', envRows);
        if (inputs.length) inputs[inputs.length - 1].focus();
      });
    }
    if (envRows) {
      envRows.addEventListener('click', function (e) {
        var btn = e.target.closest ? e.target.closest('[data-remove-env]') : null;
        if (!btn) return;
        var row = btn.closest('.env-row');
        if (row && row.parentNode) row.parentNode.removeChild(row);
      });
    }

    var radios = $$('input[name="mode"]', form);
    var applyMode = function () {
      var mode = getMode(form);
      $$('[data-when]', form).forEach(function (block) {
        var modes = (block.getAttribute('data-when') || '').split(/\s+/);
        block.hidden = modes.indexOf(mode) < 0;
      });
      var hintEl = $('#modeHint', form);
      if (hintEl) hintEl.textContent = MODE_HINTS[mode] || '';
      var submit = $('#submitRun', form);
      if (submit && submit.dataset.busy !== '1') {
        submit.textContent = mode === 'demo' ? '运行内置演示' : '开始运行';
      }
      setFormError(form, '');
    };
    radios.forEach(function (r) { r.addEventListener('change', applyMode); });
    applyMode();

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      submitRunForm(form);
    });

    loadScenarios($('#scenarioSelect', form), $('#scenarioNote', form), ctx);
    renderJobPanels();
    syncRunFormState();
  }

  /* ================================================================== */
  /* 9. 运行历史                                                         */
  /* ================================================================== */
  function historyHtml(runs) {
    if (!Array.isArray(runs) || runs.length === 0) {
      return '<div class="empty"><strong>还没有运行记录</strong>' +
        '<span>每次运行都会在 <code>&lt;workspace&gt;/runs/&lt;run_id&gt;/</code> 落盘 report.json / report.html / junit.xml。</span>' +
        '<span>先跑一次内置演示，这里就会出现记录。</span>' +
        '<a class="btn btn-primary btn-sm" href="#/run">去运行页</a></div>';
    }
    var rows = runs.map(function (r) {
      var s = r.summary || {};
      var verdict = (r.conclusion && r.conclusion.verdict) || (s.success ? '通过' : '存在失败');
      var caseCount = typeof s.total === 'number' ? s.total + ' 个用例' : '—';
      return '<tr class="is-clickable" data-href="#/runs/' + encodeURIComponent(r.run_id) + '">' +
        '<td class="tight"><a class="row-link" href="#/runs/' + encodeURIComponent(r.run_id) + '">' + esc(r.run_id) + '</a></td>' +
        '<td class="tight">' + esc(fmtTs(r.started_at)) + '</td>' +
        '<td>' + rateHtml(s) + '</td>' +
        '<td class="num">' + esc(fmtMs(r.duration_ms)) + '</td>' +
        '<td class="tight">' + esc(caseCount) + '</td>' +
        '<td>' + esc(verdict) + '</td>' +
      '</tr>';
    }).join('');
    return '<div class="table-wrap"><table class="data">' +
      '<thead><tr><th>run_id</th><th>开始时间</th><th>通过率</th><th class="num">耗时</th><th>用例</th><th>结论</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>';
  }

  function renderHistory(host, ctx) {
    host.innerHTML = '' +
      '<div class="page-head">' +
        '<div><h1>运行历史</h1><div class="sub">扫描工作区 runs/ 目录，按开始时间倒序；点击行查看详情。</div></div>' +
        '<div class="actions">' +
          '<button type="button" class="btn" data-act="history-refresh">刷新</button>' +
          '<a class="btn btn-primary" href="#/run">新建运行</a>' +
        '</div>' +
      '</div>' +
      '<div id="historyBody"><div class="loading"><span class="spinner"></span>正在读取运行历史…</div></div>';

    var body = $('#historyBody', host);
    var load = function () {
      if (!ctx.alive()) return;
      body.innerHTML = '<div class="loading"><span class="spinner"></span>正在读取运行历史…</div>';
      apiGet('/api/runs?limit=50').then(function (runs) {
        if (!ctx.alive()) return;
        body.innerHTML = historyHtml(runs);
      }).catch(function (err) {
        if (!ctx.alive()) return;
        body.innerHTML = '<div id="historyError"></div>';
        renderError($('#historyError', body), err, load, '无法读取运行历史');
      });
    };
    load();
    var refreshBtn = $('[data-act="history-refresh"]', host);
    if (refreshBtn) refreshBtn.addEventListener('click', load);
  }

  /* ================================================================== */
  /* 10. 运行详情                                                        */
  /* ================================================================== */
  var RUN_ID_RE = /^[0-9a-f]{6,32}$/;

  function healEventsHtml(events, title) {
    if (!Array.isArray(events) || events.length === 0) return '';
    var items = events.map(function (ev) {
      var okBadge = ev.success
        ? '<span class="badge ok"><span class="glyph" aria-hidden="true">✓</span>自愈成功</span>'
        : '<span class="badge bad"><span class="glyph" aria-hidden="true">✕</span>未修复</span>';
      return '<div class="heal-item">' +
        '<div class="heal-line">' +
          '<span class="mono">' + esc(ev.step_id || '未提供') + '</span>' +
          '<span class="chip">' + esc(ev.kind || 'unknown') + '</span>' +
          '<span class="chip">' + esc(ev.action || 'unknown') + '</span>' +
          okBadge +
        '</div>' +
        '<div class="heal-reason">' + esc(ev.reason || '未提供原因') + '</div>' +
        (ev.suggestion ? '<div class="heal-suggest">建议：' + esc(ev.suggestion) + '</div>' : '') +
      '</div>';
    }).join('');
    return '<div class="heal-block"><div class="hb-title">' + esc(title || '自愈事件') + '（' + events.length + '）</div>' + items + '</div>';
  }

  function stepDetailHtml(step) {
    var err = step.error || {};
    var failed = step.status === 'failed' || step.status === 'error';
    var msg = hasVal(step.message) ? step.message : err.message;
    var parts = [];

    if (failed) {
      parts.push('' +
        '<div class="failure-block">' +
          '<div class="fb-title"><span aria-hidden="true">✕</span>失败原因</div>' +
          '<dl class="kv-grid">' +
            '<dt>原因</dt><dd>' + valOrMissing(msg) + '</dd>' +
            '<dt>期望</dt><dd>' + valOrMissing(err.expected) + '</dd>' +
            '<dt>实际</dt><dd>' + valOrMissing(err.actual) + '</dd>' +
            '<dt>断言路径</dt><dd class="mono">' + valOrMissing(err.path) + '</dd>' +
            '<dt>错误类型</dt><dd class="mono">' + esc(err.type || err.kind || '未提供') + '</dd>' +
          '</dl>' +
        '</div>');
    }

    if (step.skipped_reason) {
      parts.push('<dl class="kv-grid"><dt>跳过原因</dt><dd>' + valOrMissing(step.skipped_reason) + '</dd></dl>');
    }

    if (step.heal && step.heal.length) {
      parts.push(healEventsHtml(step.heal, '步骤自愈事件'));
    }

    var inputKeys = step.inputs && typeof step.inputs === 'object' ? Object.keys(step.inputs).length : 0;
    var outputKeys = step.outputs && typeof step.outputs === 'object' ? Object.keys(step.outputs).length : 0;
    parts.push('<details class="raw"><summary>输入参数（' + inputKeys + ' 个键）</summary>' +
      '<pre class="json-block">' + esc(prettyJson(step.inputs || {})) + '</pre></details>');
    parts.push('<details class="raw"><summary>输出（' + outputKeys + ' 个键）</summary>' +
      '<pre class="json-block">' + esc(prettyJson(step.outputs || {})) + '</pre></details>');

    return '<div class="step-detail ' + (failed ? 'fail' : '') + '">' + parts.join('') + '</div>';
  }

  function stepsTableHtml(caseObj) {
    var steps = caseObj.steps || [];
    if (!steps.length) {
      return '<div class="empty" style="margin:12px 14px"><strong>该用例没有步骤记录</strong><span>report.cases[].steps 为空。</span></div>';
    }
    var rows = steps.map(function (step, idx) {
      var dur = typeof step.duration_ms === 'number' ? fmtMs(step.duration_ms) : '—';
      var attempts = typeof step.attempts === 'number' ? step.attempts : 1;
      var retries = typeof step.retries === 'number' ? step.retries : 0;
      var attemptText = String(attempts) + (retries > 0 ? '（重试 ' + retries + '）' : '');
      // 失败/异常步骤默认展开：失败证据（message/expected/actual）不该再点一次才能看到
      var open = step.status === 'failed' || step.status === 'error';
      return '' +
        '<tr class="is-clickable" data-step="' + idx + '" tabindex="0">' +
          '<td class="mono">' + esc(step.step_id || '—') + '</td>' +
          '<td class="mono">' + esc(step.atom || '—') + '</td>' +
          '<td>' + badge(step.status) + '</td>' +
          '<td class="num">' + esc(dur) + '</td>' +
          '<td class="num">' + esc(attemptText) + '</td>' +
          '<td class="tight"><button type="button" class="btn btn-icon" data-step-toggle aria-expanded="' + (open ? 'true' : 'false') + '">' +
            (open ? '收起 ▾' : '明细 ▸') + '</button></td>' +
        '</tr>' +
        '<tr class="step-detail-row"' + (open ? '' : ' hidden') + '><td colspan="6">' + stepDetailHtml(step) + '</td></tr>';
    }).join('');

    return '<div class="table-wrap" style="border:0;border-radius:0">' +
      '<table class="data"><thead><tr>' +
        '<th>步骤</th><th>原子</th><th>状态</th><th class="num">耗时</th><th class="num">尝试</th><th></th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  function caseCardHtml(caseObj, idx) {
    var steps = caseObj.steps || [];
    var healEvents = caseObj.heal_events || [];
    var hasFailure = caseObj.status === 'failed' || caseObj.status === 'error';
    var open = hasFailure ? 'true' : 'false';

    var errorBlock = '';
    if (caseObj.error) {
      var ce = caseObj.error;
      errorBlock = '<div class="failure-block"><div class="fb-title"><span aria-hidden="true">✕</span>用例级错误</div>' +
        '<dl class="kv-grid">' +
          '<dt>原因</dt><dd>' + valOrMissing(ce.message || '未提供') + '</dd>' +
          '<dt>类型</dt><dd class="mono">' + esc(ce.type || ce.kind || '未提供') + '</dd>' +
        '</dl></div>';
    }

    return '' +
      '<article class="case-card" data-open="' + open + '">' +
        '<button type="button" class="case-head" aria-expanded="' + open + '" aria-controls="case-body-' + idx + '">' +
          '<span class="caret" aria-hidden="true">' + (open === 'true' ? '▾' : '▸') + '</span>' +
          badge(caseObj.status) +
          '<span class="case-name">' + esc(caseObj.name || '(未命名用例)') + '</span>' +
          '<span class="case-id">' + esc(caseObj.case_id || '') + '</span>' +
          '<span class="case-meta">' +
            '<span>' + steps.length + ' 步</span>' +
            '<span>' + esc(typeof caseObj.duration_ms === 'number' ? fmtMs(caseObj.duration_ms) : '—') + '</span>' +
            (healEvents.length ? '<span class="badge heal">自愈 ' + healEvents.length + ' 次</span>' : '') +
          '</span>' +
        '</button>' +
        '<div class="case-body" id="case-body-' + idx + '">' +
          errorBlock +
          stepsTableHtml(caseObj) +
          healEventsHtml(healEvents, '用例自愈事件') +
        '</div>' +
      '</article>';
  }

  function planPanelHtml(report) {
    var plan = report.plan || {};
    var warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
    var rows = '' +
      '<div class="plan-row">' +
        '<span>计划来源</span><b class="mono">' + esc(plan.source || '未提供') + '</b>' +
        '<span>规划器</span><b class="mono">' + esc(plan.planner || '未提供') + '</b>' +
        '<span>用例</span><b class="mono">' + esc(String(plan.case_count !== undefined ? plan.case_count : '—')) + '</b>' +
        '<span>步骤</span><b class="mono">' + esc(String(plan.step_count !== undefined ? plan.step_count : '—')) + '</b>' +
      '</div>' +
      (plan.intent ? '<div class="plan-row"><span>意图</span><b>' + esc(plan.intent) + '</b></div>' : '');

    var envKeys = plan.env && typeof plan.env === 'object' ? Object.keys(plan.env) : [];
    var envHtml = envKeys.length
      ? '<details class="raw"><summary>运行环境变量（' + envKeys.length + ' 个）</summary><pre class="json-block">' +
        esc(prettyJson(plan.env)) + '</pre></details>'
      : '';

    var warnHtml = warnings.length
      ? '<details class="raw"><summary>计划告警（' + warnings.length + ' 条）</summary><ul class="warn-list">' +
        warnings.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('') + '</ul></details>'
      : '';

    return '<div class="panel plan-panel">' + rows + envHtml + warnHtml + '</div>';
  }

  function detailHtml(data, id) {
    var report = data.report || {};
    var summary = report.summary || {};
    var conclusion = report.conclusion || {};
    var cases = Array.isArray(report.cases) ? report.cases : [];
    var plan = report.plan || {};
    var verdict = conclusion.verdict || (summary.success ? '通过' : '存在失败');
    var verdictOk = conclusion.success === true;

    var header = '' +
      '<div class="detail-head">' +
        '<div>' +
          '<div class="title-line">' +
            '<h1>run ' + esc(id) + '</h1>' +
            '<span class="badge ' + (verdictOk ? 'ok' : 'bad') + '">' +
              '<span class="glyph" aria-hidden="true">' + (verdictOk ? '✓' : '✕') + '</span>' + esc(verdict) + '</span>' +
            (summary.healed_cases ? '<span class="badge heal">自愈 ' + esc(String(summary.healed_cases)) + ' 个用例</span>' : '') +
          '</div>' +
          '<div class="detail-meta">' +
            '<span>开始 ' + esc(fmtTs(report.started_at)) + '</span>' +
            '<span>结束 ' + esc(fmtTs(report.finished_at)) + '</span>' +
            '<span>耗时 ' + esc(fmtMs(report.duration_ms)) + '</span>' +
            '<span>计划来源 <span class="mono">' + esc(plan.source || '未提供') + '</span></span>' +
            '<span>规划器 <span class="mono">' + esc(plan.planner || '未提供') + '</span></span>' +
          '</div>' +
        '</div>' +
        '<div class="actions">' +
          '<a class="btn btn-sm" href="/api/runs/' + encodeURIComponent(id) + '/junit.xml" download="panguverse-' + esc(id) + '.xml">下载 JUnit</a>' +
          '<a class="btn btn-sm" href="/api/runs/' + encodeURIComponent(id) + '/report.html" target="_blank" rel="noopener">打开 HTML 报告</a>' +
          '<a class="btn btn-sm btn-ghost" href="#/history">返回历史</a>' +
        '</div>' +
      '</div>';

    var strip = '<dl class="summary-strip">' +
      stripCell('用例', summary.total) +
      stripCell('通过', summary.passed) +
      stripCell('失败', summary.failed) +
      stripCell('异常', summary.errored) +
      stripCell('跳过', summary.skipped) +
      stripCell('自愈用例', summary.healed_cases) +
      stripCell('步骤', summary.steps) +
      stripCell('HTTP', summary.http_requests) +
      '</dl>';

    var caseHtml = cases.length
      ? cases.map(caseCardHtml).join('')
      : '<div class="empty"><strong>该运行没有用例记录</strong><span>report.json 中 cases 为空，请查看 HTML 报告或服务端日志。</span></div>';

    return header + strip +
      '<div class="section"><div class="section-head"><h2>计划</h2><span class="hint">来自 report.plan</span></div>' +
        (Object.keys(plan).length ? planPanelHtml(report) : '<div class="empty"><strong>没有计划信息</strong><span>report.plan 为空。</span></div>') +
      '</div>' +
      '<div class="section"><div class="section-head"><h2>用例</h2><span class="hint">点击用例展开步骤；点击步骤查看 message / expected / actual</span></div>' +
        '<div id="caseList">' + caseHtml + '</div>' +
      '</div>';
  }

  function stripCell(label, value) {
    var v = (typeof value === 'number') ? value : (value === undefined || value === null ? 0 : value);
    return '<div class="cell"><dt>' + esc(label) + '</dt><dd>' + esc(String(v)) + '</dd></div>';
  }

  function toggleCase(headBtn) {
    var card = headBtn.closest('.case-card');
    if (!card) return;
    var open = card.getAttribute('data-open') === 'true';
    card.setAttribute('data-open', open ? 'false' : 'true');
    headBtn.setAttribute('aria-expanded', open ? 'false' : 'true');
    var caret = headBtn.querySelector('.caret');
    if (caret) caret.textContent = open ? '▸' : '▾';
  }

  function toggleStep(btn) {
    if (!btn) return;
    var tr = btn.closest('tr');
    if (!tr) return;
    var detail = tr.nextElementSibling;
    if (!detail || detail.className.indexOf('step-detail-row') < 0) return;
    var willOpen = detail.hidden;
    detail.hidden = !willOpen;
    btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    btn.textContent = willOpen ? '收起 ▾' : '明细 ▸';
  }

  function bindCaseInteractions(host, signal) {
    var opts = signal ? { signal: signal } : false;
    host.addEventListener('click', function (e) {
      var head = e.target.closest ? e.target.closest('.case-head') : null;
      if (head) { toggleCase(head); return; }
      var btn = e.target.closest ? e.target.closest('[data-step-toggle]') : null;
      if (btn) { toggleStep(btn); return; }
      var row = e.target.closest ? e.target.closest('tr[data-step]') : null;
      if (row) toggleStep(row.querySelector('[data-step-toggle]'));
    }, opts);
    host.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var row = e.target.closest ? e.target.closest('tr[data-step]') : null;
      if (!row) return;
      e.preventDefault();
      toggleStep(row.querySelector('[data-step-toggle]'));
    }, opts);
  }

  function renderDetail(host, ctx, id) {
    if (!RUN_ID_RE.test(id)) {
      renderError(host, new ApiError('run_id 不合法：只允许 6-32 位十六进制字符', 400, 'bad_request'), function () {
        location.hash = '#/history';
      }, '无法打开运行详情');
      return;
    }
    host.innerHTML = '<div class="loading"><span class="spinner"></span>正在读取运行 ' + esc(id) + '…</div>';
    apiGet('/api/runs/' + encodeURIComponent(id)).then(function (data) {
      if (!ctx.alive()) return;
      if (!data || typeof data !== 'object' || !data.report) {
        throw new ApiError('响应缺少 report 字段', 500, 'bad_response');
      }
      host.innerHTML = detailHtml(data, id);
      bindCaseInteractions(host, ctx.signal);
    }).catch(function (err) {
      if (!ctx.alive()) return;
      host.innerHTML = '<div class="page-head"><div><h1>run ' + esc(id) + '</h1>' +
        '<div class="sub">无法读取该运行</div></div>' +
        '<div class="actions"><a class="btn" href="#/history">返回运行历史</a></div></div><div id="detailError"></div>';
      renderError($('#detailError', host), err, function () { render(); }, '无法读取运行详情');
    });
  }

  /* ================================================================== */
  /* 11. 原子库                                                          */
  /* ================================================================== */
  function uniq(list) {
    var seen = {};
    var out = [];
    list.forEach(function (v) {
      if (!hasVal(v)) return;
      var k = String(v);
      if (seen[k]) return;
      seen[k] = 1;
      out.push(k);
    });
    return out.sort();
  }

  function collectTags(atoms) {
    var tags = [];
    atoms.forEach(function (a) {
      if (Array.isArray(a.tags)) tags = tags.concat(a.tags);
    });
    return uniq(tags);
  }

  function optionList(values, current, allLabel) {
    var html = '<option value="">' + esc(allLabel || '全部') + '</option>';
    values.forEach(function (v) {
      html += '<option value="' + esc(v) + '"' + (v === current ? ' selected' : '') + '>' + esc(v) + '</option>';
    });
    return html;
  }

  function portsTable(ports, emptyText) {
    if (!Array.isArray(ports) || ports.length === 0) {
      return '<div class="field-hint">' + esc(emptyText || '无端口') + '</div>';
    }
    var rows = ports.map(function (p) {
      return '<tr>' +
        '<td class="mono">' + esc(p.name || '—') + '</td>' +
        '<td class="mono">' + esc(p.type || '—') + '</td>' +
        '<td>' + (p.required
          ? '<span class="req-yes">必填</span>'
          : '<span class="req-no">可选</span>') + '</td>' +
      '</tr>';
    }).join('');
    return '<table class="port-table"><thead><tr><th>端口</th><th>类型</th><th>必填</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function atomDrawerHtml(atom) {
    var effects = Array.isArray(atom.effects) && atom.effects.length
      ? atom.effects.map(function (e) { return '<span class="chip">' + esc(e) + '</span>'; }).join('')
      : '<span class="missing">未提供</span>';
    var required = Array.isArray(atom.required) && atom.required.length
      ? atom.required.map(function (r) { return '<span class="chip">' + esc(r) + '</span>'; }).join('')
      : '<span class="missing">无必填输入</span>';
    var aliases = Array.isArray(atom.aliases) && atom.aliases.length
      ? atom.aliases.map(function (a) { return '<span class="chip">' + esc(a) + '</span>'; }).join('')
      : '<span class="missing">未提供</span>';
    var tags = Array.isArray(atom.tags) && atom.tags.length
      ? atom.tags.map(function (t) { return '<span class="chip">' + esc(t) + '</span>'; }).join('')
      : '<span class="missing">未提供</span>';

    return '' +
      '<div class="field"><span class="field-label">描述</span><p>' + esc(atom.description || '未提供描述') + '</p></div>' +
      '<div class="field"><span class="field-label">元信息</span>' +
        '<div>' +
          '<span class="chip">分类 ' + esc(atom.category || '—') + '</span>' +
          '<span class="chip">领域 ' + esc(atom.domain || '—') + '</span>' +
          '<span class="chip">包 ' + esc(atom.pack || '—') + '</span>' +
          '<span class="chip">版本 ' + esc(atom.version || '—') + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="field"><span class="field-label">标签</span><div>' + tags + '</div></div>' +
      '<div class="field"><span class="field-label">输入端口</span>' + portsTable(atom.input_ports, '该原子没有声明输入端口') + '</div>' +
      '<div class="field"><span class="field-label">输出端口</span>' + portsTable(atom.output_ports, '该原子没有声明输出端口') + '</div>' +
      '<div class="field"><span class="field-label">副作用 effects</span><div>' + effects + '</div></div>' +
      '<div class="field"><span class="field-label">必填输入</span><div>' + required + '</div></div>' +
      '<div class="field"><span class="field-label">别名</span><div>' + aliases + '</div></div>' +
      '<details class="raw"><summary>inputs 契约（原始）</summary><pre class="json-block">' + esc(prettyJson(atom.inputs || {})) + '</pre></details>' +
      '<details class="raw"><summary>outputs 契约（原始）</summary><pre class="json-block">' + esc(prettyJson(atom.outputs || {})) + '</pre></details>';
  }

  function atomsTableHtml(list) {
    if (!list.length) {
      return '<div class="empty"><strong>当前过滤条件没有匹配的原子</strong>' +
        '<span>清空 domain / tag / category 后重试。</span>' +
        '<button type="button" class="btn btn-sm" data-act="atoms-reset">清除筛选</button></div>';
    }
    var rows = list.map(function (atom, idx) {
      var req = Array.isArray(atom.required) && atom.required.length ? atom.required.join(', ') : '—';
      var tags = Array.isArray(atom.tags) ? atom.tags.join(' ') : '';
      var effects = Array.isArray(atom.effects) ? atom.effects.join(' ') : '';
      return '<tr class="is-clickable" data-atom-idx="' + idx + '" tabindex="0">' +
        '<td class="mono">' + esc(atom.name || '—') + '</td>' +
        '<td>' + esc(atom.category || '—') + '</td>' +
        '<td>' + esc(atom.domain || '—') + '</td>' +
        '<td class="mono">' + esc(req) + '</td>' +
        '<td>' + esc(tags || '—') + '</td>' +
        '<td>' + esc(effects || '—') + '</td>' +
        '<td class="mono">' + esc(atom.version || '—') + '</td>' +
      '</tr>';
    }).join('');
    return '<div class="table-wrap"><table class="data">' +
      '<thead><tr><th>原子</th><th>分类</th><th>领域</th><th>必填输入</th><th>标签</th><th>副作用</th><th>版本</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>';
  }

  function renderAtoms(host, ctx) {
    host.innerHTML = '' +
      '<div class="page-head">' +
        '<div><h1>原子库</h1><div class="sub">原子是可编排的最小动作；点击行查看输入/输出端口契约与副作用。</div></div>' +
      '</div>' +
      '<div class="filter-bar" id="atomFilters">' +
        '<div class="field"><label for="filterDomain">domain</label><select class="select" id="filterDomain"></select></div>' +
        '<div class="field"><label for="filterTag">tag</label><select class="select" id="filterTag"></select></div>' +
        '<div class="field"><label for="filterCategory">category</label><select class="select" id="filterCategory"></select></div>' +
        '<div class="filter-count" id="atomCount">—</div>' +
      '</div>' +
      '<div id="atomBody"><div class="loading"><span class="spinner"></span>正在读取原子库…</div></div>';

    var body = $('#atomBody', host);
    var countEl = $('#atomCount', host);
    var filterState = APP.atomFilters;
    var currentList = [];

    var applyFilters = function (atoms) {
      currentList = Array.isArray(atoms) ? atoms : [];
      body.innerHTML = atomsTableHtml(currentList);
      if (countEl) countEl.textContent = '共 ' + currentList.length + ' 个原子';
    };

    var loadFiltered = function () {
      var qs = [];
      ['domain', 'tag', 'category'].forEach(function (k) {
        if (filterState[k]) qs.push(k + '=' + encodeURIComponent(filterState[k]));
      });
      var url = '/api/atoms' + (qs.length ? '?' + qs.join('&') : '');
      apiGet(url).then(function (list) {
        if (!ctx.alive()) return;
        applyFilters(Array.isArray(list) ? list : []);
      }).catch(function (err) {
        if (!ctx.alive()) return;
        body.innerHTML = '<div id="atomError"></div>';
        renderError($('#atomError', body), err, loadFiltered, '无法读取原子列表');
      });
    };

    var bindFilters = function (atoms) {
      var dSel = $('#filterDomain', host);
      var tSel = $('#filterTag', host);
      var cSel = $('#filterCategory', host);
      if (!dSel || !tSel || !cSel) return;
      dSel.innerHTML = optionList(uniq(atoms.map(function (a) { return a.domain; })), filterState.domain, '全部领域');
      tSel.innerHTML = optionList(collectTags(atoms), filterState.tag, '全部标签');
      cSel.innerHTML = optionList(uniq(atoms.map(function (a) { return a.category; })), filterState.category, '全部分类');
      var onChange = function () {
        filterState.domain = dSel.value;
        filterState.tag = tSel.value;
        filterState.category = cSel.value;
        loadFiltered();
      };
      dSel.addEventListener('change', onChange);
      tSel.addEventListener('change', onChange);
      cSel.addEventListener('change', onChange);
    };

    apiGet('/api/atoms').then(function (atoms) {
      if (!ctx.alive()) return;
      var list = Array.isArray(atoms) ? atoms : [];
      APP.atomsAll = list;
      bindFilters(list);
      applyFilters(list);
    }).catch(function (err) {
      if (!ctx.alive()) return;
      body.innerHTML = '<div id="atomError"></div>';
      renderError($('#atomError', body), err, function () { render(); }, '无法读取原子库');
    });

    host.addEventListener('click', function (e) {
      var reset = e.target.closest ? e.target.closest('[data-act="atoms-reset"]') : null;
      if (reset) {
        filterState.domain = '';
        filterState.tag = '';
        filterState.category = '';
        $$('.filter-bar select', host).forEach(function (s) { s.value = ''; });
        loadFiltered();
        return;
      }
      var row = e.target.closest ? e.target.closest('tr[data-atom-idx]') : null;
      if (!row) return;
      var idx = Number(row.getAttribute('data-atom-idx'));
      var source = currentList[idx];
      if (!source) return;
      openDrawer(esc(source.name || 'atom'), atomDrawerHtml(source), row);
    }, ctx.signal ? { signal: ctx.signal } : false);
    host.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var row = e.target.closest ? e.target.closest('tr[data-atom-idx]') : null;
      if (!row) return;
      e.preventDefault();
      row.click();
    }, ctx.signal ? { signal: ctx.signal } : false);
  }

  /* ================================================================== */
  /* 12. 领域包                                                          */
  /* ================================================================== */
  function packCardHtml(pack) {
    var caps = Array.isArray(pack.capabilities) && pack.capabilities.length
      ? pack.capabilities.join(' / ') : '无额外能力';
    var tags = Array.isArray(pack.tags) ? pack.tags.join(' ') : '';
    return '' +
      '<button type="button" class="pack-card" data-pack="' + esc(pack.name) + '">' +
        '<div class="pack-top">' +
          '<span class="pack-name">' + esc(pack.name || '未命名包') + '</span>' +
          '<span class="badge dim">v' + esc(pack.version || '—') + '</span>' +
          (pack.builtin ? '<span class="badge">内置</span>' : '<span class="badge warn">工作区</span>') +
          '<span class="badge">' + esc(pack.domain || '未标注领域') + '</span>' +
        '</div>' +
        '<div class="pack-desc">' + esc(pack.description || '未提供描述') + '</div>' +
        '<div class="pack-foot">' +
          '<span class="pack-count">' + esc(String(typeof pack.atom_count === 'number' ? pack.atom_count : '—')) + ' 个原子</span>' +
          '<span>' + esc(caps) + '</span>' +
          (tags ? '<span class="mono">' + esc(tags) + '</span>' : '') +
        '</div>' +
      '</button>';
  }

  function packDrawerHtml(pack) {
    var atoms = Array.isArray(pack.atoms) ? pack.atoms : [];
    var atomItems = atoms.map(function (a) {
      if (typeof a === 'string') {
        return '<div class="atom-item"><div class="atom-name">' + esc(a) + '</div></div>';
      }
      return '<div class="atom-item">' +
        '<div class="atom-name">' + esc(a.name || '—') + '</div>' +
        '<div class="atom-desc">' + esc(a.description || '未提供描述') + '</div>' +
      '</div>';
    }).join('');
    var caps = Array.isArray(pack.capabilities) && pack.capabilities.length
      ? pack.capabilities.map(function (c) { return '<span class="chip">' + esc(c) + '</span>'; }).join('')
      : '<span class="missing">未提供</span>';
    var requires = Array.isArray(pack.requires) && pack.requires.length
      ? pack.requires.map(function (r) { return '<span class="chip">' + esc(r) + '</span>'; }).join('')
      : '<span class="missing">无</span>';

    return '' +
      '<div class="field"><span class="field-label">描述</span><p>' + esc(pack.description || '未提供描述') + '</p></div>' +
      '<div class="field"><span class="field-label">元信息</span><div>' +
        '<span class="chip">version ' + esc(pack.version || '—') + '</span>' +
        '<span class="chip">domain ' + esc(pack.domain || '—') + '</span>' +
        '<span class="chip">' + (pack.builtin ? '内置包' : '工作区包') + '</span>' +
      '</div></div>' +
      '<div class="field"><span class="field-label">语义 semantics</span><p class="mono">' + esc(pack.semantics || '未提供') + '</p></div>' +
      '<div class="field"><span class="field-label">能力 capabilities</span><div>' + caps + '</div></div>' +
      '<div class="field"><span class="field-label">依赖 requires</span><div>' + requires + '</div></div>' +
      '<div class="field"><span class="field-label">原子清单（' + atoms.length + '）</span>' +
        (atomItems ? '<div class="atom-list">' + atomItems + '</div>' : '<div class="field-hint">该包没有原子</div>') +
      '</div>' +
      '<details class="raw"><summary>manifest（原始）</summary><pre class="json-block">' +
        esc(prettyJson(pack.manifest || {})) + '</pre></details>';
  }

  function renderPacks(host, ctx) {
    host.innerHTML = '' +
      '<div class="page-head">' +
        '<div><h1>领域包</h1><div class="sub">领域包把原子按业务语义组织起来；点击卡片查看原子清单与 manifest。</div></div>' +
      '</div>' +
      '<div id="packBody"><div class="loading"><span class="spinner"></span>正在读取领域包…</div></div>';

    var body = $('#packBody', host);

    var load = function () {
      if (!ctx.alive()) return;
      body.innerHTML = '<div class="loading"><span class="spinner"></span>正在读取领域包…</div>';
      apiGet('/api/packs').then(function (packs) {
        if (!ctx.alive()) return;
        if (!Array.isArray(packs) || packs.length === 0) {
          body.innerHTML = '<div class="empty"><strong>没有可用领域包</strong>' +
            '<span>工作区里没有发现包定义；内置包应随安装提供。</span></div>';
          return;
        }
        body.innerHTML = '<div class="pack-grid">' + packs.map(packCardHtml).join('') + '</div>';
      }).catch(function (err) {
        if (!ctx.alive()) return;
        body.innerHTML = '<div id="packError"></div>';
        renderError($('#packError', body), err, load, '无法读取领域包');
      });
    };
    load();

    var openPack = function (name, opener) {
      openDrawer(esc(name), '<div class="loading"><span class="spinner"></span>正在读取包详情…</div>', opener);
      apiGet('/api/packs/' + encodeURIComponent(name)).then(function (pack) {
        if (!pack || typeof pack !== 'object') throw new ApiError('响应格式不正确', 500, 'bad_response');
        setDrawerBody(packDrawerHtml(pack));
      }).catch(function (err) {
        setDrawerBody(errorHtml(err, '无法读取包 ' + name));
        var retry = $('#drawerRoot [data-retry]');
        if (retry) {
          retry.addEventListener('click', function () { openPack(name, opener); });
        }
      });
    };

    host.addEventListener('click', function (e) {
      var card = e.target.closest ? e.target.closest('[data-pack]') : null;
      if (!card) return;
      openPack(card.getAttribute('data-pack'), card);
    }, ctx.signal ? { signal: ctx.signal } : false);
    host.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var card = e.target.closest ? e.target.closest('[data-pack]') : null;
      if (!card) return;
      e.preventDefault();
      card.click();
    }, ctx.signal ? { signal: ctx.signal } : false);
  }

  /* ================================================================== */
  /* 13. 路由                                                            */
  /* ================================================================== */
  function parseRoute() {
    var h = (location.hash || '').replace(/^#/, '');
    if (!h || h === '/') return { name: 'overview' };
    var m = /^\/runs\/([^\/?#]+)\/?$/.exec(h);
    if (m) return { name: 'run-detail', id: decodeURIComponent(m[1]) };
    if (h === '/overview' || h === '/run' || h === '/history' || h === '/atoms' || h === '/packs') {
      return { name: h.slice(1) };
    }
    return { name: 'notfound', path: h };
  }

  function setNav(activeName) {
    var navKey = activeName === 'run-detail' ? 'history' : activeName;
    $$('[data-nav]').forEach(function (a) {
      if (a.getAttribute('data-nav') === navKey) {
        a.setAttribute('aria-current', 'page');
      } else {
        a.removeAttribute('aria-current');
      }
    });
  }

  function notFoundHtml(path) {
    return '' +
      '<div class="page-head"><div><h1>页面不存在</h1>' +
        '<div class="sub">没有匹配的路由：<span class="mono">' + esc(path) + '</span></div></div></div>' +
      '<div class="empty"><strong>可用视图</strong>' +
        '<span><a href="#/overview">#/overview</a> · <a href="#/run">#/run</a> · <a href="#/history">#/history</a> · ' +
        '<a href="#/runs/&lt;run_id&gt;">#/runs/&lt;run_id&gt;</a> · <a href="#/atoms">#/atoms</a> · <a href="#/packs">#/packs</a></span>' +
      '</div>';
  }

  function render() {
    var seq = ++APP.seq;
    var route = parseRoute();
    var host = $('#view');
    if (!host) return;

    if (APP.controller) {
      try { APP.controller.abort(); } catch (e) { /* noop */ }
    }
    APP.controller = (typeof AbortController === 'function') ? new AbortController() : null;

    var routeKey = route.name + ':' + (route.id || '');
    if (APP.lastRoute !== routeKey && APP.lastRoute !== null) {
      try { window.scrollTo(0, 0); } catch (e) { /* noop */ }
    }
    if (APP.bannerRoute && APP.bannerRoute !== routeKey) hideBanner();
    APP.lastRoute = routeKey;

    closeDrawer();
    setNav(route.name);

    var title = ROUTE_TITLE[route.name] || 'PanguVerse';
    var crumb = $('#crumb');
    if (crumb) crumb.textContent = title;
    document.title = title + ' · PanguVerse';

    var ctx = {
      alive: function () { return seq === APP.seq; },
      signal: APP.controller ? APP.controller.signal : undefined
    };

    host.innerHTML = '<div class="loading"><span class="spinner"></span>加载中…</div>';

    var task;
    if (route.name === 'overview') task = renderOverview(host, ctx);
    else if (route.name === 'run') task = renderRun(host, ctx);
    else if (route.name === 'history') task = renderHistory(host, ctx);
    else if (route.name === 'run-detail') task = renderDetail(host, ctx, route.id);
    else if (route.name === 'atoms') task = renderAtoms(host, ctx);
    else if (route.name === 'packs') task = renderPacks(host, ctx);
    else { host.innerHTML = notFoundHtml(route.path || ''); return; }

    if (task && typeof task.catch === 'function') {
      task.catch(function (err) {
        if (!ctx.alive()) return;
        host.innerHTML = '<div id="routeError"></div>';
        renderError($('#routeError', host), err, function () { render(); }, '页面渲染失败');
      });
    }
  }

  /* ================================================================== */
  /* 14. 全局事件与启动                                                  */
  /* ================================================================== */
  function startDemo() {
    startRun({ mode: 'demo' }).then(function () {
      showBanner('info', '<strong>已提交内置演示任务。</strong> 进度显示在右下角任务面板，完成后点击「查看运行详情」。', 6000);
    }).catch(function (err) {
      if (err && err.code === 'busy') return;
      showBanner('error', '<strong>无法启动演示：</strong>' + esc(err.message), 0);
    });
  }

  function startRegression() {
    startRun({ mode: 'regression' }).then(function () {
      showBanner('info', '<strong>已提交回归任务。</strong> 复用回归库执行，不重新规划。', 6000);
    }).catch(function (err) {
      if (err && err.code === 'busy') return;
      showBanner('error', '<strong>无法启动回归：</strong>' + esc(err.message), 0);
    });
  }

  function bindGlobalEvents() {
    document.addEventListener('click', function (e) {
      var target = e.target;
      if (!target || !target.closest) return;

      var actEl = target.closest('[data-act]');
      if (actEl) {
        var act = actEl.getAttribute('data-act');
        if (act === 'run-demo') { e.preventDefault(); startDemo(); }
        else if (act === 'run-regression') { e.preventDefault(); startRegression(); }
        else if (act === 'job-dismiss') { APP.job.dismiss = true; renderJobPanels(); }
        else if (act === 'job-detail') { location.hash = '#/runs/' + actEl.getAttribute('data-run'); }
      }

      if (target.closest('[data-drawer-close]')) { closeDrawer(); return; }
      if (target.closest('[data-banner-close]')) { hideBanner(); return; }

      var row = target.closest('tr[data-href]');
      if (row && !target.closest('a') && !target.closest('button')) {
        location.hash = row.getAttribute('data-href');
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        if (!$('#drawerRoot').hidden) { closeDrawer(); return; }
        if (!$('#globalBanner').hidden) hideBanner();
      }
    });

    window.addEventListener('hashchange', render);

    window.addEventListener('unhandledrejection', function (ev) {
      var reason = ev.reason;
      var msg = reason && reason.message ? reason.message : String(reason);
      toast('未处理的异步错误：' + msg, 'error');
    });
  }

  function boot() {
    bindGlobalEvents();
    if (!location.hash || location.hash === '#' || location.hash === '#/') {
      try { history.replaceState(null, '', '#/overview'); } catch (e) { location.hash = '#/overview'; }
    }
    restoreJob();
    render();
    if (parseRoute().name !== 'overview') refreshStatus();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

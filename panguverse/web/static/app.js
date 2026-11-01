/* PanguVerse Web 控制台 — 项目 / 套件 / 运行
 *
 * 约束：
 * - 无构建步骤、无 npm、无 CDN、无外部字体与图片，离线可用；
 * - 只消费既有 /api 契约，不伪造数据；
 * - 接口与静态资源一律使用相对路径（支持整站挂在子路径）；
 * - 任何 401 都回登录页，并保留原 hash 意图；
 * - markdown 先转义再按白名单拼接，禁止把模型输出直接塞进 innerHTML；
 * - 204/空体按成功处理（删除类接口）。
 */
(function () {
  'use strict';

  var API = 'api/';
  var DEFAULT_HASH = '#/projects';
  var PID_RE = /^p-[0-9a-f]{6,12}$/;
  var SID_RE = /^s-[0-9a-f]{6,12}$/;
  var RUN_ID_RE = /^[0-9a-f]{6,32}$/;
  var PAGE_SIZE = 20;

  var STATUS = {
    passed: { label: '通过', glyph: '✓', cls: 'ok' },
    ok: { label: '正常', glyph: '✓', cls: 'ok' },
    failed: { label: '失败', glyph: '✕', cls: 'bad' },
    error: { label: '异常', glyph: '!', cls: 'bad' },
    errored: { label: '异常', glyph: '!', cls: 'bad' },
    skipped: { label: '跳过', glyph: '–', cls: 'dim' },
    running: { label: '运行中', glyph: '⏵', cls: 'run' },
    queued: { label: '排队中', glyph: '·', cls: 'dim' },
    pending: { label: '待开始', glyph: '·', cls: 'dim' },
    done: { label: '已完成', glyph: '✓', cls: 'ok' },
    partial: { label: '部分失败', glyph: '!', cls: 'warn' },
    none: { label: '未运行', glyph: '·', cls: 'dim' }
  };

  var JOB_STATUS = {
    queued: { label: '排队中', cls: 'dim' },
    running: { label: '运行中', cls: 'run' },
    done: { label: '已完成', cls: 'ok' },
    failed: { label: '失败', cls: 'bad' }
  };

  var APP = {
    seq: 0,
    controller: null,
    lastRoute: null,
    bannerRoute: null,
    auth: { authenticated: false, user: '', legacy: false, next: null, redirecting: false, loggingOut: false },
    projects: null,
    projectsError: null,
    projectsLoading: false,
    projectCache: {},
    scenarios: null,
    atoms: null,
    tasks: null,
    atomFilters: { domain: '', tag: '', category: '' },
    job: { id: null, timer: null },
    status: null,
    batchTimers: []
  };

  /* ================================================================== */
  /* 基础工具                                                            */
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

  function enc(value) { return encodeURIComponent(String(value)); }

  function safeDecode(value) {
    try { return decodeURIComponent(value); } catch (e) { return value; }
  }

  function hasVal(value) {
    if (value === undefined || value === null) return false;
    if (typeof value === 'string' && value.trim() === '') return false;
    return true;
  }

  function num(value) {
    return typeof value === 'number' && isFinite(value) ? value : 0;
  }

  function fmtNum(value) {
    if (typeof value !== 'number' || !isFinite(value)) return '—';
    try { return value.toLocaleString('zh-CN'); } catch (e) { return String(value); }
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function toMs(ts) {
    if (typeof ts !== 'number' || !isFinite(ts) || ts <= 0) return 0;
    return ts > 100000000000 ? ts : ts * 1000;
  }

  function fmtTs(ts) {
    var ms = toMs(ts);
    if (!ms) return '—';
    var d = new Date(ms);
    if (isNaN(d.getTime())) return '—';
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function fmtRel(ts) {
    var ms = toMs(ts);
    if (!ms) return '—';
    var diff = Date.now() - ms;
    if (diff < 0) return fmtTs(ts);
    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return Math.floor(diff / 60000) + ' 分钟前';
    if (diff < 86400000) return Math.floor(diff / 3600000) + ' 小时前';
    if (diff < 604800000) return Math.floor(diff / 86400000) + ' 天前';
    return fmtTs(ts);
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

  function fmtDur(ms) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms < 0) return '—';
    return fmtMs(ms);
  }

  function stringifyMaybe(value) {
    if (value === undefined || value === null) return '';
    if (typeof value === 'object') return JSON.stringify(value, null, 2);
    return String(value);
  }

  function prettyJson(value) {
    try { return JSON.stringify(value, null, 2); } catch (e) { return String(value); }
  }

  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '0';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        if (ok) resolve(); else reject(new Error('浏览器拒绝了复制操作'));
      } catch (e) { reject(e); }
    });
  }

  /* ================================================================== */
  /* 状态与格式化                                                        */
  /* ================================================================== */
  function statusMeta(status) {
    return STATUS[status] || { label: status ? String(status) : '未知', glyph: '?', cls: 'dim' };
  }

  function badge(status) {
    var meta = statusMeta(status);
    return '<span class="badge ' + meta.cls + '"><span class="glyph" aria-hidden="true">' +
      esc(meta.glyph) + '</span>' + esc(meta.label) + '</span>';
  }

  function jobBadge(status) {
    var meta = JOB_STATUS[status] || { label: status ? String(status) : '未知', cls: 'dim' };
    return '<span class="badge ' + meta.cls + '">' + esc(meta.label) + '</span>';
  }

  function dot(status) {
    var meta = statusMeta(status);
    return '<span class="dot dot-' + meta.cls + '" title="' + esc(meta.label) + '"></span>';
  }

  function runOutcome(run) {
    run = run || {};
    var summary = run.summary || {};
    var hasSummary = summary && typeof summary === 'object' && Object.keys(summary).length > 0;
    if (hasSummary) {
      var total = num(summary.total);
      var passed = num(summary.passed);
      var failed = num(summary.failed);
      var errored = num(summary.errored);
      if (failed > 0) return { status: 'failed', passed: passed, total: total };
      if (errored > 0) return { status: 'errored', passed: passed, total: total };
      if (total > 0) return { status: 'passed', passed: passed, total: total };
    }
    var conclusion = run.conclusion || {};
    if (conclusion.success === false) return { status: 'failed', passed: 0, total: 0 };
    if (conclusion.success === true) return { status: 'passed', passed: 0, total: 0 };
    if (run.status) return { status: run.status, passed: num(run.passed), total: num(run.total) };
    return { status: 'none', passed: 0, total: 0 };
  }

  function rateInfo(pr) {
    var passed = num(pr && pr.passed);
    var total = num(pr && pr.total);
    var rate = total > 0 ? Math.round((passed / total) * 1000) / 10 : null;
    return { passed: passed, total: total, rate: rate };
  }

  function rateTone(rate) {
    if (rate === null) return 'none';
    if (rate >= 100) return '';
    if (rate >= 80) return 'mid';
    return 'low';
  }

  function rateHtml(pr, opts) {
    opts = opts || {};
    var info = rateInfo(pr);
    var tone = rateTone(info.rate);
    var label = info.rate === null ? '—' : (Math.round(info.rate * 10) / 10) + '%';
    var sub = info.total > 0 ? (info.passed + '/' + info.total + ' 用例') : '暂无数据';
    return '<div class="rate">' +
      '<div class="rate-top"><span class="rate-num ' + tone + '">' + esc(label) + '</span>' +
      (opts.showSub === false ? '' : '<span class="rate-sub">' + esc(sub) + '</span>') + '</div>' +
      '<div class="rate-bar ' + tone + '"><span style="width:' + (info.rate === null ? 0 : Math.max(0, Math.min(100, info.rate))) + '%"></span></div>' +
      (opts.showSub === false ? '<span class="rate-sub">' + esc(sub) + '</span>' : '') +
    '</div>';
  }

  function runCounts(run) {
    var summary = run && run.summary ? run.summary : {};
    return {
      passed: num(summary.passed),
      total: num(summary.total)
    };
  }

  function runWhen(run) {
    return (run && (run.at || run.finished_at || run.started_at)) || 0;
  }

  /* ================================================================== */
  /* 网络请求（统一错误模型）                                            */
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
      // 204/205/304 与 2xx 空体没有 JSON：DELETE 204 必须按成功处理。
      var noContentStatus = res.status === 204 || res.status === 205 || res.status === 304;
      var emptyBody = res.headers.get('content-length') === '0';
      var read;
      if (noContentStatus || (res.ok && emptyBody)) {
        read = Promise.resolve(null);
      } else {
        read = isJson
          ? res.json().catch(function () { return null; })
          : res.text().catch(function () { return ''; });
      }

      return read.then(function (payload) {
        if (noContentStatus) return null;
        if (!res.ok) {
          var msg = '';
          var code = '';
          if (payload && typeof payload === 'object' && payload.error) {
            msg = payload.error.message || '';
            code = payload.error.code || '';
          } else if (typeof payload === 'string' && payload.trim()) {
            msg = payload.trim().slice(0, 300);
          }
          // 任何业务接口的 401 都统一回登录视图（auth 自身接口除外）
          if (res.status === 401 && path.indexOf('api/auth/') !== 0) {
            handleUnauthorized();
          }
          throw new ApiError(msg || ('请求失败（HTTP ' + res.status + '）'), res.status, code);
        }
        if (emptyBody) return null;
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
  function apiPatch(path, body) { return request(path, { method: 'PATCH', body: body }); }
  function apiDelete(path) { return request(path, { method: 'DELETE' }); }

  function errorHint(err) {
    if (!err) return '';
    if (err.code === 'network' || err.status === 0) {
      return '提示：后端未启动或端口不通。请确认 panguverse serve 正在运行后重试。';
    }
    if (err.status === 401) return '提示：登录已过期，请重新登录。';
    if (err.status === 404) return '提示：资源不存在。检查项目/套件/运行 ID，或确认后端版本包含该接口。';
    if (err.status === 400) return '提示：请求参数被拒绝，请检查填写内容（名称、场景路径、环境变量）。';
    if (err.status === 409) return '提示：已有同名任务在运行，等它结束后再试。';
    if (err.status >= 500) return '提示：服务端内部错误，详细原因见服务端日志。';
    return '';
  }

  function errorHtml(err, title) {
    var msg = err && err.message ? err.message : '未知错误';
    var code = err && err.code ? err.code : (err && err.status ? 'http_' + err.status : 'error');
    var hint = errorHint(err);
    return '<div class="error-block" role="alert">' +
      '<div class="error-title"><span aria-hidden="true">⚠</span>' + esc(title || '请求失败') +
        '<span class="error-code">' + esc(code) + '</span>' +
      '</div>' +
      '<div class="error-msg">' + esc(msg) + '</div>' +
      (hint ? '<div class="error-hint">' + esc(hint) + '</div>' : '') +
      '<div class="error-actions"><button type="button" class="btn btn-sm" data-retry>重试</button></div>' +
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

  function loadingHtml(text) {
    return '<div class="loading"><span class="spinner" aria-hidden="true"></span>' + esc(text || '加载中…') + '</div>';
  }

  function emptyHtml(title, body, actions) {
    return '<div class="empty">' +
      (title ? '<strong>' + esc(title) + '</strong>' : '') +
      (body ? '<span>' + body + '</span>' : '') +
      (actions ? '<div class="actions">' + actions + '</div>' : '') +
    '</div>';
  }

  function pageHead(title, sub, actions) {
    return '<div class="page-head"><div class="page-head-main">' +
      '<h1>' + esc(title) + '</h1>' +
      (sub ? '<div class="sub">' + sub + '</div>' : '') +
      '</div>' +
      (actions ? '<div class="actions">' + actions + '</div>' : '') +
    '</div>';
  }

  /* ================================================================== */
  /* 提示 / 横幅 / Toast                                                 */
  /* ================================================================== */
  function showBanner(kind, html, ttl) {
    if (!APP.auth || !APP.auth.authenticated) return;
    var banner = $('#globalBanner');
    if (!banner) return;
    APP.bannerRoute = APP.lastRoute || null;
    banner.dataset.kind = kind || 'info';
    banner.innerHTML = '<div class="banner-body">' + html + '</div>' +
      '<button type="button" class="banner-close" data-banner-close aria-label="关闭提示">✕</button>';
    banner.hidden = false;
    clearTimeout(showBanner._t);
    if (ttl) showBanner._t = setTimeout(hideBanner, ttl);
  }

  function hideBanner() {
    var banner = $('#globalBanner');
    if (banner) { banner.hidden = true; banner.innerHTML = ''; }
  }

  function toast(message, kind) {
    var host = $('#toasts');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'toast ' + (kind || 'info');
    el.textContent = String(message || '');
    host.appendChild(el);
    setTimeout(function () {
      el.style.transition = 'opacity .2s ease';
      el.style.opacity = '0';
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }, kind === 'error' ? 6000 : 3600);
  }

  /* ================================================================== */
  /* 模态框                                                              */
  /* ================================================================== */
  var MODAL = { open: false, busy: false, resolve: null, opts: null };

  function closeModal(result) {
    var root = $('#modalRoot');
    if (!root) return;
    root.hidden = true;
    root.innerHTML = '';
    document.body.removeAttribute('data-modal');
    MODAL.open = false;
    MODAL.busy = false;
    var resolve = MODAL.resolve;
    var opts = MODAL.opts;
    MODAL.resolve = null;
    MODAL.opts = null;
    if (opts && typeof opts.onClose === 'function') opts.onClose(result);
    if (resolve) resolve(result);
  }

  function openModal(opts) {
    opts = opts || {};
    var root = $('#modalRoot');
    if (!root) return null;
    if (MODAL.open) closeModal(null);

    var okLabel = opts.okLabel === undefined ? '保存' : opts.okLabel;
    var cancelLabel = opts.cancelLabel === undefined ? '取消' : opts.cancelLabel;
    var title = opts.title || '操作';

    root.innerHTML =
      '<div class="modal-backdrop" data-modal-close></div>' +
      '<div class="modal-card' + (opts.wide ? ' modal-wide' : '') + '" role="dialog" aria-modal="true" aria-labelledby="modalTitle">' +
        '<div class="modal-head">' +
          '<div><h2 id="modalTitle">' + esc(title) + '</h2>' +
          (opts.subtitle ? '<div class="modal-sub">' + esc(opts.subtitle) + '</div>' : '') + '</div>' +
          '<button type="button" class="btn btn-icon modal-close" data-modal-close aria-label="关闭">✕</button>' +
        '</div>' +
        '<form class="modal-body" id="modalForm" novalidate>' +
          (opts.bodyHtml || '') +
          '<div class="modal-error" id="modalError" role="alert"></div>' +
        '</form>' +
        '<div class="modal-foot">' +
          '<button type="button" class="btn" data-modal-close>' + esc(cancelLabel) + '</button>' +
          (okLabel === null ? '' : '<button type="submit" class="btn btn-primary" form="modalForm" id="modalOk">' + esc(okLabel) + '</button>') +
        '</div>' +
      '</div>';

    root.hidden = false;
    document.body.setAttribute('data-modal', '1');
    MODAL.open = true;
    MODAL.busy = false;
    MODAL.opts = opts;

    var form = $('#modalForm', root);
    if (opts.onMount) {
      try { opts.onMount(root, form); } catch (e) { /* 挂载失败不应炸整页 */ }
    }
    if (form) {
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        if (MODAL.busy || typeof opts.onSubmit !== 'function') return;
        var errEl = $('#modalError', root);
        if (errEl) { errEl.textContent = ''; errEl.setAttribute('data-show', '0'); }
        MODAL.busy = true;
        var okBtn = $('#modalOk', root);
        if (okBtn) { okBtn.disabled = true; okBtn.innerHTML = '<span class="spinner" aria-hidden="true"></span>处理中…'; }
        var result;
        try { result = opts.onSubmit(root, form); } catch (err) { result = Promise.reject(err); }
        Promise.resolve(result).then(function (value) {
          MODAL.busy = false;
          if (value === false) { restoreModalOk(root, okBtn, okLabel); return; }
          if (typeof opts.closeOnSuccess === 'function' ? opts.closeOnSuccess(value) : true) {
            closeModal(value === undefined ? true : value);
          } else {
            restoreModalOk(root, okBtn, okLabel);
          }
        }).catch(function (err) {
          MODAL.busy = false;
          restoreModalOk(root, okBtn, okLabel);
          if (errEl) {
            errEl.textContent = (err && err.message) ? err.message : '操作失败';
            errEl.setAttribute('data-show', '1');
          }
        });
      });
    }

    setTimeout(function () {
      var focusable = $$('input:not([type="hidden"]):not([disabled]), select, textarea, button', root)
        .filter(function (el) { return el.offsetParent !== null && !el.hasAttribute('data-modal-close'); });
      if (focusable.length) {
        try { focusable[0].focus(); } catch (e) { /* noop */ }
      }
    }, 20);

    return root;
  }

  function restoreModalOk(root, okBtn, label) {
    if (!okBtn) return;
    okBtn.disabled = false;
    okBtn.textContent = label;
  }

  function confirmDialog(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      MODAL.resolve = resolve;
      openModal({
        title: opts.title || '请确认',
        subtitle: opts.subtitle || '',
        bodyHtml: '<div class="muted">' + (opts.bodyHtml || '') + '</div>',
        okLabel: opts.okLabel || '确认',
        cancelLabel: '取消',
        onClose: function (result) { resolve(result === true); },
        onSubmit: function () { resolve(true); closeModal(true); return true; }
      });
      // 危险动作：把确认按钮染红
      if (opts.danger) {
        var ok = $('#modalOk');
        if (ok) { ok.classList.remove('btn-primary'); ok.classList.add('btn-danger'); }
      }
    });
  }

  function showModalError(root, message) {
    var el = $('#modalError', root || $('#modalRoot'));
    if (!el) return;
    el.textContent = message || '';
    el.setAttribute('data-show', message ? '1' : '0');
  }

  /* ================================================================== */
  /* 环境变量编辑器                                                      */
  /* ================================================================== */
  function envRowHtml(entry) {
    entry = entry || {};
    var existing = !!entry.existing;
    var raw = entry.value;
    var value = (raw === undefined || raw === null) ? '' : String(raw);
    var masked = value === '***';
    var shown = masked ? '' : value;
    var placeholder = entry.placeholder || (masked ? '***（沿用）' : (existing ? '留空沿用' : '值'));
    return '<div class="env-row" data-env-row data-existing="' + (existing ? '1' : '0') + '">' +
      '<input class="input env-key mono" data-env-key value="' + esc(entry.key || '') + '" placeholder="变量名" autocomplete="off" spellcheck="false"' +
        (existing ? ' readonly' : '') + '>' +
      '<input class="input env-val mono" data-env-value value="' + esc(shown) + '" placeholder="' + esc(placeholder) + '" autocomplete="off" spellcheck="false">' +
      '<button type="button" class="btn btn-icon" data-env-del title="删除变量" aria-label="删除变量">✕</button>' +
    '</div>';
  }

  function mountEnvEditor(hostEl, entries, opts) {
    opts = opts || {};
    if (!hostEl) return { collect: function () { return {}; } };
    var rows = hostEl.querySelector('[data-env-rows]');
    if (!rows) return { collect: function () { return {}; } };
    rows.innerHTML = (entries || []).map(envRowHtml).join('') ||
      '<div class="env-empty" data-env-empty>暂无环境变量</div>';

    hostEl.addEventListener('click', function (e) {
      var del = e.target.closest ? e.target.closest('[data-env-del]') : null;
      if (del) {
        var row = del.closest('[data-env-row]');
        if (!row) return;
        if (row.getAttribute('data-existing') === '1') {
          row.hidden = true;
          row.setAttribute('data-deleted', '1');
        } else {
          row.parentNode.removeChild(row);
        }
        refreshEnvEmpty(rows);
        return;
      }
      var add = e.target.closest ? e.target.closest('[data-env-add]') : null;
      if (add) {
        var empty = rows.querySelector('[data-env-empty]');
        if (empty) empty.parentNode.removeChild(empty);
        var wrap = document.createElement('div');
        wrap.innerHTML = envRowHtml({ key: '', value: '', existing: false, placeholder: opts.placeholder || '值' });
        var newRow = wrap.firstChild;
        rows.appendChild(newRow);
        var keyInput = newRow.querySelector('[data-env-key]');
        if (keyInput) keyInput.focus();
      }
    });

    function refreshEnvEmpty() {
      var any = rows.querySelector('[data-env-row]:not([hidden])');
      var empty = rows.querySelector('[data-env-empty]');
      if (!any && !empty) {
        var div = document.createElement('div');
        div.className = 'env-empty';
        div.setAttribute('data-env-empty', '1');
        div.textContent = '暂无环境变量';
        rows.appendChild(div);
      } else if (any && empty) {
        empty.parentNode.removeChild(empty);
      }
    }

    function collect(collectOpts) {
      collectOpts = collectOpts || {};
      var out = {};
      var dup = [];
      $$('[data-env-row]', rows).forEach(function (row) {
        var keyEl = row.querySelector('[data-env-key]');
        var valEl = row.querySelector('[data-env-value]');
        if (!keyEl || !valEl) return;
        var key = keyEl.value.trim();
        if (!key) return;
        if (row.getAttribute('data-deleted') === '1') {
          out[key] = null;
          return;
        }
        if (Object.prototype.hasOwnProperty.call(out, key)) dup.push(key);
        var val = valEl.value;
        if (val === '***') return;
        if (collectOpts.existing === false) {
          if (val !== '') out[key] = val;
          return;
        }
        // 已保存值留空 = 沿用；有值 = 覆盖
        if (val !== '') out[key] = val;
      });
      return { env: out, duplicates: dup };
    }

    return {
      collect: function (collectOpts) { return collect(collectOpts).env; },
      duplicates: function () { var d = []; $$('[data-env-row]:not([hidden])', rows).forEach(function (row) {
        var el = row.querySelector('[data-env-key]');
        if (el && el.value.trim()) d.push(el.value.trim());
      }); return d.filter(function (v, i, arr) { return arr.indexOf(v) !== i; }); },
      addRow: function (entry) {
        var empty = rows.querySelector('[data-env-empty]');
        if (empty) empty.parentNode.removeChild(empty);
        var wrap = document.createElement('div');
        wrap.innerHTML = envRowHtml(entry || {});
        rows.appendChild(wrap.firstChild);
      }
    };
  }

  function envEditorBlock(opts) {
    opts = opts || {};
    return '<div class="env-editor" data-env-editor>' +
      '<div class="env-rows" data-env-rows>' +
        ((opts.entries || []).map(envRowHtml).join('') || '<div class="env-empty" data-env-empty>暂无环境变量</div>') +
      '</div>' +
      '<button type="button" class="btn btn-sm env-add" data-env-add>＋ 添加变量</button>' +
      (opts.note ? '<div class="env-note">' + esc(opts.note) + '</div>' : '') +
    '</div>';
  }

  function envEntriesFromObject(env, existing, placeholder) {
    var out = [];
    if (!env || typeof env !== 'object') return out;
    Object.keys(env).sort().forEach(function (key) {
      out.push({ key: key, value: env[key], existing: existing !== false, placeholder: placeholder });
    });
    return out;
  }

  /* ================================================================== */
  /* 通用表格                                                            */
  /* ================================================================== */
  function dataTable(cols, rows, opts) {
    opts = opts || {};
    if (!rows.length) return opts.emptyHtml || '';
    var head = cols.map(function (c) {
      return '<th' + (c.cls ? ' class="' + c.cls + '"' : '') + '>' + esc(c.label) + '</th>';
    }).join('');
    var body = rows.map(function (row, idx) {
      var trAttrs = '';
      if (opts.rowHref) {
        trAttrs = ' class="is-clickable" data-href="' + esc(opts.rowHref(row, idx)) + '" tabindex="0"';
      }
      var tds = cols.map(function (c) {
        var html = c.render ? c.render(row, idx) : esc(row[c.key]);
        return '<td data-label="' + esc(c.label) + '"' + (c.cls ? ' class="' + c.cls + '"' : '') + '>' + html + '</td>';
      }).join('');
      return '<tr' + trAttrs + '>' + tds + '</tr>';
    }).join('');
    return '<div class="table-wrap"><table class="data-table"><thead><tr>' + head +
      '</tr></thead><tbody>' + body + '</tbody></table></div>';
  }

  /* ================================================================== */
  /* 路由                                                                */
  /* ================================================================== */
  function parseQuery(qs) {
    var out = {};
    if (!qs) return out;
    qs.split('&').forEach(function (pair) {
      if (!pair) return;
      var i = pair.indexOf('=');
      var k = i < 0 ? pair : pair.slice(0, i);
      var v = i < 0 ? '' : pair.slice(i + 1);
      out[safeDecode(k)] = safeDecode(v.replace(/\+/g, ' '));
    });
    return out;
  }

  function parseRoute() {
    var raw = (location.hash || '').replace(/^#/, '');
    var qIndex = raw.indexOf('?');
    var path = qIndex >= 0 ? raw.slice(0, qIndex) : raw;
    var query = parseQuery(qIndex >= 0 ? raw.slice(qIndex + 1) : '');
    if (!path || path === '/') return { name: 'projects' };
    var segs = path.split('/').filter(Boolean).map(safeDecode);

    // 旧书签兼容：旧 IA → 新 IA
    if (segs.length === 1) {
      var legacy = { overview: 'projects', history: 'projects', tasks: 'platform-tasks', atoms: 'platform-atoms', packs: 'platform-packs', run: 'projects' };
      if (legacy[segs[0]]) return { name: 'redirect', to: legacy[segs[0]] };
    }
    if (segs[0] === 'projects' && segs.length === 1) return { name: 'projects' };
    if (segs[0] === 'platform' && segs.length === 2 && ['atoms', 'packs', 'tasks'].indexOf(segs[1]) >= 0) {
      return { name: 'platform-' + segs[1], platform: segs[1] };
    }
    if (segs[0] === 'runs' && segs.length === 2) {
      return { name: 'run-detail', runId: segs[1], legacyDetail: true };
    }
    if (segs[0] === 'p' && segs.length >= 2) {
      var pid = segs[1];
      if (!PID_RE.test(pid)) return { name: 'notfound', path: path };
      if (segs.length === 2) return { name: 'overview', pid: pid, query: query };
      if (segs.length === 3 && ['overview', 'run', 'history', 'settings'].indexOf(segs[2]) >= 0) {
        return { name: segs[2], pid: pid, query: query };
      }
      if (segs.length === 4 && segs[2] === 'runs') {
        return { name: 'run-detail', pid: pid, runId: segs[3], query: query };
      }
    }
    return { name: 'notfound', path: path };
  }

  function routeKey(route) {
    return route.name + ':' + (route.pid || '') + ':' + (route.runId || '') + ':' + (route.platform || '');
  }

  function go(hash) {
    if (location.hash === hash) { render(); return; }
    location.hash = hash;
  }

  function notFoundHtml(path) {
    return pageHead('页面不存在', '没有匹配的路由：<span class="mono">' + esc(path || '') + '</span>') +
      emptyHtml('可用入口', '项目列表 · 平台原子库 / 领域包 / 任务模板',
        '<a class="btn btn-primary" href="#/projects">回到项目列表</a><a class="btn" href="#/platform/atoms">平台</a>');
  }

  function platformTitle(key) {
    return { atoms: '原子库', packs: '领域包', tasks: '任务模板' }[key] || '平台';
  }

  /* ================================================================== */
  /* 外壳：侧栏 / 顶栏                                                   */
  /* ================================================================== */
  function loadProjects(force) {
    if (APP.projectsPromise && !force) return APP.projectsPromise;
    APP.projectsLoading = true;
    renderSidebar();
    var promise = apiGet(API + 'projects').then(function (payload) {
      var list = payload && Array.isArray(payload.projects) ? payload.projects : [];
      APP.projects = list;
      APP.projectsError = null;
      APP.projectsLoading = false;
      list.forEach(function (p) {
        if (p && p.project_id) {
          var cached = APP.projectCache[p.project_id];
          if (!cached) APP.projectCache[p.project_id] = { project_id: p.project_id, name: p.name, base_url: p.base_url, suites: null };
        }
      });
      renderSidebar();
      return list;
    }).catch(function (err) {
      APP.projectsLoading = false;
      APP.projectsError = err;
      renderSidebar();
      throw err;
    });
    APP.projectsPromise = promise;
    return promise;
  }

  function getProject(pid, force) {
    if (!force && APP.projectCache[pid] && APP.projectCache[pid].suites) {
      return Promise.resolve(APP.projectCache[pid]);
    }
    return apiGet(API + 'projects/' + enc(pid)).then(function (project) {
      if (project && project.project_id) {
        APP.projectCache[project.project_id] = project;
        var list = APP.projects || [];
        var found = false;
        list.forEach(function (p) { if (p.project_id === project.project_id) found = true; });
        if (!found) { list.push({ project_id: project.project_id, name: project.name }); APP.projects = list; }
      }
      return project;
    });
  }

  function invalidateProject(pid) {
    delete APP.projectCache[pid];
    APP.projectsPromise = null;
  }

  function renderSidebar() {
    var host = $('#projectList');
    if (!host) return;
    var route = parseRoute();
    var activePid = route.pid || '';
    var list = APP.projects;

    if (list === null && APP.projectsLoading) {
      host.innerHTML = '<div class="side-empty"><span class="spinner" aria-hidden="true"></span><span>正在读取项目…</span></div>';
    } else if (list === null && APP.projectsError) {
      host.innerHTML = '<div class="side-empty"><span>项目列表读取失败</span>' +
        '<button type="button" class="link-btn" data-act="reload-projects">重试</button></div>';
    } else if (!list || !list.length) {
      host.innerHTML = '';
    } else {
      host.innerHTML = list.map(function (p) {
        var last = p.last_run || null;
        var st = last ? (last.status || runOutcome(last).status) : 'none';
        var meta = last
          ? (statusMeta(st).label + (last.at ? ' · ' + fmtRel(last.at) : ''))
          : (p.suite_count ? (p.suite_count + ' 个套件') : '还没有运行');
        return '<a class="project-item" href="#/p/' + enc(p.project_id) + '/overview"' +
          (p.project_id === activePid ? ' aria-current="page"' : '') + ' title="' + esc(p.name || p.project_id) + '">' +
          dot(st) +
          '<span class="pi-text"><span class="pi-name">' + esc(p.name || p.project_id) + '</span>' +
          '<span class="pi-meta">' + esc(meta) + '</span></span>' +
        '</a>';
      }).join('');
    }

    var empty = $('#sideEmpty');
    if (empty) empty.hidden = !(list !== null && !APP.projectsLoading && !APP.projectsError && (!list || !list.length));

    var navKey = route.platform || '';
    $$('.platform-links a').forEach(function (a) {
      if (a.getAttribute('data-nav') === navKey) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    var group = $('#platformGroup');
    if (group && navKey) group.open = true;
  }

  function renderTopbar(route) {
    var crumb = $('#crumb');
    var tabs = $('#projectTabs');
    var project = route.pid ? (APP.projectCache[route.pid] || null) : null;
    var title = '项目';
    var html = '';

    if (route.name === 'projects') {
      title = '项目';
    } else if (route.name === 'redirect') {
      title = 'PanguVerse';
    } else if (route.platform) {
      title = '平台';
      html = ' <span class="crumb-sub">' + esc(platformTitle(route.platform)) + '</span>';
    } else if (route.name === 'run-detail' && !route.pid) {
      title = '未归类运行';
      html = ' <span class="crumb-sub mono">' + esc(route.runId || '') + '</span>';
    } else if (route.name === 'notfound') {
      title = '页面不存在';
    } else if (route.pid) {
      title = (project && project.name) ? project.name : (route.pid || '项目');
      html = ' <span class="crumb-sub mono">' + esc(route.pid) + '</span>';
    }
    if (crumb) crumb.innerHTML = esc(title) + html;

    if (!tabs) return;
    if (route.pid && route.name !== 'run-detail') {
      tabs.hidden = false;
      var active = route.name;
      var items = [
        ['overview', '概览'],
        ['run', '运行'],
        ['history', '历史'],
        ['settings', '设置']
      ];
      tabs.innerHTML = items.map(function (item) {
        return '<a class="tab" href="#/p/' + enc(route.pid) + '/' + item[0] + '"' +
          (active === item[0] ? ' aria-current="page"' : '') + '>' + esc(item[1]) + '</a>';
      }).join('');
    } else if (route.pid && route.name === 'run-detail') {
      tabs.hidden = false;
      tabs.innerHTML = [
        ['overview', '概览'], ['run', '运行'], ['history', '历史'], ['settings', '设置']
      ].map(function (item) {
        return '<a class="tab" href="#/p/' + enc(route.pid) + '/' + item[0] + '"' +
          (item[0] === 'history' ? ' aria-current="page"' : '') + '>' + esc(item[1]) + '</a>';
      }).join('');
    } else {
      tabs.hidden = true;
      tabs.innerHTML = '';
    }
  }

  function resolveRouteProject(route) {
    return route.pid ? getProject(route.pid).catch(function () { return null; }) : Promise.resolve(null);
  }

  function render() {
    if (!APP.auth.authenticated) return;
    var seq = ++APP.seq;
    var route = parseRoute();
    var host = $('#view');
    if (!host) return;

    if (APP.controller) {
      try { APP.controller.abort(); } catch (e) { /* noop */ }
    }
    APP.controller = (typeof AbortController === 'function') ? new AbortController() : null;

    var key = routeKey(route);
    if (APP.lastRoute !== key && APP.lastRoute !== null) {
      try { window.scrollTo(0, 0); } catch (e) { /* noop */ }
    }
    if (APP.bannerRoute && APP.bannerRoute !== key) hideBanner();
    APP.lastRoute = key;
    renderSidebar();
    closeMobileNav();

    var ctx = {
      alive: function () { return seq === APP.seq; },
      signal: APP.controller ? APP.controller.signal : undefined
    };

    var pageTitle = 'PanguVerse';
    if (route.name === 'projects') pageTitle = '项目';
    else if (route.platform) pageTitle = platformTitle(route.platform);
    else if (route.pid) pageTitle = (APP.projectCache[route.pid] && APP.projectCache[route.pid].name) || '项目';
    document.title = pageTitle + ' · PanguVerse';

    if (route.name === 'redirect') {
      var target = { projects: '#/projects', 'platform-atoms': '#/platform/atoms', 'platform-packs': '#/platform/packs', 'platform-tasks': '#/platform/tasks' }[route.to] || DEFAULT_HASH;
      try { history.replaceState(null, '', target); } catch (e) { location.hash = target; }
      render();
      return;
    }

    // 顶栏需要项目名时先补一次详情（不阻塞渲染）
    var top = function (project) {
      renderTopbar(route);
      if (project && project.name) {
        var crumb = $('#crumb');
        if (crumb && route.pid) {
          crumb.innerHTML = esc(project.name) + ' <span class="crumb-sub mono">' + esc(route.pid) + '</span>';
        }
      }
    };
    renderTopbar(route);
    if (route.pid && !APP.projectCache[route.pid]) {
      resolveRouteProject(route).then(function (project) {
        if (!ctx.alive()) return;
        top(project);
      });
    } else if (route.pid) {
      top(APP.projectCache[route.pid]);
    }

    host.innerHTML = '<div class="loading"><span class="spinner" aria-hidden="true"></span>加载中…</div>';

    var task = null;
    if (route.name === 'projects') task = renderProjects(host, ctx);
    else if (route.name === 'overview') task = renderOverview(host, ctx, route.pid, route.query || {});
    else if (route.name === 'run') task = renderRun(host, ctx, route.pid, route.query || {});
    else if (route.name === 'history') task = renderHistory(host, ctx, route.pid, route.query || {});
    else if (route.name === 'settings') task = renderSettings(host, ctx, route.pid);
    else if (route.name === 'run-detail') task = renderDetail(host, ctx, route.pid || '', route.runId, !!route.legacyDetail);
    else if (route.name === 'platform-atoms') task = renderAtoms(host, ctx);
    else if (route.name === 'platform-packs') task = renderPacks(host, ctx);
    else if (route.name === 'platform-tasks') task = renderTasks(host, ctx);
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
  /* 登录 / 退出                                                         */
  /* ================================================================== */
  function showBoot() {
    var app = $('#appShell');
    if (app) app.hidden = true;
    var login = $('#loginRoot');
    if (login) { login.hidden = true; login.innerHTML = ''; }
    var boot = $('#bootRoot');
    if (boot) boot.hidden = false;
  }

  function setAuthChrome() {
    var who = $('#whoami');
    var out = $('#logoutBtn');
    var show = !!(APP.auth.authenticated);
    if (who) {
      who.hidden = !show;
      who.textContent = show ? (APP.auth.user || (APP.auth.legacy ? '无需登录' : '')) : '';
      who.title = show && APP.auth.user ? '当前账号：' + APP.auth.user : '';
    }
    if (out) out.hidden = !show || !!APP.auth.legacy;
  }

  function showAppView() {
    var boot = $('#bootRoot');
    if (boot) boot.hidden = true;
    var login = $('#loginRoot');
    if (login) { login.hidden = true; login.innerHTML = ''; }
    var app = $('#appShell');
    if (app) app.hidden = false;
    var skip = $('.skip-link');
    if (skip) skip.hidden = false;
    setAuthChrome();
  }

  function loginCardHtml(opts) {
    opts = opts || {};
    return '<div class="login-card" role="main" aria-labelledby="loginTitle">' +
      '<div class="login-brand">' +
        '<svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false">' +
          '<path d="M7 24L16 6l9 18z" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"></path>' +
          '<path d="M11.5 19h9" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"></path>' +
        '</svg>' +
        '<div><h1 id="loginTitle">登录 PanguVerse</h1><div class="login-sub">项目 · 套件 · 运行</div></div>' +
      '</div>' +
      (opts.notice ? '<div class="login-notice" role="status">' + esc(opts.notice) + '</div>' : '') +
      '<div class="login-error" id="loginError" role="alert"' + (opts.error ? '' : ' hidden') + '>' + esc(opts.error || '') + '</div>' +
      '<form class="login-form" id="loginForm" novalidate>' +
        '<div class="field"><label for="loginUser">账号</label>' +
          '<input class="input" id="loginUser" name="username" autocomplete="username" spellcheck="false" placeholder="admin" value="' + esc(opts.user || '') + '"></div>' +
        '<div class="field"><label for="loginPass">密码</label>' +
          '<input class="input" id="loginPass" name="password" type="password" autocomplete="current-password" spellcheck="false" placeholder="输入密码"></div>' +
        '<button type="submit" class="btn btn-primary" id="loginSubmit">登录</button>' +
        (opts.retry ? '<button type="button" class="btn btn-sm btn-ghost" data-auth-retry>重新检测服务端</button>' : '') +
      '</form>' +
      '<div class="login-foot">会话有效期 12 小时；刷新页面会校验服务端会话。</div>' +
    '</div>';
  }

  function setLoginError(message) {
    var el = $('#loginError');
    if (!el) return;
    el.textContent = message || '';
    el.hidden = !message;
  }

  function showLoginView(opts) {
    opts = opts || {};
    var boot = $('#bootRoot');
    if (boot) boot.hidden = true;
    var app = $('#appShell');
    if (app) app.hidden = true;
    var skip = $('.skip-link');
    if (skip) skip.hidden = true;
    var root = $('#loginRoot');
    if (!root) return;
    root.hidden = false;
    root.innerHTML = loginCardHtml(opts);
    var form = $('#loginForm', root);
    if (form) form.addEventListener('submit', function (e) { e.preventDefault(); submitLogin(form); });
    var retry = $('[data-auth-retry]', root);
    if (retry) retry.addEventListener('click', function () { initAuth(); });
    document.title = '登录 · PanguVerse';
    var userEl = $('#loginUser', root);
    var pwdEl = $('#loginPass', root);
    if (userEl && !userEl.value) userEl.focus();
    else if (pwdEl) pwdEl.focus();
  }

  function submitLogin(form) {
    if (!form || form.dataset.busy === '1') return;
    var userEl = $('#loginUser', form);
    var pwdEl = $('#loginPass', form);
    var btn = $('#loginSubmit', form);
    var username = userEl ? userEl.value.trim() : '';
    var password = pwdEl ? pwdEl.value : '';
    if (!username || !password) {
      setLoginError('请输入账号和密码。');
      if (!username && userEl) userEl.focus();
      else if (pwdEl) pwdEl.focus();
      return;
    }
    form.dataset.busy = '1';
    setLoginError('');
    if (btn) { btn.disabled = true; btn.textContent = '登录中…'; }

    apiPost(API + 'auth/login', { username: username, password: password }).then(function (res) {
      if (pwdEl) pwdEl.value = '';
      form.dataset.busy = '';
      APP.auth.authenticated = true;
      APP.auth.user = (res && res.user) || username;
      APP.auth.legacy = false;
      APP.auth.redirecting = false;
      var next = APP.auth.next;
      APP.auth.next = null;
      if (!next || next.charAt(0) !== '#' || next === '#') next = DEFAULT_HASH;

      showAppView();
      loadProjects(true).catch(function () {});
      loadStatus();
      if (location.hash !== next) location.hash = next;
      else render();
      toast('已登录：' + (APP.auth.user || username), 'ok');
    }).catch(function (err) {
      if (pwdEl) pwdEl.value = '';
      form.dataset.busy = '';
      if (btn) { btn.disabled = false; btn.textContent = '登录'; }
      setLoginError(err && err.message ? err.message : '登录失败');
      if (pwdEl) pwdEl.focus();
    });
  }

  function resetClientState() {
    stopJobWatcher();
    clearBatchTimers();
    APP.projects = null;
    APP.projectsError = null;
    APP.projectsLoading = false;
    APP.projectsPromise = null;
    APP.projectCache = {};
    APP.scenarios = null;
    APP.atoms = null;
    APP.tasks = null;
    APP.atomFilters = { domain: '', tag: '', category: '' };
    APP.bannerRoute = null;
    APP.lastRoute = null;
    APP.seq++;
    if (APP.controller) {
      try { APP.controller.abort(); } catch (e) { /* noop */ }
      APP.controller = null;
    }
    hideBanner();
    closeModal(null);
    var view = $('#view');
    if (view) view.innerHTML = '';
    var toasts = $('#toasts');
    if (toasts) toasts.innerHTML = '';
  }

  function handleUnauthorized() {
    if (!APP.auth.authenticated || APP.auth.redirecting) return;
    APP.auth.redirecting = true;
    APP.auth.legacy = false;
    var h = location.hash;
    APP.auth.next = (h && h.charAt(0) === '#' && h !== '#' && h !== DEFAULT_HASH) ? h : null;
    APP.auth.authenticated = false;
    APP.auth.user = '';
    resetClientState();
    showLoginView({ notice: '登录已过期，请重新登录' });
    setTimeout(function () { APP.auth.redirecting = false; }, 0);
  }

  function doLogout(btn) {
    if (APP.auth.loggingOut) return;
    APP.auth.loggingOut = true;
    if (btn) { btn.disabled = true; btn.textContent = '退出中…'; }
    apiPost(API + 'auth/logout', {}).then(finish).catch(function (err) {
      if (err && err.status === 401) { finish(); return; }
      APP.auth.loggingOut = false;
      if (btn) { btn.disabled = false; btn.textContent = '退出登录'; }
      toast('退出失败：' + (err && err.message ? err.message : '未知错误'), 'error');
    });
    function finish() {
      APP.auth.loggingOut = false;
      APP.auth.authenticated = false;
      APP.auth.user = '';
      APP.auth.legacy = false;
      APP.auth.next = null;
      APP.auth.redirecting = false;
      resetClientState();
      showLoginView({ notice: '已退出登录' });
    }
  }

  function initAuth() {
    showBoot();
    apiGet(API + 'auth/status').then(function (st) {
      var disabled = !!(st && (st.auth_disabled === true || st.auth_enabled === false));
      if (st && (st.authenticated === true || disabled)) {
        APP.auth.authenticated = true;
        APP.auth.user = st.user || '';
        APP.auth.legacy = disabled;
        var h = location.hash;
        APP.auth.next = (!disabled && h && h.indexOf('#/') === 0 && h !== DEFAULT_HASH) ? h : null;
        showAppView();
        loadProjects(true).catch(function () {});
        loadStatus();
        render();
        return;
      }
      APP.auth.authenticated = false;
      APP.auth.user = '';
      APP.auth.legacy = false;
      var cur = location.hash;
      APP.auth.next = (cur && cur.indexOf('#/') === 0 && cur !== DEFAULT_HASH) ? cur : null;
      showLoginView({});
    }).catch(function (err) {
      if (err && err.status === 404) {
        // 旧后端或 --no-auth 未暴露 auth/status：按无认证模式继续
        APP.auth.authenticated = true;
        APP.auth.legacy = true;
        APP.auth.user = '';
        showAppView();
        loadProjects(true).catch(function () {});
        loadStatus();
        render();
        return;
      }
      if (err && err.status === 401) {
        APP.auth.authenticated = false;
        APP.auth.legacy = false;
        showLoginView({});
        return;
      }
      showLoginView({
        error: '无法读取登录状态：' + (err && err.message ? err.message : '未知错误'),
        retry: true
      });
    });
  }

  /* ================================================================== */
  /* 平台状态                                                            */
  /* ================================================================== */
  function loadStatus() {
    var conn = $('#connState');
    apiGet(API + 'status').then(function (st) {
      APP.status = st || {};
      if (conn) { conn.dataset.state = 'ok'; var t = $('.conn-text', conn); if (t) t.textContent = '已连接'; }
      var text = $('#llmText');
      var dotEl = $('#llmDot');
      var llm = !!(st && st.llm_enabled);
      if (text) text.textContent = llm ? '模型规划已启用' : '离线规则模式';
      if (dotEl) dotEl.className = 'llm-dot ' + (llm ? 'is-on' : 'is-off');
      var facts = $('#sideFacts');
      if (facts) facts.hidden = false;
      var ws = $('#factWorkspace');
      var ver = $('#factVersion');
      if (ws) { ws.textContent = (st && (st.workspace || st.root)) || '—'; ws.title = ws.textContent; }
      if (ver) ver.textContent = (st && st.version) || '—';
    }).catch(function () {
      if (conn) { conn.dataset.state = 'bad'; var t2 = $('.conn-text', conn); if (t2) t2.textContent = '未连接'; }
      var text2 = $('#llmText');
      if (text2) text2.textContent = '平台状态不可用';
      var d2 = $('#llmDot');
      if (d2) d2.className = 'llm-dot is-off';
    });
  }

  /* ================================================================== */
  /* 项目列表页                                                          */
  /* ================================================================== */
  function projectCardHtml(p) {
    var last = p.last_run || null;
    var st = last ? (last.status || runOutcome(last).status) : 'none';
    var meta = statusMeta(st);
    var lastAt = last ? (last.at || last.started_at || last.finished_at) : 0;
    var rate = p.pass_rate || (last ? { passed: last.passed, total: last.total } : null);
    return '<a class="project-card" href="#/p/' + enc(p.project_id) + '/overview" data-search-text="' +
        esc(((p.name || '') + ' ' + (p.base_url || '')).toLowerCase()) + '">' +
      '<div class="pc-top">' +
        '<span class="pc-name">' + esc(p.name || p.project_id) + '</span>' +
        '<span class="pc-status">' + dot(st) + esc(meta.label) + '</span>' +
      '</div>' +
      '<div class="pc-url mono">' + esc(p.base_url || '未配置站点地址') + '</div>' +
      '<div class="pc-stats">' +
        '<div class="pc-stat"><b>' + esc(p.suite_count === undefined ? '—' : String(p.suite_count)) + '</b><span>套件</span></div>' +
        '<div class="pc-stat"><b>' + (rate ? (rate.total > 0 ? Math.round((num(rate.passed) / num(rate.total)) * 100) + '%' : '—') : '—') + '</b><span>通过率</span></div>' +
        '<div class="pc-stat"><b>' + esc(lastAt ? fmtRel(lastAt) : '—') + '</b><span>上次运行</span></div>' +
      '</div>' +
      '<div class="pc-foot">' + rateHtml(rate || {}, { showSub: false }) + '</div>' +
    '</a>';
  }

  function renderProjects(host, ctx) {
    host.innerHTML = pageHead('项目',
      '一个项目 = 一组套件 + 共享环境。运行、历史与总结都归属项目。',
      '<button type="button" class="btn btn-primary" data-act="new-project">＋ 新建项目</button>' +
      '<button type="button" class="btn btn-ghost" data-act="from-history">从历史创建项目</button>') +
      '<div id="projectsBody">' + loadingHtml('正在读取项目…') + '</div>';
    var body = $('#projectsBody', host);

    loadProjects(true).then(function (list) {
      if (!ctx.alive()) return;
      if (!list.length) {
        body.innerHTML = '<div class="empty hero-empty">' +
          '<div class="empty-mark" aria-hidden="true">◫</div>' +
          '<strong>先建一个项目</strong>' +
          '<p>把同一站点的套件和环境放在一起：之后每次运行都会自动归属，概览直接给出最近 5 次结果与失败 Top 3。</p>' +
          '<div class="actions">' +
            '<button type="button" class="btn btn-primary" data-act="new-project">新建项目</button>' +
            '<button type="button" class="btn" data-act="from-history">从历史创建项目</button>' +
          '</div>' +
          '<p class="hint">旧运行不会丢失：它们会显示为「未归类」，仍可通过运行详情查看。</p>' +
        '</div>';
        return;
      }
      body.innerHTML =
        '<div class="toolbar">' +
          '<label class="search" aria-label="搜索项目"><span aria-hidden="true">⌕</span>' +
            '<input type="search" data-search="projects" placeholder="搜索项目（名称或站点地址），按 / 聚焦" autocomplete="off"></label>' +
          '<span class="toolbar-spacer"></span>' +
          '<span class="filter-count" id="projectCount">共 ' + list.length + ' 个项目</span>' +
        '</div>' +
        '<div class="projects-grid" id="projectsGrid">' + list.map(projectCardHtml).join('') + '</div>' +
        '<div id="projectsNoMatch" hidden>' + emptyHtml('没有匹配的项目', '换个关键词，或清空搜索。', '<button type="button" class="btn btn-sm" data-act="clear-project-search">清空搜索</button>') + '</div>';

      var input = $('[data-search="projects"]', body);
      if (input) {
        input.addEventListener('input', function () {
          var q = input.value.trim().toLowerCase();
          var cards = $$('.project-card', body);
          var shown = 0;
          cards.forEach(function (card) {
            var hit = !q || (card.getAttribute('data-search-text') || '').indexOf(q) >= 0;
            card.hidden = !hit;
            if (hit) shown++;
          });
          var count = $('#projectCount', body);
          if (count) count.textContent = q ? ('匹配 ' + shown + ' / ' + cards.length + ' 个项目') : ('共 ' + cards.length + ' 个项目');
          var noMatch = $('#projectsNoMatch', body);
          if (noMatch) noMatch.hidden = shown > 0;
        });
      }
    }).catch(function (err) {
      if (!ctx.alive()) return;
      body.innerHTML = '<div id="projectsError"></div>';
      renderError($('#projectsError', body), err, function () { renderProjects(host, ctx); }, '无法读取项目列表');
    });
  }

  function openProjectModal() {
    var root = openModal({
      title: '新建项目',
      subtitle: '项目保存站点地址与默认环境；套件在项目设置里绑定。',
      okLabel: '创建项目',
      bodyHtml:
        '<div class="field"><label for="npName">项目名称</label>' +
          '<input class="input" id="npName" name="name" placeholder="例如 coding-log-web" autocomplete="off" maxlength="80"></div>' +
        '<div class="field"><label for="npBase">站点地址 base_url</label>' +
          '<input class="input mono" id="npBase" name="base_url" placeholder="https://seres-ai.com" autocomplete="off" spellcheck="false"></div>' +
        '<div class="field"><span class="field-label">项目默认环境变量（可选）</span>' +
          envEditorBlock({ note: '密钥保存后会以 *** 返回；这里填写的值会作为项目默认，套件 env 可覆盖。' }) + '</div>',
      onMount: function (rootEl) {
        if (!rootEl) return;
        var editor = mountEnvEditor($('[data-env-editor]', rootEl), [], { placeholder: '值' });
        rootEl._envEditor = editor;
        var name = $('#npName', rootEl);
        if (name) name.focus();
      },
      onSubmit: function (rootEl) {
        var nameEl = $('#npName', rootEl);
        var baseEl = $('#npBase', rootEl);
        var name = nameEl ? nameEl.value.trim() : '';
        if (!name) {
          if (nameEl) nameEl.focus();
          return Promise.reject(new ApiError('请填写项目名称', 0, 'validation'));
        }
        var env = (rootEl._envEditor ? rootEl._envEditor.collect({ existing: false }) : {});
        var body = { name: name, base_url: baseEl ? baseEl.value.trim() : '' };
        if (Object.keys(env).length) body.env = env;
        return apiPost(API + 'projects', body).then(function (project) {
          APP.projectsPromise = null;
          if (project && project.project_id) APP.projectCache[project.project_id] = project;
          loadProjects(true).catch(function () {});
          setTimeout(function () { go('#/p/' + enc(project.project_id) + '/overview'); }, 0);
          toast('项目已创建：' + (project.name || name), 'ok');
          return project;
        });
      }
    });
    return root;
  }

  function openHistoryGuide() {
    openModal({
      title: '从历史创建项目',
      subtitle: '旧运行不会自动归类；这里只提供迁移路径，不会自动建项目。',
      okLabel: '新建项目',
      bodyHtml:
        '<div id="guideBody">' + loadingHtml('正在查看最近运行…') + '</div>',
      onMount: function (rootEl) {
        var body = $('#guideBody', rootEl);
        apiGet(API + 'runs?limit=50').then(function (runs) {
          var list = Array.isArray(runs) ? runs : [];
          var unclassified = list.filter(function (r) {
            var meta = (r && r.meta) || {};
            return !meta.project_id;
          }).length;
          if (!body) return;
          body.innerHTML =
            '<div class="notice info"><span aria-hidden="true">ⓘ</span><div>最近 ' + list.length + ' 次运行中，有 <b>' + unclassified + '</b> 次未归类（无项目归属）。</div></div>' +
            '<ol style="margin:0 0 0 18px;padding:0;color:var(--dim);display:flex;flex-direction:column;gap:8px;">' +
              '<li><b style="color:var(--text-soft)">新建项目</b>：填写名称与站点地址。</li>' +
              '<li><b style="color:var(--text-soft)">绑定套件</b>：在项目「设置」里选择旧运行使用的场景文件。</li>' +
              '<li><b style="color:var(--text-soft)">继续使用旧运行</b>：历史运行不会被改名或删除，仍可通过运行详情查看。</li>' +
            '</ol>';
        }).catch(function (err) {
          if (body) body.innerHTML = '<div class="notice"><span aria-hidden="true">⚠</span><div>暂时无法读取历史运行：' + esc(err && err.message ? err.message : '未知错误') + '</div></div>';
        });
      },
      onSubmit: function () {
        setTimeout(openProjectModal, 0);
        return true;
      }
    });
  }

  /* ================================================================== */
  /* 项目概览                                                            */
  /* ================================================================== */
  function findSuite(project, sid) {
    var suites = (project && Array.isArray(project.suites)) ? project.suites : [];
    for (var i = 0; i < suites.length; i++) {
      if (suites[i].suite_id === sid) return suites[i];
    }
    return null;
  }

  function suiteLabel(project, sid) {
    if (!sid) return '未归类';
    var suite = findSuite(project, sid);
    return suite && suite.name ? suite.name : sid;
  }

  function runSuiteId(run) {
    if (!run) return '';
    if (run.suite_id) return run.suite_id;
    if (run.meta && run.meta.suite_id) return run.meta.suite_id;
    return '';
  }

  function runSuiteName(project, run) {
    if (!run) return '运行';
    if (run.suite_name) return run.suite_name;
    var sid = runSuiteId(run);
    return sid ? suiteLabel(project, sid) : (run.run_id ? 'run ' + run.run_id.slice(0, 8) : '运行');
  }

  function recentRunsHtml(project, runs) {
    if (!Array.isArray(runs) || !runs.length) {
      return emptyHtml('还没有运行记录',
        '绑定套件后点「一键跑全部套件」，最近 5 次结果会出现在这里。',
        '<button type="button" class="btn btn-primary" data-act="run-all" data-pid="' + esc(project.project_id) + '">一键跑全部套件</button>' +
        '<a class="btn" href="#/p/' + enc(project.project_id) + '/history">查看历史</a>');
    }
    return '<div class="run-list">' + runs.map(function (run) {
      var outcome = runOutcome(run);
      var counts = runCounts(run);
      if (!counts.total && outcome.total) counts = { passed: outcome.passed, total: outcome.total };
      var when = runWhen(run);
      var rid = run.run_id || '';
      return '<a class="run-row" href="#/p/' + enc(project.project_id) + '/runs/' + enc(rid) + '">' +
        badge(outcome.status) +
        '<span class="run-main"><b>' + esc(runSuiteName(project, run)) + '</b>' +
          '<span class="mono">' + esc(rid) + '</span></span>' +
        '<span class="run-num">' + (counts.total ? esc(counts.passed + '/' + counts.total) : '—') + '</span>' +
        '<span class="run-when">' + esc(when ? fmtRel(when) : '—') + '</span>' +
        '<span class="run-arrow" aria-hidden="true">›</span>' +
      '</a>';
    }).join('') + '</div>';
  }

  function failureTopHtml(items) {
    if (!Array.isArray(items) || !items.length) {
      return emptyHtml('最近 5 次运行没有失败用例', '失败归类会在出现失败后自动聚合到这里（最多 3 条）。');
    }
    return '<div class="fail-list">' + items.slice(0, 3).map(function (f) {
      f = f || {};
      var cases = Array.isArray(f.cases) ? f.cases : (Array.isArray(f.case_ids) ? f.case_ids : []);
      var shown = cases.slice(0, 6);
      return '<div class="fail-item">' +
        '<div class="fail-kind"><b>' + esc(f.kind || '未归类失败') + '</b>' +
          '<div class="chips">' + shown.map(function (id) { return '<span class="chip mono">' + esc(id) + '</span>'; }).join('') +
            (cases.length > shown.length ? '<span class="chip">+' + (cases.length - shown.length) + '</span>' : '') +
          '</div></div>' +
        '<span class="fail-count">' + esc(String(num(f.count))) + '</span>' +
      '</div>';
    }).join('') + '</div>';
  }

  function kpiHtml(project, summary) {
    var rate = rateInfo(summary && summary.pass_rate);
    var recent = Array.isArray(summary && summary.recent_runs) ? summary.recent_runs : [];
    var latest = recent.length ? recent[0] : (project.last_run || null);
    var latestOutcome = latest ? (latest.status || runOutcome(latest).status) : 'none';
    var failTotal = 0;
    (Array.isArray(summary && summary.failure_top) ? summary.failure_top : []).forEach(function (f) {
      failTotal += num(f && f.count);
    });
    var rateLabel = rate.rate === null ? '—' : (Math.round(rate.rate * 10) / 10) + '%';
    var rateCls = rate.rate === null ? 'dim' : (rate.rate >= 80 ? 'ok' : 'bad');
    return '<div class="kpi-grid">' +
      '<div class="kpi"><span class="kpi-label">通过率</span><span class="kpi-num ' + rateCls + '">' + esc(rateLabel) + '</span>' +
        '<span class="kpi-sub">' + (rate.total ? esc(rate.passed + '/' + rate.total + ' 用例') : '暂无数据') + '</span></div>' +
      '<div class="kpi"><span class="kpi-label">套件</span><span class="kpi-num">' + esc(String(Array.isArray(project.suites) ? project.suites.length : (project.suite_count || 0))) + '</span>' +
        '<span class="kpi-sub">运行只取当前项目套件</span></div>' +
      '<div class="kpi"><span class="kpi-label">最近运行</span><span class="kpi-num dim" style="font-size:17px">' + esc(statusMeta(latestOutcome).label) + '</span>' +
        '<span class="kpi-sub">' + esc(latest && runWhen(latest) ? fmtRel(runWhen(latest)) : '还没有运行') + '</span></div>' +
      '<div class="kpi"><span class="kpi-label">失败 Top 3 合计</span><span class="kpi-num ' + (failTotal ? 'bad' : 'ok') + '">' + esc(String(failTotal)) + '</span>' +
        '<span class="kpi-sub">最近 5 次运行的失败用例</span></div>' +
    '</div>';
  }

  function overviewSectionsHtml(project, summary) {
    var runs = Array.isArray(summary && summary.recent_runs) ? summary.recent_runs : [];
    return '<section class="section" id="recentSection">' +
        '<div class="section-head"><h2>最近 5 次运行</h2>' +
          '<span class="hint">点击任意一行进入运行详情</span>' +
          '<a class="btn btn-sm" href="#/p/' + enc(project.project_id) + '/history">全部历史</a></div>' +
        recentRunsHtml(project, runs) +
      '</section>' +
      '<section class="section" id="failureSection">' +
        '<div class="section-head"><h2>失败 Top 3</h2><span class="hint">按报告 failure_kind 聚合</span></div>' +
        failureTopHtml(summary && summary.failure_top) +
      '</section>';
  }

  function renderOverview(host, ctx, pid, query) {
    var batchId = query && query.batch ? String(query.batch) : '';
    host.innerHTML = pageHead('项目概览', '', '') + '<div id="ovLoad">' + loadingHtml('正在读取项目概览…') + '</div>';
    return Promise.all([getProject(pid), apiGet(API + 'projects/' + enc(pid) + '/summary?limit=5')])
      .then(function (results) {
        if (!ctx.alive()) return;
        var project = results[0];
        var summary = results[1] || {};
        var suites = Array.isArray(project.suites) ? project.suites : [];
        host.innerHTML =
          pageHead(project.name || pid,
            '<span class="mono">' + esc(project.base_url || '未配置站点地址') + '</span>' +
            '<span>' + suites.length + ' 个套件</span>' +
            '<span>创建于 ' + esc(fmtTs(project.created_at)) + '</span>',
            '<button type="button" class="btn btn-primary" data-act="run-all" data-pid="' + esc(pid) + '"' +
              (suites.length ? '' : ' disabled title="先绑定套件"') + '>▶ 一键跑全部套件</button>' +
            '<a class="btn" href="#/p/' + enc(pid) + '/run">选择套件运行</a>' +
            '<a class="btn btn-ghost" href="#/p/' + enc(pid) + '/settings">设置</a>') +
          '<div id="batchHost">' + (batchId ? '' : '') + '</div>' +
          '<div id="kpiHost">' + kpiHtml(project, summary) + '</div>' +
          '<div id="ovSections">' + overviewSectionsHtml(project, summary) + '</div>' +
          (suites.length ? '' :
            '<div class="notice info"><span aria-hidden="true">ⓘ</span><div>这个项目还没有绑定套件。' +
            '<a href="#/p/' + enc(pid) + '/settings">去设置里绑定</a>后就能一键运行。</div></div>');

        if (batchId) {
          mountBatchPanel($('#batchHost', host), batchId, pid, {
            onDone: function () { refreshOverview(host, pid, ctx); }
          });
        }
      });
  }

  function refreshOverview(host, pid, ctx) {
    if (!ctx.alive()) return;
    Promise.all([
      getProject(pid, true).catch(function () { return APP.projectCache[pid]; }),
      apiGet(API + 'projects/' + enc(pid) + '/summary?limit=5').catch(function () { return null; })
    ]).then(function (results) {
      if (!ctx.alive() || !results[1]) return;
      var project = results[0] || APP.projectCache[pid];
      if (!project) return;
      var kpi = $('#kpiHost', host);
      var sections = $('#ovSections', host);
      if (kpi) kpi.innerHTML = kpiHtml(project, results[1]);
      if (sections) sections.innerHTML = overviewSectionsHtml(project, results[1]);
      loadProjects(true).catch(function () {});
    });
  }

  function startRunAll(pid, btn) {
    getProject(pid).then(function (project) {
      var suites = Array.isArray(project.suites) ? project.suites : [];
      if (!suites.length) {
        toast('还没有绑定套件，先去设置里绑定', 'error');
        go('#/p/' + enc(pid) + '/settings');
        return;
      }
      if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>提交中…'; }
      return startBatch(pid, null, null).then(function (res) {
        var bid = res && res.batch_id;
        if (!bid) throw new ApiError('服务端没有返回 batch_id', 500, 'bad_payload');
        go('#/p/' + enc(pid) + '/overview?batch=' + enc(bid));
        toast('已提交 ' + suites.length + ' 个套件，串行执行', 'ok');
      }).catch(function (err) {
        if (btn) { btn.disabled = false; btn.textContent = '▶ 一键跑全部套件'; }
        toast('提交失败：' + (err && err.message ? err.message : '未知错误'), 'error');
      });
    }).catch(function (err) {
      toast('无法读取项目：' + (err && err.message ? err.message : '未知错误'), 'error');
    });
  }

  /* ================================================================== */
  /* 批量运行                                                            */
  /* ================================================================== */
  function startBatch(pid, suiteIds, envOverrides) {
    var body = {};
    if (Array.isArray(suiteIds) && suiteIds.length) body.suite_ids = suiteIds;
    if (envOverrides && Object.keys(envOverrides).length) body.env = envOverrides;
    return apiPost(API + 'projects/' + enc(pid) + '/run', body);
  }

  function clearBatchTimers() {
    (APP.batchTimers || []).forEach(function (t) { clearTimeout(t); });
    APP.batchTimers = [];
  }

  function batchRowStatus(row) {
    var total = num(row && row.total);
    var passed = num(row && row.passed);
    if (row && row.run_id && total > 0) {
      return passed >= total ? 'passed' : 'failed';
    }
    if (row && row.status === 'done') return passed > 0 ? 'passed' : 'done';
    if (row && row.status) return row.status;
    return 'pending';
  }

  function batchHtml(batch, pid) {
    var suites = Array.isArray(batch && batch.suites) ? batch.suites : [];
    var agg = (batch && batch.aggregate) || {};
    var passed = num(agg.passed);
    var total = num(agg.total);
    var suitesPassed = num(agg.suites_passed);
    var suitesFailed = num(agg.suites_failed);
    if (!suites.length && !agg.total) {
      return loadingHtml('批次已提交，正在等待第一个套件…');
    }
    var status = (batch && batch.status) || 'running';
    return '<div class="batch-head">' +
        '<h2>批量运行</h2>' + badge(status === 'failed' ? 'failed' : (status === 'done' ? 'passed' : status)) +
        '<span class="batch-id">' + esc(batch && batch.batch_id ? batch.batch_id : '') + '</span>' +
        '<span class="batch-elapsed" data-batch-elapsed>自动刷新中</span>' +
      '</div>' +
      '<div class="batch-agg">' +
        '<div><b>' + esc(String(suitesPassed)) + '</b><span>套件通过</span></div>' +
        '<div><b>' + esc(String(suitesFailed)) + '</b><span>套件失败</span></div>' +
        '<div><b>' + esc(String(passed)) + '/' + esc(String(total)) + '</b><span>用例通过</span></div>' +
        '<div><b>' + esc(fmtDur(num(agg.duration_ms))) + '</b><span>总耗时</span></div>' +
      '</div>' +
      '<div class="batch-suites">' + suites.map(function (row) {
        var st = batchRowStatus(row);
        var name = row.name || suiteLabel(APP.projectCache[pid], row.suite_id);
        var count = num(row.total) ? (num(row.passed) + '/' + num(row.total)) : '';
        return '<div class="batch-row">' +
          badge(st) +
          '<span class="batch-name">' + esc(name) + '</span>' +
          '<span class="batch-num">' + esc(count) + '</span>' +
          (row.run_id
            ? '<a class="btn btn-sm" href="#/p/' + enc(pid) + '/runs/' + enc(row.run_id) + '">查看详情</a>'
            : '<span class="hint">' + esc(row.status === 'running' ? '执行中…' : '等待中…') + '</span>') +
          (row.error ? '<div class="batch-error">' + esc(row.error) + '</div>' : '') +
        '</div>';
      }).join('') + '</div>' +
      (status === 'done' || status === 'failed'
        ? '<div class="hint" style="margin-top:8px">' + esc(status === 'done' ? '全部套件已执行完毕。' : '批次结束，部分套件没有拿到报告。') + '</div>'
        : '');
  }

  function mountBatchPanel(hostEl, batchId, pid, opts) {
    if (!hostEl) return;
    opts = opts || {};
    var startedAt = Date.now();
    var alive = true;
    var attempts = 0;
    hostEl.innerHTML = '<div class="batch-panel" id="batchPanel">' + loadingHtml('正在读取批次状态…') + '</div>';

    function stop() {
      alive = false;
      if (opts.signal && opts.signal.removeEventListener) {
        try { opts.signal.removeEventListener('abort', stop); } catch (e) { /* noop */ }
      }
    }
    if (opts.signal && opts.signal.addEventListener) {
      opts.signal.addEventListener('abort', stop);
    }

    function render(data) {
      var panel = $('#batchPanel', hostEl);
      if (!panel) return;
      panel.innerHTML = batchHtml(data, pid);
      var el = $('[data-batch-elapsed]', panel);
      if (el) {
        var secs = Math.floor((Date.now() - startedAt) / 1000);
        el.textContent = (data.status === 'done' || data.status === 'failed') ? ('结束于 ' + fmtRel(Date.now() / 1000)) : ('已等待 ' + secs + ' 秒');
      }
    }

    function tick() {
      if (!alive || !document.body.contains(hostEl)) { stop(); return; }
      apiGet(API + 'batches/' + enc(batchId)).then(function (data) {
        if (!alive || !document.body.contains(hostEl)) { stop(); return; }
        attempts = 0;
        render(data);
        if (data && (data.status === 'done' || data.status === 'failed')) {
          stop();
          if (typeof opts.onDone === 'function') opts.onDone(data);
          return;
        }
        var timer = setTimeout(tick, 1200);
        APP.batchTimers.push(timer);
      }).catch(function (err) {
        if (!alive) return;
        attempts++;
        if (attempts < 3) {
          var timer = setTimeout(tick, 1500);
          APP.batchTimers.push(timer);
          return;
        }
        stop();
        var panel = $('#batchPanel', hostEl);
        if (panel) {
          panel.innerHTML = '<div class="batch-head"><h2>批量运行</h2></div><div id="batchError"></div>';
          renderError($('#batchError', panel), err, function () { render(); }, '无法读取批次状态');
        }
      });
    }

    tick();
  }

  /* ================================================================== */
  /* 项目内运行                                                          */
  /* ================================================================== */
  function suiteRowHtml(suite) {
    var tags = Array.isArray(suite.tags) ? suite.tags : [];
    var envCount = suite.env && typeof suite.env === 'object' ? Object.keys(suite.env).length : 0;
    return '<label class="suite-row">' +
      '<input type="checkbox" name="suite" value="' + esc(suite.suite_id) + '" checked>' +
      '<span class="suite-main"><span class="suite-name">' + esc(suite.name || suite.suite_id) + '</span>' +
        '<span class="suite-scenario">' + esc(suite.scenario || '未配置场景') + '</span></span>' +
      '<span class="suite-tags">' + tags.slice(0, 3).map(function (t) { return '<span class="chip">' + esc(t) + '</span>'; }).join('') + '</span>' +
      '<span class="suite-env">' + (envCount ? ('env ' + envCount + ' 项') : '') + '</span>' +
    '</label>';
  }

  function renderRun(host, ctx, pid, query) {
    var batchId = query && query.batch ? String(query.batch) : '';
    host.innerHTML = loadingHtml('正在读取项目套件…');
    return getProject(pid).then(function (project) {
      if (!ctx.alive()) return;
      var suites = Array.isArray(project.suites) ? project.suites : [];
      var env = project.env && typeof project.env === 'object' ? project.env : {};
      var envEntries = Object.keys(env).sort().map(function (key) {
        var val = env[key];
        return {
          key: key,
          value: '',
          existing: false,
          placeholder: val === '***' ? '***（沿用）' : ('当前：' + stringifyMaybe(val))
        };
      });

      host.innerHTML = pageHead(project.name + ' · 运行',
        '只运行当前项目的套件；不会出现跨项目混跑。批量串行执行，单个套件失败不影响其余。',
        '<a class="btn" href="#/p/' + enc(pid) + '/history">运行历史</a>' +
        '<a class="btn btn-ghost" href="#/p/' + enc(pid) + '/settings">管理套件</a>') +
        '<div id="runBatchHost">' + (batchId ? '' : '') + '</div>' +
        (suites.length
          ? '<form id="runForm" class="stack">' +
              '<section class="panel">' +
                '<div class="panel-head"><h2>选择套件</h2>' +
                  '<span class="hint" id="suiteCount">已选 ' + suites.length + ' / ' + suites.length + '</span>' +
                  '<span class="row-end"></span>' +
                  '<button type="button" class="btn btn-sm" data-suite-all>全选</button>' +
                  '<button type="button" class="btn btn-sm" data-suite-none>清空</button>' +
                '</div>' +
                '<div class="suite-list" id="suiteList">' + suites.map(suiteRowHtml).join('') + '</div>' +
                '<div class="panel-foot"><button type="submit" class="btn btn-primary" id="runSubmit">▶ 运行所选套件</button>' +
                  '<span class="hint">按顺序串行执行；结果会汇总成一个批次。</span></div>' +
              '</section>' +
              '<section class="panel">' +
                '<div class="panel-head"><h2>本次环境变量覆盖</h2><span class="hint">留空沿用项目默认 / 套件默认；密钥显示为 ***</span></div>' +
                envEditorBlock({
                  entries: envEntries,
                  note: '优先级：套件 env > 项目 env > 本次填写；同名变量以项目/套件默认值为准。要改默认值请到「设置」。'
                }) +
              '</section>' +
            '</form>'
          : '<div class="empty hero-empty">' +
              '<div class="empty-mark" aria-hidden="true">⚙</div>' +
              '<strong>这个项目还没有套件</strong>' +
              '<p>先绑定一个场景文件（scenarios/...），之后才能运行。</p>' +
              '<div class="actions"><a class="btn btn-primary" href="#/p/' + enc(pid) + '/settings">去设置绑定套件</a></div>' +
            '</div>');

      if (batchId) {
        mountBatchPanel($('#runBatchHost', host), batchId, pid, {
          onDone: function () {
            loadProjects(true).catch(function () {});
            getProject(pid, true).catch(function () {});
          },
          signal: ctx.signal
        });
      }

      var form = $('#runForm', host);
      if (!form) return;
      var envEditor = mountEnvEditor($('[data-env-editor]', form), envEntries, { placeholder: '值' });
      var list = $('#suiteList', form);
      var countEl = $('#suiteCount', form);
      var submit = $('#runSubmit', form);

      function selected() {
        return $$('input[name="suite"]:checked', form).map(function (el) { return el.value; });
      }
      function updateCount() {
        var n = selected().length;
        if (countEl) countEl.textContent = '已选 ' + n + ' / ' + suites.length;
        if (submit) submit.textContent = '▶ 运行 ' + n + ' 个套件';
      }
      list.addEventListener('change', updateCount);
      var allBtn = $('[data-suite-all]', form);
      var noneBtn = $('[data-suite-none]', form);
      if (allBtn) allBtn.addEventListener('click', function () {
        $$('input[name="suite"]', form).forEach(function (el) { el.checked = true; });
        updateCount();
      });
      if (noneBtn) noneBtn.addEventListener('click', function () {
        $$('input[name="suite"]', form).forEach(function (el) { el.checked = false; });
        updateCount();
      });
      updateCount();

      form.addEventListener('submit', function (e) {
        e.preventDefault();
        var ids = selected();
        if (!ids.length) { toast('至少选择一个套件', 'error'); return; }
        if (submit) { submit.disabled = true; submit.innerHTML = '<span class="spinner" aria-hidden="true"></span>提交中…'; }
        startBatch(pid, ids, envEditor.collect({ existing: false })).then(function (res) {
          var bid = res && res.batch_id;
          if (!bid) throw new ApiError('服务端没有返回 batch_id', 500, 'bad_payload');
          go('#/p/' + enc(pid) + '/run?batch=' + enc(bid));
          toast('已提交 ' + ids.length + ' 个套件', 'ok');
        }).catch(function (err) {
          if (submit) { submit.disabled = false; submit.textContent = '▶ 运行 ' + ids.length + ' 个套件'; }
          toast('提交失败：' + (err && err.message ? err.message : '未知错误'), 'error');
        });
      });
    });
  }

  /* ================================================================== */
  /* 项目内历史                                                          */
  /* ================================================================== */
  function pageNumber(value) {
    var n = parseInt(value, 10);
    return (isFinite(n) && n > 0) ? n : 1;
  }

  function historyHash(pid, state) {
    var qs = [];
    if (state.suite) qs.push('suite=' + enc(state.suite));
    if (state.status && state.status !== 'all') qs.push('status=' + enc(state.status));
    if (state.page > 1) qs.push('page=' + enc(state.page));
    return '#/p/' + enc(pid) + '/history' + (qs.length ? '?' + qs.join('&') : '');
  }

  function historyRowHref(pid, item) {
    return '#/p/' + enc(pid) + '/runs/' + enc(item.run_id || '');
  }

  function historyColumns(project, pid) {
    return [
      {
        label: '套件',
        render: function (item) {
          return '<span class="cell-main">' + esc(runSuiteName(project, item)) + '</span>' +
            '<div class="cell-sub mono">' + esc(item.run_id || '') + '</div>';
        }
      },
      {
        label: '状态',
        render: function (item) { return badge(runOutcome(item).status); }
      },
      {
        label: '通过',
        cls: 'num',
        render: function (item) {
          var c = runCounts(item);
          var o = runOutcome(item);
          if (!c.total && o.total) c = { passed: o.passed, total: o.total };
          return c.total ? esc(c.passed + '/' + c.total) : '—';
        }
      },
      {
        label: '耗时',
        cls: 'num',
        render: function (item) { return esc(fmtDur(item.duration_ms)); }
      },
      {
        label: '开始时间',
        render: function (item) {
          var at = item.started_at || item.finished_at;
          return '<span class="nowrap">' + esc(fmtTs(at)) + '</span>' +
            '<div class="cell-sub">' + esc(at ? fmtRel(at) : '') + '</div>';
        }
      }
    ];
  }

  function renderHistory(host, ctx, pid, query) {
    host.innerHTML = loadingHtml('正在读取运行历史…');
    return getProject(pid).then(function (project) {
      if (!ctx.alive()) return;
      var state = {
        suite: query.suite || '',
        status: query.status || 'all',
        page: pageNumber(query.page)
      };
      if (['all', 'passed', 'failed', 'errored', 'done'].indexOf(state.status) < 0) state.status = 'all';
      if (state.suite && !SID_RE.test(state.suite)) state.suite = '';
      var suites = Array.isArray(project.suites) ? project.suites : [];
      var suiteOptions = suites.map(function (s) {
        return '<option value="' + esc(s.suite_id) + '"' + (s.suite_id === state.suite ? ' selected' : '') + '>' +
          esc(s.name || s.suite_id) + '</option>';
      }).join('');
      if (state.suite && !findSuite(project, state.suite)) {
        suiteOptions += '<option value="' + esc(state.suite) + '" selected>' + esc(state.suite) + '</option>';
      }

      host.innerHTML = pageHead(project.name + ' · 历史',
        '筛选条件写进 URL，刷新不丢。批量运行只包含当前项目。',
        '<button type="button" class="btn btn-primary" data-act="run-all" data-pid="' + esc(pid) + '">▶ 跑全部套件</button>' +
        '<a class="btn" href="#/p/' + enc(pid) + '/run">选择套件运行</a>') +
        '<div class="filter-bar">' +
          '<div class="field"><label for="hSuite">套件</label><select class="select" id="hSuite">' +
            '<option value="">全部套件</option>' + suiteOptions + '</select></div>' +
          '<div class="field"><label for="hStatus">状态</label><select class="select" id="hStatus">' +
            ['all=全部状态', 'passed=通过', 'failed=失败', 'errored=异常', 'done=已完成'].map(function (pair) {
              var parts = pair.split('=');
              return '<option value="' + parts[0] + '"' + (state.status === parts[0] ? ' selected' : '') + '>' + parts[1] + '</option>';
            }).join('') + '</select></div>' +
          '<div class="toolbar-spacer"></div>' +
          '<button type="button" class="btn btn-sm" id="hReload">刷新</button>' +
          '<button type="button" class="btn btn-sm btn-ghost" id="hClear">清除筛选</button>' +
        '</div>' +
        '<div id="historyBody">' + loadingHtml('正在读取运行历史…') + '</div>' +
        '<div id="historyPager" class="pagination"></div>';

      var body = $('#historyBody', host);
      var pager = $('#historyPager', host);
      var reloads = 0;
      var reqSeq = 0;

      function syncUrl() {
        var hash = historyHash(pid, state);
        try { history.replaceState(null, '', hash); } catch (e) { /* noop */ }
      }

      function load() {
        var mySeq = ++reqSeq;
        syncUrl();
        body.innerHTML = loadingHtml('正在读取运行历史…');
        var qs = ['project=' + enc(pid), 'offset=' + ((state.page - 1) * PAGE_SIZE), 'limit=' + PAGE_SIZE];
        if (state.suite) qs.push('suite=' + enc(state.suite));
        if (state.status && state.status !== 'all') qs.push('status=' + enc(state.status));
        apiGet(API + 'runs/query?' + qs.join('&')).then(function (payload) {
          if (!ctx.alive() || mySeq !== reqSeq) return;
          var items = payload && Array.isArray(payload.items) ? payload.items : [];
          var total = payload && typeof payload.total === 'number' ? payload.total : items.length;
          if (total > 0 && items.length === 0 && state.page > 1 && reloads < 1) {
            // 页码越界（例如筛选变化后），回到最后一页且只纠正一次
            reloads++;
            state.page = Math.max(1, Math.ceil(total / PAGE_SIZE));
            load();
            return;
          }
          reloads = 0;
          if (!items.length) {
            if (state.suite || (state.status && state.status !== 'all')) {
              body.innerHTML = emptyHtml('这个筛选条件没有运行',
                '换个套件或状态，或者清除筛选查看全部。',
                '<button type="button" class="btn btn-sm" id="hEmptyClear">清除筛选</button>');
              var clearBtn = $('#hEmptyClear', body);
              if (clearBtn) clearBtn.addEventListener('click', clearFilters);
            } else {
              body.innerHTML = emptyHtml('这个项目还没有运行记录',
                '绑定套件后跑一次，历史会按时间倒序出现在这里。',
                '<a class="btn btn-primary" href="#/p/' + enc(pid) + '/run">去运行</a>');
            }
            pager.innerHTML = '<span class="page-info">共 0 条</span>';
            return;
          }
          body.innerHTML = dataTable(historyColumns(project, pid), items, { rowHref: function (item) { return historyRowHref(pid, item); } });
          var pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
          pager.innerHTML = '<span class="page-info">第 ' + state.page + ' / ' + pages + ' 页 · 共 ' + total + ' 条</span>' +
            '<button type="button" class="page-btn" id="hPrev"' + (state.page <= 1 ? ' disabled' : '') + '>上一页</button>' +
            '<button type="button" class="page-btn" id="hNext"' + (state.page >= pages ? ' disabled' : '') + '>下一页</button>';
          var prev = $('#hPrev', pager);
          var next = $('#hNext', pager);
          if (prev) prev.addEventListener('click', function () {
            if (state.page > 1) { state.page--; load(); }
          });
          if (next) next.addEventListener('click', function () {
            if (state.page < pages) { state.page++; load(); }
          });
        }).catch(function (err) {
          if (!ctx.alive() || mySeq !== reqSeq) return;
          body.innerHTML = '<div id="historyError"></div>';
          renderError($('#historyError', body), err, load, '无法读取运行历史');
          pager.innerHTML = '';
        });
      }

      function clearFilters() {
        state.suite = '';
        state.status = 'all';
        state.page = 1;
        var s = $('#hSuite', host);
        var st = $('#hStatus', host);
        if (s) s.value = '';
        if (st) st.value = 'all';
        load();
      }

      var selSuite = $('#hSuite', host);
      var selStatus = $('#hStatus', host);
      if (selSuite) selSuite.addEventListener('change', function () {
        state.suite = selSuite.value;
        state.page = 1;
        load();
      });
      if (selStatus) selStatus.addEventListener('change', function () {
        state.status = selStatus.value;
        state.page = 1;
        load();
      });
      var reload = $('#hReload', host);
      if (reload) reload.addEventListener('click', load);
      var clear = $('#hClear', host);
      if (clear) clear.addEventListener('click', clearFilters);

      load();
    });
  }

  /* ================================================================== */
  /* 运行详情：失败证据 / 用例 / 步骤                                     */
  /* ================================================================== */
  function valOrMissing(value) {
    if (value === undefined || value === null || value === '') {
      return '<span class="missing">未提供</span>';
    }
    if (typeof value === 'object') {
      return '<pre class="json-block">' + esc(JSON.stringify(value, null, 2)) + '</pre>';
    }
    return esc(String(value));
  }

  function healEventsHtml(events, title) {
    if (!Array.isArray(events) || !events.length) return '';
    return '<div class="heal-block"><div class="hb-title">' + esc(title || '自愈事件') + '（' + events.length + '）</div>' +
      events.map(function (ev) {
        return '<div class="heal-item">' +
          '<div class="heal-line"><span class="mono">' + esc(ev.step_id || '未提供') + '</span>' +
            '<span class="chip">' + esc(ev.kind || 'unknown') + '</span>' +
            '<span class="chip">' + esc(ev.action || 'unknown') + '</span>' +
            (ev.success ? badge('passed') : badge('failed')) +
          '</div>' +
          '<div class="heal-reason">' + esc(ev.reason || '未提供原因') + '</div>' +
          (ev.suggestion ? '<div class="heal-suggest">建议：' + esc(ev.suggestion) + '</div>' : '') +
        '</div>';
      }).join('') + '</div>';
  }

  function stepDetailHtml(step) {
    var err = step.error || {};
    var failed = step.status === 'failed' || step.status === 'error';
    var msg = hasVal(step.message) ? step.message : err.message;
    var parts = [];
    if (failed) {
      parts.push('<div class="failure-block">' +
        '<div class="fb-title"><span aria-hidden="true">✕</span>失败原因' +
          '<button type="button" class="btn btn-sm fb-fix" data-fix-run="1">按此配置修正并重跑</button>' +
        '</div>' +
        '<dl class="kv-grid">' +
          '<dt>原因</dt><dd>' + valOrMissing(msg) + '</dd>' +
          '<dt>期望</dt><dd>' + valOrMissing(err.expected) + '</dd>' +
          '<dt>实际</dt><dd>' + valOrMissing(err.actual) + '</dd>' +
          '<dt>断言路径</dt><dd class="mono">' + valOrMissing(err.path) + '</dd>' +
          '<dt>错误类型</dt><dd class="mono">' + esc(err.type || err.kind || '未提供') + '</dd>' +
        '</dl></div>');
    }
    if (step.skipped_reason) {
      parts.push('<dl class="kv-grid"><dt>跳过原因</dt><dd>' + valOrMissing(step.skipped_reason) + '</dd></dl>');
    }
    if (step.heal && step.heal.length) parts.push(healEventsHtml(step.heal, '步骤自愈事件'));
    var inputKeys = step.inputs && typeof step.inputs === 'object' ? Object.keys(step.inputs).length : 0;
    var outputKeys = step.outputs && typeof step.outputs === 'object' ? Object.keys(step.outputs).length : 0;
    parts.push('<details class="raw"><summary>输入参数（' + inputKeys + ' 个键）</summary>' +
      '<pre class="json-block">' + esc(prettyJson(step.inputs || {})) + '</pre></details>');
    parts.push('<details class="raw"><summary>输出（' + outputKeys + ' 个键）</summary>' +
      '<pre class="json-block">' + esc(prettyJson(step.outputs || {})) + '</pre></details>');
    return parts.join('');
  }

  function stepHtml(step, idx) {
    step = step || {};
    var failed = step.status === 'failed' || step.status === 'error';
    var open = failed;
    var dur = typeof step.duration_ms === 'number' ? fmtMs(step.duration_ms) : '—';
    var attempts = typeof step.attempts === 'number' ? step.attempts : 1;
    var retries = typeof step.retries === 'number' ? step.retries : 0;
    return '<div class="step" data-step="' + idx + '" data-open="' + (open ? 'true' : 'false') + '">' +
      '<div class="step-head">' +
        '<span class="step-id">' + esc(step.step_id || ('步骤 ' + (idx + 1))) + '</span>' +
        '<span class="step-atom">' + esc(step.atom || '—') + '</span>' +
        badge(step.status) +
        '<span class="step-meta"><span>' + esc(dur) + '</span><span>尝试 ' + esc(String(attempts)) +
          (retries > 0 ? '（重试 ' + esc(String(retries)) + '）' : '') + '</span></span>' +
        '<button type="button" class="btn btn-sm" data-step-toggle aria-expanded="' + (open ? 'true' : 'false') + '">' +
          (open ? '收起 ▾' : '明细 ▸') + '</button>' +
      '</div>' +
      '<div class="step-body">' + stepDetailHtml(step) + '</div>' +
    '</div>';
  }

  function stepsHtml(caseObj) {
    var steps = Array.isArray(caseObj.steps) ? caseObj.steps : [];
    if (!steps.length) {
      return '<div class="empty" style="padding:16px"><strong>该用例没有步骤记录</strong><span>report.cases[].steps 为空。</span></div>';
    }
    return '<div class="step-list">' + steps.map(stepHtml).join('') + '</div>';
  }

  function caseCardHtml(caseObj, idx) {
    caseObj = caseObj || {};
    var steps = Array.isArray(caseObj.steps) ? caseObj.steps : [];
    var heal = Array.isArray(caseObj.heal_events) ? caseObj.heal_events : [];
    var failed = caseObj.status === 'failed' || caseObj.status === 'error';
    var open = failed;
    var errorBlock = '';
    if (caseObj.error) {
      var ce = caseObj.error;
      errorBlock = '<div class="failure-block"><div class="fb-title"><span aria-hidden="true">✕</span>用例级错误' +
        '<button type="button" class="btn btn-sm fb-fix" data-fix-run="1">按此配置修正并重跑</button></div>' +
        '<dl class="kv-grid">' +
          '<dt>原因</dt><dd>' + valOrMissing(ce.message || '未提供') + '</dd>' +
          '<dt>类型</dt><dd class="mono">' + esc(ce.type || ce.kind || '未提供') + '</dd>' +
        '</dl></div>';
    }
    return '<article class="case-card" data-open="' + (open ? 'true' : 'false') + '">' +
      '<button type="button" class="case-head" aria-expanded="' + (open ? 'true' : 'false') + '">' +
        '<span class="caret" aria-hidden="true">' + (open ? '▾' : '▸') + '</span>' +
        badge(caseObj.status) +
        '<span class="case-name">' + esc(caseObj.name || '(未命名用例)') + '</span>' +
        '<span class="case-id">' + esc(caseObj.case_id || '') + '</span>' +
        '<span class="case-meta"><span>' + steps.length + ' 步</span>' +
          '<span>' + esc(typeof caseObj.duration_ms === 'number' ? fmtMs(caseObj.duration_ms) : '—') + '</span>' +
          (heal.length ? '<span class="badge heal">自愈 ' + heal.length + ' 次</span>' : '') +
        '</span>' +
      '</button>' +
      '<div class="case-body">' + errorBlock + stepsHtml(caseObj) + healEventsHtml(heal, '用例自愈事件') + '</div>' +
    '</article>';
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

  function toggleStep(stepEl) {
    if (!stepEl) return;
    var open = stepEl.getAttribute('data-open') === 'true';
    stepEl.setAttribute('data-open', open ? 'false' : 'true');
    var btn = stepEl.querySelector('[data-step-toggle]');
    if (btn) {
      btn.setAttribute('aria-expanded', open ? 'false' : 'true');
      btn.textContent = open ? '明细 ▸' : '收起 ▾';
    }
  }

  function bindCaseInteractions(host, signal) {
    var opts = signal ? { signal: signal } : false;
    host.addEventListener('click', function (e) {
      var head = e.target.closest ? e.target.closest('.case-head') : null;
      if (head) { toggleCase(head); return; }
      var btn = e.target.closest ? e.target.closest('[data-step-toggle]') : null;
      if (btn) { toggleStep(btn.closest('.step')); return; }
      var stepHead = e.target.closest ? e.target.closest('.step-head') : null;
      if (stepHead) { toggleStep(stepHead.closest('.step')); }
    }, opts);
    host.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      if (e.target.closest && e.target.closest('button, a')) return;
      var stepEl = e.target.closest ? e.target.closest('.step-head') : null;
      if (stepEl) { e.preventDefault(); toggleStep(stepEl.closest('.step')); }
    }, opts);
  }

  function firstFailure(report) {
    var cases = (report && Array.isArray(report.cases)) ? report.cases : [];
    for (var i = 0; i < cases.length; i++) {
      var c = cases[i];
      if (!c || (c.status !== 'failed' && c.status !== 'error')) continue;
      var steps = Array.isArray(c.steps) ? c.steps : [];
      for (var j = 0; j < steps.length; j++) {
        var st = steps[j];
        if (!st || (st.status !== 'failed' && st.status !== 'error')) continue;
        var se = st.error || {};
        return {
          case: c.name || c.case_id || '',
          step: st.step_id || st.atom || '',
          message: st.message || se.message || '',
          expected: se.expected,
          actual: se.actual
        };
      }
      var ce = c.error || {};
      return {
        case: c.name || c.case_id || '',
        step: '',
        message: ce.message || '用例失败（报告未提供 message）',
        expected: ce.expected,
        actual: ce.actual
      };
    }
    var conclusion = (report && report.conclusion) || {};
    if (conclusion.success === false) {
      return {
        case: '',
        step: '',
        message: conclusion.verdict || '运行未通过，但报告没有提供失败用例明细',
        expected: null,
        actual: null
      };
    }
    return null;
  }

  function failureRowsHtml(f) {
    if (!f) return '<dt>错误</dt><dd>用例失败（报告未提供 message）</dd>';
    return '<dt>用例</dt><dd>' + valOrMissing(f.case) + '</dd>' +
      (f.step ? '<dt>步骤</dt><dd>' + valOrMissing(f.step) + '</dd>' : '') +
      '<dt>原因</dt><dd>' + valOrMissing(f.message) + '</dd>' +
      '<dt>期望</dt><dd>' + valOrMissing(f.expected) + '</dd>' +
      '<dt>实际</dt><dd>' + valOrMissing(f.actual) + '</dd>';
  }

  function fixRunPanelHtml(report) {
    var f = firstFailure(report);
    if (!f) return '';
    return '<div class="fix-panel" role="region" aria-label="失败修正入口">' +
      '<div class="fix-panel-main">' +
        '<div class="fix-kicker">这次运行没有通过</div>' +
        '<h2>先看错误，再按这套配置改完重跑</h2>' +
        '<dl class="kv-grid compact">' + failureRowsHtml(f) + '</dl>' +
      '</div>' +
      '<div class="fix-panel-actions">' +
        '<button type="button" class="btn btn-primary" data-fix-run="1">按此配置修正并重跑</button>' +
        '<span class="fix-panel-note">保留同一次运行的配置；可改环境变量后重跑。</span>' +
      '</div>' +
    '</div>';
  }

  function planPanelHtml(report) {
    var plan = (report && report.plan) || {};
    var warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
    var rows = '<div class="plan-row">' +
      '<span>计划来源</span><b class="mono">' + esc(plan.source || '未提供') + '</b>' +
      '<span>规划器</span><b class="mono">' + esc(plan.planner || '未提供') + '</b>' +
      '<span>用例</span><b class="mono">' + esc(String(plan.case_count === undefined ? '—' : plan.case_count)) + '</b>' +
      '<span>步骤</span><b class="mono">' + esc(String(plan.step_count === undefined ? '—' : plan.step_count)) + '</b>' +
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

  function stripCell(label, value) {
    var v = (value === undefined || value === null) ? 0 : value;
    return '<div class="cell"><dt>' + esc(label) + '</dt><dd>' + esc(String(v)) + '</dd></div>';
  }

  function detailHtml(data, id, pid, legacy) {
    var report = data.report || {};
    var summary = report.summary || {};
    var conclusion = report.conclusion || {};
    var cases = Array.isArray(report.cases) ? report.cases : [];
    var plan = report.plan || {};
    var meta = report.meta || {};
    var verdict = conclusion.verdict || (summary.success ? '通过' : '存在失败');
    var verdictOk = conclusion.success === true;
    var projectId = meta.project_id || '';
    var suiteId = meta.suite_id || '';
    var project = projectId ? APP.projectCache[projectId] : (pid ? APP.projectCache[pid] : null);
    var backHref = pid ? ('#/p/' + enc(pid) + '/history') : '#/projects';
    var backLabel = pid ? '返回历史' : '返回项目';

    var projectNotice = '';
    if (legacy) {
      projectNotice = '<div class="notice"><span aria-hidden="true">ⓘ</span><div>这是一次未归类运行（没有项目归属），只读展示。' +
        (projectId ? '归属项目 <a href="#/p/' + enc(projectId) + '/runs/' + enc(id) + '">' + esc(project && project.name ? project.name : projectId) + '</a>。' : '') +
        '</div></div>';
    } else if (projectId && projectId !== pid) {
      projectNotice = '<div class="notice"><span aria-hidden="true">ⓘ</span><div>该运行归属项目 <a href="#/p/' + enc(projectId) + '/runs/' + enc(id) + '">' +
        esc(project && project.name ? project.name : projectId) + '</a>，与当前地址中的项目不同。</div></div>';
    }

    var header = '<div class="detail-head">' +
      '<div class="detail-head-main">' +
        '<div class="title-line">' +
          '<h1>run ' + esc(id) + '</h1>' +
          '<span class="badge ' + (verdictOk ? 'ok' : 'bad') + '">' +
            '<span class="glyph" aria-hidden="true">' + (verdictOk ? '✓' : '✕') + '</span>' + esc(verdict) + '</span>' +
          (summary.healed_cases ? '<span class="badge heal">自愈 ' + esc(String(summary.healed_cases)) + ' 个用例</span>' : '') +
        '</div>' +
        '<div class="detail-meta">' +
          '<span>开始 <b>' + esc(fmtTs(report.started_at)) + '</b></span>' +
          '<span>结束 <b>' + esc(fmtTs(report.finished_at)) + '</b></span>' +
          '<span>耗时 <b>' + esc(fmtMs(report.duration_ms)) + '</b></span>' +
          '<span>计划来源 <b class="mono">' + esc(plan.source || '未提供') + '</b></span>' +
          '<span>规划器 <b class="mono">' + esc(plan.planner || '未提供') + '</b></span>' +
          (projectId ? '<span>项目 <b>' + esc(project && project.name ? project.name : projectId) + '</b></span>' : '<span>项目 <b>未归类</b></span>') +
          (suiteId ? '<span>套件 <b>' + esc(suiteLabel(project, suiteId)) + '</b></span>' : '') +
        '</div>' +
      '</div>' +
      '<div class="detail-actions">' +
        '<a class="btn btn-sm" href="' + API + 'runs/' + enc(id) + '/junit.xml" download="panguverse-' + esc(id) + '.xml">下载 JUnit</a>' +
        '<a class="btn btn-sm" href="' + API + 'runs/' + enc(id) + '/report.html" target="_blank" rel="noopener">打开 HTML 报告</a>' +
        '<a class="btn btn-sm btn-ghost" href="' + backHref + '">' + esc(backLabel) + '</a>' +
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
      : emptyHtml('该运行没有用例记录', 'report.json 中 cases 为空，请查看 HTML 报告或服务端日志。');

    return projectNotice + header + strip + insightBlockHtml() + fixRunPanelHtml(report) +
      '<section class="section"><div class="section-head"><h2>计划</h2><span class="hint">来自 report.plan</span></div>' +
        (Object.keys(plan).length ? planPanelHtml(report) : emptyHtml('没有计划信息', 'report.plan 为空。')) +
      '</section>' +
      '<section class="section"><div class="section-head"><h2>用例</h2>' +
        '<span class="hint">点击用例展开步骤；失败步骤默认展开，含 message / expected / actual</span></div>' +
        '<div class="case-list">' + caseHtml + '</div>' +
      '</section>';
  }

  function renderDetail(host, ctx, pid, rid, legacy) {
    var id = rid || '';
    if (!RUN_ID_RE.test(id)) {
      host.innerHTML = pageHead('无法打开运行详情', 'run_id 不合法') +
        errorHtml(new ApiError('run_id 不合法：只允许 6-32 位十六进制字符', 400, 'bad_request'), '无法打开运行详情');
      return;
    }
    host.innerHTML = loadingHtml('正在读取运行 ' + id + '…');
    return apiGet(API + 'runs/' + enc(id)).then(function (data) {
      if (!ctx.alive()) return;
      if (!data || typeof data !== 'object' || !data.report) {
        throw new ApiError('响应缺少 report 字段', 500, 'bad_response');
      }
      var meta = data.report.meta || {};
      if (meta.project_id && PID_RE.test(meta.project_id) && !APP.projectCache[meta.project_id]) {
        getProject(meta.project_id).catch(function () {});
      }
      host.innerHTML = detailHtml(data, id, pid, legacy);
      bindCaseInteractions(host, ctx.signal);
      initInsight(host, ctx, id);
      host.addEventListener('click', function (e) {
        var btn = e.target.closest ? e.target.closest('[data-fix-run]') : null;
        if (!btn) return;
        e.preventDefault();
        openRerunDialog(pid, id, data);
      }, ctx.signal ? { signal: ctx.signal } : false);
    }).catch(function (err) {
      if (!ctx.alive()) return;
      host.innerHTML = pageHead('run ' + id, '无法读取该运行',
        '<a class="btn" href="' + (pid ? '#/p/' + enc(pid) + '/history' : '#/projects') + '">返回</a>') +
        '<div id="detailError"></div>';
      renderError($('#detailError', host), err, function () { render(); }, '无法读取运行详情');
    });
  }

  /* ================================================================== */
  /* AI 总结（LLM 可选 + 规则兜底；来源必须诚实标注）                      */
  /* ================================================================== */
  var INSIGHT_PRIORITY = {
    P0: { label: '先修（阻断）' },
    P1: { label: '重要' },
    P2: { label: '改进' }
  };

  /* 最小 markdown：输入先 esc()，再按白名单拼接标签。
   * 不解析链接/图片/内联 HTML；summary_md 里的标签只作为文本显示。 */
  function insightInline(escaped) {
    var codes = [];
    var text = escaped.replace(/`([^`]+)`/g, function (_, code) {
      codes.push(code);
      return '\u0000' + (codes.length - 1) + '\u0000';
    });
    text = text
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
      .replace(/(^|[^\w*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
    return text.replace(/\u0000(\d+)\u0000/g, function (_, i) {
      return '<code>' + codes[Number(i)] + '</code>';
    });
  }

  function markdownHtml(src) {
    if (!src) return '';
    var lines = String(src).replace(/\r\n?/g, '\n').split('\n');
    var out = [];
    var lists = [];
    var para = [];
    var fence = null;
    var fenceBuf = [];

    function flushPara() {
      if (!para.length) return;
      out.push('<p>' + insightInline(esc(para.join(' '))) + '</p>');
      para = [];
    }
    function closeLists() {
      while (lists.length) out.push('</' + lists.pop() + '>');
    }
    function openList(tag) {
      if (lists.length && lists[lists.length - 1] === tag) return;
      closeLists();
      lists.push(tag);
      out.push('<' + tag + '>');
    }
    function flushFence() {
      out.push('<pre class="md-code"><code>' + esc(fenceBuf.join('\n')) + '</code></pre>');
      fence = null;
      fenceBuf = [];
    }

    lines.forEach(function (line) {
      if (fence !== null) {
        if (/^\s*```/.test(line)) flushFence();
        else fenceBuf.push(line);
        return;
      }
      var mFence = /^\s*```(\S*)\s*$/.exec(line);
      if (mFence) { flushPara(); closeLists(); fence = mFence[1] || 'code'; fenceBuf = []; return; }
      if (!line.trim()) { flushPara(); closeLists(); return; }
      if (/^\s{0,3}([-*_])\1{2,}\s*$/.test(line)) { flushPara(); closeLists(); out.push('<hr>'); return; }
      var mHead = /^\s{0,3}(#{1,6})\s+(.*)$/.exec(line);
      if (mHead) {
        flushPara(); closeLists();
        var level = Math.min(6, mHead[1].length + 2);
        out.push('<h' + level + '>' + insightInline(esc(mHead[2].replace(/\s+#+\s*$/, ''))) + '</h' + level + '>');
        return;
      }
      var mQuote = /^\s{0,3}>\s?(.*)$/.exec(line);
      if (mQuote) { flushPara(); closeLists(); out.push('<blockquote>' + insightInline(esc(mQuote[1])) + '</blockquote>'); return; }
      var mUl = /^\s*[-*+]\s+(.*)$/.exec(line);
      if (mUl) { flushPara(); openList('ul'); out.push('<li>' + insightInline(esc(mUl[1])) + '</li>'); return; }
      var mOl = /^\s*(\d+)[.)]\s+(.*)$/.exec(line);
      if (mOl) { flushPara(); openList('ol'); out.push('<li>' + insightInline(esc(mOl[2])) + '</li>'); return; }
      if (lists.length) closeLists();
      para.push(line);
    });

    if (fence !== null) flushFence();
    flushPara();
    closeLists();
    return out.join('');
  }

  function insightSourceMeta(payload) {
    var p = payload || {};
    if (p.source === 'llm') {
      var model = p.model ? String(p.model) : '';
      return {
        kind: 'llm',
        label: 'AI 模型总结' + (model ? '（' + model + '）' : '（模型未标注）'),
        title: 'AI 总结',
        model: model,
        glyph: '✦'
      };
    }
    if (p.source === 'rules') {
      return { kind: 'rules', label: '离线规则总结', title: '运行总结', model: '', glyph: '⚙' };
    }
    return { kind: 'unknown', label: '来源未标注', title: '运行总结', model: '', glyph: '?' };
  }

  function insightCasesHtml(ids) {
    if (!Array.isArray(ids) || !ids.length) return '';
    return '<div class="insight-cases">' + ids.map(function (id) {
      return '<span class="chip mono">' + esc(id) + '</span>';
    }).join('') + '</div>';
  }

  function insightStatsHtml(stats) {
    if (!stats || typeof stats !== 'object') return '';
    var rows = [
      ['用例总数', stats.total],
      ['通过', stats.passed],
      ['失败', stats.failed],
      ['异常', stats.errored],
      ['跳过', stats.skipped],
      ['自愈用例', stats.healed_cases],
      ['总耗时', (typeof stats.duration_ms === 'number') ? fmtMs(stats.duration_ms) : stats.duration_ms]
    ].filter(function (row) { return row[1] !== undefined && row[1] !== null; });
    if (!rows.length) return '';
    return '<div class="insight-stats-wrap"><table class="insight-stats">' +
      '<caption>统计核对 <span>与 report.summary 对应</span></caption><tbody>' +
      rows.map(function (row) {
        return '<tr><th scope="row">' + esc(row[0]) + '</th><td class="mono">' + esc(String(row[1])) + '</td></tr>';
      }).join('') +
    '</tbody></table></div>';
  }

  function insightFindingsHtml(findings) {
    if (!Array.isArray(findings) || !findings.length) return '';
    return '<details class="insight-findings-block">' +
      '<summary>关键发现（' + findings.length + '）<span>来自报告里的失败归类</span></summary>' +
      '<ul class="insight-findings">' + findings.map(function (f) {
        f = f || {};
        return '<li><div class="insight-finding-title"><span class="chip">' + esc(f.kind || 'finding') + '</span>' +
          esc(f.title || '未命名发现') + '</div>' +
          (f.detail ? '<div class="insight-finding-detail">' + esc(f.detail) + '</div>' : '') +
          insightCasesHtml(f.case_ids) + '</li>';
      }).join('') + '</ul></details>';
  }

  function insightSuggestionsHtml(suggestions) {
    if (!Array.isArray(suggestions) || !suggestions.length) {
      return '<div class="insight-none">这次没有可执行建议。</div>';
    }
    var groups = {};
    suggestions.forEach(function (s) {
      s = s || {};
      var key = String(s.priority || '其他').toUpperCase();
      if (!groups[key]) groups[key] = [];
      groups[key].push(s);
    });
    var rank = { P0: 0, P1: 1, P2: 2 };
    var keys = Object.keys(groups).sort(function (a, b) {
      var ra = rank[a] === undefined ? 9 : rank[a];
      var rb = rank[b] === undefined ? 9 : rank[b];
      if (ra !== rb) return ra - rb;
      return a < b ? -1 : (a > b ? 1 : 0);
    });
    return '<div class="insight-subhead"><h4>建议</h4><span class="hint">按优先级分组，附相关用例</span></div>' +
      keys.map(function (key) {
        var meta = INSIGHT_PRIORITY[key];
        var list = groups[key];
        return '<div class="insight-sugg-group">' +
          '<div class="insight-sugg-head">' +
            '<span class="prio prio-' + esc(key.toLowerCase()) + '">' + esc(key) + '</span>' +
            '<span class="insight-sugg-label">' + esc(meta ? meta.label : '其他') + '</span>' +
            '<span class="insight-sugg-count">' + list.length + ' 条</span>' +
          '</div><ul class="insight-sugg-list">' +
          list.map(function (s) {
            return '<li><div class="insight-sugg-title">' + esc(s.title || '未命名建议') + '</div>' +
              (s.detail ? '<div class="insight-sugg-detail">' + esc(s.detail) + '</div>' : '') +
              insightCasesHtml(s.case_ids) + '</li>';
          }).join('') +
          '</ul></div>';
      }).join('');
  }

  function insightPayloadHtml(payload) {
    var p = payload || {};
    var meta = insightSourceMeta(p);
    var out = '<div class="insight-source-line">' +
      '<span class="insight-source insight-source-' + meta.kind + '">' +
        '<span aria-hidden="true">' + meta.glyph + '</span> ' + esc(meta.label) + '</span>' +
      (meta.kind === 'rules' ? '<span class="insight-offline">未调用模型 · 由报告规则推导</span>' : '') +
      '<span class="insight-when">生成于 ' + esc(fmtTs(p.generated_at)) + '</span>' +
    '</div>';
    if (meta.kind === 'rules' && p.degraded_reason) {
      out += '<p class="insight-degraded"><strong>降级原因：</strong>' + esc(p.degraded_reason) + '</p>';
    }
    if (p.headline) out += '<h3 class="insight-headline">' + esc(p.headline) + '</h3>';
    out += '<div class="md-body">' + markdownHtml(p.summary_md || '') + '</div>';
    out += insightFindingsHtml(p.findings);
    out += insightStatsHtml(p.stats);
    out += insightSuggestionsHtml(p.suggestions);
    return out;
  }

  function insightBlockHtml() {
    return '<section class="insight-block" id="insightBlock" data-state="loading" aria-labelledby="insightTitle">' +
      '<div class="insight-head">' +
        '<div class="insight-head-main"><h2 id="insightTitle">AI 总结</h2>' +
          '<span class="insight-source" id="insightSource" hidden></span></div>' +
        '<div class="insight-actions" id="insightActions"></div>' +
      '</div>' +
      '<div class="insight-body" id="insightBody"><div class="insight-loading">' +
        '<span class="spinner" aria-hidden="true"></span>正在读取总结缓存…</div></div>' +
    '</section>';
  }

  function initInsight(host, ctx, id) {
    var block = $('#insightBlock', host);
    if (!block) return;
    var titleEl = $('#insightTitle', block);
    var sourceEl = $('#insightSource', block);
    var bodyEl = $('#insightBody', block);
    var actionsEl = $('#insightActions', block);
    var path = API + 'runs/' + enc(id) + '/insight';
    var state = { phase: 'loading', payload: null, error: null };
    var timer = null;
    var startedAt = 0;

    function clearTimer() {
      if (timer) { clearInterval(timer); timer = null; }
    }
    function elapsedText() { return ((Date.now() - startedAt) / 1000).toFixed(1) + ' s'; }
    function startTimer() {
      startedAt = Date.now();
      clearTimer();
      timer = setInterval(function () {
        if (!ctx.alive()) { clearTimer(); return; }
        $$('[data-insight-elapsed]', block).forEach(function (el) { el.textContent = elapsedText(); });
      }, 100);
    }
    if (ctx.signal && ctx.signal.addEventListener) ctx.signal.addEventListener('abort', clearTimer);

    function setSource(meta) {
      if (!meta) {
        sourceEl.hidden = true;
        sourceEl.className = 'insight-source';
        sourceEl.textContent = '';
        return;
      }
      sourceEl.hidden = false;
      sourceEl.className = 'insight-source insight-source-' + meta.kind;
      sourceEl.innerHTML = '<span aria-hidden="true">' + meta.glyph + '</span> ' + esc(meta.label);
    }

    function renderActions() {
      if (state.phase === 'loading') { actionsEl.innerHTML = '<span class="insight-elapsed">读取中…</span>'; return; }
      if (state.phase === 'generating') {
        actionsEl.innerHTML = '<button type="button" class="btn btn-sm" disabled>' +
          '<span class="spinner" aria-hidden="true"></span>生成中 <span class="insight-elapsed" data-insight-elapsed>0.0 s</span></button>';
        return;
      }
      if (state.payload) {
        actionsEl.innerHTML = '<button type="button" class="btn btn-sm btn-ghost" data-insight-copy>复制总结</button>' +
          '<button type="button" class="btn btn-sm" data-insight-refresh>重新生成</button>';
        return;
      }
      actionsEl.innerHTML = '<span class="insight-elapsed">尚未生成</span>';
    }

    function emptyHtml2() {
      return '<div class="insight-empty"><div><strong>还没有生成过总结</strong>' +
        '<p>总结会先尝试调用已配置的模型；没有可用密钥时自动降级为离线规则，并在来源处明确标注。结果缓存在运行目录，刷新页面不会重复消耗。</p></div>' +
        '<button type="button" class="btn btn-primary" data-insight-generate>生成总结</button></div>';
    }

    function insightErrorHtml2(err, canReload) {
      var msg = err && err.message ? err.message : '未知错误';
      var hint = '';
      if (err && err.status === 404) hint = '提示：当前后端尚未提供 AI 总结接口（GET/POST .../insight 返回 404）。';
      else if (err && (err.code === 'network' || err.status === 0)) hint = '提示：后端未启动或端口不通。';
      else if (err && err.status >= 500) hint = '提示：服务端生成异常，详细原因见服务端日志。';
      return '<div class="insight-error" role="alert">' +
        '<div class="insight-error-title"><span aria-hidden="true">⚠</span>总结生成失败</div>' +
        '<div class="insight-error-msg">' + esc(msg) + '</div>' +
        (hint ? '<div class="insight-error-hint">' + esc(hint) + '</div>' : '') +
        '<div class="insight-error-actions"><button type="button" class="btn btn-sm" data-insight-' +
          (canReload ? 'reload' : 'retry') + '>重试</button></div></div>';
    }

    function renderState() {
      block.setAttribute('data-state', state.phase);
      var p = state.payload;
      if (state.phase === 'loading') {
        block.removeAttribute('data-source');
        if (titleEl) titleEl.textContent = 'AI 总结';
        setSource(null);
        bodyEl.innerHTML = '<div class="insight-loading"><span class="spinner" aria-hidden="true"></span>正在读取总结缓存…</div>';
        renderActions();
        return;
      }
      if (state.phase === 'generating') {
        bodyEl.innerHTML = '<div class="insight-loading"><span class="spinner" aria-hidden="true"></span>正在生成总结…首次生成可能需要几秒' +
          '<span class="insight-elapsed" data-insight-elapsed>' + esc(elapsedText()) + '</span></div>';
        renderActions();
        return;
      }
      if (state.phase === 'empty') {
        block.removeAttribute('data-source');
        if (titleEl) titleEl.textContent = 'AI 总结';
        setSource(null);
        bodyEl.innerHTML = emptyHtml2();
        renderActions();
        return;
      }
      if (state.phase === 'ready' && p) {
        var meta = insightSourceMeta(p);
        block.setAttribute('data-source', meta.kind);
        if (titleEl) titleEl.textContent = meta.title;
        setSource(meta);
        bodyEl.innerHTML = insightPayloadHtml(p);
        renderActions();
        return;
      }
      if (p) {
        var pmeta = insightSourceMeta(p);
        block.setAttribute('data-source', pmeta.kind);
        if (titleEl) titleEl.textContent = pmeta.title;
        setSource(pmeta);
        bodyEl.innerHTML = insightErrorHtml2(state.error, false) + insightPayloadHtml(p);
      } else {
        block.removeAttribute('data-source');
        if (titleEl) titleEl.textContent = 'AI 总结';
        setSource(null);
        bodyEl.innerHTML = insightErrorHtml2(state.error, true);
      }
      renderActions();
    }

    function load() {
      state.phase = 'loading';
      state.payload = null;
      state.error = null;
      renderState();
      apiGet(path).then(function (resp) {
        if (!ctx.alive()) return;
        if (resp && resp.available === true) { state.phase = 'ready'; state.payload = resp; }
        else { state.phase = 'empty'; }
        renderState();
      }).catch(function (err) {
        if (!ctx.alive()) return;
        state.phase = 'error';
        state.error = err;
        renderState();
      });
    }

    function generate(refresh) {
      if (state.phase === 'generating') return;
      var previous = state.payload;
      state.phase = 'generating';
      state.error = null;
      block.removeAttribute('data-source');
      if (titleEl) titleEl.textContent = 'AI 总结';
      setSource(null);
      startTimer();
      renderState();
      apiPost(path, { refresh: !!refresh }).then(function (resp) {
        if (!ctx.alive()) return;
        clearTimer();
        if (!resp || resp.available !== true) {
          state.phase = 'error';
          state.payload = previous;
          state.error = new ApiError('服务端没有返回可用的总结（available=false）', 200, 'bad_payload');
          renderState();
          return;
        }
        state.phase = 'ready';
        state.payload = resp;
        renderState();
        toast(refresh ? '总结已重新生成' : '总结已生成', 'ok');
      }).catch(function (err) {
        if (!ctx.alive()) return;
        clearTimer();
        state.phase = 'error';
        state.payload = previous;
        state.error = err;
        renderState();
      });
    }

    function copySummary() {
      var p = state.payload;
      if (!p) return;
      var meta = insightSourceMeta(p);
      var lines = [];
      if (p.headline) lines.push(String(p.headline));
      if (p.summary_md) lines.push('', String(p.summary_md));
      var list = Array.isArray(p.suggestions) ? p.suggestions : [];
      if (list.length) {
        lines.push('', '## 建议');
        list.forEach(function (s) {
          s = s || {};
          lines.push('[' + (s.priority || '-') + '] ' + (s.title || '') + (s.detail ? '：' + s.detail : ''));
        });
      }
      lines.push('', '来源：' + meta.label + (p.degraded_reason ? ' · ' + p.degraded_reason : ''));
      copyToClipboard(lines.join('\n')).then(function () { toast('总结已复制到剪贴板', 'ok'); })
        .catch(function () { toast('复制失败：浏览器拒绝了剪贴板访问，可手动选中文本复制', 'error'); });
    }

    block.addEventListener('click', function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t.closest('[data-insight-generate]')) { e.preventDefault(); generate(false); return; }
      if (t.closest('[data-insight-refresh]')) { e.preventDefault(); generate(true); return; }
      if (t.closest('[data-insight-copy]')) { e.preventDefault(); copySummary(); return; }
      if (t.closest('[data-insight-retry]')) { e.preventDefault(); generate(false); return; }
      if (t.closest('[data-insight-reload]')) { e.preventDefault(); load(); return; }
    }, ctx.signal ? { signal: ctx.signal } : false);

    load();
  }

  /* ================================================================== */
  /* 修正重跑                                                            */
  /* ================================================================== */
  function openRerunDialog(pid, rid, data) {
    var report = (data && data.report) || {};
    var meta = report.meta || {};
    var failure = firstFailure(report);
    var projectId = meta.project_id || (pid && PID_RE.test(pid) ? pid : '');
    var suiteId = meta.suite_id || '';
    var useProject = !!(projectId && suiteId && PID_RE.test(projectId));
    var project = projectId ? APP.projectCache[projectId] : null;

    apiGet(API + 'runs/' + enc(rid) + '/config').catch(function () { return { env: {} }; }).then(function (cfg) {
      var env = (cfg && cfg.env && typeof cfg.env === 'object') ? cfg.env : {};
      var entries = Object.keys(env).sort().map(function (key) {
        var val = env[key];
        return {
          key: key,
          value: '',
          existing: false,
          placeholder: val === '***' ? '***（沿用）' : ('当前：' + stringifyMaybe(val))
        };
      });
      var destination = useProject
        ? '将作为项目「' + (project && project.name ? project.name : projectId) + '」的套件「' + suiteLabel(project, suiteId) + '」重新运行，结果自动归属项目。'
        : '这是一次未归类运行；重跑结果仍为未归类运行（可在运行详情里查看）。';

      openModal({
        title: '按此运行修正并重跑',
        subtitle: failure ? ('失败：' + (failure.case || '') + (failure.step ? ' / ' + failure.step : '')) : '',
        okLabel: '提交重跑',
        wide: true,
        bodyHtml:
          '<div class="modal-failure">' +
            '<div class="row" style="margin-bottom:8px"><b>失败证据</b>' +
              (failure ? '<span class="badge bad"><span class="glyph" aria-hidden="true">✕</span>未通过</span>' : '') +
            '</div>' +
            '<dl class="kv-grid">' + failureRowsHtml(failure) + '</dl>' +
          '</div>' +
          '<div class="field"><span class="field-label">环境变量修正（留空沿用原运行/项目默认）</span>' +
            envEditorBlock({
              entries: entries,
              note: useProject
                ? '只发送填写的值；同名变量以套件/项目默认值为准，要改默认值请先在「设置」里修改。'
                : '只发送填写的值；密钥原值不会显示。'
            }) +
          '</div>' +
          '<div class="notice info"><span aria-hidden="true">ⓘ</span><div>' + esc(destination) + '</div></div>',
        onMount: function (rootEl) {
          var editor = mountEnvEditor($('[data-env-editor]', rootEl), entries, { placeholder: '值' });
          rootEl._envEditor = editor;
        },
        onSubmit: function (rootEl) {
          var overrides = rootEl._envEditor ? rootEl._envEditor.collect({ existing: false }) : {};
          if (useProject) {
            return startBatch(projectId, [suiteId], overrides).then(function (res) {
              var bid = res && res.batch_id;
              if (!bid) throw new ApiError('服务端没有返回 batch_id', 500, 'bad_payload');
              setTimeout(function () { go('#/p/' + enc(projectId) + '/run?batch=' + enc(bid)); }, 0);
              toast('已提交修正后的运行', 'ok');
              return true;
            });
          }
          return apiPost(API + 'runs/' + enc(rid) + '/rerun', { env: overrides }).then(function (res) {
            var jobId = res && res.job_id;
            if (!jobId) throw new ApiError('服务端没有返回 job_id', 500, 'bad_payload');
            setTimeout(function () {
              watchJob(jobId, {
                label: '修正重跑',
                hrefFor: function (job) { return job && job.run_id ? '#/runs/' + enc(job.run_id) : ''; }
              });
            }, 0);
            toast('已提交重跑任务', 'ok');
            return true;
          });
        }
      });
    }).catch(function (err) {
      toast('无法读取运行配置：' + (err && err.message ? err.message : '未知错误'), 'error');
    });
  }

  /* ================================================================== */
  /* 后台任务浮层                                                        */
  /* ================================================================== */
  function stopJobWatcher(hide) {
    if (APP.job.timer) { clearTimeout(APP.job.timer); APP.job.timer = null; }
    if (APP.job.hideTimer) { clearTimeout(APP.job.hideTimer); APP.job.hideTimer = null; }
    APP.job.id = null;
    var dock = $('#jobDock');
    if (dock && hide !== false) { dock.hidden = true; dock.innerHTML = ''; }
  }

  function jobHref(job, opts) {
    if (opts && typeof opts.hrefFor === 'function') return opts.hrefFor(job);
    return job && job.run_id ? '#/runs/' + enc(job.run_id) : '';
  }

  function renderJobDock(job, opts, error) {
    var dock = $('#jobDock');
    if (!dock) return;
    dock.hidden = false;
    var status = job ? job.status : (error ? 'failed' : 'queued');
    var done = status === 'done' || status === 'failed' || !!error;
    var href = jobHref(job, opts);
    var phases = (job && Array.isArray(job.phases)) ? job.phases.slice(-5) : [];
    dock.innerHTML = '<div class="job-card">' +
      '<div class="job-head">' +
        (done ? '' : '<span class="spinner" aria-hidden="true"></span>') +
        '<b>' + esc((opts && opts.label) || '后台任务') + '</b>' +
        jobBadge(status) +
        '<button type="button" class="btn btn-icon" data-act="job-close" aria-label="关闭任务面板">✕</button>' +
      '</div>' +
      '<div class="job-body">' +
        (error
          ? '<div class="error-msg">' + esc(error) + '</div>'
          : '<div class="job-status-line">' + esc(done ? (status === 'done' ? '任务已完成' : '任务失败') : '正在执行，可继续浏览其他页面…') + '</div>' +
            (job && job.summary ? '<div class="hint">用例 ' + esc(String(num(job.summary.passed))) + '/' + esc(String(num(job.summary.total))) + '</div>' : '') +
            (job && job.error ? '<div class="error-msg">' + esc(job.error) + '</div>' : '')) +
        (phases.length
          ? '<div class="job-phases">' + phases.map(function (ph) {
              return '<span>' + esc(ph.name || '') + ' · ' + esc(ph.status || '') + (ph.detail ? ' — ' + esc(ph.detail) : '') + '</span>';
            }).join('') + '</div>'
          : '') +
        (href ? '<a class="btn btn-sm btn-primary" href="' + href + '">查看运行详情</a>' : '') +
      '</div>' +
    '</div>';
  }

  function watchJob(jobId, opts) {
    stopJobWatcher();
    opts = opts || {};
    APP.job.id = jobId;
    if (APP.job.hideTimer) { clearTimeout(APP.job.hideTimer); APP.job.hideTimer = null; }
    var finished = false;
    renderJobDock({ status: 'queued', job_id: jobId }, opts);

    function tick() {
      if (APP.job.id !== jobId) return;
      apiGet(API + 'jobs/' + enc(jobId)).then(function (job) {
        if (APP.job.id !== jobId) return;
        if (!job) throw new ApiError('job 不存在', 404, 'not_found');
        renderJobDock(job, opts);
        if (job.status === 'done' || job.status === 'failed') {
          APP.job.timer = null;
          if (!finished) {
            finished = true;
            if (typeof opts.onDone === 'function') opts.onDone(job);
            toast(job.status === 'done' ? '任务已完成' : '任务失败，请查看详情', job.status === 'done' ? 'ok' : 'error');
            // 完成后再停留 12 秒，避免在手机/详情页长期遮挡内容；可手动关闭
            APP.job.hideTimer = setTimeout(function () {
              if (APP.job.id === jobId) stopJobWatcher();
            }, 12000);
          }
          return;
        }
        APP.job.timer = setTimeout(tick, 1200);
      }).catch(function (err) {
        if (APP.job.id !== jobId) return;
        APP.job.timer = null;
        renderJobDock(null, opts, err && err.status === 404 ? '任务记录已丢失（后端可能重启过）' : ('读取任务失败：' + (err && err.message ? err.message : '未知错误')));
      });
    }

    // 首次延迟一点，给提交接口落库留出时间
    APP.job.timer = setTimeout(tick, 500);
  }

  /* ================================================================== */
  /* 项目设置（基本信息 + 套件管理）                                      */
  /* ================================================================== */
  function loadScenarios(force) {
    if (APP.scenarios && !force) return Promise.resolve(APP.scenarios);
    return apiGet(API + 'scenarios').then(function (list) {
      APP.scenarios = Array.isArray(list) ? list : [];
      return APP.scenarios;
    });
  }

  function scenarioOptionsHtml(list, current) {
    var out = '<option value="">选择场景文件…</option>';
    var found = false;
    (list || []).forEach(function (s) {
      if (!s || !s.id) return;
      if (s.id === current) found = true;
      var label = (s.name || s.id) + ' · ' + s.id + (s.source === 'builtin' ? '（内置）' : '') + (s.error ? '（解析失败）' : '');
      out += '<option value="' + esc(s.id) + '"' + (s.id === current ? ' selected' : '') + (s.error ? ' disabled' : '') + '>' + esc(label) + '</option>';
    });
    if (current && !found) {
      out += '<option value="' + esc(current) + '" selected>' + esc(current) + '（当前值）</option>';
    }
    return out;
  }

  function suiteAdminRowHtml(suite) {
    var tags = Array.isArray(suite.tags) ? suite.tags : [];
    var envCount = suite.env && typeof suite.env === 'object' ? Object.keys(suite.env).length : 0;
    return '<div class="suite-admin-row" data-suite-id="' + esc(suite.suite_id) + '">' +
      '<div class="suite-admin-main"><span class="suite-admin-name">' + esc(suite.name || suite.suite_id) + '</span>' +
        '<span class="suite-scenario">' + esc(suite.scenario || '未配置场景') + '</span>' +
        '<span class="chips">' + tags.map(function (t) { return '<span class="chip">' + esc(t) + '</span>'; }).join('') +
          (envCount ? '<span class="chip">env ' + envCount + ' 项</span>' : '') + '</span></div>' +
      '<div class="suite-admin-actions">' +
        '<button type="button" class="btn btn-sm" data-suite-edit="' + esc(suite.suite_id) + '">编辑</button>' +
        '<button type="button" class="btn btn-sm btn-ghost" data-suite-del="' + esc(suite.suite_id) + '">删除</button>' +
      '</div>' +
    '</div>';
  }

  function renderSettings(host, ctx, pid) {
    host.innerHTML = loadingHtml('正在读取项目设置…');
    return getProject(pid, true).then(function (project) {
      if (!ctx.alive()) return;
      var suites = Array.isArray(project.suites) ? project.suites : [];
      var env = project.env && typeof project.env === 'object' ? project.env : {};
      var entries = envEntriesFromObject(env, true);

      host.innerHTML = pageHead(project.name + ' · 设置',
        '项目保存站点与默认环境；套件把场景文件绑定到项目。删除套件或项目都不会删除历史运行。',
        '<a class="btn" href="#/p/' + enc(pid) + '/overview">返回概览</a>') +
        '<div class="settings-grid">' +
          '<section class="panel">' +
            '<div class="panel-head"><h2>基本信息</h2></div>' +
            '<form id="projectSettingsForm" class="stack">' +
              '<div class="field"><label for="sName">项目名称</label>' +
                '<input class="input" id="sName" value="' + esc(project.name || '') + '" maxlength="80" autocomplete="off"></div>' +
              '<div class="field"><label for="sBase">站点地址 base_url</label>' +
                '<input class="input mono" id="sBase" value="' + esc(project.base_url || '') + '" autocomplete="off" spellcheck="false"></div>' +
              '<div class="field"><span class="field-label">项目默认环境变量</span>' +
                envEditorBlock({ entries: entries, note: '密钥以 *** 返回；留空沿用，点 ✕ 删除该键。套件 env 会覆盖同名项目变量。' }) +
              '</div>' +
              '<div class="form-actions"><button type="submit" class="btn btn-primary" id="sSave">保存设置</button>' +
                '<span class="hint" id="sSaveHint"></span></div>' +
            '</form>' +
          '</section>' +
          '<section class="panel">' +
            '<div class="panel-head"><h2>套件</h2><span class="hint">' + suites.length + ' 个</span>' +
              '<button type="button" class="btn btn-sm btn-primary row-end" data-act="new-suite" data-pid="' + esc(pid) + '">＋ 绑定套件</button></div>' +
            (suites.length
              ? '<div class="suite-admin-list" id="suiteAdminList">' + suites.map(suiteAdminRowHtml).join('') + '</div>'
              : emptyHtml('还没有绑定套件', '绑定一个场景文件（scenarios/...）后即可在项目内运行。',
                  '<button type="button" class="btn btn-primary" data-act="new-suite" data-pid="' + esc(pid) + '">绑定套件</button>')) +
          '</section>' +
        '</div>' +
        '<section class="panel danger-zone" style="margin-top:16px">' +
          '<div class="panel-head"><h2>危险操作</h2></div>' +
          '<div class="danger-row"><p>删除项目只删除项目配置与套件绑定，<b>不会删除任何历史运行</b>。此操作不可撤销。</p>' +
            '<button type="button" class="btn btn-danger" data-act="delete-project" data-pid="' + esc(pid) + '">删除项目</button></div>' +
        '</section>';

      var form = $('#projectSettingsForm', host);
      if (form) {
        var editor = mountEnvEditor($('[data-env-editor]', form), entries, { placeholder: '值' });
        form.addEventListener('submit', function (e) {
          e.preventDefault();
          var nameEl = $('#sName', form);
          var baseEl = $('#sBase', form);
          var save = $('#sSave', form);
          var hint = $('#sSaveHint', form);
          var name = nameEl ? nameEl.value.trim() : '';
          if (!name) {
            if (nameEl) nameEl.focus();
            if (hint) hint.textContent = '项目名称不能为空';
            return;
          }
          var envPatch = editor.collect({ existing: true });
          if (save) { save.disabled = true; save.innerHTML = '<span class="spinner" aria-hidden="true"></span>保存中…'; }
          if (hint) hint.textContent = '';
          apiPatch(API + 'projects/' + enc(pid), { name: name, base_url: baseEl ? baseEl.value.trim() : '', env: envPatch }).then(function (updated) {
            if (!ctx.alive()) return;
            var merged = updated && updated.project_id ? updated : project;
            if (!merged.suites) merged.suites = suites;
            APP.projectCache[pid] = merged;
            APP.projectsPromise = null;
            loadProjects(true).catch(function () {});
            renderTopbar(parseRoute());
            if (save) { save.disabled = false; save.textContent = '保存设置'; }
            if (hint) hint.textContent = '已保存';
            toast('项目设置已保存', 'ok');
          }).catch(function (err) {
            if (save) { save.disabled = false; save.textContent = '保存设置'; }
            if (hint) hint.textContent = '保存失败：' + (err && err.message ? err.message : '未知错误');
          });
        });
      }

      var list = $('#suiteAdminList', host);
      if (list) {
        list.addEventListener('click', function (e) {
          var edit = e.target.closest ? e.target.closest('[data-suite-edit]') : null;
          var del = e.target.closest ? e.target.closest('[data-suite-del]') : null;
          if (edit) {
            var sid = edit.getAttribute('data-suite-edit');
            var suite = findSuite(project, sid);
            if (suite) openSuiteModal(pid, project, suite);
            return;
          }
          if (del) {
            var dsid = del.getAttribute('data-suite-del');
            var dsuite = findSuite(project, dsid);
            if (!dsuite) return;
            confirmDialog({
              title: '删除套件',
              bodyHtml: '确定解除套件「' + esc(dsuite.name || dsid) + '」与项目的绑定？<br>历史运行不会被删除。',
              okLabel: '删除套件',
              danger: true
            }).then(function (ok) {
              if (!ok) return;
              apiDelete(API + 'projects/' + enc(pid) + '/suites/' + enc(dsid)).then(function () {
                invalidateProject(pid);
                loadProjects(true).catch(function () {});
                toast('套件已删除', 'ok');
                render();
              }).catch(function (err) {
                toast('删除失败：' + (err && err.message ? err.message : '未知错误'), 'error');
              });
            });
          }
        });
      }
    });
  }

  function openSuiteModal(pid, project, suite) {
    var editing = !!suite;
    loadScenarios().then(function (scenarios) {
      var env = editing && suite.env && typeof suite.env === 'object' ? suite.env : {};
      var entries = envEntriesFromObject(env, true);
      var tags = editing && Array.isArray(suite.tags) ? suite.tags.join(', ') : '';
      openModal({
        title: editing ? '编辑套件' : '绑定套件',
        subtitle: '套件把场景文件与项目绑定；env 覆盖项目默认。',
        okLabel: editing ? '保存套件' : '绑定套件',
        wide: true,
        bodyHtml:
          '<div class="form-grid">' +
            '<div class="field"><label for="suiteName">套件名称</label>' +
              '<input class="input" id="suiteName" value="' + esc(editing ? (suite.name || '') : '') + '" placeholder="例如 站点只读巡检" maxlength="80" autocomplete="off"></div>' +
            '<div class="field"><label for="suiteTags">标签（逗号分隔）</label>' +
              '<input class="input" id="suiteTags" value="' + esc(tags) + '" placeholder="smoke, read-only" autocomplete="off"></div>' +
          '</div>' +
          '<div class="field"><label for="suiteScenario">场景文件</label>' +
            '<select class="select mono" id="suiteScenario">' + scenarioOptionsHtml(scenarios, editing ? suite.scenario : '') + '</select>' +
            '<span class="hint">来自 workspace/scenarios 或内置场景；越界路径会被后端拒绝。</span></div>' +
          '<div class="field"><span class="field-label">套件环境变量（覆盖项目默认）</span>' +
            envEditorBlock({ entries: entries, note: editing ? '已有密钥显示为 ***；留空沿用，点 ✕ 删除该键。' : '可选。这里的同名变量会覆盖项目默认。' }) + '</div>',
        onMount: function (rootEl) {
          var editor = mountEnvEditor($('[data-env-editor]', rootEl), entries, { placeholder: '值' });
          rootEl._envEditor = editor;
        },
        onSubmit: function (rootEl) {
          var nameEl = $('#suiteName', rootEl);
          var scenarioEl = $('#suiteScenario', rootEl);
          var tagsEl = $('#suiteTags', rootEl);
          var name = nameEl ? nameEl.value.trim() : '';
          var scenario = scenarioEl ? scenarioEl.value : '';
          if (!name) { if (nameEl) nameEl.focus(); return Promise.reject(new ApiError('请填写套件名称', 0, 'validation')); }
          if (!scenario) { if (scenarioEl) scenarioEl.focus(); return Promise.reject(new ApiError('请选择场景文件', 0, 'validation')); }
          var tagsValue = tagsEl ? tagsEl.value.split(/[,，\s]+/).map(function (t) { return t.trim(); }).filter(Boolean) : [];
          var body = {
            name: name,
            scenario: scenario,
            tags: tagsValue,
            env: rootEl._envEditor ? rootEl._envEditor.collect({ existing: true }) : {}
          };
          var req = editing
            ? apiPatch(API + 'projects/' + enc(pid) + '/suites/' + enc(suite.suite_id), body)
            : apiPost(API + 'projects/' + enc(pid) + '/suites', body);
          return req.then(function () {
            invalidateProject(pid);
            loadProjects(true).catch(function () {});
            setTimeout(render, 0);
            toast(editing ? '套件已更新' : '套件已绑定', 'ok');
            return true;
          });
        }
      });
    }).catch(function (err) {
      toast('无法读取场景列表：' + (err && err.message ? err.message : '未知错误'), 'error');
    });
  }

  /* ================================================================== */
  /* 平台 · 原子库                                                       */
  /* ================================================================== */
  function uniq(list) {
    var seen = {};
    var out = [];
    (list || []).forEach(function (v) {
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
    (atoms || []).forEach(function (a) {
      if (a && Array.isArray(a.tags)) tags = tags.concat(a.tags);
    });
    return uniq(tags);
  }

  function optionList(values, current, allLabel) {
    return '<option value="">' + esc(allLabel || '全部') + '</option>' + (values || []).map(function (v) {
      return '<option value="' + esc(v) + '"' + (v === current ? ' selected' : '') + '>' + esc(v) + '</option>';
    }).join('');
  }

  function portsTable(ports, emptyText) {
    if (!Array.isArray(ports) || !ports.length) {
      return '<div class="field-hint">' + esc(emptyText || '无端口') + '</div>';
    }
    return '<table class="ports"><thead><tr><th>端口</th><th>类型</th><th>必填</th></tr></thead><tbody>' +
      ports.map(function (p) {
        return '<tr><td class="mono">' + esc(p.name || '—') + '</td><td class="mono">' + esc(p.type || '—') + '</td>' +
          '<td>' + (p.required ? '<span class="req-yes">必填</span>' : '<span class="req-no">可选</span>') + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  function atomDetailHtml(atom) {
    var effects = Array.isArray(atom.effects) && atom.effects.length
      ? atom.effects.map(function (e) { return '<span class="chip">' + esc(e) + '</span>'; }).join('')
      : '<span class="missing">未提供</span>';
    var required = Array.isArray(atom.required) && atom.required.length
      ? atom.required.map(function (r) { return '<span class="chip mono">' + esc(r) + '</span>'; }).join('')
      : '<span class="missing">无必填输入</span>';
    var aliases = Array.isArray(atom.aliases) && atom.aliases.length
      ? atom.aliases.map(function (a) { return '<span class="chip mono">' + esc(a) + '</span>'; }).join('')
      : '<span class="missing">未提供</span>';
    var tags = Array.isArray(atom.tags) && atom.tags.length
      ? atom.tags.map(function (t) { return '<span class="chip">' + esc(t) + '</span>'; }).join('')
      : '<span class="missing">未提供</span>';
    return '<div class="atom-detail">' +
      '<div class="field"><span class="field-label">描述</span><p>' + esc(atom.description || '未提供描述') + '</p></div>' +
      '<div class="field"><span class="field-label">元信息</span><div class="chips">' +
        '<span class="chip">分类 ' + esc(atom.category || '—') + '</span>' +
        '<span class="chip">领域 ' + esc(atom.domain || '—') + '</span>' +
        '<span class="chip">包 ' + esc(atom.pack || '—') + '</span>' +
        '<span class="chip">版本 ' + esc(atom.version || '—') + '</span>' +
      '</div></div>' +
      '<div class="field"><span class="field-label">标签</span><div class="chips">' + tags + '</div></div>' +
      '<div class="field"><span class="field-label">输入端口</span>' + portsTable(atom.input_ports, '该原子没有声明输入端口') + '</div>' +
      '<div class="field"><span class="field-label">输出端口</span>' + portsTable(atom.output_ports, '该原子没有声明输出端口') + '</div>' +
      '<div class="field"><span class="field-label">副作用 effects</span><div class="chips">' + effects + '</div></div>' +
      '<div class="field"><span class="field-label">必填输入</span><div class="chips">' + required + '</div></div>' +
      '<div class="field"><span class="field-label">别名</span><div class="chips">' + aliases + '</div></div>' +
      '<details class="raw"><summary>inputs 契约（原始）</summary><pre class="json-block">' + esc(prettyJson(atom.inputs || {})) + '</pre></details>' +
      '<details class="raw"><summary>outputs 契约（原始）</summary><pre class="json-block">' + esc(prettyJson(atom.outputs || {})) + '</pre></details>' +
    '</div>';
  }

  function atomsTableHtml(list) {
    if (!list.length) {
      return emptyHtml('当前条件没有匹配的原子', '清空 domain / tag / category 或搜索词后重试。',
        '<button type="button" class="btn btn-sm" data-act="atoms-reset">清除筛选</button>');
    }
    return dataTable([
      { label: '原子', render: function (a) { return '<span class="atom-name">' + esc(a.name || '—') + '</span>'; } },
      { label: '分类', render: function (a) { return esc(a.category || '—'); } },
      { label: '领域', render: function (a) { return esc(a.domain || '—'); } },
      { label: '必填输入', render: function (a) {
          var req = Array.isArray(a.required) && a.required.length ? a.required.join(', ') : '—';
          return '<span class="mono">' + esc(req) + '</span>';
        } },
      { label: '标签', render: function (a) {
          var tags = Array.isArray(a.tags) ? a.tags : [];
          return tags.length ? tags.slice(0, 5).map(function (t) { return '<span class="chip">' + esc(t) + '</span>'; }).join(' ') : '—';
        } }
    ], list, {
      rowHref: function (a) { return '#atom:' + (a.name || ''); }
    });
  }

  function renderAtoms(host, ctx) {
    host.innerHTML = pageHead('原子库', '原子是可编排的最小动作；点击任意一行查看输入/输出端口契约与副作用。') +
      '<div class="filter-bar">' +
        '<div class="field"><label for="filterDomain">domain</label><select class="select" id="filterDomain"></select></div>' +
        '<div class="field"><label for="filterTag">tag</label><select class="select" id="filterTag"></select></div>' +
        '<div class="field"><label for="filterCategory">category</label><select class="select" id="filterCategory"></select></div>' +
        '<label class="search" aria-label="搜索原子"><span aria-hidden="true">⌕</span>' +
          '<input type="search" data-search="atoms" placeholder="搜索名称或描述，按 / 聚焦" autocomplete="off"></label>' +
        '<span class="filter-count" id="atomCount">—</span>' +
      '</div>' +
      '<div id="atomBody">' + loadingHtml('正在读取原子库…') + '</div>';

    var body = $('#atomBody', host);
    var filters = APP.atomFilters;
    var query = '';
    var current = [];

    function apply(list) {
      var q = query.trim().toLowerCase();
      current = (list || []).filter(function (a) {
        if (filters.domain && a.domain !== filters.domain) return false;
        if (filters.tag && (!Array.isArray(a.tags) || a.tags.indexOf(filters.tag) < 0)) return false;
        if (filters.category && a.category !== filters.category) return false;
        if (q) {
          var hay = ((a.name || '') + ' ' + (a.description || '') + ' ' + (Array.isArray(a.tags) ? a.tags.join(' ') : '')).toLowerCase();
          if (hay.indexOf(q) < 0) return false;
        }
        return true;
      });
      body.innerHTML = atomsTableHtml(current);
      var count = $('#atomCount', host);
      if (count) count.textContent = '共 ' + current.length + ' 个原子';
    }

    apiGet(API + 'atoms').then(function (atoms) {
      if (!ctx.alive()) return;
      var list = Array.isArray(atoms) ? atoms : [];
      APP.atoms = list;
      var dSel = $('#filterDomain', host);
      var tSel = $('#filterTag', host);
      var cSel = $('#filterCategory', host);
      if (dSel) dSel.innerHTML = optionList(uniq(list.map(function (a) { return a.domain; })), filters.domain, '全部领域');
      if (tSel) tSel.innerHTML = optionList(collectTags(list), filters.tag, '全部标签');
      if (cSel) cSel.innerHTML = optionList(uniq(list.map(function (a) { return a.category; })), filters.category, '全部分类');
      function onChange() {
        filters.domain = dSel ? dSel.value : '';
        filters.tag = tSel ? tSel.value : '';
        filters.category = cSel ? cSel.value : '';
        apply(list);
      }
      if (dSel) dSel.addEventListener('change', onChange);
      if (tSel) tSel.addEventListener('change', onChange);
      if (cSel) cSel.addEventListener('change', onChange);
      apply(list);
    }).catch(function (err) {
      if (!ctx.alive()) return;
      body.innerHTML = '<div id="atomError"></div>';
      renderError($('#atomError', body), err, function () { renderAtoms(host, ctx); }, '无法读取原子库');
    });

    var search = $('[data-search="atoms"]', host);
    if (search) search.addEventListener('input', function () {
      query = search.value;
      apply(APP.atoms || []);
    });

    host.addEventListener('click', function (e) {
      var reset = e.target.closest ? e.target.closest('[data-act="atoms-reset"]') : null;
      if (reset) {
        filters.domain = '';
        filters.tag = '';
        filters.category = '';
        query = '';
        $$('.filter-bar select', host).forEach(function (s) { s.value = ''; });
        if (search) search.value = '';
        apply(APP.atoms || []);
        return;
      }
      var row = e.target.closest ? e.target.closest('tr[data-href^="#atom:"]') : null;
      if (!row) return;
      var name = row.getAttribute('data-href').slice(6);
      var atom = (APP.atoms || []).filter(function (a) { return a.name === name; })[0];
      if (!atom) return;
      openModal({ title: atom.name || '原子', subtitle: atom.category || '', okLabel: null, cancelLabel: '关闭', wide: true, bodyHtml: atomDetailHtml(atom) });
    }, ctx.signal ? { signal: ctx.signal } : false);
  }

  /* ================================================================== */
  /* 平台 · 领域包                                                       */
  /* ================================================================== */
  function packCardHtml(pack) {
    var caps = Array.isArray(pack.capabilities) && pack.capabilities.length ? pack.capabilities.join(' / ') : '无额外能力';
    var tags = Array.isArray(pack.tags) ? pack.tags.join(' ') : '';
    return '<button type="button" class="pack-card" data-pack="' + esc(pack.name) + '" data-search-text="' +
      esc(((pack.name || '') + ' ' + (pack.description || '') + ' ' + (pack.domain || '')).toLowerCase()) + '">' +
      '<div class="pack-top"><span class="pack-name">' + esc(pack.name || '未命名包') + '</span>' +
        '<span class="badge dim">v' + esc(pack.version || '—') + '</span>' +
        (pack.builtin ? '<span class="badge">内置</span>' : '<span class="badge warn">工作区</span>') +
        '<span class="badge">' + esc(pack.domain || '未标注领域') + '</span>' +
      '</div>' +
      '<div class="pack-desc">' + esc(pack.description || '未提供描述') + '</div>' +
      '<div class="pack-foot"><span class="pack-count">' + esc(String(typeof pack.atom_count === 'number' ? pack.atom_count : '—')) + ' 个原子</span>' +
        '<span>' + esc(caps) + '</span>' +
        (tags ? '<span class="mono">' + esc(tags) + '</span>' : '') +
      '</div>' +
    '</button>';
  }

  function packDetailHtml(pack) {
    var atoms = Array.isArray(pack.atoms) ? pack.atoms : [];
    var caps = Array.isArray(pack.capabilities) ? pack.capabilities : [];
    return '<div class="stack">' +
      '<p>' + esc(pack.description || '未提供描述') + '</p>' +
      '<div class="chips">' +
        '<span class="chip">domain ' + esc(pack.domain || '—') + '</span>' +
        '<span class="chip">v' + esc(pack.version || '—') + '</span>' +
        '<span class="chip">' + (pack.builtin ? '内置' : '工作区') + '</span>' +
        '<span class="chip">' + esc(String(pack.atom_count !== undefined ? pack.atom_count : atoms.length)) + ' 个原子</span>' +
      '</div>' +
      (caps.length ? '<div class="field"><span class="field-label">能力</span><div class="chips">' +
        caps.map(function (c) { return '<span class="chip">' + esc(c) + '</span>'; }).join('') + '</div></div>' : '') +
      '<div class="field"><span class="field-label">包含原子</span><div class="chips">' +
        (atoms.length ? atoms.map(function (a) { return '<span class="chip mono">' + esc(a) + '</span>'; }).join('') : '<span class="missing">未提供</span>') +
      '</div></div>' +
      (pack.manifest ? '<details class="raw"><summary>manifest（原始）</summary><pre class="json-block">' + esc(prettyJson(pack.manifest)) + '</pre></details>' : '') +
    '</div>';
  }

  function renderPacks(host, ctx) {
    host.innerHTML = pageHead('领域包', '领域包把一组原子与能力语义打包；点击查看包含的原子与 manifest。') +
      '<div id="packBody">' + loadingHtml('正在读取领域包…') + '</div>';
    var body = $('#packBody', host);
    apiGet(API + 'packs').then(function (packs) {
      if (!ctx.alive()) return;
      var list = Array.isArray(packs) ? packs : [];
      if (!list.length) {
        body.innerHTML = emptyHtml('还没有注册领域包', '内置包会随发行版提供；工作区包放在 packs/ 下。');
        return;
      }
      body.innerHTML = '<div class="toolbar"><label class="search" aria-label="搜索领域包"><span aria-hidden="true">⌕</span>' +
        '<input type="search" data-search="packs" placeholder="搜索名称或描述，按 / 聚焦" autocomplete="off"></label>' +
        '<span class="toolbar-spacer"></span><span class="filter-count">共 ' + list.length + ' 个领域包</span></div>' +
        '<div class="pack-grid" id="packGrid">' + list.map(packCardHtml).join('') + '</div>';
      var input = $('[data-search="packs"]', host);
      if (input) input.addEventListener('input', function () {
        var q = input.value.trim().toLowerCase();
        $$('.pack-card', body).forEach(function (card) {
          card.hidden = !!q && (card.getAttribute('data-search-text') || '').indexOf(q) < 0;
        });
      });
      body.addEventListener('click', function (e) {
        var card = e.target.closest ? e.target.closest('[data-pack]') : null;
        if (!card) return;
        var name = card.getAttribute('data-pack');
        apiGet(API + 'packs/' + enc(name)).then(function (pack) {
          openModal({ title: pack.name || name, subtitle: pack.domain || '', okLabel: null, cancelLabel: '关闭', wide: true, bodyHtml: packDetailHtml(pack) });
        }).catch(function (err) {
          toast('无法读取领域包：' + (err && err.message ? err.message : '未知错误'), 'error');
        });
      }, ctx.signal ? { signal: ctx.signal } : false);
    }).catch(function (err) {
      if (!ctx.alive()) return;
      body.innerHTML = '<div id="packError"></div>';
      renderError($('#packError', body), err, function () { renderPacks(host, ctx); }, '无法读取领域包');
    });
  }

  /* ================================================================== */
  /* 平台 · 任务模板                                                     */
  /* ================================================================== */
  var MODE_LABEL = { demo: '演示', scenario: '场景', intent: '意图', regression: '回归' };

  function taskTarget(task) {
    if (task.mode === 'scenario') return task.scenario || '未配置场景';
    if (task.mode === 'intent') return task.intent || '未配置意图';
    return MODE_LABEL[task.mode] || task.mode || '—';
  }

  function tasksHtml(tasks) {
    if (!tasks.length) {
      return emptyHtml('还没有任务模板', '任务模板适合固定场景的一键运行；项目内的套件更适合多套件编排。',
        '<button type="button" class="btn btn-primary" data-act="new-task">新建任务</button>');
    }
    return dataTable([
      { label: '任务', render: function (t) {
          return '<span class="cell-main">' + esc(t.name || t.task_id) + '</span>' +
            '<div class="cell-sub">' + esc(MODE_LABEL[t.mode] || t.mode || '—') + ' · ' + esc(t.task_id) + '</div>';
        } },
      { label: '目标', render: function (t) { return '<span class="mono word-break">' + esc(taskTarget(t)) + '</span>'; } },
      { label: '最近状态', render: function (t) {
          return t.last_status ? badge(t.last_status === 'done' ? 'passed' : t.last_status) : '<span class="missing">未运行</span>';
        } },
      { label: '更新时间', render: function (t) { return '<span class="nowrap">' + esc(fmtTs(t.updated_at)) + '</span>'; } },
      { label: '操作', cls: 'tight', render: function (t) {
          return '<span class="row">' +
            '<button type="button" class="btn btn-sm" data-task-run="' + esc(t.task_id) + '">运行</button>' +
            '<button type="button" class="btn btn-sm" data-task-edit="' + esc(t.task_id) + '">编辑</button>' +
            '<button type="button" class="btn btn-sm btn-ghost" data-task-del="' + esc(t.task_id) + '">删除</button>' +
          '</span>';
        } }
    ], tasks);
  }

  function renderTasks(host, ctx) {
    host.innerHTML = pageHead('任务模板', '固定的单场景/意图任务，适合一键回归；项目内的套件用于多套件编排。',
      '<button type="button" class="btn btn-primary" data-act="new-task">＋ 新建任务</button>') +
      '<div id="taskBody">' + loadingHtml('正在读取任务模板…') + '</div>';
    var body = $('#taskBody', host);
    var tasks = [];

    function refresh() {
      body.innerHTML = loadingHtml('正在读取任务模板…');
      return apiGet(API + 'tasks').then(function (payload) {
        if (!ctx.alive()) return;
        tasks = payload && Array.isArray(payload.tasks) ? payload.tasks : [];
        APP.tasks = tasks;
        if (!tasks.length) {
          body.innerHTML = tasksHtml(tasks);
          return;
        }
        body.innerHTML = '<div class="toolbar"><label class="search" aria-label="搜索任务"><span aria-hidden="true">⌕</span>' +
          '<input type="search" data-search="tasks" placeholder="搜索任务名称，按 / 聚焦" autocomplete="off"></label>' +
          '<span class="toolbar-spacer"></span><span class="filter-count">共 ' + tasks.length + ' 个任务</span></div>' +
          '<div id="taskTable">' + tasksHtml(tasks) + '</div>';
        var input = $('[data-search="tasks"]', body);
        if (input) input.addEventListener('input', function () {
          var q = input.value.trim().toLowerCase();
          var filtered = tasks.filter(function (t) { return ((t.name || '') + ' ' + (t.task_id || '')).toLowerCase().indexOf(q) >= 0; });
          var table = $('#taskTable', body);
          if (table) table.innerHTML = tasksHtml(filtered);
        });
      }).catch(function (err) {
        if (!ctx.alive()) return;
        body.innerHTML = '<div id="taskError"></div>';
        renderError($('#taskError', body), err, refresh, '无法读取任务模板');
      });
    }

    body.addEventListener('click', function (e) {
      var run = e.target.closest ? e.target.closest('[data-task-run]') : null;
      var edit = e.target.closest ? e.target.closest('[data-task-edit]') : null;
      var del = e.target.closest ? e.target.closest('[data-task-del]') : null;
      if (run) { runTaskById(run.getAttribute('data-task-run'), run, refresh); return; }
      if (edit) { openTaskModal(edit.getAttribute('data-task-edit'), refresh); return; }
      if (del) {
        var id = del.getAttribute('data-task-del');
        var task = tasks.filter(function (t) { return t.task_id === id; })[0];
        confirmDialog({
          title: '删除任务模板',
          bodyHtml: '确定删除任务「' + esc(task ? (task.name || id) : id) + '」？<br>历史运行不会被删除。',
          okLabel: '删除任务',
          danger: true
        }).then(function (ok) {
          if (!ok) return;
          apiDelete(API + 'tasks/' + enc(id)).then(function () {
            toast('任务已删除', 'ok');
            refresh();
          }).catch(function (err) {
            toast('删除失败：' + (err && err.message ? err.message : '未知错误'), 'error');
          });
        });
      }
    }, ctx.signal ? { signal: ctx.signal } : false);

    refresh();
  }

  function runTaskById(taskId, btn, onDone) {
    if (btn && btn.disabled) return;
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner" aria-hidden="true"></span>'; }
    apiPost(API + 'tasks/' + enc(taskId) + '/run', {}).then(function (res) {
      var jobId = res && res.job_id;
      if (!jobId) throw new ApiError('服务端没有返回 job_id', 500, 'bad_payload');
      watchJob(jobId, {
        label: '任务运行',
        hrefFor: function (job) { return job && job.run_id ? '#/runs/' + enc(job.run_id) : ''; },
        onDone: function () { if (typeof onDone === 'function') onDone(); }
      });
      toast('已提交任务运行', 'ok');
    }).catch(function (err) {
      toast('提交失败：' + (err && err.message ? err.message : '未知错误'), 'error');
    }).then(function () {
      if (btn) { btn.disabled = false; btn.textContent = '运行'; }
    });
  }

  function openTaskModal(taskId, onSaved) {
    var editing = !!taskId;
    var loadTask = editing ? apiGet(API + 'tasks/' + enc(taskId)) : Promise.resolve(null);
    Promise.all([loadTask, loadScenarios()]).then(function (results) {
      var task = results[0];
      var scenarios = results[1];
      var mode = task && task.mode ? task.mode : 'scenario';
      var fixedMode = mode === 'demo' || mode === 'regression';
      var env = task && task.env && typeof task.env === 'object' ? task.env : {};
      var entries = envEntriesFromObject(env, editing);
      var envKeysOnly = task && Array.isArray(task.env_keys) ? task.env_keys : [];
      if (editing && !Object.keys(env).length && envKeysOnly.length) {
        // 旧后端只给键名时，仍展示可覆盖输入（留空沿用）
        entries = envKeysOnly.map(function (k) { return { key: k, value: '', existing: true }; });
      }
      var scenarioField = '<div class="field" id="taskScenarioField"' + (mode === 'intent' || fixedMode ? ' hidden' : '') + '>' +
        '<label for="taskScenario">场景文件</label><select class="select mono" id="taskScenario">' +
        scenarioOptionsHtml(scenarios, task ? task.scenario : '') + '</select></div>';
      var intentField = '<div class="field" id="taskIntentField"' + (mode === 'intent' ? '' : ' hidden') + '>' +
        '<label for="taskIntent">意图</label><textarea class="textarea" id="taskIntent" placeholder="用一句自然语言描述要测什么">' +
        esc(task ? (task.intent || '') : '') + '</textarea></div>';
      var modeField = fixedMode
        ? '<div class="notice info"><span aria-hidden="true">ⓘ</span><div>这是' + esc(MODE_LABEL[mode] || mode) + '任务，只能修改名称与环境变量；运行目标保持原样。</div></div>'
        : '<div class="field"><label for="taskMode">运行模式</label><select class="select" id="taskMode">' +
            '<option value="scenario"' + (mode === 'scenario' ? ' selected' : '') + '>场景（固定 YAML）</option>' +
            '<option value="intent"' + (mode === 'intent' ? ' selected' : '') + '>意图（自然语言）</option>' +
          '</select></div>';

      openModal({
        title: editing ? '编辑任务' : '新建任务',
        subtitle: '任务模板是单场景/意图的一键入口；多套件编排请使用项目。',
        okLabel: editing ? '保存任务' : '创建任务',
        wide: true,
        bodyHtml:
          '<div class="field"><label for="taskName">任务名称</label>' +
            '<input class="input" id="taskName" value="' + esc(task ? (task.name || '') : '') + '" placeholder="例如 站点冒烟" maxlength="80" autocomplete="off"></div>' +
          modeField + scenarioField + intentField +
          '<div class="field"><span class="field-label">环境变量</span>' +
            envEditorBlock({ entries: entries, note: '密钥以 *** 返回；留空沿用，点 ✕ 删除该键。' }) + '</div>' +
          '<div class="row">' +
            '<label class="check"><input type="checkbox" id="taskRegression"' + (task && task.regression ? ' checked' : '') + '> 复用回归库</label>' +
            '<label class="check"><input type="checkbox" id="taskNoLlm"' + (task && task.no_llm ? ' checked' : '') + '> 不调用模型</label>' +
          '</div>',
        onMount: function (rootEl) {
          var editor = mountEnvEditor($('[data-env-editor]', rootEl), entries, { placeholder: '值' });
          rootEl._envEditor = editor;
          var modeSel = $('#taskMode', rootEl);
          if (modeSel) {
            modeSel.addEventListener('change', function () {
              var sf = $('#taskScenarioField', rootEl);
              var inf = $('#taskIntentField', rootEl);
              if (sf) sf.hidden = modeSel.value !== 'scenario';
              if (inf) inf.hidden = modeSel.value !== 'intent';
            });
          }
        },
        onSubmit: function (rootEl) {
          var nameEl = $('#taskName', rootEl);
          var name = nameEl ? nameEl.value.trim() : '';
          if (!name) { if (nameEl) nameEl.focus(); return Promise.reject(new ApiError('请填写任务名称', 0, 'validation')); }
          var body = {
            name: name,
            env: rootEl._envEditor ? rootEl._envEditor.collect({ existing: editing }) : {},
            regression: !!($('#taskRegression', rootEl) && $('#taskRegression', rootEl).checked),
            no_llm: !!($('#taskNoLlm', rootEl) && $('#taskNoLlm', rootEl).checked)
          };
          if (!fixedMode) {
            var modeSel = $('#taskMode', rootEl);
            var targetMode = modeSel ? modeSel.value : 'scenario';
            body.mode = targetMode;
            if (targetMode === 'scenario') {
              var sc = $('#taskScenario', rootEl);
              if (!sc || !sc.value) return Promise.reject(new ApiError('请选择场景文件', 0, 'validation'));
              body.scenario = sc.value;
            } else {
              var intentEl = $('#taskIntent', rootEl);
              var intent = intentEl ? intentEl.value.trim() : '';
              if (!intent) return Promise.reject(new ApiError('请填写意图', 0, 'validation'));
              body.intent = intent;
            }
          }
          var req = editing ? apiPatch(API + 'tasks/' + enc(taskId), body) : apiPost(API + 'tasks', body);
          return req.then(function () {
            toast(editing ? '任务已更新' : '任务已创建', 'ok');
            if (typeof onSaved === 'function') setTimeout(onSaved, 0);
            return true;
          });
        }
      });
    }).catch(function (err) {
      toast('无法打开任务：' + (err && err.message ? err.message : '未知错误'), 'error');
    });
  }

  /* ================================================================== */
  /* 移动端导航                                                          */
  /* ================================================================== */
  function openMobileNav() {
    var sidebar = $('#sidebar');
    var scrim = $('#sidebarScrim');
    var btn = $('[data-act="toggle-sidebar"]');
    if (sidebar) sidebar.classList.add('is-open');
    if (scrim) scrim.hidden = false;
    if (btn) btn.setAttribute('aria-expanded', 'true');
  }

  function closeMobileNav() {
    var sidebar = $('#sidebar');
    var scrim = $('#sidebarScrim');
    var btn = $('[data-act="toggle-sidebar"]');
    if (sidebar) sidebar.classList.remove('is-open');
    if (scrim) scrim.hidden = true;
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  /* ================================================================== */
  /* 全局动作                                                            */
  /* ================================================================== */
  function deleteProject(pid) {
    getProject(pid).then(function (project) {
      confirmDialog({
        title: '删除项目',
        bodyHtml: '确定删除项目「' + esc(project.name || pid) + '」？<br>' +
          '只会删除项目配置与套件绑定，<b>不会删除任何历史运行</b>。',
        okLabel: '删除项目',
        danger: true
      }).then(function (ok) {
        if (!ok) return;
        apiDelete(API + 'projects/' + enc(pid)).then(function () {
          invalidateProject(pid);
          APP.projects = null;
          loadProjects(true).catch(function () {});
          toast('项目已删除', 'ok');
          go('#/projects');
        }).catch(function (err) {
          toast('删除失败：' + (err && err.message ? err.message : '未知错误'), 'error');
        });
      });
    }).catch(function (err) {
      toast('无法读取项目：' + (err && err.message ? err.message : '未知错误'), 'error');
    });
  }

  var ACTIONS = {
    'logout': function (el) { doLogout(el); },
    'toggle-sidebar': function () { openMobileNav(); },
    'close-sidebar': function () { closeMobileNav(); },
    'new-project': function () { openProjectModal(); },
    'from-history': function () { openHistoryGuide(); },
    'reload-projects': function () {
      loadProjects(true).catch(function (err) {
        toast('项目列表读取失败：' + (err && err.message ? err.message : '未知错误'), 'error');
      });
    },
    'run-all': function (el) {
      if (el && el.disabled) return;
      startRunAll(el.getAttribute('data-pid') || '', el);
    },
    'new-suite': function (el) {
      var pid = el.getAttribute('data-pid') || '';
      if (!pid) return;
      getProject(pid).then(function (project) { openSuiteModal(pid, project, null); })
        .catch(function (err) { toast('无法读取项目：' + (err && err.message ? err.message : '未知错误'), 'error'); });
    },
    'delete-project': function (el) { deleteProject(el.getAttribute('data-pid') || ''); },
    'job-close': function () { stopJobWatcher(); },
    'new-task': function () { openTaskModal(null, function () { render(); }); },
    'clear-project-search': function () {
      var input = $('[data-search="projects"]');
      if (input) {
        input.value = '';
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus();
      }
    }
  };

  function bindGlobalEvents() {
    document.addEventListener('click', function (e) {
      var target = e.target;
      if (!target || !target.closest) return;

      var actEl = target.closest('[data-act]');
      if (actEl) {
        var act = actEl.getAttribute('data-act');
        if (ACTIONS[act]) {
          e.preventDefault();
          ACTIONS[act](actEl);
          return;
        }
      }
      if (target.closest('[data-modal-close]')) { closeModal(null); return; }
      if (target.closest('[data-banner-close]')) { hideBanner(); return; }

      var row = target.closest('tr[data-href]');
      if (row && !target.closest('a') && !target.closest('button')) {
        var href = row.getAttribute('data-href');
        if (href && href.indexOf('#/') === 0) location.hash = href;
      }
    });

    document.addEventListener('keydown', function (e) {
      if (MODAL.open) {
        if (e.key === 'Escape') { e.preventDefault(); closeModal(null); return; }
        if (e.key === 'Tab') {
          var root = $('#modalRoot');
          if (!root) return;
          var f = $$('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])', root)
            .filter(function (el) { return el.offsetParent !== null && !el.disabled; });
          if (!f.length) return;
          var first = f[0];
          var last = f[f.length - 1];
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
        return;
      }

      if (e.key === 'Escape') { closeMobileNav(); return; }

      var t = e.target;
      var typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
      if (typing) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      if (e.key === '/') {
        var searches = $$('[data-search]').filter(function (el) { return el.offsetParent !== null; });
        if (searches.length) {
          e.preventDefault();
          searches[0].focus();
          try { searches[0].select(); } catch (err) { /* noop */ }
        }
        return;
      }

      if (e.key === 'g') {
        bindGlobalEvents._g = Date.now();
        return;
      }
      if (e.key === 'p' && bindGlobalEvents._g && (Date.now() - bindGlobalEvents._g) < 900) {
        bindGlobalEvents._g = 0;
        e.preventDefault();
        go('#/projects');
        return;
      }
      bindGlobalEvents._g = 0;

      if (e.key === 'Enter') {
        var activeRow = t && t.closest ? t.closest('tr[data-href]') : null;
        if (activeRow) {
          var href = activeRow.getAttribute('data-href');
          if (href && href.indexOf('#/') === 0) location.hash = href;
        }
      }
    });

    window.addEventListener('hashchange', render);

    window.addEventListener('unhandledrejection', function (ev) {
      var reason = ev.reason;
      var msg = reason && reason.message ? reason.message : String(reason);
      toast('未处理的异步错误：' + msg, 'error');
    });
  }

  /* ================================================================== */
  /* 启动                                                                */
  /* ================================================================== */
  function boot() {
    bindGlobalEvents();
    if (!location.hash || location.hash === '#' || location.hash === '#/') {
      try { history.replaceState(null, '', DEFAULT_HASH); } catch (e) { location.hash = DEFAULT_HASH; }
    }
    initAuth();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

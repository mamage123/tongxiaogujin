/* 通晓古今 · 前端公共工具（无构建，原生 JS） */

const TXGJ = (() => {
  // ---------------- 设备指纹（FR-ACC-06：仅用于防刷计数，不是追踪）
  function deviceToken() {
    let t = localStorage.getItem('txgj_device');
    if (!t) {
      t = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2)) + Date.now().toString(36);
      localStorage.setItem('txgj_device', t);
    }
    // 同时写 cookie，后端用它算 device_hash
    document.cookie = `txgj_device=${t}; path=/; max-age=${60 * 60 * 24 * 365}; SameSite=Lax`;
    return t;
  }

  // ---------------- API
  async function api(path, opts = {}) {
    const init = {
      method: opts.method || (opts.body ? 'POST' : 'GET'),
      headers: { ...(opts.headers || {}) },
      credentials: 'same-origin',
    };
    if (opts.body instanceof FormData) {
      init.body = opts.body;
    } else if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      throw { code: 'E_NETWORK', message: '网络中断，请检查连接后重试' };
    }
    let json = null;
    try { json = await res.json(); } catch (_) { /* 可能是 HTML */ }
    if (!res.ok || (json && json.ok === false)) {
      const err = (json && json.ok === false) ? json : { code: 'E_HTTP_' + res.status, message: `请求失败（${res.status}）` };
      err.status = res.status;
      throw err;
    }
    return json ? json.data : null;
  }

  // ---------------- Toast
  function toast(msg, type = '') {
    let box = document.getElementById('toasts');
    if (!box) {
      box = document.createElement('div');
      box.id = 'toasts';
      document.body.appendChild(box);
    }
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 2600);
    setTimeout(() => el.remove(), 3000);
  }

  function errToast(e) {
    toast((e && e.message) || '操作失败', 'err');
    if (e && e.detail && Object.keys(e.detail).length) console.warn('[api]', e);
  }

  // ---------------- 格式化
  function yuan(mill) {
    const m = Number(mill || 0);
    return (m / 1000).toFixed(2);
  }
  function fmtBytes(n) {
    n = Number(n || 0);
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
    return (n / 1024 / 1024).toFixed(2) + ' MB';
  }
  function fmtTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const p = (x) => String(x).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function relTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const s = Math.floor((Date.now() - d.getTime()) / 1000);
    if (s < 60) return '刚刚';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    return Math.floor(s / 86400) + ' 天前';
  }

  // ---------------- 分片上传（FR-UPL-05 断点续传）
  async function uploadFile(file, onProgress) {
    const init = await api('/api/upload/init', {
      body: { filename: file.name, size: file.size },
    });
    const { upload_id, chunk_size, total_chunks } = init;

    // 断点续传：先问服务端已收到哪些分片
    let received = new Set();
    try {
      const sess = await api(`/api/upload/session/${upload_id}`);
      received = new Set(sess.received || []);
    } catch (_) { /* 忽略，全量上传 */ }

    for (let i = 0; i < total_chunks; i++) {
      if (received.has(i)) {
        onProgress && onProgress(Math.round(((i + 1) / total_chunks) * 100));
        continue;
      }
      const start = i * chunk_size;
      const blob = file.slice(start, Math.min(start + chunk_size, file.size));
      const fd = new FormData();
      fd.append('file', blob, `${file.name}.part${i}`);
      let tries = 0;
      for (;;) {
        try {
          await api(`/api/upload/chunk?upload_id=${encodeURIComponent(upload_id)}&index=${i}`, {
            method: 'POST', body: fd,
          });
          break;
        } catch (e) {
          tries++;
          if (tries >= 3) throw e;
          await new Promise((r) => setTimeout(r, 600 * tries));
        }
      }
      onProgress && onProgress(Math.round(((i + 1) / total_chunks) * 100));
    }
    const done = await api('/api/upload/complete', { body: { upload_id } });
    return done;
  }

  // ---------------- 语音/剪贴板
  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制', 'ok');
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
      toast('已复制', 'ok');
    }
  }

  deviceToken();

  return { api, toast, errToast, yuan, fmtBytes, fmtTime, relTime, uploadFile, copy, deviceToken };
})();

window.TXGJ = TXGJ;

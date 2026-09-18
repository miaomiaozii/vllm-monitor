'use strict';
const $ = s => document.querySelector(s);
const el = id => document.getElementById(id);
function svg(paths) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}
const IC = {
  gpu:  svg('<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/>'),
  cpu:  svg('<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 4v16M15 4v16M4 9h16M4 15h16"/>'),
  mem:  svg('<path d="M6 19v-3M10 19v-3M14 19v-3M18 19v-3"/><path d="M4 11h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2Z"/><path d="M6 11V7a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v4"/>'),
  disk: svg('<line x1="22" y1="12" x2="2" y2="12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" y1="16" x2="6.01" y2="16"/><line x1="10" y1="16" x2="10.01" y2="16"/>'),
  net:  svg('<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>'),
  vllm: svg('<rect width="20" height="8" x="2" y="2" rx="2"/><rect width="20" height="8" x="2" y="14" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>'),
  tok:  svg('<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>'),
};
const HMAX = 120;
let hist = {
  gpu0_util: [], gpu0_mem: [], cpu_util: [], mem_pct: [],
  vllm_tps: [], vllm_ttft: [], vllm_kv: [], net_rx: [],
};

function push(arr, v) { if (v == null || isNaN(v)) return; arr.push(v); if (arr.length > HMAX) arr.shift(); }

function fmtBytes(mb) {
  if (mb == null) return '—';
  if (mb < 1024) return mb.toFixed(0) + ' MB';
  if (mb < 1024 * 1024) return (mb / 1024).toFixed(2) + ' GB';
  return (mb / 1024 / 1024).toFixed(2) + ' TB';
}
function fmtUptime(s) {
  if (s == null) return '—';
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d) return d + 'd ' + h + 'h'; if (h) return h + 'h ' + m + 'm'; return m + 'm';
}
function bar(v, max, cls) {
  const w = Math.max(0, Math.min(100, (v / (max || 100)) * 100));
  return `<div class="bar ${cls || ''}"><i style="width:${w.toFixed(1)}%"></i></div>`;
}
function barwrap(label, valTxt, v, max, cls) {
  return `<div class="barwrap"><div class="lbl"><span>${label}</span><b>${valTxt}</b></div>${bar(v, max, cls)}</div>`;
}
function row(k, v, dim) {
  return `<div class="row"><span class="k">${k}</span><span class="v ${dim ? 'dim' : ''}">${v}</span></div>`;
}
async function jget(u) {
  const r = await fetch(u, { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
function spark(id, data, color, max) {
  const c = el(id); if (!c) return;
  const ctx = c.getContext('2d');
  const W = c.width = c.clientWidth || 300, H = c.height = c.clientHeight || 40;
  ctx.clearRect(0, 0, W, H);
  if (!data || data.length < 2) {
    ctx.fillStyle = '#586069'; ctx.font = '11px sans-serif';
    ctx.fillText('collecting…', 8, H / 2 + 3); return;
  }
  const mx = max != null ? max : (Math.max(...data) * 1.15 || 1), mn = 0;
  const step = W / (data.length - 1);
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = i * step, y = H - ((v - mn) / (mx - mn || 1)) * (H - 6) - 3;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.stroke();
  ctx.lineTo((data.length - 1) * step, H); ctx.lineTo(0, H); ctx.closePath();
  const g = ctx.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, color + '44'); g.addColorStop(1, color + '00');
  ctx.fillStyle = g; ctx.fill();
}

function renderHost(h) {
  if (!h) return;
  el('ver').textContent = ' v' + (h.monitor_version || '?');
  el('hostinfo').innerHTML =
    `<span><b>${h.hostname || '?'}</b></span>` +
    `<span>${h.os || ''} ${h.release || ''}</span>` +
    `<span>CPU <b>${h.cores || '?'}c</b></span>` +
    `<span>Py ${h.python || ''}</span>` +
    `<span>Uptime <b>${fmtUptime(h.uptime_s)}</b></span>`;
}
let lastAlertsKey = null;
function renderAlerts(alerts) {
  const box = el('alerts');
  const key = JSON.stringify(alerts || []);
  if (key === lastAlertsKey) return;  // 内容不变不重渲染, 避免 2s 刷新重放 fade 动画造成闪烁
  lastAlertsKey = key;
  if (!alerts || !alerts.length) { box.innerHTML = ''; return; }
  box.innerHTML = alerts.map(a => {
    const sev = a.sev || a.level || 'warn';
    const tag = sev === 'crit' ? 'CRIT' : 'WARN';
    return `<div class="alert ${sev}"><span class="tag">${tag}</span>` +
      `<span class="mono">${a.area || a.collector || ''}</span><span>${a.msg}</span></div>`;
  }).join('');
}
function gpuCard(gpu, procs) {
  const memPct = gpu.mem_total_mib ? gpu.mem_used_mib / gpu.mem_total_mib * 100 : 0;
  const hot = (gpu.temp_c || 0) >= 85;
  return `<div class="card">
    <h3><span class="ico">${IC.gpu}</span>GPU ${gpu.index}<span class="sub">${(gpu.name || '').replace('Tesla ', '')}</span></h3>
    <div class="big ${hot ? 'bad' : ''}">${gpu.util_gpu.toFixed(0)}<small>% 计算</small></div>
    ${barwrap('显存', fmtBytes(gpu.mem_used_mib) + ' / ' + fmtBytes(gpu.mem_total_mib), memPct, 100, 'purple')}
    ${barwrap('计算利用率', gpu.util_gpu.toFixed(0) + '%', gpu.util_gpu, 100)}
    ${row('温度', gpu.temp_c.toFixed(0) + ' °C', hot ? 'bad' : '')}
    ${row('功率', (gpu.power_w || 0).toFixed(0) + ' / ' + (gpu.power_limit_w || 0).toFixed(0) + ' W')}
    ${row('核心频率', (gpu.clock_mhz || 0).toFixed(0) + ' / ' + (gpu.clock_max_mhz || 0).toFixed(0) + ' MHz')}
    ${row('ECC 不可纠正', gpu.ecc_uncorrected || 0, (gpu.ecc_uncorrected || 0) > 0 ? 'bad' : 'dim')}
    <canvas class="spark" id="sp_gpu${gpu.index}" height="40"></canvas>
  </div>`;
}
function renderGPU(g) {
  if (!g || !g.available)
    return `<div class="card offline"><h3><span class="ico">${IC.gpu}</span>GPU</h3><p class="dim">${(g && g.error) || '无数据'}</p></div>`;
  return (g.gpus || []).map((gpu, i) => gpuCard(gpu, i)).join('');
}
function renderCPU(c) {
  if (!c || !c.available) return '';
  const cores = (c.per_core || []).slice(0, 12).map(p =>
    `<div class="core" title="core ${p.id}: ${p.util}%"><i style="height:${p.util.toFixed(0)}%"></i><span>${p.id}</span></div>`).join('');
  return `<div class="card">
    <h3><span class="ico">${IC.cpu}</span>CPU</h3>
    <div class="big">${c.util.toFixed(1)}<small>% 平均</small></div>
    ${row('核心数', c.cores)}
    ${row('负载 1/5/15m', (c.load1 || 0).toFixed(2) + ' / ' + (c.load5 || 0).toFixed(2) + ' / ' + (c.load15 || 0).toFixed(2))}
    <div class="cores">${cores}</div>
    <canvas class="spark" id="sp_cpu" height="40"></canvas>
  </div>`;
}
function renderMem(m) {
  if (!m || !m.available) return '';
  return `<div class="card">
    <h3><span class="ico">${IC.mem}</span>内存</h3>
    <div class="big">${m.used_pct.toFixed(1)}<small>% 已用</small></div>
    ${barwrap('RAM', fmtBytes(m.used_mb) + ' / ' + fmtBytes(m.total_mb), m.used_pct, 100, 'blue')}
    ${row('可用', fmtBytes(m.avail_mb))}
    ${row('缓存', fmtBytes(m.cached_mb))}
    ${row('Swap', (m.swap_total_mb ? fmtBytes(m.swap_used_mb) + ' / ' + fmtBytes(m.swap_total_mb) : '无'))}
    <canvas class="spark" id="sp_mem" height="40"></canvas>
  </div>`;
}
function renderDisk(d) {
  if (!d || !d.available) return '';
  const mounts = (d.mounts || []).map(m =>
    barwrap(m.mount, m.used_gb.toFixed(0) + 'G / ' + m.total_gb.toFixed(0) + 'G · ' + m.used_pct + '%', m.used_pct, 100,
      m.used_pct > 90 ? '' : 'blue')).join('');
  return `<div class="card">
    <h3><span class="ico">${IC.disk}</span>磁盘</h3>
    ${mounts}
    ${row('读 / 写', (d.read_mb_s || 0).toFixed(1) + ' / ' + (d.write_mb_s || 0).toFixed(1) + ' MB/s')}
  </div>`;
}
function renderNet(n) {
  if (!n || !n.available) return '';
  const ifaces = (n.ifaces || []).map(f =>
    row(f['if'], '↓' + f.rx_mb_s + ' ↑' + f.tx_mb_s + ' MB/s')).join('');
  return `<div class="card">
    <h3><span class="ico">${IC.net}</span>网络</h3>
    ${ifaces || row('—', '无活动接口')}
    <canvas class="spark" id="sp_net" height="40"></canvas>
  </div>`;
}
function tsShort(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function procMeta(p) {
  const parts = [p.short];
  if (p.tp) parts.push(`TP${p.tp}`);
  if (p.max_len) parts.push(`${Math.round(p.max_len / 1024)}K`);
  if (p.seqs) parts.push(`${p.seqs}并发`);
  if (p.gpu_mem_util) parts.push(`显存${p.gpu_mem_util}`);
  if (p.cuda_devices) parts.push(`GPU ${p.cuda_devices}`);
  if (p.pid) parts.push(`PID ${p.pid}`);
  return parts.join(' · ');
}
function peakLine(pk) {
  if (!pk) return '';
  const d = pk.decode > 0 ? `${pk.decode.toFixed(1)} tok/s <small>(${tsShort(pk.decode_ts)})</small>` : '—';
  const pf = pk.prefill > 0 ? `${Math.round(pk.prefill).toLocaleString()} tok/s <small>(${tsShort(pk.prefill_ts)})</small>` : '—';
  return `<div class="peakline">🏆 本配置峰值 decode <b>${d}</b> · prefill <b>${pf}</b></div>`;
}
function portOf(url) {
  const m = String(url).match(/:(\d+)/);
  return m ? m[1] : '?';
}
// 每实例本周期用量 (来自 /api/tokens 的 instances 明细, 30s 刷新; 与实例卡 2s 刷新异步属正常)
function tokLine(url) {
  const ti = tokInst[url];
  if (!ti) return '';
  const pl = { day: '今日', week: '本周', month: '本月', year: '本年' }[tokPeriod] || '本周期';
  const pre = ti.estimated ? '≈' : '';
  const tip = ti.estimated ? ` title="该实例周期起点前无监控数据, 自 ${new Date(ti.since * 1000).toLocaleString()} 起累计"` : '';
  return `<div class="tokline"><span class="k" ${tip}>${pl} 用量${ti.estimated ? ' (监控后)' : ''}</span>
    <b><span class="tin">${pre}${fmtTok(ti.prompt)} 输入</span> · <span class="tout">${pre}${fmtTok(ti.gen)} 输出</span> · ${ti.requests.toLocaleString()} 请求</b></div>`;
}
// 每个 vLLM 实例一张独立卡片 (几个实例 = 几个卡片)
function vllmCard(inst) {
  const port = portOf(inst.url);
  if (!inst.online)
    return `<div class="card card.offline">
      <h3><span class="ico">${IC.vllm}</span>实例 :${port}
        <span class="badge off">OFFLINE</span><span class="url">${inst.url}</span></h3>
      <div class="dim" style="padding:6px 2px">无法连接 ${inst.error || ''}</div></div>`;
  const mtp = inst.mtp_accept_rate;
  const p = inst.proc;
  const served = (p && p.served) || inst.model || '';
  return `<div class="card">
    <h3><span class="ico">${IC.vllm}</span>实例 :${port}
      <span class="badge on">ONLINE</span><span class="url">${served}</span></h3>
    ${p ? `<div class="instmeta">${procMeta(p)}</div>` : ''}
    <div class="grid4">
      <div class="stat"><div class="l">Prefill</div><div class="n">${inst.prompt_toks_s != null ? inst.prompt_toks_s : '—'}<small> tok/s</small></div></div>
      <div class="stat"><div class="l">Decode</div><div class="n">${inst.gen_toks_s != null ? inst.gen_toks_s : '—'}<small> tok/s</small></div></div>
      <div class="stat"><div class="l">TTFT</div><div class="n">${inst.ttft_ms != null ? inst.ttft_ms : '—'}<small> ms</small></div></div>
      <div class="stat"><div class="l">ITL</div><div class="n">${inst.itl_ms != null ? inst.itl_ms : '—'}<small> ms</small></div></div>
    </div>
    ${tokLine(inst.url)}
    ${barwrap('KV 缓存', (inst.kv_pct || 0).toFixed(1) + '%', inst.kv_pct || 0, 100, 'purple')}
    <div class="barwrap"><div class="lbl"><span>请求</span><b>运行 ${inst.running||0} · 排队 ${inst.waiting||0}</b></div>
      <div class="bar"><i style="width:${Math.min(100,(inst.running||0)*10)}%"></i></div></div>
    ${mtp != null ? `<div class="barwrap"><div class="lbl"><span>MTP 接受率</span><b>${mtp}%</b></div>
      ${bar(mtp, 100, 'purple')}</div>` : ''}
    ${row('前缀缓存命中', inst.prefix_hit_rate != null ? inst.prefix_hit_rate + '%' : '—')}
    ${row('累计抢占', inst.preemptions || 0, (inst.preemptions || 0) > 0 ? 'bad' : 'dim')}
    ${peakLine(inst.peak)}
  </div>`;
}
// 配置横幅 (全宽, #lower 首行, 内部按 #lower 同款 2:3 两列分): 配置名+全时峰值都在左半 (vLLM 区上方), 右半 (Token 区上方) 留空
function cfglineHTML(v) {
  let left;
  if (!v || !v.available)
    left = `<span class="cfgbadge" style="background:none;border-color:#30363d;color:#8b949e">未检测到 vLLM 服务 (自动识别中)</span>`;
  else {
    const op = v.overall_peak;
    left = `<span class="cfgbadge">${v.cfg_name || '当前配置: 未识别 (自动识别中)'}</span>
      ${op ? `<span class="peakall">全时峰值 decode <b>${op.val.toFixed(1)}</b> tok/s <small>(${tsShort(op.ts)})</small></span>` : ''}`;
  }
  return `<div class="cfgleft">${left}</div><div class="cfgright"></div>`;
}
// vLLM 左列 body: 每实例一张独立卡片 (N 实例 = N 卡片)
function vllmBody(v) {
  if (!v || !v.available)
    return `<div class="card"><h3><span class="ico">${IC.vllm}</span>vLLM 服务</h3>
      <p class="dim" style="padding:8px 2px">未检测到 vLLM 服务 (自动识别中)</p></div>`;
  return (v.instances || []).map(vllmCard).join('');
}

// 防御性渲染: 单个卡片出错不拖垮整个页面
function safe(fn, arg) {
  try { return fn(arg); }
  catch (e) { console.error('render error', e);
    return `<div class="card offline"><h3>渲染异常</h3><p class="dim">${e && e.message || e}</p></div>`; }
}

let started = Date.now();
async function tick() {
  let s;
  try { s = await jget('/api/status'); }
  catch (e) {
    el('dot').className = 'dot off';
    el('alerts').innerHTML = `<div class="alert crit"><span class="tag">CRIT</span><span class="mono">monitor</span><span>无法连接监控服务: ${e}</span></div>`;
    return;
  }
  el('dot').className = 'dot';
  renderHost(s.host);
  renderAlerts(s.alerts);
  // push history
  const v0 = (s.vllm && s.vllm.instances && s.vllm.instances[0]) || {};
  const g0 = (s.gpu && s.gpu.gpus && s.gpu.gpus[0]) || {};
  push(hist.gpu0_util, g0.util_gpu);
  push(hist.gpu0_mem, g0.mem_used_mib ? g0.mem_used_mib / 1024 : null);
  if (s.cpu && s.cpu.available) push(hist.cpu_util, s.cpu.util);
  if (s.mem && s.mem.available) push(hist.mem_pct, s.mem.used_pct);
  if (s.vllm && v0.online) { push(hist.vllm_tps, v0.gen_toks_s); push(hist.vllm_ttft, v0.ttft_ms); push(hist.vllm_kv, v0.kv_pct); }
  if (s.net && s.net.available) push(hist.net_rx, s.net.rx_mb_s);

  el('grid').innerHTML =
    safe(renderGPU, s.gpu) + safe(renderCPU, s.cpu) + safe(renderMem, s.mem) +
    safe(renderDisk, s.disk) + safe(renderNet, s.net);
  if (el('cfgline')) el('cfgline').innerHTML = safe(cfglineHTML, s.vllm);
  if (el('vllmcol')) el('vllmcol').innerHTML = safe(vllmBody, s.vllm);

  if (s.gpu && s.gpu.available)
    (s.gpu.gpus || []).forEach(g => spark('sp_gpu' + g.index, hist.gpu0_util, '#3fb950', 100));
  spark('sp_cpu', hist.cpu_util, '#58a6ff', 100);
  spark('sp_mem', hist.mem_pct, '#bc8cff', 100);
  spark('sp_net', hist.net_rx, '#39c5cf');
  spark('sp_vllm_tps', hist.vllm_tps, '#39c5cf');
  el('clock').textContent = new Date().toLocaleTimeString();
}
tick();
setInterval(tick, 2000);

// ---------- Token 统计 (日/周/月/年) ----------
let tokPeriod = 'day';
let tokInst = {};  // /api/tokens 的每实例本周期用量明细
function fmtTok(n) {
  if (n == null || isNaN(n)) return '—';
  if (n >= 1e8) { const v = n / 1e8; return (v >= 100 ? v.toFixed(0) : v.toFixed(2)) + '亿'; }
  if (n >= 1e4) { const v = n / 1e4; return (v >= 1000 ? v.toFixed(0) : v >= 100 ? v.toFixed(1) : v.toFixed(2)) + '万'; }
  return String(Math.round(n));
}
function tokChart(data) {
  const c = el('tok_chart'); if (!c) return;
  const ctx = c.getContext('2d');
  const W = c.width = c.clientWidth || 900, H = c.height = c.clientHeight || 90;
  ctx.clearRect(0, 0, W, H);
  const padL = 6, padR = 46, padT = 8, padB = 18;
  if (!data || !data.length) {
    ctx.fillStyle = '#586069'; ctx.font = '11px sans-serif';
    ctx.fillText('暂无数据 (每 15s 采样累积中)', padL, H / 2);
    return;
  }
  const max = Math.max(...data.map(d => Math.max(d.prompt, d.gen))) * 1.15 || 1;
  const n = data.length;
  const x = i => padL + (n === 1 ? (W - padL - padR) / 2 : i * (W - padL - padR) / (n - 1));
  const y = v => padT + (1 - v / max) * (H - padT - padB);
  ctx.strokeStyle = '#21262d'; ctx.lineWidth = 1;
  ctx.fillStyle = '#586069'; ctx.font = '10px sans-serif';
  for (let i = 0; i <= 2; i++) {
    const v = max * i / 2, yy = y(v);
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(W - padR, yy); ctx.stroke();
    ctx.fillText(fmtTok(v), W - padR + 6, yy + 3);
  }
  const tickN = Math.min(8, n);
  for (let i = 0; i < tickN; i++) {
    const idx = n === 1 ? 0 : Math.round(i * (n - 1) / (tickN - 1));
    ctx.fillText(data[idx].label,
      Math.min(Math.max(x(idx) - 14, padL), W - padR - 34), H - 5);
  }
  const drawSeries = (key, color) => {
    if (n === 1) {
      ctx.beginPath(); ctx.moveTo(x(0), y(data[0][key])); ctx.lineTo(x(0), H - padB);
      ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.stroke();
      return;
    }
    ctx.beginPath();
    data.forEach((d, i) => i ? ctx.lineTo(x(i), y(d[key])) : ctx.moveTo(x(0), y(d[key])));
    ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.stroke();
    ctx.lineTo(x(n - 1), H - padB); ctx.lineTo(x(0), H - padB); ctx.closePath();
    ctx.fillStyle = color + '22'; ctx.fill();
  };
  drawSeries('prompt', '#58a6ff');
  drawSeries('gen', '#3fb950');
}
async function loadTokens() {
  let d;
  try { d = await jget('/api/tokens?period=' + tokPeriod); }
  catch (e) {
    el('toknums').innerHTML = `<div class="dim" style="padding:10px 2px">获取失败: ${e}</div>`;
    return;
  }
  const p = d.prompt_total || 0, g = d.gen_total || 0, rq = d.requests_total || 0;
  tokInst = d.instances || {};  // 供实例卡片显示每实例本周期用量
  const pre = d.estimated ? '≈' : '';
  const pl = { day: '今日', week: '本周', month: '本月', year: '本年' }[tokPeriod] || '';
  const atStr = new Date((d.anchor_ts || d.start) * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const cutStr = d.cutover ? new Date(d.cutover * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
  // 切换点在周期内 (旧+新混合): 数值覆盖整个周期, 不标"自X"; 否则新口径有锚点 → "自锚点"
  const sinceTag = (d.estimated && !d.cutover) ? ' · 自 ' + atStr : '';
  el('toknums').innerHTML =
    `<div class="toknum"><div class="l">${pl} 输入 (prompt)${sinceTag}</div><div class="n in" title="${p.toLocaleString()} tokens">${pre}${fmtTok(p)}</div></div>` +
    `<div class="toknum"><div class="l">${pl} 输出 (generation)${sinceTag}</div><div class="n out" title="${g.toLocaleString()} tokens">${pre}${fmtTok(g)}</div></div>` +
    `<div class="toknum"><div class="l">${pl} 请求数${sinceTag}</div><div class="n">${rq.toLocaleString()}</div></div>` +
    `<div class="toknum"><div class="l">每请求平均 输入/输出</div><div class="n sm">${rq ? fmtTok(p / rq) : '—'} / ${rq ? fmtTok(g / rq) : '—'}</div></div>`;
  el('toknote').innerHTML = d.estimated
    ? (d.cutover
        ? `<span class="k">说明</span><span class="v dim">${cutStr} 前为旧口径 (在线实例计数器求和, 即此前的记录); ${cutStr} 起为每实例新口径 (≈); 下个周期起有基线后自动变为精确值</span>`
        : `<span class="k">说明</span><span class="v dim">周期起点前无该实例监控数据, 数值为<b>自 ${new Date((d.anchor_ts || d.start) * 1000).toLocaleString()}</b> 的累计 (≈), 不是整个${pl}; 下个周期起有基线后自动变为精确值</span>`)
    : `<span class="k">范围</span><span class="v dim">自 ${new Date(d.start * 1000).toLocaleString()} · 每 15s 采样 · 按实例独立锚定 (实例增删/重启安全)</span>`;
  tokChart(d.series || []);
}
(function initTokens() {
  const sw = el('tokswitch');
  if (!sw) return;
  sw.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
    tokPeriod = b.dataset.p;
    sw.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    loadTokens();
  }));
  loadTokens();
  setInterval(loadTokens, 30000);  // token 计数 15s 粒度, 30s 刷新足够
})();

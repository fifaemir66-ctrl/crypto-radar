/* Crypto Radar -- website.
 * Laadt de analyse (data.json, elk uur bijgewerkt) en houdt prijzen live bij via de Binance-websocket. */

const COLORS = { BTC: '#f7931a', ETH: '#627eea', SOL: '#9945ff', XRP: '#0a7fc2', BNB: '#e0a800', DOGE: '#c2a633' };
const STATUS_ORDER = { setup: 0, zone: 1, wait: 2, down: 3 };
const WS_URL = 'wss://data-stream.binance.vision/stream?streams=';
const REST_TICKER = 'https://data-api.binance.vision/api/v3/ticker/24hr?symbols=';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let DATA = null;
let selected = null;
const live = {};          // symbol -> { price, change }
let charts = null;        // { main, rsi, candles, volume }
let ws = null, wsRetry = 0, pollTimer = null;

/* ---------- formatting ---------- */
function decimals(p) { return p >= 1000 ? 0 : p >= 100 ? 2 : p >= 1 ? 3 : 5; }
function fmt(p) {
  if (p == null || isNaN(p)) return '–';
  const d = decimals(Math.abs(p));
  return new Intl.NumberFormat('nl-NL', { minimumFractionDigits: d, maximumFractionDigits: d }).format(p);
}
function pct(x, sign = true) {
  if (x == null || isNaN(x)) return '–';
  const s = new Intl.NumberFormat('nl-NL', { minimumFractionDigits: 1, maximumFractionDigits: 2 }).format(Math.abs(x));
  return (sign ? (x >= 0 ? '+' : '−') : '') + s + '%';
}
function eur(x) { return '€' + new Intl.NumberFormat('nl-NL', { maximumFractionDigits: 0 }).format(x); }
function one(x) { return new Intl.NumberFormat('nl-NL', { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(x); }
function ago(iso) {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return 'zojuist';
  if (m < 60) return `${m} min geleden`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h} uur geleden` : `${Math.floor(h / 24)} d geleden`;
}
function timeNL(iso) {
  return new Date(iso).toLocaleString('nl-NL', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

/* ---------- helpers ---------- */
function icon(c) {
  const base = c.base || c.symbol.replace(/USDT$/, '');
  return `<span class="coin-icon" style="background:${COLORS[base] || 'var(--accent)'}">${esc(base.slice(0, 4))}</span>`;
}
function badge(st) { return `<span class="badge s-${st.key}"><i></i>${esc(st.label)}</span>`; }
function priceOf(c) { return live[c.symbol]?.price ?? c.price; }
function changeOf(c) {
  if (live[c.symbol]?.change != null) return live[c.symbol].change;
  return (c.price - c.day_open) / c.day_open * 100;
}
function zoneInfo(c) {
  const p = priceOf(c);
  if (!c.zone1) return { text: '–', inZone: false };
  if (p >= c.zone1.low * 0.995 && p <= c.zone1.high * 1.005) return { text: 'In zone 1', inZone: true };
  if (p < c.zone1.low) return { text: 'Onder zone 1', inZone: false };
  return { text: pct((p - c.zone1.high) / p * 100, false) + ' erboven', inZone: false };
}

function sparkline(c) {
  const pts = c.chart.candles.slice(-60).map((k) => k[4]);
  const min = Math.min(...pts), max = Math.max(...pts), w = 300, h = 42;
  const d = pts.map((v, i) => `${i ? 'L' : 'M'}${(i / (pts.length - 1) * w).toFixed(1)},${(h - 3 - (v - min) / (max - min || 1) * (h - 6)).toFixed(1)}`).join(' ');
  const up = pts[pts.length - 1] >= pts[0];
  const col = up ? 'var(--up)' : 'var(--down)';
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${d} L${w},${h} L0,${h} Z" fill="${col}" opacity=".08"/><path d="${d}" fill="none" stroke="${col}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
}

/* ---------- render: overzicht ---------- */
function renderKpis() {
  const count = (k) => DATA.coins.filter((c) => c.status.key === k).length;
  const tiles = [
    ['Setups', count('setup'), 'var(--setup)'],
    ['In koopzone', count('zone'), 'var(--zone)'],
    ['Wachten', count('wait'), 'var(--wait)'],
    ['Geen trend', count('down'), 'var(--neg)'],
  ];
  $('kpis').innerHTML = tiles.map(([l, v, col]) =>
    `<div class="kpi"><div class="label"><i style="background:${col}"></i>${l}</div><div class="value">${v}</div></div>`).join('');
}

function renderCards() {
  const coins = [...DATA.coins].sort((a, b) => STATUS_ORDER[a.status.key] - STATUS_ORDER[b.status.key]);
  $('cards').innerHTML = coins.map((c) => {
    const ch = changeOf(c), z = zoneInfo(c);
    return `<button class="card${c.symbol === selected ? ' active' : ''}" data-sym="${c.symbol}">
      <div class="card-top">
        <div class="coin-id">${icon(c)}<div><div class="coin-name">${esc(c.name)}</div><div class="muted">${esc(c.symbol)}</div></div></div>
        ${badge(c.status)}
      </div>
      <div class="card-price"><span class="p mono" data-price="${c.symbol}">${fmt(priceOf(c))}</span>
        <span class="chg ${ch >= 0 ? 'up' : 'down'}" data-chg="${c.symbol}">${pct(ch)}</span></div>
      ${sparkline(c)}
      <div class="card-meta">
        <div><div class="k">Trend</div><div class="v">${esc(c.trend)}</div></div>
        <div><div class="k">RSI</div><div class="v">${one(c.rsi)}</div></div>
        <div><div class="k">Zone 1</div><div class="v" data-zone="${c.symbol}">${esc(z.text)}</div></div>
      </div>
    </button>`;
  }).join('');
  document.querySelectorAll('.card').forEach((el) => el.addEventListener('click', () => select(el.dataset.sym, true)));
}

function renderHistory() {
  const h = DATA.history || [];
  $('history').innerHTML = h.length ? h.slice(0, 20).map((e) =>
    `<li><div class="muted">${timeNL(e.time)}</div><div><div class="h-title">${esc(e.title)}</div><div class="h-msg">${esc(e.message)}</div></div></li>`).join('')
    : '<li class="empty">Nog geen meldingen. Zodra een coin in een koopzone komt of er een setup is, verschijnt die hier en op je telefoon.</li>';
}

function renderUpdated() {
  if (DATA) $('updated').textContent = `Analyse ${ago(DATA.generated_at)}`;
}

/* ---------- render: detail ---------- */
function planHtml(p) {
  if (!p) return '';
  return `<div class="plan">
      <div><div class="k">Entry</div><div class="v mono">${fmt(p.entry)}</div></div>
      <div><div class="k">Stop (−${one(p.stop_pct)}%)</div><div class="v mono">${fmt(p.stop)}</div></div>
      <div><div class="k">Target (+${one(p.win_pct)}%)</div><div class="v mono">${fmt(p.target)}</div></div>
    </div>
    <div class="plan-foot">
      <span class="badge ${p.rr >= 2 ? 's-setup' : 's-down'}">R/R ${one(p.rr)}x</span>
      <span>Positie <b>${eur(p.position)}</b>${p.capped ? ' (max. kapitaal)' : ''} · verlies bij stop ≈ ${eur(p.loss_eur)} · winst bij target ≈ ${eur(p.win_eur)}</span>
    </div>`;
}

function renderDetail() {
  const c = DATA.coins.find((x) => x.symbol === selected);
  if (!c) { $('detail').hidden = true; return; }
  $('detail').hidden = false;
  $('d-icon').outerHTML = icon(c).replace('class="coin-icon"', 'class="coin-icon lg" id="d-icon"');
  $('d-name').textContent = c.name;
  $('d-sub').textContent = `${c.symbol} · 1D · ${c.source}`;
  updateDetailPrice();

  $('d-verdict').className = `verdict s-${c.status.key}`;
  $('d-verdict').innerHTML = `<b>${esc(c.status.title)}</b><span>${esc(c.status.text)}</span>`;
  const mark = { ok: '✓', nee: '✕', wacht: '…' };
  $('d-checklist').innerHTML = c.checklist.map((i) =>
    `<li><span class="dot ${i.state}">${mark[i.state]}</span><span class="n">${esc(i.name)}</span><span class="t">${esc(i.text)}</span></li>`).join('');

  $('d-scenarios').innerHTML = c.scenarios.map((s) =>
    `<div class="scen"><div class="scen-h"><span class="scen-k">${s.key}</span><span>${esc(s.title)}</span></div><p>${esc(s.text)}</p>${planHtml(s.plan)}</div>`).join('');

  $('d-trend').innerHTML = c.trend_reasons.map((r) => `<li>${esc(r)}</li>`).join('');
  const f = c.fib;
  let fib = f ? `<p class="muted">Laatste stijging ${fmt(f.low)} → ${fmt(f.high)}</p>
      <div class="fib-row"><span>38,2%</span><b class="mono">${fmt(f.f382)}</b></div>
      <div class="fib-row"><span>50%</span><b class="mono">${fmt(f.f50)}</b></div>
      <div class="fib-row"><span>61,8%</span><b class="mono">${fmt(f.f618)}</b></div>` : '<p class="muted">Nog niet genoeg toppen.</p>';
  if (c.pullback_vol != null) {
    fib += `<p style="margin-top:10px">Volume in de terugval: <b>${Math.round(c.pullback_vol * 100)}%</b> van de stijging ervoor. ${c.pullback_vol < 1 ? 'Rustige winstneming (gezond).' : 'Verkopers zijn actief (let op).'}</p>`;
  }
  $('d-fib').innerHTML = fib;
  $('d-zones').innerHTML = c.zones.length ? c.zones.map((z, i) =>
    `<li><b class="mono">${fmt(z.low)} – ${fmt(z.high)}</b> · ${z.score} niveau${z.score > 1 ? 's' : ''}<br><span class="muted">${z.items.map((it) => esc(it.name)).join(', ')}</span></li>`).join('')
    : '<li class="muted">Geen zones onder de prijs gevonden.</li>';

  buildChart(c);
}

function updateDetailPrice() {
  const c = DATA?.coins.find((x) => x.symbol === selected);
  if (!c) return;
  const ch = changeOf(c);
  $('d-price').textContent = fmt(priceOf(c));
  $('d-change').className = `chg ${ch >= 0 ? 'up' : 'down'}`;
  $('d-change').textContent = `${pct(ch)} (24u)`;
}

/* ---------- grafiek ---------- */
function buildChart(c) {
  if (typeof LightweightCharts === 'undefined') return;
  if (charts) { charts.main.remove(); charts.rsi.remove(); charts = null; }
  const text = css('--muted'), grid = css('--border');
  const base = {
    layout: { background: { color: 'transparent' }, textColor: text, fontFamily: 'Inter, sans-serif' },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    rightPriceScale: { borderColor: grid }, timeScale: { borderColor: grid, rightOffset: 6 },
    crosshair: { mode: 0 }, localization: { priceFormatter: fmt, locale: 'nl-NL' },
  };
  const el = $('chart'), rel = $('rsi');
  const main = LightweightCharts.createChart(el, { ...base, width: el.clientWidth, height: el.clientHeight });
  const up = css('--up'), down = css('--down');
  const candles = main.addCandlestickSeries({ upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
  candles.setData(c.chart.candles.map((k) => ({ time: k[0], open: k[1], high: k[2], low: k[3], close: k[4] })));
  const volume = main.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
  main.priceScale('vol').applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
  const vcol = (k) => (k[4] >= k[1] ? up : down) + '55';
  volume.setData(c.chart.candles.map((k) => ({ time: k[0], value: k[5], color: vcol(k) })));
  main.addLineSeries({ color: css('--ema50'), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
    .setData(c.chart.ema50.map(([t, v]) => ({ time: t, value: v })));
  if (c.chart.ema200.length) {
    main.addLineSeries({ color: css('--ema200'), lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
      .setData(c.chart.ema200.map(([t, v]) => ({ time: t, value: v })));
  }
  candles.setMarkers(c.chart.markers.map(([t, kind, label]) => ({
    time: t, position: kind === 'top' ? 'aboveBar' : 'belowBar', shape: kind === 'top' ? 'arrowDown' : 'arrowUp',
    color: ['HH', 'HL'].includes(label) ? up : ['LH', 'LL'].includes(label) ? down : text, text: label,
  })));
  const lc = { zone1: css('--zone1'), zone2: css('--zone2'), target: css('--target'), stop: down };
  c.chart.lines.forEach((l) => candles.createPriceLine({ price: l.price, color: lc[l.kind], lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: l.title }));

  const rsi = LightweightCharts.createChart(rel, { ...base, width: rel.clientWidth, height: rel.clientHeight,
    localization: { locale: 'nl-NL' }, timeScale: { ...base.timeScale, visible: false } });
  const rs = rsi.addLineSeries({ color: css('--rsi'), lineWidth: 2, priceLineVisible: false });
  rs.setData(c.chart.rsi.map(([t, v]) => ({ time: t, value: v })));
  [70, 50, 30].forEach((v) => rs.createPriceLine({ price: v, color: text, lineWidth: 1, lineStyle: 2, axisLabelVisible: v !== 50, title: '' }));

  main.timeScale().subscribeVisibleLogicalRangeChange((r) => r && rsi.timeScale().setVisibleLogicalRange(r));
  const n = c.chart.candles.length;
  main.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 150), to: n + 5 });
  charts = { main, rsi, candles, volume, vcol };
}

new ResizeObserver(() => {
  if (!charts) return;
  charts.main.applyOptions({ width: $('chart').clientWidth });
  charts.rsi.applyOptions({ width: $('rsi').clientWidth });
}).observe(document.body);

/* ---------- selectie ---------- */
function select(sym, scroll) {
  selected = sym;
  if (location.hash !== '#' + sym) history.replaceState(null, '', '#' + sym);
  document.querySelectorAll('.card').forEach((el) => el.classList.toggle('active', el.dataset.sym === sym));
  renderDetail();
  if (scroll) $('detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ---------- live prijzen ---------- */
function applyTick(sym, price, change) {
  const prev = live[sym]?.price;
  live[sym] = { price, change };
  const p = document.querySelector(`[data-price="${sym}"]`);
  if (p) {
    p.textContent = fmt(price);
    if (prev != null && price !== prev) {
      p.classList.remove('flash-up', 'flash-down');
      void p.offsetWidth;
      p.classList.add(price > prev ? 'flash-up' : 'flash-down');
      setTimeout(() => p.classList.remove('flash-up', 'flash-down'), 700);
    }
  }
  const ch = document.querySelector(`[data-chg="${sym}"]`);
  if (ch && change != null) { ch.textContent = pct(change); ch.className = `chg ${change >= 0 ? 'up' : 'down'}`; }
  const c = DATA?.coins.find((x) => x.symbol === sym);
  const z = document.querySelector(`[data-zone="${sym}"]`);
  if (c && z) z.textContent = zoneInfo(c).text;
  if (sym === selected) updateDetailPrice();
}

function setLive(on, label) {
  $('live').className = 'live' + (on ? ' on' : '');
  $('live').querySelector('span').textContent = label;
}

function connect() {
  if (!DATA || ws) return;
  const streams = DATA.coins.flatMap((c) => [`${c.symbol.toLowerCase()}@miniTicker`, `${c.symbol.toLowerCase()}@kline_1d`]).join('/');
  try { ws = new WebSocket(WS_URL + streams); } catch { return startPolling(); }
  ws.onopen = () => { wsRetry = 0; setLive(true, 'Live'); stopPolling(); };
  ws.onmessage = (ev) => {
    const { data } = JSON.parse(ev.data);
    if (data.e === '24hrMiniTicker') {
      const price = +data.c, open = +data.o;
      applyTick(data.s, price, (price - open) / open * 100);
    } else if (data.e === 'kline' && data.s === selected && charts) {
      const k = data.k, t = Math.floor(k.t / 1000);
      const row = [t, +k.o, +k.h, +k.l, +k.c, +k.v];
      try {
        charts.candles.update({ time: t, open: row[1], high: row[2], low: row[3], close: row[4] });
        charts.volume.update({ time: t, value: row[5], color: charts.vcol(row) });
      } catch { /* candle ouder dan laatste punt: negeren */ }
    }
  };
  ws.onclose = () => {
    ws = null; setLive(false, 'Opnieuw verbinden…'); startPolling();
    setTimeout(connect, Math.min(30000, 2000 * 2 ** wsRetry++));
  };
  ws.onerror = () => ws && ws.close();
}

async function pollOnce() {
  try {
    const syms = JSON.stringify(DATA.coins.map((c) => c.symbol));
    const r = await fetch(REST_TICKER + encodeURIComponent(syms));
    (await r.json()).forEach((t) => applyTick(t.symbol, +t.lastPrice, +t.priceChangePercent));
    if (!ws) setLive(true, 'Live (30s)');
  } catch { setLive(false, 'Offline'); }
}
function startPolling() { if (!pollTimer) { pollOnce(); pollTimer = setInterval(pollOnce, 30000); } }
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

/* ---------- data laden ---------- */
async function load() {
  try {
    const r = await fetch('data.json?t=' + Date.now(), { cache: 'no-store' });
    const fresh = await r.json();
    if (DATA && fresh.generated_at === DATA.generated_at) return renderUpdated();
    DATA = fresh;
  } catch (e) {
    $('cards').innerHTML = '<div class="panel empty">Kon data.json niet laden. Draai eerst monitor.py.</div>';
    return;
  }
  const hash = location.hash.slice(1);
  if (!selected || !DATA.coins.some((c) => c.symbol === selected)) {
    selected = DATA.coins.some((c) => c.symbol === hash) ? hash : DATA.coins[0]?.symbol;
  }
  renderKpis(); renderCards(); renderHistory(); renderUpdated(); renderDetail();
  Object.entries(live).forEach(([s, v]) => applyTick(s, v.price, v.change));
  connect();
}

/* ---------- thema ---------- */
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  if (DATA) renderDetail();
}
try { applyTheme(localStorage.getItem('radar-theme')); } catch { /* opslag niet beschikbaar */ }
$('theme').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === 'dark'
    : matchMedia('(prefers-color-scheme: dark)').matches;
  const next = dark ? 'light' : 'dark';
  try { localStorage.setItem('radar-theme', next); } catch { /* negeren */ }
  applyTheme(next);
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => DATA && renderDetail());

window.addEventListener('hashchange', () => {
  const s = location.hash.slice(1);
  if (DATA?.coins.some((c) => c.symbol === s) && s !== selected) select(s, true);
});

load();
setInterval(load, 5 * 60 * 1000);
setInterval(renderUpdated, 30 * 1000);

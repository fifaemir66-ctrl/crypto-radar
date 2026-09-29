/* Emir Isiklar · Radar -- website.
 * Laadt het overzicht (data.json) en per symbool de details (charts/<SYM>.json).
 * Crypto-prijzen lopen live mee via de Binance-websocket; aandelen worden elk uur bijgewerkt. */

const COLORS = {
  BTC: '#f7931a', ETH: '#627eea', SOL: '#9945ff', XRP: '#0a7fc2', BNB: '#e0a800', DOGE: '#c2a633', ADA: '#0033ad',
  AVAX: '#e84142', LINK: '#2a5ada', DOT: '#e6007a', LTC: '#345d9d', TRX: '#eb0029', TON: '#0098ea', SUI: '#4da2ff',
  NEAR: '#00a37a', BCH: '#0ac18e',
};
const STOCK_COLORS = ['#2563eb', '#7c3aed', '#0891b2', '#db2777', '#059669', '#d97706', '#4f46e5', '#0f766e'];
const STATUS_ORDER = { setup: 0, zone: 1, wait: 2, down: 3 };
const FILTERS = [['all', 'Alle'], ['setup', 'Setup'], ['zone', 'In zone'], ['wait', 'Wachten'], ['down', 'Geen trend']];
const WS_URL = 'wss://data-stream.binance.vision/stream?streams=';
const REST_TICKER = 'https://data-api.binance.vision/api/v3/ticker/24hr?symbols=';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* geen opslag */ } },
};

let DATA = null;
let market = store.get('radar-market') || 'crypto';
let filter = 'all';
let query = '';
let selected = null;
let pane = 'rsi';
const details = new Map();   // symbool -> detail-json
const live = {};             // symbool -> { price, change }
let charts = null;
let ws = null, wsRetry = 0, pollTimer = null;

/* ---------- formattering ---------- */
function decimals(p) { return p >= 1000 ? 0 : p >= 100 ? 2 : p >= 1 ? 3 : p >= 0.01 ? 4 : 7; }
function fmt(p) {
  if (p == null || isNaN(p)) return '–';
  const d = decimals(Math.abs(p));
  return new Intl.NumberFormat('nl-NL', { minimumFractionDigits: Math.min(d, 4), maximumFractionDigits: d }).format(p);
}
function pct(x, sign = true) {
  if (x == null || isNaN(x)) return '–';
  const s = new Intl.NumberFormat('nl-NL', { minimumFractionDigits: 1, maximumFractionDigits: 2 }).format(Math.abs(x));
  return (sign ? (x >= 0 ? '+' : '−') : '') + s + '%';
}
const nf = (x, d = 1) => new Intl.NumberFormat('nl-NL', { minimumFractionDigits: d, maximumFractionDigits: d }).format(x);
const eur = (x) => '€' + new Intl.NumberFormat('nl-NL', { maximumFractionDigits: 0 }).format(x);
function ago(iso) {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return 'zojuist';
  if (m < 60) return `${m} min geleden`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h} uur geleden` : `${Math.floor(h / 24)} d geleden`;
}
const timeNL = (iso) => new Date(iso).toLocaleString('nl-NL', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const dateNL = (t) => new Date(t * 1000).toLocaleDateString('nl-NL', { day: 'numeric', month: 'short' });
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

/* ---------- helpers ---------- */
function iconHtml(a, extra = '') {
  let col = COLORS[a.base];
  if (!col) col = STOCK_COLORS[[...a.symbol].reduce((s, c) => s + c.charCodeAt(0), 0) % STOCK_COLORS.length];
  return `<span class="coin-icon${a.market === 'stocks' ? ' sq' : ''}${extra}" style="background:${col}">${esc(a.base.slice(0, 4))}</span>`;
}
const badge = (st) => `<span class="badge s-${st.key}"><i></i>${esc(st.label)}</span>`;
const assets = () => DATA ? DATA.assets.filter((a) => a.market === market) : [];
const find = (sym) => DATA?.assets.find((a) => a.symbol === sym);
const priceOf = (a) => live[a.symbol]?.price ?? a.price;
function changeOf(a) {
  if (live[a.symbol]?.change != null) return live[a.symbol].change;
  return a.ref_price ? (a.price - a.ref_price) / a.ref_price * 100 : null;
}
function zoneText(a) {
  const p = priceOf(a);
  if (!a.zone1) return '–';
  if (p >= a.zone1.low * 0.995 && p <= a.zone1.high * 1.005) return 'In zone 1';
  if (p < a.zone1.low) return 'Onder zone 1';
  return pct((p - a.zone1.high) / p * 100, false) + ' erboven';
}

function sparkline(a) {
  const pts = a.spark.filter((v) => v != null);
  if (live[a.symbol]) pts[pts.length - 1] = live[a.symbol].price;
  const min = Math.min(...pts), max = Math.max(...pts), w = 300, h = 38;
  const d = pts.map((v, i) => `${i ? 'L' : 'M'}${(i / (pts.length - 1) * w).toFixed(1)},${(h - 3 - (v - min) / (max - min || 1) * (h - 6)).toFixed(1)}`).join(' ');
  const col = pts[pts.length - 1] >= pts[0] ? 'var(--up)' : 'var(--down)';
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${d} L${w},${h} L0,${h} Z" fill="${col}" opacity=".08"/><path d="${d}" fill="none" stroke="${col}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
}

/* ---------- overzicht ---------- */
function renderSwitch() {
  ['crypto', 'stocks'].forEach((m) => {
    $('tab-' + m).classList.toggle('on', m === market);
    $('tab-' + m).setAttribute('aria-selected', m === market);
    $('n-' + m).textContent = DATA ? DATA.assets.filter((a) => a.market === m).length : 0;
  });
}

function renderKpis() {
  const list = assets();
  const count = (k) => list.filter((a) => a.status.key === k).length;
  const tiles = [['Setups', count('setup'), 'var(--setup)'], ['In koopzone', count('zone'), 'var(--zone)'],
    ['Wachten', count('wait'), 'var(--wait)'], ['Geen trend', count('down'), 'var(--neg)']];
  $('kpis').innerHTML = tiles.map(([l, v, col]) =>
    `<div class="kpi"><div class="label"><i style="background:${col}"></i>${l}</div><div class="value">${v}</div></div>`).join('');
}

function renderFilters() {
  $('filters').innerHTML = FILTERS.map(([k, l]) => `<button class="chip${k === filter ? ' on' : ''}" data-filter="${k}">${l}</button>`).join('');
  $('filters').querySelectorAll('.chip').forEach((b) => b.addEventListener('click', () => { filter = b.dataset.filter; renderFilters(); renderCards(); }));
}

function renderMarketNote() {
  if (market === 'crypto') {
    $('market-note').textContent = 'Live koersen via Binance · analyse op de dagchart, elk uur bijgewerkt · dagslot 02:00 NL-tijd (zomertijd)';
  } else {
    const open = assets().some((a) => a.market_open);
    $('market-note').textContent = `Koersen via Yahoo Finance, elk uur bijgewerkt · Amerikaanse beurs ${open ? 'is nu open' : 'is nu gesloten'} (15:30–22:00 NL-tijd) · bedragen in USD`;
  }
}

function renderCards() {
  const q = query.trim().toLowerCase();
  const list = assets()
    .filter((a) => filter === 'all' || a.status.key === filter)
    .filter((a) => !q || a.name.toLowerCase().includes(q) || a.symbol.toLowerCase().includes(q))
    .sort((a, b) => STATUS_ORDER[a.status.key] - STATUS_ORDER[b.status.key] || b.confluence - a.confluence);
  $('cards').innerHTML = list.length ? list.map((a) => {
    const ch = changeOf(a);
    return `<button class="card${a.symbol === selected ? ' active' : ''}" data-sym="${a.symbol}">
      <div class="card-top">
        <div class="coin-id">${iconHtml(a)}<div><div class="coin-name">${esc(a.name)}</div><div class="muted">${esc(a.symbol)}</div></div></div>
        ${badge(a.status)}
      </div>
      <div class="card-price"><span class="p mono" data-price="${a.symbol}">${fmt(priceOf(a))}</span>
        <span class="chg ${ch >= 0 ? 'up' : 'down'}" data-chg="${a.symbol}">${pct(ch)}</span></div>
      ${sparkline(a)}
      <div class="card-meta">
        <div><div class="k">Trend</div><div class="v">${esc(a.trend)}</div></div>
        <div><div class="k">Bevestiging</div><div class="v">${a.confluence}/7 · RSI ${nf(a.rsi, 0)}</div></div>
        <div><div class="k">Zone 1</div><div class="v" data-zone="${a.symbol}">${esc(zoneText(a))}</div></div>
      </div>
    </button>`;
  }).join('') : '<div class="panel empty empty-cards">Niets gevonden met dit filter.</div>';
  $('cards').querySelectorAll('.card').forEach((el) => el.addEventListener('click', () => select(el.dataset.sym, true)));
}

function renderHistory() {
  const h = (DATA.history || []).filter((e) => (e.market || 'crypto') === market);
  $('history').innerHTML = h.length ? h.slice(0, 25).map((e) =>
    `<li><div class="muted">${timeNL(e.time)}</div><div><div class="h-title">${esc(e.title)}</div><div class="h-msg">${esc(e.message)}</div></div></li>`).join('')
    : '<li class="empty">Nog geen meldingen voor deze markt. Zodra iets in een koopzone komt of er een setup is, verschijnt het hier en op je telefoon.</li>';
}

const renderUpdated = () => DATA && ($('updated').textContent = `Analyse ${ago(DATA.generated_at)}`);

function renderOverview() {
  renderSwitch(); renderKpis(); renderFilters(); renderMarketNote(); renderCards(); renderHistory(); renderUpdated();
}

/* ---------- detail ---------- */
function planHtml(p) {
  if (!p) return '';
  return `<div class="plan">
      <div><div class="k">Entry</div><div class="v mono">${fmt(p.entry)}</div></div>
      <div><div class="k">Stop (−${nf(p.stop_pct)}%)</div><div class="v mono">${fmt(p.stop)}</div></div>
      <div><div class="k">Target (+${nf(p.win_pct)}%)</div><div class="v mono">${fmt(p.target)}</div></div>
    </div>
    <div class="plan-foot">
      <span class="badge ${p.rr >= 2 ? 's-setup' : 's-down'}">R/R ${nf(p.rr)}x</span>
      <span>Positie <b>${eur(p.position)}</b>${p.capped ? ' (max. kapitaal)' : ''} · verlies bij stop ≈ ${eur(p.loss_eur)} · winst bij target ≈ ${eur(p.win_eur)}</span>
    </div>`;
}

const MARK = { ok: '✓', nee: '✕', wacht: '…', info: 'i' };
const listItems = (items) => items.map((i) =>
  `<li><span class="dot ${i.state}">${MARK[i.state]}</span><span class="n">${esc(i.name)}</span><span class="t">${esc(i.text)}</span></li>`).join('');

function poolsHtml(a, d) {
  if (!d.pools.length) return '<p class="muted">Geen equal highs/lows gevonden in de laatste 150 candles.</p>';
  const price = priceOf(a);
  let rows = '', nowDone = false;
  for (const p of d.pools) {
    if (!nowDone && p.price < price) {
      rows += `<tr class="now"><td colspan="5"><b>Prijs nu ${fmt(price)}</b></td></tr>`;
      nowDone = true;
    }
    rows += `<tr><td><span class="kind ${p.kind}">${p.kind}</span></td><td class="mono">${fmt(p.price)}</td>
      <td>${pct((p.price - price) / price * 100)}</td><td>${p.touches}× · ${dateNL(p.t_last)}</td>
      <td class="st-${p.status}">${p.status}</td></tr>`;
  }
  if (!nowDone) rows += `<tr class="now"><td colspan="5"><b>Prijs nu ${fmt(price)}</b></td></tr>`;
  return `<div class="pools-wrap"><table class="pools"><thead><tr><th>Type</th><th>Niveau</th><th>Afstand</th><th>Raakt · laatst</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function profileHtml(a, d) {
  const pr = d.profile, bins = pr.bins;
  const lo = bins[0][0], hi = bins[bins.length - 1][1], maxV = Math.max(...bins.map((b) => b[2]));
  const W = 400, H = 300, L = 70, y = (p) => H - 8 - (p - lo) / (hi - lo) * (H - 16);
  const price = priceOf(a);
  let bars = '';
  bins.forEach(([bl, bh, v]) => {
    const inVa = bh > pr.val && bl < pr.vah, isPoc = pr.poc >= bl && pr.poc <= bh;
    const col = isPoc ? 'var(--accent-2)' : inVa ? 'var(--accent)' : 'var(--poc)';
    const op = isPoc ? 1 : inVa ? 0.55 : 0.3;
    bars += `<rect x="${L}" y="${y(bh) + 0.5}" width="${(v / maxV) * (W - L - 8)}" height="${Math.max(1, y(bl) - y(bh) - 1)}" fill="${col}" opacity="${op}" rx="1.5"/>`;
  });
  const label = (p, txt, col) => `<line x1="${L - 4}" x2="${W}" y1="${y(p)}" y2="${y(p)}" stroke="${col}" stroke-dasharray="3 3"/>
      <text x="${L - 8}" y="${y(p) + 4}" text-anchor="end" font-size="11" fill="${col}">${txt}</text>`;
  const inRange = price >= lo && price <= hi;
  return `<svg class="profile-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Volume profile">
      ${bars}
      <text x="${L - 8}" y="14" text-anchor="end" font-size="11" fill="var(--muted)">${fmt(hi)}</text>
      <text x="${L - 8}" y="${H - 4}" text-anchor="end" font-size="11" fill="var(--muted)">${fmt(lo)}</text>
      ${label(pr.poc, 'POC', 'var(--accent-2)')}
      ${inRange ? label(price, 'Nu', 'var(--text)') : ''}
    </svg>
    <div class="profile-facts">
      <span>POC <b class="mono">${fmt(pr.poc)}</b></span>
      <span>Value area <b class="mono">${fmt(pr.val)} – ${fmt(pr.vah)}</b></span>
      <span class="muted">laatste ${pr.lookback} dagen</span>
    </div>`;
}

function scoreHtml(n) {
  const r = 26, c = 2 * Math.PI * r, col = n >= 5 ? 'var(--setup)' : n >= 4 ? 'var(--zone)' : n >= 3 ? 'var(--wait)' : 'var(--neg)';
  return `<svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="${r}" fill="none" stroke="var(--surface-2)" stroke-width="7"/>
      <circle cx="32" cy="32" r="${r}" fill="none" stroke="${col}" stroke-width="7" stroke-linecap="round"
        stroke-dasharray="${c * n / 7} ${c}" transform="rotate(-90 32 32)"/></svg>
    <div><b>${n} / 7</b><span class="muted">indicatoren bevestigen de koopkant</span></div>`;
}

async function loadDetail(sym) {
  const key = `${sym}@${DATA.generated_at}`;
  if (details.has(key)) return details.get(key);
  const r = await fetch(`charts/${sym}.json?t=${encodeURIComponent(DATA.generated_at)}`);
  const d = await r.json();
  details.set(key, d);
  return d;
}

async function renderDetail() {
  const a = find(selected);
  if (!a) { $('detail').hidden = true; return; }
  let d;
  try { d = await loadDetail(a.symbol); } catch { $('detail').hidden = true; return; }
  if (a.symbol !== selected) return;
  $('detail').hidden = false;
  $('d-icon').outerHTML = iconHtml(a, ' lg').replace('<span ', '<span id="d-icon" ');
  $('d-name').textContent = a.name;
  $('d-sub').textContent = `${a.symbol} · 1D · ${a.source}${a.market === 'stocks' ? (a.market_open ? ' · beurs open' : ' · beurs gesloten') : ''}`;
  updateDetailPrice();

  $('d-verdict').className = `verdict s-${a.status.key}`;
  $('d-verdict').innerHTML = `<b>${esc(a.status.title)}</b><span>${esc(a.status.text)}</span>`;
  $('d-checklist').innerHTML = listItems(d.checklist);
  $('d-scenarios').innerHTML = d.scenarios.map((s) =>
    `<div class="scen"><div class="scen-h"><span class="scen-k">${s.key}</span><span>${esc(s.title)}</span></div><p>${esc(s.text)}</p>${planHtml(s.plan)}</div>`).join('');
  $('d-pools').innerHTML = poolsHtml(a, d);
  $('d-profile').innerHTML = profileHtml(a, d);
  $('d-score').innerHTML = scoreHtml(d.confluence);
  $('d-extras').innerHTML = listItems(d.extras);
  $('d-trend').innerHTML = d.trend_reasons.map((t) => `<li>${esc(t)}</li>`).join('');
  const f = d.fib;
  let fib = f ? `<div class="fib-title">Fibonacci van ${fmt(f.low)} → ${fmt(f.high)}</div>
      <div class="fib-row"><span>38,2%</span><b class="mono">${fmt(f.f382)}</b></div>
      <div class="fib-row"><span>50%</span><b class="mono">${fmt(f.f50)}</b></div>
      <div class="fib-row"><span>61,8%</span><b class="mono">${fmt(f.f618)}</b></div>` : '';
  if (d.pullback_vol != null) {
    fib += `<p class="note">Volume in de terugval: <b>${Math.round(d.pullback_vol * 100)}%</b> van de stijging ervoor. ${d.pullback_vol < 1 ? 'Rustige winstneming (gezond).' : 'Verkopers zijn actief (let op).'}</p>`;
  }
  $('d-fib').innerHTML = fib;
  $('d-zones').innerHTML = d.zones.length ? d.zones.map((z) =>
    `<li><b class="mono">${fmt(z.low)} – ${fmt(z.high)}</b> · ${z.score} niveau${z.score > 1 ? 's' : ''}<br><span class="muted">${z.items.map((i) => esc(i.name)).join(', ')}</span></li>`).join('')
    : '<li class="muted">Geen zones onder de prijs gevonden.</li>';
  buildChart(a, d);
}

function updateDetailPrice() {
  const a = find(selected);
  if (!a) return;
  const ch = changeOf(a);
  $('d-price').textContent = fmt(priceOf(a));
  $('d-change').className = `chg ${ch >= 0 ? 'up' : 'down'}`;
  $('d-change').textContent = `${pct(ch)} ${a.market === 'crypto' ? '(24u)' : '(vandaag)'}`;
}

/* ---------- grafiek ---------- */
function buildChart(a, d) {
  if (typeof LightweightCharts === 'undefined') return;
  if (charts) { charts.main.remove(); charts.sub.remove(); charts = null; }
  const text = css('--muted'), grid = css('--border'), up = css('--up'), down = css('--down');
  const base = {
    layout: { background: { color: 'transparent' }, textColor: text, fontFamily: 'Inter, sans-serif' },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    rightPriceScale: { borderColor: grid, minimumWidth: 78 }, timeScale: { borderColor: grid, rightOffset: 6 },
    crosshair: { mode: 0 }, localization: { priceFormatter: fmt, locale: 'nl-NL' },
  };
  const el = $('chart'), sel = $('sub'), c = d.chart;
  const main = LightweightCharts.createChart(el, { ...base, width: el.clientWidth, height: el.clientHeight });
  const candles = main.addCandlestickSeries({ upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
  candles.setData(c.candles.map((k) => ({ time: k[0], open: k[1], high: k[2], low: k[3], close: k[4] })));
  const volume = main.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
  main.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
  const vcol = (k) => (k[4] >= k[1] ? up : down) + '55';
  volume.setData(c.candles.map((k) => ({ time: k[0], value: k[5], color: vcol(k) })));
  const line = (data, color, width) => main.addLineSeries({ color, lineWidth: width, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
    .setData(data.map(([t, v]) => ({ time: t, value: v })));
  line(c.ema50, css('--ema50'), 2);
  if (c.ema200.length) line(c.ema200, css('--ema200'), 1);
  candles.setMarkers(c.markers.map(([t, kind, label]) => ({
    time: t, position: kind === 'top' ? 'aboveBar' : 'belowBar', shape: kind === 'top' ? 'arrowDown' : 'arrowUp',
    color: ['HH', 'HL'].includes(label) ? up : ['LH', 'LL'].includes(label) ? down : text, text: label,
  })));
  const lc = { zone1: css('--zone1'), zone2: css('--zone2'), target: css('--target'), stop: down, bsl: css('--bsl'), ssl: css('--ssl'), poc: css('--poc') };
  c.lines.forEach((l) => candles.createPriceLine({ price: l.price, color: lc[l.kind], lineWidth: l.kind === 'poc' ? 1 : 1,
    lineStyle: l.kind === 'poc' ? 1 : l.kind === 'bsl' || l.kind === 'ssl' ? 3 : 2, axisLabelVisible: true, title: l.title }));

  const sub = LightweightCharts.createChart(sel, { ...base, width: sel.clientWidth, height: sel.clientHeight,
    localization: { locale: 'nl-NL' }, timeScale: { ...base.timeScale, visible: false } });
  if (pane === 'rsi') {
    const rs = sub.addLineSeries({ color: css('--rsi'), lineWidth: 2, priceLineVisible: false });
    rs.setData(c.rsi.map(([t, v]) => ({ time: t, value: v })));
    [70, 50, 30].forEach((v) => rs.createPriceLine({ price: v, color: text, lineWidth: 1, lineStyle: 2, axisLabelVisible: v !== 50, title: '' }));
  } else {
    sub.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false })
      .setData(c.macd.map(([t, , , h], i, arr) => ({ time: t, value: h,
        color: (h >= 0 ? up : down) + (i > 0 && Math.abs(h) < Math.abs(arr[i - 1][3]) ? '66' : 'cc') })));
    sub.addLineSeries({ color: css('--accent'), lineWidth: 2, priceLineVisible: false, lastValueVisible: false }).setData(c.macd.map(([t, m]) => ({ time: t, value: m })));
    sub.addLineSeries({ color: css('--ema50'), lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(c.macd.map(([t, , s]) => ({ time: t, value: s })));
  }
  main.timeScale().subscribeVisibleLogicalRangeChange((r) => r && sub.timeScale().setVisibleLogicalRange(r));
  const n = c.candles.length;
  main.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 160), to: n + 5 });
  charts = { main, sub, candles, volume, vcol, symbol: a.symbol };
}

new ResizeObserver(() => {
  if (!charts) return;
  charts.main.applyOptions({ width: $('chart').clientWidth });
  charts.sub.applyOptions({ width: $('sub').clientWidth });
}).observe(document.body);

document.querySelectorAll('.pane-tabs button').forEach((b) => b.addEventListener('click', () => {
  pane = b.dataset.pane;
  document.querySelectorAll('.pane-tabs button').forEach((x) => x.classList.toggle('on', x === b));
  const a = find(selected), d = a && details.get(`${a.symbol}@${DATA.generated_at}`);
  if (a && d) buildChart(a, d);
}));

/* ---------- navigatie ---------- */
function setMarket(m, keepSelection) {
  market = m;
  store.set('radar-market', m);
  if (!keepSelection || find(selected)?.market !== m) {
    const first = [...assets()].sort((a, b) => STATUS_ORDER[a.status.key] - STATUS_ORDER[b.status.key])[0];
    selected = first?.symbol || null;
    if (selected) history.replaceState(null, '', '#' + selected);
  }
  renderOverview();
  renderDetail();
}
['crypto', 'stocks'].forEach((m) => $('tab-' + m).addEventListener('click', () => m !== market && setMarket(m)));
$('search').addEventListener('input', (e) => { query = e.target.value; renderCards(); });

function select(sym, scroll) {
  const a = find(sym);
  if (!a) return;
  selected = sym;
  if (location.hash !== '#' + sym) history.replaceState(null, '', '#' + sym);
  if (a.market !== market) { setMarket(a.market, true); } else {
    document.querySelectorAll('.card').forEach((el) => el.classList.toggle('active', el.dataset.sym === sym));
    renderDetail();
  }
  if (scroll) setTimeout(() => $('detail').scrollIntoView({ behavior: 'smooth', block: 'start' }), 60);
}

/* ---------- live crypto-prijzen ---------- */
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
  const a = find(sym), z = document.querySelector(`[data-zone="${sym}"]`);
  if (a && z) z.textContent = zoneText(a);
  if (sym === selected) updateDetailPrice();
}

function setLive(on, label) {
  $('live').className = 'live' + (on ? ' on' : '');
  $('live').querySelector('span').textContent = label;
}

function connect() {
  const cryptos = DATA?.assets.filter((a) => a.market === 'crypto') || [];
  if (!cryptos.length || ws) return;
  const streams = cryptos.flatMap((a) => [`${a.symbol.toLowerCase()}@miniTicker`, `${a.symbol.toLowerCase()}@kline_1d`]).join('/');
  try { ws = new WebSocket(WS_URL + streams); } catch { return startPolling(); }
  ws.onopen = () => { wsRetry = 0; setLive(true, 'Live'); stopPolling(); };
  ws.onmessage = (ev) => {
    const { data } = JSON.parse(ev.data);
    if (data.e === '24hrMiniTicker') {
      const price = +data.c, open = +data.o;
      applyTick(data.s, price, (price - open) / open * 100);
    } else if (data.e === 'kline' && charts && data.s === charts.symbol) {
      const k = data.k, t = Math.floor(k.t / 1000), row = [t, +k.o, +k.h, +k.l, +k.c, +k.v];
      try {
        charts.candles.update({ time: t, open: row[1], high: row[2], low: row[3], close: row[4] });
        charts.volume.update({ time: t, value: row[5], color: charts.vcol(row) });
      } catch { /* oudere candle: negeren */ }
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
    const syms = JSON.stringify(DATA.assets.filter((a) => a.market === 'crypto').map((a) => a.symbol));
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
    if (!fresh.assets) throw new Error('oud formaat');
    if (DATA && fresh.generated_at === DATA.generated_at) return renderUpdated();
    DATA = fresh;
  } catch {
    $('cards').innerHTML = '<div class="panel empty empty-cards">Kon de data niet laden. De eerstvolgende analyse-run vult dit automatisch.</div>';
    return;
  }
  const hashed = find(location.hash.slice(1));
  if (hashed) { market = hashed.market; selected = hashed.symbol; }
  if (!find(selected) || find(selected).market !== market) {
    const first = [...assets()].sort((a, b) => STATUS_ORDER[a.status.key] - STATUS_ORDER[b.status.key])[0];
    selected = first?.symbol || null;
  }
  renderOverview();
  await renderDetail();
  Object.entries(live).forEach(([s, v]) => applyTick(s, v.price, v.change));
  connect();
}

/* ---------- thema ---------- */
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  if (DATA) renderDetail();
}
applyTheme(store.get('radar-theme'));
$('theme').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === 'dark'
    : matchMedia('(prefers-color-scheme: dark)').matches;
  const next = dark ? 'light' : 'dark';
  store.set('radar-theme', next);
  applyTheme(next);
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => DATA && renderDetail());
window.addEventListener('hashchange', () => {
  const s = location.hash.slice(1);
  if (find(s) && s !== selected) select(s, true);
});

load();
setInterval(load, 5 * 60 * 1000);
setInterval(renderUpdated, 30 * 1000);

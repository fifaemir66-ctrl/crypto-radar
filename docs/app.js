/* ZoneHunter -- website.
 * Level 1: market overview (signal per market + one-line reason).
 * Level 2: asset page (signal card, plan, chart).
 * Level 3: expandable details (checklist, scenarios, liquidity, volume profile, indicators, trend, levels).
 * Crypto prices update live via the Binance websocket; stocks refresh hourly. */

const COLORS = {
  BTC: '#f7931a', ETH: '#627eea', SOL: '#9945ff', XRP: '#0a7fc2', BNB: '#e0a800', DOGE: '#c2a633', ADA: '#0033ad',
  AVAX: '#e84142', LINK: '#2a5ada', DOT: '#e6007a', LTC: '#345d9d', TRX: '#eb0029', TON: '#0098ea', SUI: '#4da2ff',
  NEAR: '#00a37a', BCH: '#0ac18e',
};
const STOCK_COLORS = ['#2563eb', '#7c3aed', '#0891b2', '#db2777', '#059669', '#d97706', '#4f46e5', '#0f766e'];
const ORDER = { buy: 0, ready: 1, watch: 2, avoid: 3 };
const TILES = [
  ['buy', 'BUY SIGNAL', 'All strategy rules met'],
  ['ready', 'GET READY', 'In a buy zone, waiting for trigger'],
  ['watch', 'WATCH', 'Uptrend, waiting for a pullback'],
  ['avoid', 'AVOID', 'No uptrend, stay out'],
];
const WS_URL = 'wss://data-stream.binance.vision/stream?streams=';
const REST_TICKER = 'https://data-api.binance.vision/api/v3/ticker/24hr?symbols=';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

let DATA = null;
let route = { view: 'home', market: 'crypto' };
let filter = null;
let query = '';
let alertFilter = 'all';
let pane = 'rsi';
const overlays = { zones: true, ema: true, liq: false, swings: false };
const details = new Map();
const live = {};
let charts = null;
let ws = null, wsRetry = 0, pollTimer = null;

/* ---------- formatting ---------- */
function decimals(p) { return p >= 1000 ? 0 : p >= 1 ? 2 : p >= 0.01 ? 4 : 7; }
function fmt(p) {
  if (p == null || isNaN(p)) return '–';
  const d = decimals(Math.abs(p));
  return new Intl.NumberFormat('en-US', { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d }).format(p);
}
function pct(x, sign = true) {
  if (x == null || isNaN(x)) return '–';
  const s = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 2 }).format(Math.abs(x));
  return (sign ? (x >= 0 ? '+' : '−') : '') + s + '%';
}
const nf = (x, d = 1) => new Intl.NumberFormat('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }).format(x);
const eur = (x) => '€' + new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(x);
function ago(iso) {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h} h ago` : `${Math.floor(h / 24)} d ago`;
}
const when = (iso) => new Date(iso).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const dayLabel = (d) => d ? new Date(d + 'T12:00:00Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : '–';
const shortDate = (t) => new Date(t * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

/* ---------- helpers ---------- */
function iconHtml(a, extra = '', id = '') {
  let col = COLORS[a.base];
  if (!col) col = STOCK_COLORS[[...a.symbol].reduce((s, c) => s + c.charCodeAt(0), 0) % STOCK_COLORS.length];
  return `<span ${id ? `id="${id}" ` : ''}class="coin-icon${a.market === 'stocks' ? ' sq' : ''}${extra}" style="background:${col}">${esc(a.base.slice(0, 4))}</span>`;
}
const sigBadge = (s) => `<span class="sig sig-${s.key}">${esc(s.label)}</span>`;
const find = (sym) => DATA?.assets.find((a) => a.symbol === sym);
const marketAssets = (m) => DATA ? DATA.assets.filter((a) => a.market === m) : [];
const priceOf = (a) => live[a.symbol]?.price ?? a.price;
function changeOf(a) {
  if (live[a.symbol]?.change != null) return live[a.symbol].change;
  return a.ref_price ? (a.price - a.ref_price) / a.ref_price * 100 : null;
}
const sigColor = (k) => `var(--${k})`;
function liveZoneNote(a) {
  if (!a.zone1 || a.signal.key === 'avoid') return '';
  const p = priceOf(a);
  if (p >= a.zone1.low * 0.99 && p <= a.zone1.high * 1.01) return 'Live price is inside the buy zone now';
  if (p < a.zone1.low) return 'Live price is below the buy zone';
  return `Buy zone ${fmt(a.zone1.low)}–${fmt(a.zone1.high)} · ${pct((p - a.zone1.high) / p * 100, false)} away`;
}

/* ---------- routing ---------- */
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  if (h.startsWith('asset/')) return { view: 'asset', symbol: h.slice(6) };
  if (h === 'alerts') return { view: 'alerts' };
  if (h === 'stocks' || h === 'crypto') return { view: 'home', market: h };
  if (h && find(h)) return { view: 'asset', symbol: h };           // old links: #BTCUSDT
  return { view: 'home', market: store.get('zh-market') || 'crypto' };
}

function render() {
  route = parseHash();
  ['home', 'asset', 'alerts'].forEach((v) => { $('view-' + v).hidden = route.view !== v; });
  const navKey = route.view === 'asset' ? find(route.symbol)?.market : route.view === 'alerts' ? 'alerts' : route.market;
  document.querySelectorAll('[data-nav]').forEach((a) => a.classList.toggle('on', a.dataset.nav === navKey));
  if (!DATA) return;
  if (route.view === 'home') { store.set('zh-market', route.market); renderHome(); }
  else if (route.view === 'asset') renderAsset();
  else renderAlerts();
}
window.addEventListener('hashchange', () => { render(); window.scrollTo(0, 0); });

/* ---------- level 1: overview ---------- */
function renderHome() {
  const m = route.market, list = marketAssets(m);
  $('home-title').textContent = m === 'crypto' ? `Crypto · ${list.length} markets` : `Stocks · ${list.length} markets`;
  $('updated').textContent = `Updated ${ago(DATA.generated_at)}`;
  $('tiles').innerHTML = TILES.map(([k, l, d]) => {
    const n = list.filter((a) => a.signal.key === k).length;
    return `<button class="tile${filter === k ? ' on' : ''}" data-k="${k}"><span class="sig sig-${k}">${l}</span><span class="n">${n}</span><span class="d">${d}</span></button>`;
  }).join('');
  $('tiles').querySelectorAll('.tile').forEach((b) => b.addEventListener('click', () => { filter = filter === b.dataset.k ? null : b.dataset.k; renderHome(); }));
  if (m === 'crypto') {
    $('market-note').textContent = 'Live prices · signals set at the daily close (00:00 UTC)';
  } else {
    const open = list.some((a) => a.market_open);
    $('market-note').textContent = `US market ${open ? 'open' : 'closed'} · prices refresh hourly · signals set at the US close`;
  }
  const q = query.trim().toLowerCase();
  const rows = list
    .filter((a) => !filter || a.signal.key === filter)
    .filter((a) => !q || a.name.toLowerCase().includes(q) || a.symbol.toLowerCase().includes(q))
    .sort((a, b) => ORDER[a.signal.key] - ORDER[b.signal.key] || b.signal.strength - a.signal.strength);
  $('list').innerHTML = rows.length ? rows.map((a) => {
    const ch = changeOf(a);
    const note = liveZoneNote(a);
    return `<a class="row" href="#/asset/${a.symbol}">
      <div class="name-cell coin-id">${iconHtml(a)}<div><div class="coin-name">${esc(a.name)}</div><div class="muted">${esc(a.symbol)}</div></div></div>
      <div class="price-cell"><div class="p mono" data-price="${a.symbol}">${fmt(priceOf(a))}</div><div class="chg ${ch >= 0 ? 'up' : 'down'}" data-chg="${a.symbol}">${pct(ch)}</div></div>
      <div class="sig-cell">${sigBadge(a.signal)}<div class="meter" title="Signal strength ${a.signal.strength}/100"><i style="width:${a.signal.strength}%;background:${sigColor(a.signal.key)}"></i></div></div>
      <div class="reason">${esc(a.signal.reason)}${note ? `<small data-zone="${a.symbol}">${esc(note)}</small>` : ''}</div>
      <div class="chev">›</div>
    </a>`;
  }).join('') : '<div class="empty">Nothing here with this filter.</div>';
}
$('search').addEventListener('input', (e) => { query = e.target.value; renderHome(); });

/* ---------- level 2 + 3: asset ---------- */
async function loadDetail(sym) {
  const key = `${sym}@${DATA.generated_at}`;
  if (details.has(key)) return details.get(key);
  const r = await fetch(`charts/${sym}.json?t=${encodeURIComponent(DATA.generated_at)}`);
  const d = await r.json();
  details.set(key, d);
  return d;
}

function ring(v, color, size = 92) {
  const r = 38, c = 2 * Math.PI * r;
  return `<svg viewBox="0 0 92 92" width="${size}" height="${size}" aria-hidden="true">
    <circle cx="46" cy="46" r="${r}" fill="none" stroke="var(--surface-2)" stroke-width="9"/>
    <circle cx="46" cy="46" r="${r}" fill="none" stroke="${color}" stroke-width="9" stroke-linecap="round" stroke-dasharray="${c * v / 100} ${c}" transform="rotate(-90 46 46)"/>
    <text x="46" y="53" text-anchor="middle" font-size="22" font-weight="800" fill="var(--text)">${v}</text></svg>`;
}

function planGrid(p) {
  if (!p) return '';
  return `<div class="plan">
    <div><div class="k">Entry</div><div class="v mono">${fmt(p.entry)}</div></div>
    <div><div class="k">Stop (−${nf(p.stop_pct)}%)</div><div class="v mono">${fmt(p.stop)}</div></div>
    <div><div class="k">Target (+${nf(p.win_pct)}%)</div><div class="v mono">${fmt(p.target)}</div></div>
    <div><div class="k">Risk / reward</div><div class="v">${nf(p.rr)}x</div></div>
    <div><div class="k">Position${p.capped ? ' (max)' : ''}</div><div class="v">${eur(p.position)} <span class="muted">risk ${eur(p.loss_eur)}</span></div></div>
  </div>`;
}

const MARK = { ok: '✓', no: '✕', wait: '…', info: 'i' };
const checkItems = (items) => items.map((i) =>
  `<li><span class="dot ${i.state}">${MARK[i.state]}</span><span class="n">${esc(i.name)}</span><span class="t">${esc(i.text)}</span></li>`).join('');

function poolsHtml(a, d) {
  if (!d.pools.length) return '<p class="muted">No equal highs or lows found in the last 150 days.</p>';
  const price = priceOf(a);
  let rows = '', done = false;
  const nowRow = `<tr class="now"><td colspan="5"><b>Price now ${fmt(price)}</b></td></tr>`;
  for (const p of d.pools) {
    if (!done && p.price < price) { rows += nowRow; done = true; }
    rows += `<tr><td><span class="kind ${p.kind}">${p.kind}</span></td><td class="mono">${fmt(p.price)}</td>
      <td>${pct((p.price - price) / price * 100)}</td><td>${p.touches}× · ${shortDate(p.t_last)}</td><td class="st-${p.status}">${p.status}</td></tr>`;
  }
  if (!done) rows += nowRow;
  return `<div class="pools-wrap"><table class="pools"><thead><tr><th>Type</th><th>Level</th><th>Distance</th><th>Touches · last</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function profileHtml(a, d) {
  const pr = d.profile, bins = pr.bins;
  const lo = bins[0][0], hi = bins[bins.length - 1][1], maxV = Math.max(...bins.map((b) => b[2]));
  const W = 420, H = 280, L = 74, y = (p) => H - 8 - (p - lo) / (hi - lo) * (H - 16);
  const price = priceOf(a);
  let bars = '';
  bins.forEach(([bl, bh, v]) => {
    const inVa = bh > pr.val && bl < pr.vah, isPoc = pr.poc >= bl && pr.poc <= bh;
    const col = isPoc ? 'var(--accent-2)' : inVa ? 'var(--accent)' : 'var(--poc)';
    bars += `<rect x="${L}" y="${y(bh) + 0.5}" width="${(v / maxV) * (W - L - 8)}" height="${Math.max(1, y(bl) - y(bh) - 1)}" fill="${col}" opacity="${isPoc ? 1 : inVa ? 0.55 : 0.3}" rx="1.5"/>`;
  });
  const mark = (p, txt, col) => `<line x1="${L - 4}" x2="${W}" y1="${y(p)}" y2="${y(p)}" stroke="${col}" stroke-dasharray="3 3"/>
      <text x="${L - 8}" y="${y(p) + 4}" text-anchor="end" font-size="11" fill="${col}">${txt}</text>`;
  return `<svg class="profile-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Volume profile">${bars}
      <text x="${L - 8}" y="14" text-anchor="end" font-size="11" fill="var(--muted)">${fmt(hi)}</text>
      <text x="${L - 8}" y="${H - 4}" text-anchor="end" font-size="11" fill="var(--muted)">${fmt(lo)}</text>
      ${mark(pr.poc, 'POC', 'var(--accent-2)')}${price >= lo && price <= hi ? mark(price, 'Now', 'var(--text)') : ''}</svg>
    <div class="profile-facts"><span>POC <b class="mono">${fmt(pr.poc)}</b></span><span>Value area <b class="mono">${fmt(pr.val)} – ${fmt(pr.vah)}</b></span><span class="muted">last ${pr.lookback} days</span></div>`;
}

async function renderAsset() {
  const a = find(route.symbol);
  if (!a) { location.hash = '#/crypto'; return; }
  $('back').href = `#/${a.market}`;
  $('back').textContent = `← ${a.market === 'crypto' ? 'Crypto' : 'Stocks'}`;
  $('a-icon').outerHTML = iconHtml(a, ' lg', 'a-icon');
  $('a-name').textContent = a.name;
  $('a-sub').textContent = `${a.symbol} · daily chart · ${a.source}${a.market === 'stocks' ? (a.market_open ? ' · market open' : ' · market closed') : ''}`;
  updateAssetPrice();

  let d;
  try { d = await loadDetail(a.symbol); } catch { $('a-signal').innerHTML = '<p class="muted">Could not load details.</p>'; return; }
  if (route.view !== 'asset' || route.symbol !== a.symbol) return;
  const s = d.signal;
  const scenA = d.scenarios.find((x) => x.key === 'A');
  const plan = s.key === 'buy' ? d.plan_close : s.key === 'avoid' ? null : scenA?.plan;
  $('a-signal').className = `signal-card k-${s.key}`;
  $('a-signal').innerHTML = `
    <div class="top">${sigBadge(s)}<span class="since">since ${dayLabel(a.signal_since)} · based on the ${dayLabel(d.close_day)} daily close</span></div>
    <p class="reason">${esc(s.reason)}.</p>
    <p class="plan-text">${esc(s.plan)}</p>
    <div class="strength">${ring(s.strength, sigColor(s.key))}<b>Signal strength</b></div>
    ${plan ? `<div style="grid-column:1/-1" class="muted">${s.key === 'buy' ? 'Trade plan' : 'Plan if the trigger comes'} · position sized so the stop costs about ${eur(DATA.config.risk_eur)}${
      s.key !== 'buy' && plan.rr < 2 ? ` · <b style="color:var(--avoid)">risk/reward below 2x: this zone alone would not give a buy signal</b>` : ''}</div>${planGrid(plan)}` : ''}`;

  const passed = d.checklist.filter((c) => c.state === 'ok').length;
  $('s-check').textContent = `${passed} of ${d.checklist.length} checks passed`;
  $('a-checklist').innerHTML = checkItems(d.checklist);
  $('s-scen').textContent = `${d.scenarios.length} scenarios`;
  $('a-scenarios').innerHTML = d.scenarios.map((x) => `<div class="scen"><div class="scen-h"><span class="scen-k">${x.key}</span><span>${esc(x.title)}</span></div><p>${esc(x.text)}</p>${x.plan ? `<div class="mini-plan">
      <div><div class="k">Entry</div><div class="v mono">${fmt(x.plan.entry)}</div></div>
      <div><div class="k">Stop</div><div class="v mono">${fmt(x.plan.stop)}</div></div>
      <div><div class="k">Target</div><div class="v mono">${fmt(x.plan.target)}</div></div></div>
      <div class="plan-foot">R/R ${nf(x.plan.rr)}x · position ${eur(x.plan.position)} · risk ≈ ${eur(x.plan.loss_eur)}</div>` : ''}</div>`).join('');
  const openPools = d.pools.filter((p) => p.status === 'open').length;
  $('s-liq').textContent = `${openPools} untapped pool${openPools === 1 ? '' : 's'}`;
  $('a-pools').innerHTML = poolsHtml(a, d);
  $('s-prof').textContent = `POC ${fmt(d.profile.poc)}`;
  $('a-profile').innerHTML = profileHtml(a, d);
  $('s-ind').textContent = `${d.confluence} of 7 positive`;
  $('a-extras').innerHTML = checkItems(d.extras);
  $('s-trend').textContent = `${d.trend} · weekly ${d.weekly_trend}`;
  $('a-trend').innerHTML = d.trend_reasons.map((t) => `<li>${esc(t)}</li>`).join('');
  const f = d.fib;
  let fib = f ? `<div class="fib-title">Fibonacci of the last swing ${fmt(f.low)} → ${fmt(f.high)}</div>
      <div class="fib-row"><span>38.2%</span><b class="mono">${fmt(f.f382)}</b></div>
      <div class="fib-row"><span>50%</span><b class="mono">${fmt(f.f50)}</b></div>
      <div class="fib-row"><span>61.8%</span><b class="mono">${fmt(f.f618)}</b></div>` : '';
  if (d.pullback_vol != null) fib += `<p class="note">Volume during the pullback is ${Math.round(d.pullback_vol * 100)}% of the rally before it: ${d.pullback_vol < 1 ? 'calm profit-taking (healthy).' : 'sellers are active (careful).'}</p>`;
  $('a-fib').innerHTML = fib;
  $('s-zones').textContent = `${d.zones.length} zones below price`;
  $('a-zones').innerHTML = d.zones.length ? d.zones.map((z) =>
    `<li><b class="mono">${fmt(z.low)} – ${fmt(z.high)}</b> · ${z.score} level${z.score > 1 ? 's' : ''}<br><span class="muted">${z.items.map((i) => esc(i.name)).join(', ')}</span></li>`).join('')
    : '<li class="muted">No support zones below price.</li>';
  buildChart(a, d);
}

function updateAssetPrice() {
  const a = route.view === 'asset' && find(route.symbol);
  if (!a) return;
  const ch = changeOf(a);
  $('a-price').textContent = fmt(priceOf(a));
  $('a-change').className = `chg ${ch >= 0 ? 'up' : 'down'}`;
  $('a-change').textContent = `${pct(ch)} ${a.market === 'crypto' ? '24h' : 'today'}`;
}

/* ---------- chart ---------- */
const LINE_KINDS = { zones: ['zone1', 'zone2', 'target', 'stop'], liq: ['bsl', 'ssl', 'poc'] };
function buildChart(a, d) {
  if (typeof LightweightCharts === 'undefined') return;
  if (charts) { charts.main.remove(); charts.sub.remove(); charts = null; }
  const text = css('--muted'), grid = css('--border'), up = css('--up'), down = css('--down');
  const base = {
    layout: { background: { color: 'transparent' }, textColor: text, fontFamily: 'Inter, sans-serif' },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    rightPriceScale: { borderColor: grid, minimumWidth: 78 }, timeScale: { borderColor: grid, rightOffset: 6 },
    crosshair: { mode: 0 }, localization: { priceFormatter: fmt, locale: 'en-US' },
  };
  const el = $('chart'), sel = $('sub'), c = d.chart;
  const main = LightweightCharts.createChart(el, { ...base, width: el.clientWidth, height: el.clientHeight });
  const candles = main.addCandlestickSeries({ upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
  candles.setData(c.candles.map((k) => ({ time: k[0], open: k[1], high: k[2], low: k[3], close: k[4] })));
  const volume = main.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
  main.priceScale('vol').applyOptions({ scaleMargins: { top: 0.86, bottom: 0 } });
  const vcol = (k) => (k[4] >= k[1] ? up : down) + '55';
  volume.setData(c.candles.map((k) => ({ time: k[0], value: k[5], color: vcol(k) })));
  const legend = [];
  if (overlays.ema) {
    const add = (data, color, w) => main.addLineSeries({ color, lineWidth: w, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
      .setData(data.map(([t, v]) => ({ time: t, value: v })));
    add(c.ema50, css('--ema50'), 2);
    if (c.ema200.length) add(c.ema200, css('--ema200'), 1);
    legend.push(['--ema50', 'EMA 50'], ['--ema200', 'EMA 200']);
  }
  if (overlays.swings) {
    candles.setMarkers(c.markers.map(([t, kind, label]) => ({
      time: t, position: kind === 'top' ? 'aboveBar' : 'belowBar', shape: kind === 'top' ? 'arrowDown' : 'arrowUp',
      color: ['HH', 'HL'].includes(label) ? up : ['LH', 'LL'].includes(label) ? down : text, text: label,
    })));
  }
  const col = { zone1: css('--zone1'), zone2: css('--zone2'), target: css('--target'), stop: down, bsl: css('--bsl'), ssl: css('--ssl'), poc: css('--poc') };
  const shown = [...(overlays.zones ? LINE_KINDS.zones : []), ...(overlays.liq ? LINE_KINDS.liq : [])];
  c.lines.filter((l) => shown.includes(l.kind)).forEach((l) => candles.createPriceLine({
    price: l.price, color: col[l.kind], lineWidth: l.kind.startsWith('zone') ? 2 : 1,
    lineStyle: l.kind === 'poc' ? 1 : l.kind === 'bsl' || l.kind === 'ssl' ? 3 : 2, axisLabelVisible: true, title: l.title }));
  if (overlays.zones) legend.push(['--zone1', 'Buy zone 1'], ['--zone2', 'Buy zone 2'], ['--target', 'Target'], ['--down', 'Trend line']);
  if (overlays.liq) legend.push(['--bsl', 'BSL (equal highs)'], ['--ssl', 'SSL (equal lows)'], ['--poc', 'Volume POC']);
  $('legend').innerHTML = legend.map(([v, l]) => `<span><i style="background:var(${v})"></i>${l}</span>`).join('');

  const sub = LightweightCharts.createChart(sel, { ...base, width: sel.clientWidth, height: sel.clientHeight,
    localization: { locale: 'en-US' }, timeScale: { ...base.timeScale, visible: false } });
  if (pane === 'rsi') {
    const rs = sub.addLineSeries({ color: css('--rsi'), lineWidth: 2, priceLineVisible: false });
    rs.setData(c.rsi.map(([t, v]) => ({ time: t, value: v })));
    [70, 30].forEach((v) => rs.createPriceLine({ price: v, color: text, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' }));
  } else {
    sub.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false })
      .setData(c.macd.map(([t, , , h], i, arr) => ({ time: t, value: h,
        color: (h >= 0 ? up : down) + (i > 0 && Math.abs(h) < Math.abs(arr[i - 1][3]) ? '66' : 'cc') })));
    sub.addLineSeries({ color: css('--accent'), lineWidth: 2, priceLineVisible: false, lastValueVisible: false }).setData(c.macd.map(([t, m]) => ({ time: t, value: m })));
    sub.addLineSeries({ color: css('--ema50'), lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(c.macd.map(([t, , s]) => ({ time: t, value: s })));
  }
  main.timeScale().subscribeVisibleLogicalRangeChange((r) => r && sub.timeScale().setVisibleLogicalRange(r));
  const n = c.candles.length;
  main.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 140), to: n + 5 });
  charts = { main, sub, candles, volume, vcol, symbol: a.symbol, data: d };
}

function rebuild() {
  const a = route.view === 'asset' && find(route.symbol);
  if (a && charts?.data) buildChart(a, charts.data);
}
$('overlays').querySelectorAll('.tg').forEach((b) => b.addEventListener('click', () => {
  overlays[b.dataset.ov] = !overlays[b.dataset.ov];
  b.classList.toggle('on', overlays[b.dataset.ov]);
  rebuild();
}));
$('panes').querySelectorAll('.tg').forEach((b) => b.addEventListener('click', () => {
  pane = b.dataset.pane;
  $('panes').querySelectorAll('.tg').forEach((x) => x.classList.toggle('on', x === b));
  rebuild();
}));
new ResizeObserver(() => {
  if (!charts) return;
  charts.main.applyOptions({ width: $('chart').clientWidth });
  charts.sub.applyOptions({ width: $('sub').clientWidth });
}).observe(document.body);

/* ---------- alerts ---------- */
function renderAlerts() {
  const h = (DATA.history || []).filter((e) => alertFilter === 'all' || (e.market || 'crypto') === alertFilter);
  $('history').innerHTML = h.length ? h.map((e) => {
    const title = e.symbol && find(e.symbol) ? `<a href="#/asset/${e.symbol}">${esc(e.title)} ›</a>` : esc(e.title);
    return `<li><div class="muted">${when(e.time)}</div><div><div class="h-title">${title}</div><div class="h-msg">${esc(e.message)}</div></div></li>`;
  }).join('') : '<li class="muted">No alerts yet.</li>';
}
$('alert-filter').querySelectorAll('.tg').forEach((b) => b.addEventListener('click', () => {
  alertFilter = b.dataset.af;
  $('alert-filter').querySelectorAll('.tg').forEach((x) => x.classList.toggle('on', x === b));
  renderAlerts();
}));

/* ---------- live crypto prices ---------- */
function applyTick(sym, price, change) {
  const prev = live[sym]?.price;
  live[sym] = { price, change };
  document.querySelectorAll(`[data-price="${sym}"]`).forEach((p) => {
    p.textContent = fmt(price);
    if (prev != null && price !== prev) {
      p.classList.remove('flash-up', 'flash-down');
      void p.offsetWidth;
      p.classList.add(price > prev ? 'flash-up' : 'flash-down');
      setTimeout(() => p.classList.remove('flash-up', 'flash-down'), 700);
    }
  });
  const ch = document.querySelector(`[data-chg="${sym}"]`);
  if (ch && change != null) { ch.textContent = pct(change); ch.className = `chg ${change >= 0 ? 'up' : 'down'}`; }
  const a = find(sym), z = document.querySelector(`[data-zone="${sym}"]`);
  if (a && z) z.textContent = liveZoneNote(a);
  if (route.view === 'asset' && route.symbol === sym) updateAssetPrice();
}
function setLive(on, label) {
  $('live').className = 'live' + (on ? ' on' : '');
  $('live').querySelector('span').textContent = label;
}
function connect() {
  const cryptos = marketAssets('crypto');
  if (!cryptos.length || ws) return;
  const streams = cryptos.flatMap((a) => [`${a.symbol.toLowerCase()}@miniTicker`, `${a.symbol.toLowerCase()}@kline_1d`]).join('/');
  try { ws = new WebSocket(WS_URL + streams); } catch { return startPolling(); }
  ws.onopen = () => { wsRetry = 0; setLive(true, 'Live'); stopPolling(); };
  ws.onmessage = (ev) => {
    const { data } = JSON.parse(ev.data);
    if (data.e === '24hrMiniTicker') {
      const price = +data.c, open = +data.o;
      applyTick(data.s, price, (price - open) / open * 100);
    } else if (data.e === 'kline' && charts && data.s === charts.symbol && route.view === 'asset') {
      const k = data.k, t = Math.floor(k.t / 1000), row = [t, +k.o, +k.h, +k.l, +k.c, +k.v];
      try {
        charts.candles.update({ time: t, open: row[1], high: row[2], low: row[3], close: row[4] });
        charts.volume.update({ time: t, value: row[5], color: charts.vcol(row) });
      } catch { /* older candle: ignore */ }
    }
  };
  ws.onclose = () => {
    ws = null; setLive(false, 'Reconnecting…'); startPolling();
    setTimeout(connect, Math.min(30000, 2000 * 2 ** wsRetry++));
  };
  ws.onerror = () => ws && ws.close();
}
async function pollOnce() {
  try {
    const syms = JSON.stringify(marketAssets('crypto').map((a) => a.symbol));
    const r = await fetch(REST_TICKER + encodeURIComponent(syms));
    (await r.json()).forEach((t) => applyTick(t.symbol, +t.lastPrice, +t.priceChangePercent));
    if (!ws) setLive(true, 'Live (30s)');
  } catch { setLive(false, 'Offline'); }
}
function startPolling() { if (!pollTimer) { pollOnce(); pollTimer = setInterval(pollOnce, 30000); } }
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

/* ---------- data ---------- */
async function load() {
  try {
    const r = await fetch('data.json?t=' + Date.now(), { cache: 'no-store' });
    const fresh = await r.json();
    if (!fresh.assets || !fresh.assets[0]?.signal) throw new Error('old format');
    if (DATA && fresh.generated_at === DATA.generated_at) {
      if (route.view === 'home') $('updated').textContent = `Updated ${ago(DATA.generated_at)}`;
      return;
    }
    DATA = fresh;
  } catch {
    $('view-home').hidden = false;
    $('list').innerHTML = '<div class="empty">Data is being prepared. The next analysis run fills this in automatically.</div>';
    return;
  }
  render();
  Object.entries(live).forEach(([s, v]) => applyTick(s, v.price, v.change));
  connect();
}

/* ---------- theme ---------- */
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  rebuild();
}
applyTheme(store.get('zh-theme'));
$('theme').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === 'dark'
    : matchMedia('(prefers-color-scheme: dark)').matches;
  const next = dark ? 'light' : 'dark';
  store.set('zh-theme', next);
  applyTheme(next);
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', rebuild);

render();
load();
setInterval(load, 5 * 60 * 1000);

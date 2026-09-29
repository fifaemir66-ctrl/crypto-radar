"""
Radar -- analyse-engine voor crypto en aandelen.

Voert de technische-analyse-checklist uit op dagcandles:
  * trend (HH/HL + EMA 50), steunzones (oude toppen, Fibonacci, EMA 50, volume, liquiditeit)
  * liquiditeitszones: equal highs (BSL) / equal lows (SSL), sweeps, volume profile (POC / value area)
  * RSI, candle-patroon, volume, risico/winst met positiegrootte
  * extra bevestiging: EMA 20/200, MACD, ADX, Stoch RSI, Bollinger, OBV, ATR

Educatief hulpmiddel -- geen financieel advies.
"""

import csv
import io
import json
import time
import urllib.request

STOP_BUF = 0.985  # stop 1,5% onder de onderkant van de zone

BINANCE_URLS = [
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
]


# ================================================================ data

def _get(url, as_json=True):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (radar)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if as_json else raw.decode()


def _binance(symbol, limit):
    last_err = None
    for base in BINANCE_URLS:
        try:
            raw = _get(f"{base}?symbol={symbol}&interval=1d&limit={limit}")
            return [{"t": k[0] // 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                     "c": float(k[4]), "v": float(k[5])} for k in raw]
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Binance: {last_err}")


def _coinbase(symbol):
    base = symbol[:-4] if symbol.endswith("USDT") else symbol[:-3]
    raw = _get(f"https://api.exchange.coinbase.com/products/{base}-USD/candles?granularity=86400")
    rows = sorted(raw, key=lambda k: k[0])
    return [{"t": int(k[0]), "o": float(k[3]), "h": float(k[2]), "l": float(k[1]),
             "c": float(k[4]), "v": float(k[5])} for k in rows]


def fetch_crypto(symbol, limit=400):
    """Geeft (candles, bron, laatste_gesloten). De laatste crypto-candle is altijd nog open."""
    try:
        return _binance(symbol, limit), "Binance", False
    except Exception as e:
        try:
            return _coinbase(symbol), "Coinbase", False
        except Exception as e2:
            raise RuntimeError(f"Geen data voor {symbol} ({e}; Coinbase: {e2})")


def _yahoo(ticker):
    last_err = None
    for host in ("query1", "query2"):
        for attempt in range(2):
            try:
                d = _get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?range=2y&interval=1d")
                res = d["chart"]["result"][0]
                q = res["indicators"]["quote"][0]
                candles = []
                for i, t in enumerate(res["timestamp"]):
                    o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
                    if None in (o, h, l, c):
                        continue
                    candles.append({"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": float(v or 0)})
                meta = res["meta"]
                reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
                now = time.time()
                live = bool(reg) and reg["start"] <= now < reg["end"] and candles[-1]["t"] >= reg["start"] - 3600
                if meta.get("regularMarketPrice") and live:
                    candles[-1]["c"] = float(meta["regularMarketPrice"])
                return candles, not live, meta.get("currency", "USD")
            except Exception as e:
                last_err = e
                time.sleep(1.5)
    raise RuntimeError(f"Yahoo: {last_err}")


def _stooq(ticker):
    raw = _get(f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d", as_json=False)
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows or "Close" not in rows[0]:
        raise RuntimeError("Stooq: geen data")
    candles = []
    for r in rows[-500:]:
        t = int(time.mktime(time.strptime(r["Date"], "%Y-%m-%d")))
        candles.append({"t": t, "o": float(r["Open"]), "h": float(r["High"]), "l": float(r["Low"]),
                        "c": float(r["Close"]), "v": float(r.get("Volume") or 0)})
    return candles


def fetch_stock(ticker):
    """Geeft (candles, bron, laatste_gesloten, valuta)."""
    try:
        candles, closed, cur = _yahoo(ticker)
        return candles, "Yahoo Finance", closed, cur
    except Exception as e:
        try:
            return _stooq(ticker), "Stooq", True, "USD"
        except Exception as e2:
            raise RuntimeError(f"Geen data voor {ticker} ({e}; {e2})")


# ================================================================ indicatoren

def ema(values, n):
    out, k, prev = [], 2 / (n + 1), None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def sma(values, n):
    out = [None] * len(values)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1:i + 1]
        if None not in w:
            out[i] = sum(w) / n
    return out


def rma(values, n):
    """Wilder-gemiddelde; accepteert None aan het begin."""
    out = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or len(values) - start < n:
        return out
    prev = sum(values[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def rsi(values, n=14):
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(values)):
        if i > n:
            avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
            avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def true_range(candles):
    tr = [candles[0]["h"] - candles[0]["l"]]
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]["c"]
        tr.append(max(c["h"] - c["l"], abs(c["h"] - p), abs(c["l"] - p)))
    return tr


def atr(candles, n=14):
    return rma(true_range(candles), n)


def macd(closes, fast=12, slow=26, signal=9):
    line = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    sig = ema(line, signal)
    return line, sig, [a - b for a, b in zip(line, sig)]


def stoch_rsi(rsi_vals, n=14, k=3, d=3):
    raw = [None] * len(rsi_vals)
    for i in range(n - 1, len(rsi_vals)):
        w = rsi_vals[i - n + 1:i + 1]
        if None in w:
            continue
        lo, hi = min(w), max(w)
        raw[i] = 50.0 if hi == lo else (rsi_vals[i] - lo) / (hi - lo) * 100
    kk = sma(raw, k)
    return kk, sma(kk, d)


def bollinger(closes, n=20, mult=2):
    mid = sma(closes, n)
    up, lo = [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        sd = (sum((x - mid[i]) ** 2 for x in w) / n) ** 0.5
        up[i], lo[i] = mid[i] + mult * sd, mid[i] - mult * sd
    return mid, up, lo


def obv(candles):
    out, total = [0.0], 0.0
    for i in range(1, len(candles)):
        if candles[i]["c"] > candles[i - 1]["c"]:
            total += candles[i]["v"]
        elif candles[i]["c"] < candles[i - 1]["c"]:
            total -= candles[i]["v"]
        out.append(total)
    return out


def adx(candles, n=14):
    pdm, mdm = [None], [None]
    for i in range(1, len(candles)):
        up = candles[i]["h"] - candles[i - 1]["h"]
        dn = candles[i - 1]["l"] - candles[i]["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    tr = [None] + true_range(candles)[1:]
    atr_s, p_s, m_s = rma(tr, n), rma(pdm, n), rma(mdm, n)
    pdi, mdi, dx = [None] * len(candles), [None] * len(candles), [None] * len(candles)
    for i in range(len(candles)):
        if atr_s[i]:
            pdi[i] = 100 * p_s[i] / atr_s[i]
            mdi[i] = 100 * m_s[i] / atr_s[i]
            s = pdi[i] + mdi[i]
            dx[i] = 0.0 if s == 0 else 100 * abs(pdi[i] - mdi[i]) / s
    return rma(dx, n), pdi, mdi


# ================================================================ structuur

def pivots(candles, length):
    """Toppen/bodems: hoogste/laagste punt binnen `length` candles links en rechts."""
    highs, lows = [], []
    for i in range(length, len(candles) - length):
        h, l = candles[i]["h"], candles[i]["l"]
        left, right = candles[i - length:i], candles[i + 1:i + length + 1]
        if h > max(c["h"] for c in left) and h >= max(c["h"] for c in right):
            highs.append(i)
        if l < min(c["l"] for c in left) and l <= min(c["l"] for c in right):
            lows.append(i)
    return highs, lows


def label_pivots(candles, highs, lows):
    labels = []
    for kind, idxs, key, up, down, first in (("top", highs, "h", "HH", "LH", "Top"),
                                             ("bodem", lows, "l", "HL", "LL", "Bodem")):
        prev = None
        for i in idxs:
            p = candles[i][key]
            labels.append({"i": i, "t": candles[i]["t"], "price": p, "kind": kind,
                           "label": first if prev is None else (up if p > prev else down)})
            prev = p
    return sorted(labels, key=lambda x: x["i"])


def candle_pattern(c, prev):
    body = abs(c["c"] - c["o"])
    rng = c["h"] - c["l"] or 1e-9
    upper = c["h"] - max(c["o"], c["c"])
    lower = min(c["o"], c["c"]) - c["l"]
    green = c["c"] > c["o"]
    prev_body = abs(prev["c"] - prev["o"])
    prev_real = prev_body >= 0.25 * ((prev["h"] - prev["l"]) or 1e-9)

    if prev_real and green and prev["c"] < prev["o"] and c["c"] >= prev["o"] and c["o"] <= prev["c"] and body > prev_body:
        return "Bullish engulfing", "bullish"
    if prev_real and not green and prev["c"] > prev["o"] and c["o"] >= prev["c"] and c["c"] <= prev["o"] and body > prev_body:
        return "Bearish engulfing", "bearish"
    if body <= 0.1 * rng:
        return "Doji (besluiteloosheid)", "neutraal"
    if lower >= 2 * body and upper <= 0.35 * rng:
        return ("Hammer (groen)" if green else "Hammer (rood)"), "bullish"
    if upper >= 2 * body and lower <= 0.35 * rng:
        return "Shooting star", "bearish"
    if body >= 0.7 * rng:
        return ("Sterke groene candle" if green else "Sterke rode candle"), ("bullish" if green else "bearish")
    return ("Kleine groene candle" if green else "Kleine rode candle"), "neutraal"


def liquidity_pools(closed, atr_now, lookback=150, piv_len=3):
    """Equal highs (BSL) en equal lows (SSL): plekken waar veel stop-losses liggen.

    status: open (nog niet geraakt), gesweept (wick erdoor, slot terug) of doorbroken (slot erdoor).
    """
    start = max(0, len(closed) - lookback)
    seg = closed[start:]
    highs, lows = pivots(seg, piv_len)
    ref_price = seg[-1]["c"]
    tol = max(0.25 * atr_now, ref_price * 0.002)
    pools = []
    for kind, idxs, key in (("BSL", highs, "h"), ("SSL", lows, "l")):
        pts = sorted((seg[i][key], i) for i in idxs)
        clusters = []
        for p, i in pts:
            if clusters and p - clusters[-1][0][0] <= tol:
                clusters[-1].append((p, i))
            else:
                clusters.append([(p, i)])
        for cl in clusters:
            if len(cl) < 2:
                continue
            level = max(p for p, _ in cl) if kind == "BSL" else min(p for p, _ in cl)
            last_i = max(i for _, i in cl)

            def status_of(candles):
                swept = False
                for c in candles:
                    beyond_close = c["c"] > level if kind == "BSL" else c["c"] < level
                    beyond_wick = c["h"] > level if kind == "BSL" else c["l"] < level
                    if beyond_close:
                        return "doorbroken"
                    swept = swept or beyond_wick
                return "gesweept" if swept else "open"

            after = seg[last_i + 1:]
            pools.append({
                "kind": kind, "price": level, "touches": len(cl),
                "t_first": seg[min(i for _, i in cl)]["t"], "t_last": seg[last_i]["t"],
                "status": status_of(after),
                "status_before_last": status_of(after[:-1]) if after else "open",
                "formed_before_last": last_i < len(seg) - 1,
            })
    return pools


def volume_profile(closed, lookback=120, bins=40):
    seg = closed[-lookback:]
    lo, hi = min(c["l"] for c in seg), max(c["h"] for c in seg)
    step = (hi - lo) / bins or 1e-9
    vol = [0.0] * bins
    for c in seg:
        b0 = min(bins - 1, int((c["l"] - lo) / step))
        b1 = min(bins - 1, int((c["h"] - lo) / step))
        share = c["v"] / (b1 - b0 + 1)
        for b in range(b0, b1 + 1):
            vol[b] += share
    total = sum(vol) or 1
    poc = max(range(bins), key=lambda b: vol[b])
    lo_b, hi_b, acc = poc, poc, vol[poc]
    while acc < 0.7 * total and (lo_b > 0 or hi_b < bins - 1):
        down = vol[lo_b - 1] if lo_b > 0 else -1
        up = vol[hi_b + 1] if hi_b < bins - 1 else -1
        if up >= down:
            hi_b += 1; acc += up
        else:
            lo_b -= 1; acc += down
    mean = total / bins
    hvn, cur = [], None
    for b in range(bins):
        if vol[b] > 1.3 * mean:
            if cur and cur["hi_b"] == b - 1:
                cur["hi_b"] = b
            else:
                cur = {"lo_b": b, "hi_b": b}
                hvn.append(cur)
    edge = lambda b: lo + b * step
    return {
        "bins": [{"low": edge(b), "high": edge(b + 1), "vol": vol[b]} for b in range(bins)],
        "poc": edge(poc) + step / 2, "val": edge(lo_b), "vah": edge(hi_b + 1),
        "hvn": [{"low": edge(z["lo_b"]), "high": edge(z["hi_b"] + 1)} for z in hvn],
        "lookback": len(seg),
    }


def cluster_levels(levels, tol=0.015):
    """Voeg niveaus samen die binnen `tol` (1,5%) van elkaar liggen tot zones."""
    levels = sorted(levels, key=lambda x: x["price"])
    zones = []
    for lv in levels:
        if zones and lv["price"] <= zones[-1]["low"] * (1 + tol):
            zones[-1]["items"].append(lv)
            zones[-1]["high"] = max(zones[-1]["high"], lv["price"])
        else:
            zones.append({"low": lv["price"], "high": lv["price"], "items": [lv]})
    for z in zones:
        z["score"] = len(z["items"])
    return zones


# ================================================================ formattering

def fmt(x):
    if x is None:
        return "-"
    if x >= 1000:
        return f"{x:,.0f}".replace(",", ".")
    if x >= 1:
        return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if x >= 0.01:
        return f"{x:.4f}".replace(".", ",")
    return f"{x:.8f}".rstrip("0").replace(".", ",")


def dec(x, n=1):
    return f"{x:.{n}f}".replace(".", ",")


def eur(x):
    return f"€{x:,.0f}".replace(",", ".")


# ================================================================ analyse

def analyse(symbol, candles, pivot_len=7, risk_eur=10, capital=500, last_closed=False):
    if last_closed:
        closed, live = candles, candles[-1]
    else:
        closed, live = candles[:-1], candles[-1]
    closes = [c["c"] for c in closed]
    price = live["c"]

    ema20, ema50, ema200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    rsi14 = rsi(closes, 14)
    atr14 = atr(closed, 14)
    atr_now = atr14[-1] or (closed[-1]["h"] - closed[-1]["l"])
    m_line, m_sig, m_hist = macd(closes)
    adx14, pdi, mdi = adx(closed, 14)
    st_k, st_d = stoch_rsi(rsi14)
    bb_mid, bb_up, bb_lo = bollinger(closes)
    obv_v = obv(closed)
    obv_sma = sma(obv_v, 20)

    highs, lows = pivots(closed, pivot_len)
    labels = label_pivots(closed, highs, lows)
    tops = [x for x in labels if x["kind"] == "top"]
    bottoms = [x for x in labels if x["kind"] == "bodem"]

    # --- 1. trend
    score, reasons = 0, []
    if len(tops) >= 2:
        hh = tops[-1]["price"] > tops[-2]["price"]
        score += 1 if hh else -1
        reasons.append(f"Laatste top is een {'HH' if hh else 'LH'} ({fmt(tops[-1]['price'])} {'>' if hh else '<'} {fmt(tops[-2]['price'])})")
    if len(bottoms) >= 2:
        hl = bottoms[-1]["price"] > bottoms[-2]["price"]
        score += 1 if hl else -1
        reasons.append(f"Laatste bodem is een {'HL' if hl else 'LL'} ({fmt(bottoms[-1]['price'])} {'>' if hl else '<'} {fmt(bottoms[-2]['price'])})")
    above_ema = price > ema50[-1]
    score += 1 if above_ema else -1
    reasons.append(f"Prijs {'boven' if above_ema else 'onder'} EMA 50 ({fmt(ema50[-1])})")
    ema_slope = ema50[-1] - ema50[-11]
    score += 1 if ema_slope > 0 else -1
    reasons.append("EMA 50 stijgt" if ema_slope > 0 else "EMA 50 daalt")
    if score >= 4:
        trend, trend_ok = "Sterk omhoog", True
    elif score >= 2:
        trend, trend_ok = "Omhoog", True
    elif score <= -2:
        trend, trend_ok = "Omlaag", False
    else:
        trend, trend_ok = "Zijwaarts", False

    # --- Fibonacci
    fib = None
    if len(tops) >= 2:
        a, b = tops[-2]["i"], tops[-1]["i"]
        sl_i = min(range(a, b + 1), key=lambda i: closed[i]["l"])
        sh_i = max(range(sl_i, len(closed)), key=lambda i: closed[i]["h"])
        sl, sh = closed[sl_i]["l"], closed[sh_i]["h"]
        diff = sh - sl
        fib = {"low": sl, "high": sh, "low_i": sl_i, "high_i": sh_i,
               "38.2": sh - 0.382 * diff, "50": sh - 0.5 * diff, "61.8": sh - 0.618 * diff}

    # --- liquiditeit en volume profile
    pools = liquidity_pools(closed, atr_now)
    profile = volume_profile(closed)
    ssl_open = sorted((p for p in pools if p["kind"] == "SSL" and p["status"] == "open" and p["price"] < price),
                      key=lambda p: -p["price"])
    bsl_open = sorted((p for p in pools if p["kind"] == "BSL" and p["status"] == "open" and p["price"] > price),
                      key=lambda p: p["price"])
    last = closed[-1]
    sweep = next((p for p in pools if p["kind"] == "SSL" and p["formed_before_last"]
                  and p["status_before_last"] == "open" and last["l"] < p["price"] < last["c"]), None)
    sweep_bear = next((p for p in pools if p["kind"] == "BSL" and p["formed_before_last"]
                       and p["status_before_last"] == "open" and last["h"] > p["price"] > last["c"]), None)

    # --- steunniveaus onder de prijs -> zones
    levels = []
    for t in tops:
        if t["price"] < price:
            levels.append({"price": t["price"], "name": f"Oude top {t['label']}"})
    for bt in bottoms[-4:]:
        if bt["price"] < price:
            levels.append({"price": bt["price"], "name": f"Bodem {bt['label']}"})
    if fib:
        for k in ("38.2", "50", "61.8"):
            if fib[k] < price:
                levels.append({"price": fib[k], "name": f"Fibonacci {k.replace('.', ',')}%"})
    if ema50[-1] < price:
        levels.append({"price": ema50[-1], "name": "EMA 50"})
    if profile["poc"] < price:
        levels.append({"price": profile["poc"], "name": "Volume POC"})
    for z in profile["hvn"]:
        mid = (z["low"] + z["high"]) / 2
        if mid < price and abs(mid - profile["poc"]) / price > 0.01:
            levels.append({"price": mid, "name": "Hoog-volume zone"})
    for p in ssl_open[:2]:
        levels.append({"price": p["price"], "name": f"SSL (equal lows, {p['touches']}x)"})
    levels = [lv for lv in levels if lv["price"] > price * 0.8]
    zones = cluster_levels(levels)
    zones.sort(key=lambda z: -z["high"])
    zone1 = zones[0] if zones else None
    zone2 = max(zones[1:], key=lambda z: (z["score"], z["high"])) if len(zones) > 1 else None

    # --- target: eerste obstakel boven de prijs (oude top, swing-high of open BSL)
    above = [t["price"] for t in tops if t["price"] > price]
    if fib and fib["high"] > price:
        above.append(fib["high"])
    if bsl_open:
        above.append(bsl_open[0]["price"])
    target = min(above) if above else None
    last_bottom = bottoms[-1]["price"] if bottoms else None

    # --- 2. plek
    in_zone = bool(zone1 and zone1["low"] * 0.995 <= price <= zone1["high"] * 1.005)
    dist_zone = (price - zone1["high"]) / price * 100 if zone1 else None

    # --- 4. RSI
    r_now, r_prev = rsi14[-1], rsi14[-6]
    rsi_ok = r_now < 70

    # --- 5. candle / liquidity sweep (laatste gesloten)
    pattern, bias = candle_pattern(closed[-1], closed[-2])
    candle_ok = bias == "bullish" or sweep is not None
    candle_text = pattern + (f" + SSL-sweep op {fmt(sweep['price'])}" if sweep else "")

    # --- 6. volume
    vols = [c["v"] for c in closed]
    avg30 = sum(vols[-30:]) / 30
    vol_ratio = vols[-1] / avg30 if avg30 else 0
    vol_ok = vol_ratio >= 0.8
    pullback_vol = None
    if fib:
        up_leg, pb_leg = vols[fib["low_i"]:fib["high_i"] + 1], vols[fib["high_i"] + 1:]
        if up_leg and pb_leg and sum(up_leg):
            pullback_vol = (sum(pb_leg) / len(pb_leg)) / (sum(up_leg) / len(up_leg))

    # --- stop: onder de zone, en onder open liquiditeit vlak eronder (daar worden stops gejaagd)
    def stop_for(zone):
        stop = zone["low"] * STOP_BUF
        below = [p["price"] for p in ssl_open if zone["low"] * 0.97 <= p["price"] < zone["low"]]
        if below:
            stop = min(stop, min(below) * 0.995)
        return stop

    def plan(entry, stop, tgt):
        if not (entry and stop and tgt) or stop >= entry or tgt <= entry:
            return None
        stop_pct = (entry - stop) / entry
        full = risk_eur / stop_pct
        position = min(full, capital)
        return {"entry": entry, "stop": stop, "target": tgt,
                "stop_pct": stop_pct * 100, "win_pct": (tgt - entry) / entry * 100,
                "rr": (tgt - entry) / (entry - stop),
                "position": position, "capped": position < full,
                "loss_eur": position * stop_pct, "win_eur": position * (tgt - entry) / entry}

    plan_now = plan(price, stop_for(zone1), target) if zone1 else None
    plan_z1 = plan(zone1["high"], stop_for(zone1), target) if zone1 else None
    plan_z2 = plan(zone2["high"], stop_for(zone2), target) if zone2 else None
    rr_ok = bool(plan_now and plan_now["rr"] >= 2)

    # --- extra bevestiging
    pct_b = None
    if bb_up[-1] is not None and bb_up[-1] != bb_lo[-1]:
        pct_b = (price - bb_lo[-1]) / (bb_up[-1] - bb_lo[-1])
    hist_up = m_hist[-1] > m_hist[-2]
    extras = [
        {"name": "EMA 200", "state": "ok" if price > ema200[-1] else "nee",
         "text": f"Prijs {'boven' if price > ema200[-1] else 'onder'} EMA 200 ({fmt(ema200[-1])}): lange termijn {'omhoog' if price > ema200[-1] else 'omlaag'}"},
        {"name": "EMA 20/50", "state": "ok" if ema20[-1] > ema50[-1] else "nee",
         "text": f"EMA 20 {'boven' if ema20[-1] > ema50[-1] else 'onder'} EMA 50: korte termijn {'sterk' if ema20[-1] > ema50[-1] else 'zwak'}"},
        {"name": "MACD", "state": "ok" if m_hist[-1] > 0 or (hist_up and m_hist[-1] > m_hist[-3]) else "nee",
         "text": f"Histogram {'positief' if m_hist[-1] > 0 else 'negatief'} en {'stijgend' if hist_up else 'dalend'}"},
        {"name": "ADX", "state": "ok" if adx14[-1] and adx14[-1] >= 20 and pdi[-1] > mdi[-1] else ("wacht" if adx14[-1] and adx14[-1] < 20 else "nee"),
         "text": f"ADX {dec(adx14[-1] or 0, 0)}: {'sterke' if (adx14[-1] or 0) >= 25 else 'matige' if (adx14[-1] or 0) >= 20 else 'zwakke'} trend, "
                 f"{'kopers (+DI)' if pdi[-1] > mdi[-1] else 'verkopers (−DI)'} sterker"},
        {"name": "Stoch RSI", "state": "ok" if st_k[-1] is not None and st_k[-1] < 30 else ("nee" if st_k[-1] is not None and st_k[-1] > 80 else "wacht"),
         "text": f"K {dec(st_k[-1] or 0, 0)}: " + ("oversold, ruimte voor opleving" if (st_k[-1] or 50) < 30 else "overkocht" if (st_k[-1] or 50) > 80 else "neutraal")},
        {"name": "Bollinger", "state": "ok" if pct_b is not None and pct_b < 0.3 else ("nee" if pct_b is not None and pct_b > 0.95 else "wacht"),
         "text": f"%B {dec(pct_b or 0, 2)}: " + ("dicht bij onderband" if (pct_b or .5) < 0.3 else "tegen bovenband" if (pct_b or .5) > 0.95 else "midden in de band")},
        {"name": "OBV", "state": "ok" if obv_sma[-1] is not None and obv_v[-1] > obv_sma[-1] else "nee",
         "text": "Accumulatie: OBV boven 20-daags gemiddelde" if obv_sma[-1] is not None and obv_v[-1] > obv_sma[-1] else "Distributie: OBV onder 20-daags gemiddelde"},
    ]
    conf = sum(1 for e in extras if e["state"] == "ok")
    atr_pct = atr_now / price * 100
    extras.append({"name": "ATR", "state": "info", "text": f"Gemiddelde dagbeweging ≈ {dec(atr_pct)}% ({fmt(atr_now)})"})

    # --- liquiditeit-regel voor de checklist
    if sweep:
        liq = {"state": "ok", "text": f"SSL op {fmt(sweep['price'])} gesweept en terug erboven: stops zijn opgehaald"}
    elif sweep_bear:
        liq = {"state": "nee", "text": f"BSL op {fmt(sweep_bear['price'])} gesweept en afgewezen: let op"}
    elif ssl_open and (price - ssl_open[0]["price"]) / price < 0.03:
        liq = {"state": "wacht", "text": f"Open SSL op {fmt(ssl_open[0]['price'])} ({dec((price - ssl_open[0]['price']) / price * 100)}% lager): prijs kan die nog ophalen"}
    else:
        liq = {"state": "ok", "text": "Geen open liquiditeit vlak onder de prijs"}

    checklist = [
        {"name": "Trend", "state": "ok" if trend_ok else "nee", "text": trend},
        {"name": "Plek", "state": "ok" if in_zone else "wacht",
         "text": "In zone 1" if in_zone else (f"{dec(dist_zone)}% boven zone 1" if dist_zone is not None else "Geen zone gevonden")},
        {"name": "EMA 50", "state": "ok" if above_ema else "nee",
         "text": f"Prijs {'boven' if above_ema else 'onder'} EMA 50 ({fmt(ema50[-1])}), {'stijgend' if ema_slope > 0 else 'dalend'}"},
        {"name": "RSI", "state": "ok" if rsi_ok else "nee",
         "text": f"{dec(r_now)} ({'daalt' if r_now < r_prev else 'stijgt'}, was {dec(r_prev)})"},
        {"name": "Signaal", "state": "ok" if candle_ok else "nee", "text": candle_text},
        {"name": "Liquiditeit", "state": liq["state"], "text": liq["text"]},
        {"name": "Volume", "state": "ok" if vol_ok else "wacht", "text": f"{vol_ratio * 100:.0f}% van 30-daags gemiddelde"},
        {"name": "Risico/winst", "state": "ok" if rr_ok else "nee",
         "text": f"{dec(plan_now['rr'])}x bij instap nu" if plan_now else "Niet te berekenen"},
        {"name": "Bevestiging", "state": "ok" if conf >= 4 else "wacht", "text": f"{conf} van 7 extra indicatoren positief"},
    ]

    setup = trend_ok and in_zone and candle_ok and rsi_ok and rr_ok
    if setup:
        status = {"key": "setup", "label": "Setup", "title": "Mogelijke setup volgens je checklist",
                  "text": "Alle kernpunten zijn groen. Controleer het zelf op de chart voordat je iets doet."}
    elif not trend_ok:
        status = {"key": "down", "label": "Trend negatief" if score <= -2 else "Geen trend",
                  "title": "Geen setup: trend werkt niet mee",
                  "text": "Volgens je regels koop je alleen in een uptrend. Wachten."}
    elif in_zone:
        status = {"key": "zone", "label": "In zone", "title": "In de koopzone: wacht op signaal",
                  "text": "De prijs staat in zone 1. Wacht op een gesloten hammer, bullish engulfing of SSL-sweep."}
    else:
        status = {"key": "wait", "label": "Wachten", "title": "Geen setup: wachten",
                  "text": "De trend is goed, maar de prijs is nog niet op een goede plek."}

    scenarios = []
    if zone1:
        scenarios.append({"key": "A", "title": f"Terugval naar zone 1 ({fmt(zone1['low'])} – {fmt(zone1['high'])})",
                          "text": "Wacht op een gesloten hammer, bullish engulfing of SSL-sweep in de zone.", "plan": plan_z1})
    if zone2:
        scenarios.append({"key": "B", "title": f"Diepere terugval naar zone 2 ({fmt(zone2['low'])} – {fmt(zone2['high'])})",
                          "text": f"Sterkere zone ({zone2['score']} niveaus samen). Zelfde signaal afwachten.", "plan": plan_z2})
    if target:
        what = "open BSL (equal highs)" if bsl_open and target == bsl_open[0]["price"] else "weerstand"
        scenarios.append({"key": "C", "title": f"Dagslot boven {fmt(target)} ({what})",
                          "text": "Uitbraak: de trend gaat verder. Niet najagen, wacht op de volgende terugval.", "plan": None})
    if last_bottom:
        scenarios.append({"key": "D", "title": f"Dagslot onder {fmt(last_bottom)}",
                          "text": "Laatste bodem gebroken: trend in gevaar. Geen koopplannen.", "plan": None})

    return {
        "symbol": symbol, "price": price, "live": live, "closed": closed, "last_closed": last_closed,
        "ema20": ema20, "ema50": ema50, "ema200": ema200, "rsi": rsi14,
        "macd": (m_line, m_sig, m_hist), "labels": labels,
        "trend": trend, "trend_ok": trend_ok, "trend_score": score, "trend_reasons": reasons,
        "fib": fib, "zones": zones, "zone1": zone1, "zone2": zone2,
        "target": target, "last_bottom": last_bottom,
        "pools": pools, "profile": profile, "sweep": sweep,
        "in_zone": in_zone, "dist_zone": dist_zone, "setup": setup, "status": status,
        "pattern": candle_text, "rsi_now": r_now, "vol_ratio": vol_ratio, "pullback_vol": pullback_vol,
        "checklist": checklist, "extras": extras, "confluence": conf,
        "scenarios": scenarios, "plan_now": plan_now, "atr_pct": atr_pct,
    }

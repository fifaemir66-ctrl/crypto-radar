#!/usr/bin/env python3
"""
Radar -- monitor voor crypto en aandelen.

Draait elk uur (GitHub Actions of lokaal):
  * analyseert alle crypto en aandelen uit config.json
  * schrijft docs/data.json (overzicht) en docs/charts/<SYMBOOL>.json (details) voor de website
  * stuurt ntfy-meldingen bij: koopzone, setup na dagslot, trendbreuk, dagoverzicht
  * houdt in state.json bij wat al gemeld is (geen dubbele meldingen)

Gebruik:
    python3 monitor.py            normale run
    python3 monitor.py --dry-run  geen meldingen, state.json niet bijwerken (website-data wel)
    python3 monitor.py --test     stuur een testmelding

Educatief hulpmiddel -- geen financieel advies.
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

from analysis import analyse, dec, eur, fetch_crypto, fetch_stock, fmt

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT, "config.json")
STATE_FILE = os.path.join(ROOT, "state.json")
DOCS = os.path.join(ROOT, "docs")
TOPIC_FILE = os.path.join(ROOT, ".ntfy_topic")

CHART_CANDLES = 260
HISTORY_MAX = 60
MARKET_LABEL = {"crypto": "Crypto", "stocks": "Aandelen"}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=2)


def site_url(config):
    if config.get("site_url"):
        return config["site_url"]
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        owner, name = repo.split("/")
        return f"https://{owner}.github.io/{name}/"
    return None


def ntfy_topic():
    topic = os.environ.get("NTFY_TOPIC")
    if not topic and os.path.exists(TOPIC_FILE):
        topic = open(TOPIC_FILE).read().strip()
    return topic


def send(title, message, priority=3, tags=None, click=None, dry_run=False):
    print(f"\n[melding p{priority}] {title}\n{message}")
    topic = ntfy_topic()
    if dry_run:
        return
    if not topic:
        print("  (geen NTFY_TOPIC ingesteld, melding niet verstuurd)")
        return
    body = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"  ntfy-fout: {e}")


def plan_lines(p):
    if not p:
        return ""
    return (f"Entry {fmt(p['entry'])} · Stop {fmt(p['stop'])} (−{dec(p['stop_pct'])}%) · "
            f"Target {fmt(p['target'])} · R/R {dec(p['rr'])}x\n"
            f"Positie {eur(p['position'])}{' (max. kapitaal)' if p['capped'] else ''} → verlies bij stop ≈ {eur(p['loss_eur'])}")


# ---------------------------------------------------------------- output voor de website

def r(x):
    return None if x is None else float(f"{x:.8g}")


def chart_payload(a):
    closed = a["closed"]
    n = min(len(closed), CHART_CANDLES)
    off = len(closed) - n
    cs = closed[off:] + ([] if a["last_closed"] else [a["live"]])
    rng = range(off, len(closed))
    m_line, m_sig, m_hist = a["macd"]
    lines = []
    if a["zone1"]:
        lines += [{"price": a["zone1"]["high"], "title": "Zone 1", "kind": "zone1"},
                  {"price": a["zone1"]["low"], "title": "", "kind": "zone1"}]
    if a["zone2"]:
        lines += [{"price": a["zone2"]["high"], "title": "Zone 2", "kind": "zone2"},
                  {"price": a["zone2"]["low"], "title": "", "kind": "zone2"}]
    if a["target"]:
        lines.append({"price": a["target"], "title": "Target", "kind": "target"})
    if a["last_bottom"]:
        lines.append({"price": a["last_bottom"], "title": "Trendgrens", "kind": "stop"})
    price = a["price"]
    open_pools = [p for p in a["pools"] if p["status"] == "open" and abs(p["price"] - price) / price < 0.2]
    for p in sorted([p for p in open_pools if p["kind"] == "BSL"], key=lambda p: p["price"])[:2]:
        lines.append({"price": p["price"], "title": "BSL", "kind": "bsl"})
    for p in sorted([p for p in open_pools if p["kind"] == "SSL"], key=lambda p: -p["price"])[:2]:
        lines.append({"price": p["price"], "title": "SSL", "kind": "ssl"})
    lines.append({"price": a["profile"]["poc"], "title": "POC", "kind": "poc"})
    return {
        "candles": [[c["t"], r(c["o"]), r(c["h"]), r(c["l"]), r(c["c"]), round(c["v"], 2)] for c in cs],
        "ema50": [[closed[i]["t"], r(a["ema50"][i])] for i in rng],
        "ema200": [[closed[i]["t"], r(a["ema200"][i])] for i in range(max(off, 199), len(closed))],
        "rsi": [[closed[i]["t"], round(a["rsi"][i], 2)] for i in rng if a["rsi"][i] is not None],
        "macd": [[closed[i]["t"], r(m_line[i]), r(m_sig[i]), r(m_hist[i])] for i in range(max(off, 33), len(closed))],
        "markers": [[p["t"], p["kind"], p["label"]] for p in a["labels"] if p["i"] >= off],
        "lines": [l for l in lines if l["price"]],
    }


def detail_payload(a):
    fib, prof = a["fib"], a["profile"]
    return {
        "trend_reasons": a["trend_reasons"], "checklist": a["checklist"], "extras": a["extras"],
        "confluence": a["confluence"], "scenarios": a["scenarios"],
        "zones": [{"low": z["low"], "high": z["high"], "score": z["score"],
                   "items": [{"name": i["name"], "price": i["price"]} for i in z["items"]]} for z in a["zones"][:5]],
        "fib": fib and {"low": fib["low"], "high": fib["high"], "f382": fib["38.2"], "f50": fib["50"], "f618": fib["61.8"]},
        "pullback_vol": a["pullback_vol"], "atr_pct": a["atr_pct"],
        "pools": sorted([{k: p[k] for k in ("kind", "price", "touches", "t_first", "t_last", "status")} for p in a["pools"]
                         if abs(p["price"] - a["price"]) / a["price"] < 0.25], key=lambda p: -p["price"]),
        "profile": {"poc": prof["poc"], "vah": prof["vah"], "val": prof["val"], "lookback": prof["lookback"],
                    "hvn": prof["hvn"], "bins": [[r(b["low"]), r(b["high"]), round(b["vol"], 2)] for b in prof["bins"]]},
        "chart": chart_payload(a),
    }


def summary_payload(a, meta, market, source, currency):
    closed = a["closed"]
    ref = closed[-2]["c"] if a["last_closed"] else closed[-1]["c"]
    if market == "crypto":
        ref = a["live"]["o"]  # crypto: sinds 00:00 UTC (live 24u komt via de websocket)
    return {
        "symbol": a["symbol"], "name": meta.get("name", a["symbol"]), "base": meta.get("base", a["symbol"]),
        "market": market, "source": source, "currency": currency,
        "price": a["price"], "ref_price": ref, "market_open": not a["last_closed"],
        "status": a["status"], "trend": a["trend"], "rsi": round(a["rsi_now"], 1),
        "confluence": a["confluence"], "in_zone": a["in_zone"], "dist_zone": a["dist_zone"],
        "zone1": a["zone1"] and {"low": a["zone1"]["low"], "high": a["zone1"]["high"]},
        "spark": [r(c["c"]) for c in (closed[-59:] + ([] if a["last_closed"] else [a["live"]]))],
    }


# ---------------------------------------------------------------- run

def assets_from(config):
    out = [("crypto", m) for m in config.get("crypto", config.get("coins", []))]
    out += [("stocks", m) for m in config.get("stocks", [])]
    return out


def run(dry_run=False):
    config = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {"coins": {}, "history": []})
    first_run = not state["coins"]
    url = site_url(config)
    now = datetime.now(timezone.utc)
    summaries, errors = [], []
    day_summary = {"crypto": [], "stocks": []}
    daily_close = {"crypto": False, "stocks": False}
    new_assets = []

    def log_event(sym, market, kind, title, message, priority, tags):
        state["history"].insert(0, {"time": now.isoformat(timespec="seconds"), "symbol": sym, "market": market,
                                    "kind": kind, "title": title, "message": message})
        del state["history"][HISTORY_MAX:]
        send(title, message, priority, tags, url and (f"{url}#{sym}" if sym else url), dry_run)

    for market, meta in assets_from(config):
        sym = meta["symbol"]
        try:
            if market == "crypto":
                candles, source, last_closed = fetch_crypto(sym, 400)
                currency = "USDT"
            else:
                candles, source, last_closed, currency = fetch_stock(sym)
            a = analyse(sym, candles, config.get("pivot", 7), config.get("risk_eur", 10),
                        config.get("capital_eur", 500), last_closed=last_closed)
        except Exception as e:
            print(f"{sym}: {e}", file=sys.stderr)
            errors.append({"symbol": sym, "market": market, "error": str(e)[:300]})
            continue

        name = meta.get("name", sym)
        label = name if market == "crypto" else f"{name} ({sym})"
        is_new = sym not in state["coins"]
        s = state["coins"].setdefault(sym, {})
        closed_t = a["closed"][-1]["t"]
        new_close = s.get("last_closed_t") != closed_t
        price, zone1 = a["price"], a["zone1"]

        # 1. Na de dagslot: setup of trendbreuk
        if new_close and s.get("last_closed_t") is not None:
            daily_close[market] = True
            last = a["closed"][-1]
            if a["setup"]:
                log_event(sym, market, "setup", f"✅ {label}: setup volgens je checklist",
                          f"Trend {a['trend'].lower()}, prijs in zone 1, signaal: {a['pattern']}.\n"
                          f"Bevestiging: {a['confluence']}/7 extra indicatoren.\n{plan_lines(a['plan_now'])}\n"
                          "Controleer het zelf op de chart. Geen advies.", 5, ["white_check_mark"])
            elif a["last_bottom"] and last["c"] < a["last_bottom"] and not s.get("broken_alerted"):
                log_event(sym, market, "trend", f"⚠️ {label}: dagslot onder de laatste bodem",
                          f"Slot {fmt(last['c'])} < laatste bodem {fmt(a['last_bottom'])}. "
                          "De uptrend is in gevaar: geen koopplannen.", 4, ["warning"])
                s["broken_alerted"] = True
            if a["last_bottom"] and last["c"] >= a["last_bottom"]:
                s["broken_alerted"] = False
        s["last_closed_t"] = closed_t

        # 2. Elk uur: prijs komt in de koopzone
        near = bool(zone1 and zone1["low"] * 0.995 <= price <= zone1["high"] * 1.005)
        alerted = s.get("zone_level")
        if near and a["trend_ok"] and (alerted is None or zone1["high"] < alerted * 0.99):
            if is_new and not first_run:
                new_assets.append(f"🎯 {label} {fmt(price)} in zone")
            else:
                p = a["scenarios"][0]["plan"] if a["scenarios"] else None
                log_event(sym, market, "zone", f"🎯 {label} is in de koopzone",
                          f"Prijs {fmt(price)} in zone {fmt(zone1['low'])} – {fmt(zone1['high'])}.\n"
                          f"Wacht op de dagslot en kijk of er een hammer, bullish engulfing of SSL-sweep komt.\n{plan_lines(p)}",
                          4, ["dart"])
            s["zone_level"] = zone1["low"]
        elif alerted is not None and price > alerted * 1.03:
            s["zone_level"] = None

        save_json(os.path.join(DOCS, "charts", f"{sym}.json"), detail_payload(a), compact=True)
        summaries.append(summary_payload(a, meta, market, source, currency))
        icon = {"setup": "✅", "zone": "🎯", "wait": "⏳", "down": "❌"}[a["status"]["key"]]
        day_summary[market].append(f"{icon} {name} {fmt(price)} · {a['status']['label']}")
        print(f"{sym:<9} {fmt(price):>12}  {a['status']['label']:<15} trend: {a['trend']:<13} bevestiging {a['confluence']}/7")

    for market in ("crypto", "stocks"):
        if daily_close[market] and day_summary[market]:
            log_event(None, market, "summary", f"📊 Dagoverzicht {MARKET_LABEL[market]}",
                      "\n".join(day_summary[market]), 2, ["bar_chart"])
    if first_run and not dry_run:
        send("📡 Radar is actief", "Je ontvangt vanaf nu meldingen.", 3, ["satellite"], url)
    elif new_assets:
        send("📡 Radar uitgebreid", f"{len(summaries)} markten worden nu gevolgd.\n" + "\n".join(new_assets),
             3, ["satellite"], url, dry_run)

    save_json(os.path.join(DOCS, "data.json"), {
        "generated_at": now.isoformat(timespec="seconds"),
        "config": {"risk_eur": config.get("risk_eur", 10), "capital_eur": config.get("capital_eur", 500)},
        "assets": summaries, "errors": errors, "history": state["history"],
    }, compact=True)
    if not dry_run:
        save_json(STATE_FILE, state)


def main():
    ap = argparse.ArgumentParser(description="Radar monitor")
    ap.add_argument("--dry-run", action="store_true", help="Geen meldingen en state.json niet bijwerken")
    ap.add_argument("--test", action="store_true", help="Stuur een testmelding")
    args = ap.parse_args()
    if args.test:
        send("🔔 Testmelding Radar", "Als je dit ziet, werken je meldingen.", 3, ["bell"],
             site_url(load_json(CONFIG_FILE, {})))
        return
    run(args.dry_run)


if __name__ == "__main__":
    main()

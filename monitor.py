#!/usr/bin/env python3
"""
Crypto Radar -- monitor.

Draait elk uur (GitHub Actions of lokaal):
  * analyseert alle coins uit config.json
  * schrijft docs/data.json voor de website
  * stuurt ntfy-meldingen bij: coin in koopzone, setup na dagslot, trend gebroken
  * houdt in state.json bij wat al gemeld is (geen dubbele meldingen)

Gebruik:
    python3 monitor.py            normale run
    python3 monitor.py --dry-run  niets versturen, alleen tonen
    python3 monitor.py --test     stuur een testmelding

Educatief hulpmiddel -- geen financieel advies.
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

from analysis import analyse, dec, eur, fetch_klines, fmt

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT, "config.json")
STATE_FILE = os.path.join(ROOT, "state.json")
DATA_FILE = os.path.join(ROOT, "docs", "data.json")
TOPIC_FILE = os.path.join(ROOT, ".ntfy_topic")

CHART_CANDLES = 240
HISTORY_MAX = 50


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
    repo = os.environ.get("GITHUB_REPOSITORY")  # "gebruiker/crypto-radar"
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


def chart_payload(a):
    closed = a["closed"]
    n = min(len(closed), CHART_CANDLES)
    off = len(closed) - n
    cs = closed[off:] + [a["live"]]
    lines = []
    if a["zone1"]:
        lines += [{"price": a["zone1"]["high"], "title": "Zone 1", "kind": "zone1"},
                  {"price": a["zone1"]["low"], "title": "", "kind": "zone1"}]
    if a["zone2"]:
        lines += [{"price": a["zone2"]["high"], "title": "Zone 2", "kind": "zone2"},
                  {"price": a["zone2"]["low"], "title": "", "kind": "zone2"}]
    if a["target"]:
        lines.append({"price": a["target"], "title": "Weerstand", "kind": "target"})
    if a["last_bottom"]:
        lines.append({"price": a["last_bottom"], "title": "Trendgrens", "kind": "stop"})
    r = lambda x: round(x, 8)
    return {
        "candles": [[c["t"], r(c["o"]), r(c["h"]), r(c["l"]), r(c["c"]), round(c["v"], 2)] for c in cs],
        "ema50": [[closed[i]["t"], r(a["ema50"][i])] for i in range(off, len(closed))],
        "ema200": [[closed[i]["t"], r(a["ema200"][i])] for i in range(max(off, 199), len(closed))],
        "rsi": [[closed[i]["t"], round(a["rsi"][i], 2)] for i in range(off, len(closed)) if a["rsi"][i] is not None],
        "markers": [[p["t"], p["kind"], p["label"], r(p["price"])] for p in a["labels"] if p["i"] >= off],
        "lines": lines,
    }


def coin_payload(a, meta, source):
    fib = a["fib"]
    return {
        "symbol": a["symbol"], "name": meta.get("name", a["symbol"]), "base": meta.get("base"),
        "source": source, "price": a["price"], "day_open": a["live"]["o"],
        "status": a["status"], "trend": a["trend"], "trend_reasons": a["trend_reasons"],
        "rsi": round(a["rsi_now"], 1), "pattern": a["pattern"],
        "in_zone": a["in_zone"], "dist_zone": a["dist_zone"],
        "zone1": a["zone1"] and {"low": a["zone1"]["low"], "high": a["zone1"]["high"]},
        "zone2": a["zone2"] and {"low": a["zone2"]["low"], "high": a["zone2"]["high"]},
        "last_bottom": a["last_bottom"], "target": a["target"],
        "zones": [{"low": z["low"], "high": z["high"], "score": z["score"],
                   "items": [{"name": i["name"], "price": i["price"]} for i in z["items"]]} for z in a["zones"][:4]],
        "fib": fib and {"low": fib["low"], "high": fib["high"], "f382": fib["38.2"], "f50": fib["50"], "f618": fib["61.8"]},
        "pullback_vol": a["pullback_vol"], "vol_ratio": a["vol_ratio"],
        "checklist": a["checklist"], "scenarios": a["scenarios"],
        "chart": chart_payload(a),
    }


def run(dry_run=False):
    config = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {"coins": {}, "history": []})
    first_run = not state["coins"]
    url = site_url(config)
    now = datetime.now(timezone.utc)
    coins_out, errors, summary = [], [], []

    def log_event(sym, kind, title, message, priority, tags):
        state["history"].insert(0, {"time": now.isoformat(timespec="seconds"), "symbol": sym,
                                    "kind": kind, "title": title, "message": message})
        del state["history"][HISTORY_MAX:]
        send(title, message, priority, tags, url and f"{url}#{sym}", dry_run)

    daily_close = False
    for meta in config["coins"]:
        sym = meta["symbol"]
        try:
            candles, source = fetch_klines(sym, "1d", 400)
            a = analyse(sym, candles, config.get("pivot", 7), config.get("risk_eur", 10), config.get("capital_eur", 500))
        except Exception as e:
            print(f"{sym}: {e}", file=sys.stderr)
            errors.append({"symbol": sym, "error": str(e)})
            continue

        name = meta.get("name", sym)
        s = state["coins"].setdefault(sym, {})
        closed_t = a["closed"][-1]["t"]
        new_close = s.get("last_closed_t") != closed_t
        price, zone1 = a["price"], a["zone1"]

        # 1. Na de dagslot: setup of trendbreuk
        if new_close and s.get("last_closed_t") is not None:
            daily_close = True
            last = a["closed"][-1]
            if a["setup"]:
                p = a["plan_now"]
                log_event(sym, "setup", f"✅ {name}: setup volgens je checklist",
                          f"Trend {a['trend'].lower()}, prijs in zone 1, signaal: {a['pattern']}.\n{plan_lines(p)}\n"
                          "Controleer het zelf op de chart. Geen advies.", 5, ["white_check_mark"])
            elif a["last_bottom"] and last["c"] < a["last_bottom"] and not s.get("broken_alerted"):
                log_event(sym, "trend", f"⚠️ {name}: dagslot onder de laatste bodem",
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
            p = a["scenarios"][0]["plan"] if a["scenarios"] else None
            log_event(sym, "zone", f"🎯 {name} is in de koopzone",
                      f"Prijs {fmt(price)} in zone {fmt(zone1['low'])} – {fmt(zone1['high'])}.\n"
                      f"Wacht op de dagslot (02:00) en kijk of er een hammer of bullish engulfing komt.\n{plan_lines(p)}",
                      4, ["dart"])
            s["zone_level"] = zone1["low"]
        elif alerted is not None and price > alerted * 1.03:
            s["zone_level"] = None  # prijs is weggelopen: volgende keer opnieuw melden

        coins_out.append(coin_payload(a, meta, source))
        icon = {"setup": "✅", "zone": "🎯", "wait": "⏳", "down": "❌"}[a["status"]["key"]]
        summary.append(f"{icon} {name} {fmt(price)} · {a['status']['label']}")
        print(f"{sym:<9} {fmt(price):>10}  {a['status']['label']:<15} trend: {a['trend']}")

    if daily_close and summary:
        log_event(None, "summary", "📊 Dagoverzicht Crypto Radar", "\n".join(summary), 2, ["bar_chart"])
    if first_run and summary and not dry_run:
        send("📡 Crypto Radar is actief", "Je ontvangt vanaf nu meldingen.\n" + "\n".join(summary), 3, ["satellite"], url, dry_run)

    save_json(DATA_FILE, {
        "generated_at": now.isoformat(timespec="seconds"),
        "config": {"risk_eur": config.get("risk_eur", 10), "capital_eur": config.get("capital_eur", 500),
                   "pivot": config.get("pivot", 7)},
        "coins": coins_out, "errors": errors, "history": state["history"],
    }, compact=True)
    if not dry_run:
        save_json(STATE_FILE, state)


def main():
    ap = argparse.ArgumentParser(description="Crypto Radar monitor")
    ap.add_argument("--dry-run", action="store_true", help="Niets versturen of state opslaan")
    ap.add_argument("--test", action="store_true", help="Stuur een testmelding")
    args = ap.parse_args()
    if args.test:
        send("🔔 Testmelding Crypto Radar", "Als je dit ziet, werken je meldingen.", 3, ["bell"],
             site_url(load_json(CONFIG_FILE, {})))
        return
    run(args.dry_run)


if __name__ == "__main__":
    main()

# ZoneHunter

Live technische analyse van crypto (16) en aandelen (14), met meldingen op je telefoon. Website in het Engels.

- **Signalen** (volgens vaste strategie-regels, bepaald bij de dagslot zodat ze niet heen en weer springen):
  - 🟢 **BUY SIGNAL**: uptrend (dag + week), dagslot in een koopzone, trigger (hammer, bullish engulfing of liquidity sweep), RSI < 70, risico/winst ≥ 2x
  - 🟡 **GET READY**: in de koopzone, wacht op de trigger
  - 🔵 **WATCH**: uptrend, wacht op een terugval naar de koopzone
  - 🔴 **AVOID**: geen uptrend
- **Website:** laag 1 = overzicht per markt, laag 2 = pagina per markt met signaal, plan en grafiek, laag 3 = uitklapbare details (checklist, scenario's, liquiditeit, volume profile, indicatoren).
- **Meldingen (ntfy):** alleen als een signaal bij de dagslot verandert, plus één dagoverzicht per markt.

> Educatief hulpmiddel, geen financieel advies. Controleer altijd zelf.

## Bestanden

| Bestand | Wat |
|---|---|
| `analysis.py` | De analyse: trend, zones, Fibonacci, liquiditeit (equal highs/lows, sweeps), volume profile, RSI, MACD, ADX, Stoch RSI, Bollinger, OBV, ATR, risico/winst |
| `monitor.py` | Draait de analyse, stuurt meldingen, schrijft `docs/data.json` en `docs/charts/*.json` |
| `config.json` | Je coins (`crypto`) en aandelen (`stocks`), risico per trade (€10) en kapitaal (€500) |
| `docs/` | De website (GitHub Pages) |
| `.github/workflows/radar.yml` | De planning: elk uur |
| `.ntfy_topic` | Je geheime meldingskanaal (wordt niet geüpload) |

## Lokaal testen

```bash
cd ~/Documents/crypto-radar
python3 monitor.py --test       # stuur een testmelding naar je telefoon
python3 monitor.py --dry-run    # analyse draaien zonder meldingen
python3 -m http.server 8000 --directory docs   # website openen op http://localhost:8000
```

## Online zetten (eenmalig)

1. **ntfy op je telefoon:** installeer de app *ntfy* → **+** → vul je kanaalnaam in (staat in `.ntfy_topic`) → *Subscribe*.
2. **GitHub-account** aanmaken op github.com (gratis).
3. **GitHub Desktop** installeren (desktop.github.com) en inloggen.
   - *File → Add local repository* → kies `Documenten/crypto-radar`.
   - Klik op **Commit to main**, daarna op **Publish repository**.
   - Zet het vinkje **Keep this code private uit** (GitHub Pages is alleen gratis voor openbare repositories).
4. **Geheim kanaal toevoegen** op github.com in je repository:
   *Settings → Secrets and variables → Actions → New repository secret*
   - Name: `NTFY_TOPIC`
   - Secret: je kanaalnaam uit `.ntfy_topic`
5. **Website aanzetten:** *Settings → Pages* → Source: *Deploy from a branch* → Branch: `main`, map `/docs` → *Save*.
   Na een minuut staat hij op `https://<jouw-gebruikersnaam>.github.io/crypto-radar/`.
6. **Monitor starten:** tabblad *Actions* → *ZoneHunter* → **Run workflow**.
   Je krijgt de melding "🎯 ZoneHunter is live". Daarna draait hij vanzelf elk uur.

## Aanpassen

- Coins toevoegen of verwijderen: `crypto` in `config.json` (Binance-symbolen, bijvoorbeeld `ADAUSDT`).
- Aandelen toevoegen: `stocks` in `config.json` (Yahoo-tickers, bijvoorbeeld `AAPL`).
- Risico of kapitaal: `risk_eur` en `capital_eur` in `config.json`.
- Na een wijziging: in GitHub Desktop **Commit** en **Push**.

## Goed om te weten

- Signalen gebruiken alleen **gesloten dagcandles** (crypto: 00:00 UTC = 02:00 NL-zomertijd; aandelen: beurssluiting 22:00 NL-tijd).
- Aandelenkoersen komen elk uur van Yahoo Finance (niet realtime); crypto loopt live mee op de website.
- GitHub start geplande taken soms een paar minuten later dan gepland.
- De repository is openbaar: je code en analyses zijn zichtbaar, je kanaalnaam niet (die staat als geheim opgeslagen).

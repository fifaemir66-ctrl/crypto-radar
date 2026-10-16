# ZoneHunter

Technische analyse van crypto (15) en aandelen (20) met een **langetermijnvisie**, en meldingen op je telefoon. Website in het Engels.

- **Langetermijnsignaal** (hoofdsignaal, bepaald bij de **weekslot**, verandert dus hooguit 1x per week):
  - 🟢 **PULLBACK + UP WEEK**: langetermijn-uptrend (boven een stijgende 40-weeks gemiddelde, positief 12-maands rendement), echte terugval (week-RSI ≤ 50 in de laatste 6 weken) naar de waardezone (20/40-weeks gemiddelde) en een week die hoger sluit
  - 🟡 **IN VALUE ZONE**: uptrend, teruggevallen in de waardezone
  - 🔵 **UPTREND**: vasthouden, nog geen terugval
  - 🔴 **NO UPTREND**: volgens deze regels geen nieuw geld (geen verkoopsignaal)
- **Eerlijk:** in de backtest (~8 jaar, 35 markten) was het terugval-moment **niet beter** dan op een vaste dag kopen (bij crypto slechter). De trendstatus beperkte vooral diepe dalingen, ten koste van wat rendement.
- **Kortetermijntiming:** de oude dagelijkse analyse (zones, liquiditeit, candles) blijft zichtbaar op de site als extra informatie.
- **Meldingen (ntfy):** een update op **maandag en donderdag** (na 06:00 UTC) met alle wijzigingen, plus een **extra bericht alleen bij een nieuwe "pullback + up week"** (gebundeld, nooit 's nachts, max. 1x per 8 weken per markt).
- **Betrouwbaarheid:** data-aanvragen worden herhaald bij storingen, er zijn reservebronnen (Coinbase, Stooq), een markt zonder verse data blijft tot 7 dagen zichtbaar als "stale", en de GitHub-workflow draait eerst de tests.

> Educatief hulpmiddel, geen financieel advies. Controleer altijd zelf.

## Bestanden

| Bestand | Wat |
|---|---|
| `longterm.py` | Het langetermijnsignaal op weekcandles |
| `backtest.py` | Test het langetermijnsignaal op jaren historie (`python3 backtest.py`) |
| `tests/` | Automatische tests (`python3 -m unittest discover tests`) |
| `analysis.py` | Data ophalen en de dagelijkse (kortetermijn)analyse: trend, zones, Fibonacci, liquiditeit (equal highs/lows, sweeps), volume profile, RSI, MACD, ADX, Stoch RSI, Bollinger, OBV, ATR, risico/winst |
| `monitor.py` | Draait de analyse, stuurt meldingen, schrijft `docs/data.json` en `docs/charts/*.json` |
| `config.json` | Je coins (`crypto`) en aandelen (`stocks`), risico per trade (€10), kapitaal (€500) en de update-dagen (`digest_days`, `digest_hour_utc`) |
| `docs/` | De website (GitHub Pages) |
| `.github/workflows/radar.yml` | De planning: elk uur |
| `.ntfy_topic` | Je geheime meldingskanaal (wordt niet geüpload) |

## Lokaal testen

```bash
cd ~/Documents/crypto-radar
python3 monitor.py --test       # stuur een testmelding naar je telefoon
python3 monitor.py --dry-run    # analyse draaien zonder meldingen
python3 -m unittest discover tests   # automatische tests
python3 backtest.py             # langetermijnsignaal testen op jaren historie
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

- Het langetermijnsignaal gebruikt alleen **afgesloten weken** (een week telt vanaf maandag 00:00 UTC).
- Aandelen buiten de VS gebruik je met hun Yahoo-ticker, bijvoorbeeld `ADYEN.AS` (Amsterdam).
- Aandelenkoersen komen elk uur van Yahoo Finance (niet realtime); crypto loopt live mee op de website.
- GitHub start geplande taken soms een paar minuten later dan gepland.
- De repository is openbaar: je code en analyses zijn zichtbaar, je kanaalnaam niet (die staat als geheim opgeslagen).

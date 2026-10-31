# Festival-weeragent – Wisdom Valley, Chon Buri

Een kleine agent die gratis draait op GitHub en via Telegram:

1. **Elke ochtend rond 08:00 (Thaise tijd)** een Engels weerbericht stuurt voor vandaag en morgen, met focus op onweer, windstoten en regen, per dagdeel en met werkadvies voor de opbouw.
2. **Elk half uur** de komende 6 uur controleert en een **noodmelding** stuurt bij onweer, windstoten boven je drempel of wolkbreuken – en een "all clear" als het gevaar voorbij is.
3. **Elke maand** zelf uitzoekt welk weermodel voor deze plek het meest accuraat was, en dat model daarna gebruikt.

Locatie: 12.9539 N, 101.0220 E (uit je Google Maps-link).

---

## Hoe het "beste model" gekozen wordt

De databron is [Open-Meteo](https://open-meteo.com) (gratis, geen sleutel). Die bewaart een archief van **oude voorspellingen** per model. De agent:

- haalt de voorspellingen van de afgelopen 2 jaar op voor ECMWF IFS, ECMWF AIFS (AI), GFS, ICON, JMA, GEM, UK Met Office, Météo-France en CMA;
- vergelijkt ze per blok van 6 uur met wat er echt gebeurde (ERA5-heranalyse);
- scoort **regen** (Equitable Threat Score bij ≥ 2 mm en ≥ 10 mm) en **windstoten** (gemiddelde fout);
- weegt de huidige seizoensmaanden even zwaar als de hele periode (droog seizoen ≠ moesson);
- schrijft de winnaar en de volledige ranglijst naar `state/best_model.json`.

Het dagrapport toont ook wat 4 andere modellen zeggen, zodat je ziet hoe zeker de voorspelling is.

> **Eerlijke kanttekening:** ERA5 is zelf een ECMWF-product en heeft een grove resolutie (~30 km). Dat bevoordeelt ECMWF licht en onderschat lokale buien en windstoten. Wil je het strenger: koppel later een echt weerstation (bv. U-Tapao/Pattaya via Meteostat) of een eigen windmeter op het terrein als "waarheid".

## Wat de noodmelding wel en niet kan

- ✅ Waarschuwt typisch **1–6 uur vooraf** op basis van de modellen (onweerscodes, CAPE, windstoten), met bevestiging door een tweede model.
- ⚠️ Individuele onweersbuien zijn in de tropen moeilijk exact te plaatsen: timing kan 1–2 uur afwijken en een bui kan net naast het terrein vallen.
- ❌ Het is **geen bliksemdetectie**. Voor "bliksem binnen 10 km – nú stoppen" heb je een realtime bliksemdienst nodig (bv. Vaisala, Earth Networks, of een draagbare bliksemdetector op het terrein). Gebruik deze agent als vroegwaarschuwing, niet als enige veiligheidsmaatregel.

---

## Installatie (± 20 minuten)

### 1. Telegram-bot maken
1. Open Telegram, zoek **@BotFather**, stuur `/newbot` en volg de stappen. Je krijgt een **token** (`123456:ABC...`).
2. Maak een groep voor je crew (bv. "Site Weather"), voeg de bot toe.
3. Stuur een bericht in de groep, open daarna `https://api.telegram.org/bot<TOKEN>/getUpdates` in je browser en noteer het `chat.id` (bij groepen begint het met `-100...`).
   Tip: maak eventueel een tweede groep alleen voor alarmen, met meldingen op "luid".

### 2. GitHub-repository
1. Maak een account op github.com en een nieuwe repository, bv. `festival-weather`.
   - **Publiek** = onbeperkt gratis Actions-minuten.
   - **Privé** = 2000 gratis minuten/maand; het halfuurlijkse alarm verbruikt ±1500. Zet het dan op elk uur (`0 * * * *` in `storm-alert.yml`).
2. Upload alle bestanden uit deze map (inclusief de map `.github`).

### 3. Geheimen instellen
In de repo: **Settings → Secrets and variables → Actions → New repository secret**

| Naam | Waarde |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token van BotFather |
| `TELEGRAM_CHAT_IDS` | chat-id('s), komma-gescheiden |
| `TELEGRAM_ALERT_CHAT_IDS` | *(optioneel)* aparte groep voor alarmen |
| `ANTHROPIC_API_KEY` | *(optioneel)* dan schrijft Claude het rapport als lopende tekst; zonder sleutel krijg je de vaste template |

### 4. Eerste run
1. Tab **Actions** → *Monthly model backtest* → **Run workflow**. Na een paar minuten staat het beste model in `state/best_model.json`.
2. *Daily weather report* → **Run workflow** om het rapport meteen te testen.
3. *Storm alert check* → **Run workflow** (stuurt alleen iets als er echt iets op komst is).

Vanaf dan loopt alles automatisch.

---

## Drempels aanpassen

Stel ze af op het windmanagementplan van je constructies (podia, torens, LED-wanden). Pas de standaardwaarden bovenaan `festival_weather.py` aan, of voeg ze als `env:` toe in de workflows:

| Variabele | Standaard | Betekenis |
|---|---|---|
| `GUST_WATCH` | 40 km/u | opletten bij liften / grote panelen |
| `GUST_WARNING` | 60 km/u | alarm: werk op hoogte stoppen, doeken strijken |
| `GUST_SEVERE` | 75 km/u | ernstig: evacuatie volgens plan |
| `RAIN_HEAVY_MM_H` | 10 mm/u | wolkbreuk |
| `CAPE_HIGH` | 1500 J/kg | onstabiele lucht (onweerspotentieel) |
| `ALERT_HORIZON_H` | 6 | hoeveel uur vooruit het alarm kijkt |
| `ALERT_COOLDOWN_H` | 3 | min. uren tussen herhaalde alarmen (escalatie gaat altijd door) |

Andere locatie? Zet `SITE_NAME`, `SITE_LAT`, `SITE_LON` en `SITE_TZ`.

## Lokaal testen

```bash
pip install requests
python festival_weather.py backtest
python festival_weather.py report --dry-run   # print i.p.v. versturen
python festival_weather.py alert --dry-run
```

## Bestanden

- `festival_weather.py` – de agent (backtest / report / alert)
- `.github/workflows/` – planning: dagrapport 08:00, alarm elk half uur, backtest maandelijks
- `state/` – beste model en verstuurde alarmen (wordt automatisch bijgewerkt)

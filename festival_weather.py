#!/usr/bin/env python3
"""
Festival weather agent - Wisdom Valley, Chon Buri, Thailand.

Three modes:
  backtest  Compare archived forecasts of several global models with what actually
            happened (ERA5 reanalysis) and store the most accurate model.
  report    Daily English weather briefing (rain, wind gusts, thunderstorms) sent to Telegram.
  alert     Check the next hours for thunderstorms / damaging gusts / cloudbursts and
            send an emergency message when needed (run every 30 min).

Data: Open-Meteo (free, no API key). Optional: ANTHROPIC_API_KEY to let Claude
write the briefing text; without it a built-in template is used.
"""
import argparse
import datetime as dt
import html
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------- config
SITE = os.getenv("SITE_NAME", "Wisdom Valley, Chon Buri")
LAT = float(os.getenv("SITE_LAT", "12.9539"))
LON = float(os.getenv("SITE_LON", "101.0220"))
TZ = os.getenv("SITE_TZ", "Asia/Bangkok")

CANDIDATE_MODELS = {
    "ecmwf_ifs025": "ECMWF IFS",
    "ecmwf_aifs025_single": "ECMWF AIFS (AI)",
    "gfs_seamless": "NOAA GFS",
    "icon_seamless": "DWD ICON",
    "jma_seamless": "JMA GSM",
    "gem_seamless": "Canada GEM",
    "ukmo_seamless": "UK Met Office",
    "meteofrance_seamless": "Meteo-France ARPEGE",
    "cma_grapes_global": "CMA GRAPES",
}

# Thresholds (km/h, mm). Align these with the wind management plan of your structures!
GUST_WATCH = float(os.getenv("GUST_WATCH", "40"))      # caution for lifts / large panels
GUST_WARNING = float(os.getenv("GUST_WARNING", "60"))  # stop work at height, secure scrims
GUST_SEVERE = float(os.getenv("GUST_SEVERE", "75"))    # evacuate structures per plan
RAIN_HEAVY_MM_H = float(os.getenv("RAIN_HEAVY_MM_H", "10"))
CAPE_HIGH = float(os.getenv("CAPE_HIGH", "1500"))
ALERT_HORIZON_H = int(os.getenv("ALERT_HORIZON_H", "6"))
ALERT_COOLDOWN_H = float(os.getenv("ALERT_COOLDOWN_H", "3"))
THUNDER_CODES = {95, 96, 99}

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTFC_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_VARS = ["temperature_2m", "precipitation", "weather_code", "wind_speed_10m",
               "wind_gusts_10m", "wind_direction_10m", "cape"]

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MODEL_FILE = STATE_DIR / "best_model.json"
ALERT_FILE = STATE_DIR / "alert_state.json"
DEFAULT_MODEL = "ecmwf_ifs025"


# --------------------------------------------------------------------------- helpers
def get(url, params, tries=3):
    err = ""
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=90)
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}: {r.text[:300]}"
        except requests.RequestException as e:
            err = str(e)
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url} failed: {err}")


def series(hourly, var):
    """Return the hourly series for var, tolerating Open-Meteo's model suffixes."""
    if var in hourly:
        return hourly[var]
    for k, v in hourly.items():
        if k.startswith(var + "_"):
            return v
    return None


def to_rows(hourly):
    times = hourly["time"]
    cols = {v: series(hourly, v) for v in HOURLY_VARS}
    rows = []
    for i, t in enumerate(times):
        row = {"time": t}
        for v, s in cols.items():
            row[v] = s[i] if s is not None and i < len(s) else None
        rows.append(row)
    return rows


def now_local():
    return dt.datetime.now(ZoneInfo(TZ)).replace(tzinfo=None)


def model_name(m):
    return CANDIDATE_MODELS.get(m, m)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    STATE_DIR.mkdir(exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def best_model():
    data = load_json(MODEL_FILE, {})
    return data.get("best", DEFAULT_MODEL), data


def fetch_forecast(model, days=3):
    d = get(FORECAST_URL, {
        "latitude": LAT, "longitude": LON, "hourly": ",".join(HOURLY_VARS),
        "models": model, "timezone": TZ, "wind_speed_unit": "kmh", "forecast_days": days,
    })
    return to_rows(d["hourly"])


def fetch_all(models, days):
    out, failed = {}, []
    for m in models:
        try:
            rows = fetch_forecast(m, days)
            if any(r["precipitation"] is not None for r in rows):
                out[m] = rows
            else:
                failed.append(m)
        except RuntimeError as e:
            print(f"[warn] {m}: {e}", file=sys.stderr)
            failed.append(m)
    return out, failed


def wind_dir(deg):
    if deg is None:
        return ""
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg % 360) / 45 + 0.5) % 8]


def thunder_level(row):
    """0 none, 1 low, 2 moderate, 3 high."""
    code, cape, p = row.get("weather_code"), row.get("cape") or 0, row.get("precipitation") or 0
    if code in THUNDER_CODES:
        return 3
    if cape >= CAPE_HIGH and p >= 1:
        return 2
    if cape >= 1000:
        return 1
    return 0


TL_TEXT = {0: "none", 1: "low", 2: "moderate", 3: "HIGH"}


# --------------------------------------------------------------------------- telegram
def send_telegram(text, dry_run=False):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chats = [c.strip() for c in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]
    if dry_run or not token or not chats:
        print(text)
        if not dry_run:
            print("\n[info] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS not set - printed only.")
        return
    for chat in chats:
        for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)]:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                "chat_id": chat, "text": chunk, "parse_mode": "HTML",
                "disable_web_page_preview": True}, timeout=30)
            if r.status_code != 200:
                # retry without formatting so the message always arrives
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": chunk}, timeout=30)


# --------------------------------------------------------------------------- backtest
def _blocks(times, precip, gusts):
    """Aggregate hourly data into 6-hour blocks: rain sum and max gust."""
    out = {}
    for i, t in enumerate(times):
        p = precip[i] if precip and i < len(precip) else None
        if p is None:
            continue
        g = gusts[i] if gusts and i < len(gusts) else None
        k = (t[:10], int(t[11:13]) // 6)
        b = out.setdefault(k, {"rain": 0.0, "gust": None, "n": 0})
        b["rain"] += p
        b["n"] += 1
        if g is not None:
            b["gust"] = g if b["gust"] is None else max(b["gust"], g)
    return {k: v for k, v in out.items() if v["n"] == 6}


def _ets(pairs, thr):
    a = b = c = d = 0
    for f, o in pairs:
        F, O = f >= thr, o >= thr
        if F and O:
            a += 1
        elif F:
            b += 1
        elif O:
            c += 1
        else:
            d += 1
    n = a + b + c + d
    if n == 0 or (a + c) == 0:
        return None, None
    ar = (a + b) * (a + c) / n
    den = a + b + c - ar
    ets = None if den == 0 else (a - ar) / den
    bias = (a + b) / (a + c)
    return ets, bias


def _metrics(fc, ob, months=None):
    keys = [k for k in fc if k in ob and (months is None or int(k[0][5:7]) in months)]
    rain = [(fc[k]["rain"], ob[k]["rain"]) for k in keys]
    gust = [(fc[k]["gust"], ob[k]["gust"]) for k in keys
            if fc[k]["gust"] is not None and ob[k]["gust"] is not None]
    ets_rain, bias_rain = _ets(rain, 2.0)
    ets_heavy, _ = _ets(rain, 10.0)
    mae = sum(abs(f - o) for f, o in gust) / len(gust) if gust else None
    gbias = sum(f - o for f, o in gust) / len(gust) if gust else None
    return {"blocks": len(keys), "days": round(len(keys) / 4),
            "ets_rain_2mm": ets_rain, "ets_heavy_10mm": ets_heavy, "rain_freq_bias": bias_rain,
            "gust_mae_kmh": mae, "gust_bias_kmh": gbias}


def _rank_score(metrics):
    """Weighted average rank (1 = best) across rain, heavy rain and gust skill."""
    weights = {"ets_rain_2mm": (0.35, True), "ets_heavy_10mm": (0.25, True), "gust_mae_kmh": (0.40, False)}
    models = list(metrics)
    score = {m: 0.0 for m in models}
    for key, (w, higher_better) in weights.items():
        vals = sorted(models, key=lambda m: (metrics[m][key] is None,
                                             -(metrics[m][key] or 0) if higher_better else (metrics[m][key] or 0)))
        for rank, m in enumerate(vals, 1):
            score[m] += w * rank
    return score


def cmd_backtest(args):
    end = dt.date.today() - dt.timedelta(days=7)  # ERA5 is published with ~5 days delay
    start = end - dt.timedelta(days=args.days)
    common = {"latitude": LAT, "longitude": LON, "start_date": start.isoformat(),
              "end_date": end.isoformat(), "hourly": "precipitation,wind_gusts_10m",
              "timezone": TZ, "wind_speed_unit": "kmh"}
    print(f"Backtest {start} -> {end} at {LAT},{LON}")
    truth = get(ARCHIVE_URL, {**common, "models": "era5"})["hourly"]
    ob = _blocks(truth["time"], series(truth, "precipitation"), series(truth, "wind_gusts_10m"))

    month = dt.date.today().month
    season = {((month + d - 1) % 12) + 1 for d in (-1, 0, 1)}
    overall, seasonal, skipped = {}, {}, {}
    for m in CANDIDATE_MODELS:
        try:
            h = get(HISTFC_URL, {**common, "models": m})["hourly"]
        except RuntimeError as e:
            skipped[m] = str(e)[:120]
            continue
        fc = _blocks(h["time"], series(h, "precipitation"), series(h, "wind_gusts_10m"))
        mo = _metrics(fc, ob)
        if mo["days"] < args.min_days:
            skipped[m] = f"only {mo['days']} days of archived forecasts"
            continue
        overall[m] = mo
        seasonal[m] = _metrics(fc, ob, season)
        print(f"  {model_name(m):22s} days={mo['days']:4d} ETS2={mo['ets_rain_2mm']} "
              f"ETS10={mo['ets_heavy_10mm']} gustMAE={mo['gust_mae_kmh']}")

    if not overall:
        sys.exit("No model had enough data - check connectivity to open-meteo.com")
    s_all = _rank_score(overall)
    s_sea = _rank_score({m: v for m, v in seasonal.items() if v["blocks"] >= 120} or overall)
    final = {m: 0.5 * s_all[m] + 0.5 * s_sea.get(m, s_all[m]) for m in overall}
    ranking = sorted(final, key=final.get)
    result = {
        "best": ranking[0], "best_name": model_name(ranking[0]),
        "ranking": [{"model": m, "name": model_name(m), "score": round(final[m], 3),
                     "overall": overall[m], "season": seasonal[m]} for m in ranking],
        "skipped": skipped, "period": [start.isoformat(), end.isoformat()],
        "season_months": sorted(season), "truth": "ERA5 reanalysis (Open-Meteo archive)",
        "method": "6-hour blocks; ETS for rain >=2 mm and >=10 mm, MAE of max gust; "
                  "rank-weighted 35/25/40, half full period, half current season",
        "computed_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    save_json(MODEL_FILE, result)
    print(f"\nBest model: {model_name(ranking[0])}  (ranking: {', '.join(model_name(m) for m in ranking)})")


# --------------------------------------------------------------------------- report
BLOCKS = [("Night", 0, 6), ("Morning", 6, 12), ("Afternoon", 12, 18), ("Evening", 18, 24)]


def summarize_day(rows, day):
    rows = [r for r in rows if r["time"].startswith(day)]
    if not rows:
        return None
    blocks = []
    for name, h0, h1 in BLOCKS:
        br = [r for r in rows if h0 <= int(r["time"][11:13]) < h1]
        if not br:
            continue
        blocks.append({
            "name": f"{name} {h0:02d}-{h1:02d}",
            "rain_mm": round(sum(r["precipitation"] or 0 for r in br), 1),
            "max_rain_rate": round(max(r["precipitation"] or 0 for r in br), 1),
            "max_gust": round(max(r["wind_gusts_10m"] or 0 for r in br)),
            "thunder": max(thunder_level(r) for r in br),
        })
    peak = max(rows, key=lambda r: r["wind_gusts_10m"] or 0)
    hrs = [int(r["time"][11:13]) for r in rows if thunder_level(r) >= 2]
    spans = []
    for h in hrs:
        if spans and h == spans[-1][1] + 1:
            spans[-1][1] = h
        else:
            spans.append([h, h])
    thunder_window = ", ".join(f"{a:02d}-{(b + 1) % 24:02d}h" for a, b in spans)
    s = {
        "date": day,
        "rain_mm": round(sum(r["precipitation"] or 0 for r in rows), 1),
        "max_gust": round(peak["wind_gusts_10m"] or 0),
        "max_gust_time": peak["time"][11:16],
        "max_gust_dir": wind_dir(peak["wind_direction_10m"]),
        "max_wind": round(max(r["wind_speed_10m"] or 0 for r in rows)),
        "max_rain_rate": round(max(r["precipitation"] or 0 for r in rows), 1),
        "thunder": max(thunder_level(r) for r in rows),
        "thunder_window": thunder_window or None,
        "temp_max": round(max(r["temperature_2m"] or 0 for r in rows)),
        "blocks": blocks,
    }
    if s["max_gust"] >= GUST_SEVERE or (s["thunder"] == 3 and s["max_gust"] >= GUST_WARNING):
        s["level"] = "RED"
    elif s["max_gust"] >= GUST_WARNING or s["thunder"] >= 2 or s["max_rain_rate"] >= RAIN_HEAVY_MM_H:
        s["level"] = "AMBER"
    else:
        s["level"] = "GREEN"
    return s


def advice(s):
    out = []
    if s["max_gust"] >= GUST_WARNING:
        out.append("Stop work at height and crane lifts during peak gusts; secure or lower banners, "
                   "scrims and LED walls according to the wind management plan.")
    elif s["max_gust"] >= GUST_WATCH:
        out.append("Check wind before crane lifts and when handling large panels or roof covers.")
    if s["thunder"] >= 2:
        out.append("Lightning: stop outdoor and elevated work when thunder is heard; resume 30 min "
                   "after the last thunder. Keep shelter locations known to all crews.")
    if s["max_rain_rate"] >= RAIN_HEAVY_MM_H:
        out.append("Cloudbursts possible: protect power distribution and cable runs, check drainage "
                   "and ground conditions for forklifts and trucks.")
    if not out:
        out.append("No weather restrictions expected for build activities.")
    return out


LEVEL_ICON = {"GREEN": "🟢", "AMBER": "🟠", "RED": "🔴"}
TL_ICON = {0: "–", 1: "low", 2: "⚡ moderate", 3: "⚡⚡ HIGH"}


def template_report(data):
    e = html.escape
    lines = [f"<b>🌦 Site weather – {e(SITE)}</b>",
             f"<i>{e(data['issued'])} · model: {e(data['model_name'])}</i>", ""]
    for label, s in (("TODAY", data["today"]), ("TOMORROW", data["tomorrow"])):
        if not s:
            continue
        d = dt.date.fromisoformat(s["date"]).strftime("%a %d %b")
        lines.append(f"<b>{label} – {d}  {LEVEL_ICON[s['level']]} {s['level']}</b>")
        lines.append(f"Rain {s['rain_mm']} mm · peak gust {s['max_gust']} km/h "
                     f"{s['max_gust_dir']} at {s['max_gust_time']} · thunder {TL_TEXT[s['thunder']]}"
                     + (f" ({s['thunder_window']})" if s["thunder_window"] else "")
                     + f" · max {s['temp_max']}°C")
        rows = [f"{b['name']:<15}{b['rain_mm']:>5} mm {b['max_gust']:>4} km/h  {TL_ICON[b['thunder']]}"
                for b in s["blocks"]]
        lines.append("<pre>" + e("\n".join(rows)) + "</pre>")
        for a in advice(s):
            lines.append(f"• {e(a)}")
        lines.append("")
    if data["agreement"]:
        lines.append("<b>Model check (today)</b>")
        rows = [f"{a['name']:<20}{a['rain_mm']:>5} mm {a['max_gust']:>4} km/h  "
                f"{'⚡' if a['thunder'] >= 2 else '–'}" for a in data["agreement"]]
        lines.append("<pre>" + e("\n".join(rows)) + "</pre>")
    return "\n".join(lines).strip()


def claude_report(data, draft):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        "You are the site meteorologist for an outdoor festival build in Thailand. Write the "
        "morning weather briefing in English for the production and rigging crew, based ONLY on "
        "the JSON data below (never invent numbers). Priorities: thunderstorms/lightning, wind "
        "gusts, rain. Lead with a one-line verdict for today, then time windows that matter for "
        "work at height, crane lifts and electrics, then tomorrow, then how much the models agree "
        "(name the uncertainty honestly). Use Telegram HTML only (<b>, <i>, <pre>), no markdown, "
        "max 2500 characters.\n\nDATA:\n" + json.dumps(data, ensure_ascii=False)
        + "\n\nTEMPLATE DRAFT (for reference):\n" + draft)
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 1500,
                  "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"]).strip() or None
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"[warn] Claude text failed, using template: {e}", file=sys.stderr)
        return None


def cmd_report(args):
    best, info = best_model()
    ranked = [r["model"] for r in info.get("ranking", [])] or list(CANDIDATE_MODELS)
    others = [m for m in ranked if m != best][:4]
    fc, failed = fetch_all([best] + others, days=3)
    if best not in fc:  # fall back to next best available
        if not fc:
            send_telegram(f"⚠️ Weather report for {html.escape(SITE)} failed: no model data available.",
                          args.dry_run)
            sys.exit(1)
        best = next(m for m in ranked if m in fc) if any(m in fc for m in ranked) else next(iter(fc))
    now = now_local()
    today, tomorrow = now.date().isoformat(), (now.date() + dt.timedelta(days=1)).isoformat()
    agreement = []
    for m in [best] + [o for o in others if o in fc]:
        s = summarize_day(fc[m], today)
        if s:
            agreement.append({"model": m, "name": model_name(m), "rain_mm": s["rain_mm"],
                              "max_gust": s["max_gust"], "thunder": s["thunder"]})
    data = {
        "site": SITE, "issued": now.strftime("%a %d %b %Y %H:%M") + " local time",
        "model": best, "model_name": model_name(best),
        "model_selection": (f"most accurate in backtest {info['period'][0]}..{info['period'][1]}"
                            if info.get("period") else "default (no backtest yet)"),
        "today": summarize_day(fc[best], today),
        "tomorrow": summarize_day(fc[best], tomorrow),
        "agreement": agreement,
        "thresholds_kmh": {"watch": GUST_WATCH, "warning": GUST_WARNING, "severe": GUST_SEVERE},
    }
    draft = template_report(data)
    text = (None if args.no_ai else claude_report(data, draft)) or draft
    send_telegram(text, args.dry_run)


# --------------------------------------------------------------------------- alert
def evaluate_window(rows, start, end):
    return [r for r in rows if start <= dt.datetime.fromisoformat(r["time"]) < end]


def cmd_alert(args):
    best, info = best_model()
    ranked = [r["model"] for r in info.get("ranking", [])] or list(CANDIDATE_MODELS)
    others = [m for m in ranked if m != best][:2]
    fc, _ = fetch_all([best] + others, days=2)
    if best not in fc:
        if not fc:
            print("No data; skipping this run.")
            return
        best = next(iter(fc))
    now = now_local()
    start = now.replace(minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(hours=ALERT_HORIZON_H)
    win = {m: evaluate_window(rows, start, end) for m, rows in fc.items()}
    bw = win[best]

    current = {}
    # Thunderstorms
    th = [r for r in bw if thunder_level(r) == 3]
    agree = [model_name(m) for m in win if m != best and any(thunder_level(r) == 3 for r in win[m])]
    if th:
        current["thunder"] = {
            "level": 2 if agree else 1,
            "text": (f"⚡ Thunderstorm risk {'HIGH' if agree else 'possible'} "
                     f"{th[0]['time'][11:16]}–{(dt.datetime.fromisoformat(th[-1]['time']) + dt.timedelta(hours=1)).strftime('%H:%M')}"
                     f" ({model_name(best)}" + (f"; also {', '.join(agree)}" if agree else "") + ")"),
        }
    # Gusts
    peak = max(bw, key=lambda r: r["wind_gusts_10m"] or 0) if bw else None
    if peak and (peak["wind_gusts_10m"] or 0) >= GUST_WARNING:
        g = round(peak["wind_gusts_10m"])
        current["gust"] = {
            "level": 3 if g >= GUST_SEVERE else 2,
            "text": f"💨 Wind gusts up to {g} km/h {wind_dir(peak['wind_direction_10m'])} around "
                    f"{peak['time'][11:16]}" + (" – SEVERE" if g >= GUST_SEVERE else ""),
        }
    # Cloudburst
    wet = max(bw, key=lambda r: r["precipitation"] or 0) if bw else None
    if wet and (wet["precipitation"] or 0) >= RAIN_HEAVY_MM_H:
        current["rain"] = {"level": 2, "text": f"🌧 Cloudburst: {wet['precipitation']:.0f} mm/h around "
                                               f"{wet['time'][11:16]}"}

    state = load_json(ALERT_FILE, {})
    to_send, now_s = [], now.isoformat(timespec="minutes")
    for k, c in current.items():
        prev = state.get(k)
        due = (prev is None or c["level"] > prev["level"] or
               (now - dt.datetime.fromisoformat(prev["sent_at"])).total_seconds() / 3600 >= ALERT_COOLDOWN_H)
        if due:
            to_send.append(c["text"])
            state[k] = {"level": c["level"], "sent_at": now_s}
    cleared = [k for k in list(state) if k not in current]
    for k in cleared:
        del state[k]

    if to_send:
        actions = []
        if "gust" in current:
            actions.append("Stop work at height and crane lifts; secure/lower scrims, banners and LED walls per wind plan.")
        if "thunder" in current:
            actions.append("Clear roofs, towers and open field when thunder is heard; get crews to shelter.")
        if "rain" in current:
            actions.append("Protect power distribution and cable runs.")
        msg = (f"🚨 <b>WEATHER ALERT – {html.escape(SITE)}</b>\n<i>next {ALERT_HORIZON_H} h, issued "
               f"{now.strftime('%H:%M')}</i>\n\n" + "\n".join(html.escape(t) for t in to_send)
               + "\n\n<b>Action:</b>\n" + "\n".join("• " + html.escape(a) for a in actions)
               + "\n\n<i>Model-based warning: storm timing can shift ±1–2 h. Watch the sky and lightning maps.</i>")
        send_telegram(msg, args.dry_run)
    elif cleared:
        send_telegram(f"✅ <b>All clear – {html.escape(SITE)}</b>\nNo thunderstorms or damaging gusts "
                      f"expected in the next {ALERT_HORIZON_H} h ({now.strftime('%H:%M')}).", args.dry_run)
    else:
        print(f"{now_s}: nothing to report.")
    save_json(ALERT_FILE, state)


# --------------------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest")
    b.add_argument("--days", type=int, default=730)
    b.add_argument("--min-days", type=int, default=90)
    r = sub.add_parser("report")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--no-ai", action="store_true")
    a = sub.add_parser("alert")
    a.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    {"backtest": cmd_backtest, "report": cmd_report, "alert": cmd_alert}[args.cmd](args)


if __name__ == "__main__":
    main()

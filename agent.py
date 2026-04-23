"""
Morel Monitor Agent
Runs daily via GitHub Actions. Fetches Open-Meteo weather data for each burn
location, evaluates qualifying days against morel foraging criteria, identifies
multi-day foraging windows, and writes two outputs:

  - data.json     : consumed by the dashboard (index.html)
  - calendar.ics  : subscribed by Google Calendar for event notifications
"""

import json
import sys
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


REPO_ROOT = Path(__file__).parent
BURNS_PATH = REPO_ROOT / "burns.json"
CRITERIA_PATH = REPO_ROOT / "criteria.json"
DATA_OUT = REPO_ROOT / "data.json"
ICS_OUT = REPO_ROOT / "calendar.ics"

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
TIMEZONE = "America/Los_Angeles"

HTTP_TIMEOUT_SEC = 20
USER_AGENT = "morel-monitor/1.0 (github.com/Shroomer-HQ/morel-monitor)"


# ---------------------------------------------------------------------------
# Weather fetching
# ---------------------------------------------------------------------------

def http_get_json(url: str) -> dict:
    """Simple GET that returns parsed JSON. No external deps."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_burn_weather(burn: dict, lookback_days: int, forecast_days: int) -> dict:
    """
    Fetch combined archive (trailing) + forecast (leading) daily weather.
    Open-Meteo's forecast endpoint actually serves both if you set past_days,
    which avoids stitching two APIs together.
    """
    params = {
        "latitude": burn["lat"],
        "longitude": burn["lon"],
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
        ]),
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": TIMEZONE,
        "past_days": lookback_days,
        "forecast_days": forecast_days,
    }
    url = f"{OPEN_METEO_FORECAST}?{urlencode(params)}"
    raw = http_get_json(url)

    daily = raw.get("daily", {})
    dates = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    precip = daily.get("precipitation_sum", [])

    days = []
    for i, d in enumerate(dates):
        days.append({
            "date": d,
            "tmax_f": tmax[i] if i < len(tmax) else None,
            "tmin_f": tmin[i] if i < len(tmin) else None,
            "precip_in": precip[i] if i < len(precip) else None,
        })
    return {"burn_id": burn["id"], "days": days}


# ---------------------------------------------------------------------------
# Criteria evaluation
# ---------------------------------------------------------------------------

def evaluate_days(days: list, criteria: dict) -> list:
    """
    Annotate each day with a 'qualifies' boolean and a 'reasons' list.
    Skips days that are missing data.
    """
    c = criteria["daily_conditions"]
    out = []
    for i, day in enumerate(days):
        reasons = []
        qualifies = True

        if day["tmax_f"] is None or day["tmin_f"] is None or day["precip_in"] is None:
            out.append({**day, "qualifies": False, "reasons": ["missing data"]})
            continue

        # Temperature band
        if day["tmax_f"] < c["temp_max_min_f"]:
            qualifies = False
            reasons.append(f"tmax {day['tmax_f']:.0f}°F < {c['temp_max_min_f']}°F")
        if day["tmax_f"] > c["temp_max_max_f"]:
            qualifies = False
            reasons.append(f"tmax {day['tmax_f']:.0f}°F > {c['temp_max_max_f']}°F")
        if day["tmin_f"] < c["temp_min_floor_f"]:
            qualifies = False
            reasons.append(f"tmin {day['tmin_f']:.0f}°F < {c['temp_min_floor_f']}°F")

        # Trailing 14-day precipitation
        start = max(0, i - 13)
        trailing_14 = sum(
            d["precip_in"] for d in days[start:i + 1] if d["precip_in"] is not None
        )
        if trailing_14 < c["trailing_rain_14d_min_in"]:
            qualifies = False
            reasons.append(f"14d rain {trailing_14:.2f}\" < {c['trailing_rain_14d_min_in']}\"")

        # Trailing 3-day precipitation (moisture freshness)
        start3 = max(0, i - 2)
        trailing_3 = sum(
            d["precip_in"] for d in days[start3:i + 1] if d["precip_in"] is not None
        )
        if trailing_3 < c["trailing_rain_3d_min_in"]:
            qualifies = False
            reasons.append(f"3d rain {trailing_3:.2f}\" < {c['trailing_rain_3d_min_in']}\"")

        # No hard freeze in trailing window
        freeze_start = max(0, i - (c["no_freeze_trailing_days"] - 1))
        freeze_days = [
            d for d in days[freeze_start:i + 1]
            if d["tmin_f"] is not None and d["tmin_f"] <= c["freeze_threshold_f"]
        ]
        if freeze_days:
            qualifies = False
            reasons.append(f"hard freeze in trailing {c['no_freeze_trailing_days']}d")

        out.append({
            **day,
            "qualifies": qualifies,
            "trailing_rain_14d_in": round(trailing_14, 2),
            "trailing_rain_3d_in": round(trailing_3, 2),
            "reasons": reasons,
        })
    return out


def find_windows(evaluated_days: list, criteria: dict, today_iso: str) -> list:
    """
    Identify consecutive runs of qualifying days of length >= min_consecutive_days.
    Only returns windows that end today or in the future (not purely historical).
    """
    min_len = criteria["window"]["min_consecutive_days"]
    min_lead = criteria["window"]["min_lead_time_days"]

    windows = []
    run_start = None
    for i, day in enumerate(evaluated_days):
        if day["qualifies"]:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and (i - run_start) >= min_len:
                windows.append((run_start, i - 1))
            run_start = None
    if run_start is not None and (len(evaluated_days) - run_start) >= min_len:
        windows.append((run_start, len(evaluated_days) - 1))

    today = date.fromisoformat(today_iso)
    results = []
    for start_i, end_i in windows:
        start_date = evaluated_days[start_i]["date"]
        end_date = evaluated_days[end_i]["date"]
        # Filter: only keep windows that overlap today or future
        if date.fromisoformat(end_date) < today:
            continue
        # Lead time check
        lead = (date.fromisoformat(start_date) - today).days
        if lead < min_lead:
            # Still include ongoing windows (start in the past), skip only if entirely too soon
            if date.fromisoformat(end_date) < today:
                continue
        results.append({
            "start_date": start_date,
            "end_date": end_date,
            "length_days": end_i - start_i + 1,
            "days": evaluated_days[start_i:end_i + 1],
        })
    return results


# ---------------------------------------------------------------------------
# Output generation
# ---------------------------------------------------------------------------

def build_data_json(burns: list, criteria: dict, burn_results: list, today_iso: str) -> dict:
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_for_date": today_iso,
        "timezone": TIMEZONE,
        "criteria": criteria,
        "burns": burns,
        "results": burn_results,
    }


def window_uid(burn_id: str, start_date: str) -> str:
    """Stable UID so Google Calendar updates (not duplicates) existing events."""
    raw = f"{burn_id}-{start_date}"
    return f"{hashlib.md5(raw.encode()).hexdigest()}@morel-monitor"


def escape_ics(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def build_ics(burn_results: list, burns_by_id: dict, today_iso: str) -> str:
    """
    Build an iCalendar feed. Each qualifying window becomes an all-day event
    with a stable UID derived from (burn_id, start_date). Google Calendar will
    update existing events when the feed changes.
    """
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Morel Monitor//Shroomer-HQ//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Morel Foraging Windows",
        "X-WR-TIMEZONE:America/Los_Angeles",
        "X-WR-CALDESC:Automated morel foraging windows based on burn scar weather analysis.",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    for burn_result in burn_results:
        burn = burns_by_id[burn_result["burn_id"]]
        for window in burn_result["windows"]:
            uid = window_uid(burn["id"], window["start_date"])
            # All-day events: DTSTART is inclusive, DTEND is exclusive.
            start_dt = date.fromisoformat(window["start_date"])
            end_dt = date.fromisoformat(window["end_date"]) + timedelta(days=1)

            # Summarize conditions across the window
            avg_tmax = sum(d["tmax_f"] for d in window["days"]) / len(window["days"])
            total_precip_14d = window["days"][-1]["trailing_rain_14d_in"]

            desc_parts = [
                f"{burn['name']} — {burn['location']}",
                f"Elevation: {burn['elev_low_ft']}–{burn['elev_high_ft']} ft",
                f"Window: {window['length_days']} day(s)",
                f"Avg daily high: {avg_tmax:.0f}°F",
                f"14-day trailing rain (end of window): {total_precip_14d:.2f}\"",
                "",
                "Daily breakdown:",
            ]
            for d in window["days"]:
                desc_parts.append(
                    f"  {d['date']}: {d['tmax_f']:.0f}/{d['tmin_f']:.0f}°F, "
                    f"{d['precip_in']:.2f}\" precip, 14d={d['trailing_rain_14d_in']:.2f}\""
                )
            desc_parts.append("")
            desc_parts.append(f"Generated {today_iso} by morel-monitor agent.")
            description = "\n".join(desc_parts)

            summary = f"🍄 {burn['name']} — foraging window"

            lines.extend([
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART;VALUE=DATE:{start_dt.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{end_dt.strftime('%Y%m%d')}",
                f"SUMMARY:{escape_ics(summary)}",
                f"DESCRIPTION:{escape_ics(description)}",
                f"LOCATION:{escape_ics(burn['location'])}",
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            ])

    lines.append("END:VCALENDAR")
    # iCal lines must be CRLF-separated
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    burns_cfg = json.loads(BURNS_PATH.read_text())
    criteria = json.loads(CRITERIA_PATH.read_text())
    burns = burns_cfg["burns"]
    burns_by_id = {b["id"]: b for b in burns}

    today_iso = date.today().isoformat()
    lookback = criteria["lookback_days"]
    forecast = criteria["forecast_days"]

    burn_results = []
    errors = []

    for burn in burns:
        try:
            weather = fetch_burn_weather(burn, lookback, forecast)
            evaluated = evaluate_days(weather["days"], criteria)
            windows = find_windows(evaluated, criteria, today_iso)
            burn_results.append({
                "burn_id": burn["id"],
                "days": evaluated,
                "windows": windows,
                "error": None,
            })
            print(f"[ok] {burn['name']}: {len(windows)} window(s)")
        except (URLError, HTTPError, KeyError, ValueError) as e:
            msg = f"{type(e).__name__}: {e}"
            errors.append({"burn_id": burn["id"], "error": msg})
            burn_results.append({
                "burn_id": burn["id"],
                "days": [],
                "windows": [],
                "error": msg,
            })
            print(f"[err] {burn['name']}: {msg}", file=sys.stderr)

    # Write outputs
    data = build_data_json(burns, criteria, burn_results, today_iso)
    DATA_OUT.write_text(json.dumps(data, indent=2))
    print(f"Wrote {DATA_OUT}")

    ics = build_ics(burn_results, burns_by_id, today_iso)
    ICS_OUT.write_text(ics)
    print(f"Wrote {ICS_OUT}")

    # Summary
    total_windows = sum(len(r["windows"]) for r in burn_results)
    print(f"\nSummary: {total_windows} foraging window(s) across {len(burns)} burns.")
    if errors:
        print(f"Encountered {len(errors)} error(s). See above.")
        # Don't fail the whole run on individual burn errors — we still want
        # the dashboard and calendar updated with what we got.

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Razor Clam Monitor Agent

Scrapes WDFW's razor clam seasons page for approved/tentative dig dates, scores
each dig by tide depth and daylight alignment, and writes:

  - razor-clams.json : structured data (for optional dashboard integration)
  - razor-clams.ics  : iCalendar feed for Google Calendar subscription

Each dig becomes a 3-hour timed event (2h before low tide to 1h after), with
a priority label in the summary and full tide / daylight / beach details in
the description.

Priority classification:
  - priority   : tide <= -0.5 ft AND dig time in daylight
  - standard   : tide <= +0.5 ft AND dig time in daylight
  - marginal   : tide <= +0.5 ft but in the dark
  - low        : anything else

Beaches: Long Beach, Twin Harbors, Copalis, Mocrocks, Kalaloch.
Kalaloch has been closed in recent seasons but is included for future-proofing.
"""

import re
import json
import sys
import math
import hashlib
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).parent
OUT_JSON = REPO_ROOT / "razor-clams.json"
OUT_ICS = REPO_ROOT / "razor-clams.ics"

WDFW_SEASONS_URL = "https://wdfw.wa.gov/fishing/shellfishing-regulations/razor-clams"
# Fallback: the newsroom page lists recent releases with URLs we can follow.
WDFW_NEWSROOM = "https://wdfw.wa.gov/newsroom/news-releases"

USER_AGENT = (
    "Mozilla/5.0 (compatible; razor-clam-monitor/1.0; "
    "+https://github.com/Shroomer-HQ/morel-monitor)"
)
TIMEOUT_SEC = 30
PACIFIC = ZoneInfo("America/Los_Angeles")

# Central Washington coast — used for solar calculation.
COAST_LAT = 46.9
COAST_LON = -124.1

# Known beaches and their coords. Any beach name the scraper encounters that's
# not in this dict will still be included in the event description, but won't
# be linked to coordinates.
BEACHES = {
    "Long Beach":    {"lat": 46.35, "lon": -124.05},
    "Twin Harbors":  {"lat": 46.85, "lon": -124.11},
    "Copalis":       {"lat": 47.13, "lon": -124.18},
    "Mocrocks":      {"lat": 47.21, "lon": -124.24},
    "Kalaloch":      {"lat": 47.60, "lon": -124.38},
}


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

DIG_RE = re.compile(
    r"""
    (?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+
    (?P<day>\d{1,2}),\s+
    (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+
    (?P<hour>\d{1,2}):(?P<minute>\d{2})\s*
    (?P<meridiem>[ap])\.?m\.?;\s*
    (?P<tide>-?\d+\.\d+)\s+feet;\s*
    (?P<beaches>[^;·•\n]+?)
    (?=\s*(?:·|•|\n|$|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d))
    """,
    re.VERBOSE | re.IGNORECASE,
)

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def http_get(url: str) -> str:
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urlopen(req, timeout=TIMEOUT_SEC) as resp:
        raw = resp.read()
        encoding = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(encoding, errors="replace")


def strip_html(html: str) -> str:
    """Crude but dependency-free HTML → text."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    entities = {
        "&nbsp;": " ", "&amp;": "&", "&quot;": '"', "&#39;": "'",
        "&apos;": "'", "&ndash;": "–", "&mdash;": "—",
        "&#8211;": "–", "&#8212;": "—", "&#8217;": "'",
        "&#183;": "·", "&middot;": "·",
    }
    for k, v in entities.items():
        text = text.replace(k, v)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text


def parse_digs(text: str, reference_year: int) -> list:
    today = date.today()
    digs = []
    for m in DIG_RE.finditer(text):
        try:
            month_key = m.group("month").lower()[:3]
            month = MONTH_MAP[month_key]
            day = int(m.group("day"))
            hour = int(m.group("hour"))
            minute = int(m.group("minute"))
            meridiem = m.group("meridiem").lower()
            if meridiem == "p" and hour != 12:
                hour += 12
            elif meridiem == "a" and hour == 12:
                hour = 0
            tide = float(m.group("tide"))

            beaches_raw = m.group("beaches").strip().rstrip(".").rstrip(",")
            beach_tokens = re.split(r",\s*(?:and\s+)?|\s+and\s+", beaches_raw)
            beach_list = [b.strip() for b in beach_tokens if b.strip()]
            # Normalize: strip trailing punctuation, match against known beaches
            beach_list = [
                b for b in beach_list
                if any(b.lower().startswith(known.lower()) for known in BEACHES)
                or b in BEACHES
            ]

            # Year inference: dates more than 180 days in the past probably belong
            # to next year.
            year = reference_year
            candidate = date(year, month, day)
            if (today - candidate).days > 180:
                year += 1
                candidate = date(year, month, day)

            dig_dt = datetime(year, month, day, hour, minute, tzinfo=PACIFIC)
            digs.append({
                "date": candidate.isoformat(),
                "weekday": dig_dt.strftime("%A"),
                "low_tide_local": dig_dt.strftime("%I:%M %p").lstrip("0"),
                "low_tide_24h": dig_dt.strftime("%H:%M"),
                "low_tide_ft": tide,
                "beaches": beach_list,
                "series": "morning" if hour < 12 else "evening",
                "datetime_iso": dig_dt.isoformat(),
            })
        except (KeyError, ValueError) as e:
            print(f"[warn] skipped unparseable match: {e}", file=sys.stderr)
            continue

    # Dedupe by (date, time) — the same dig often appears in multiple places.
    seen = set()
    uniq = []
    for d in digs:
        key = (d["date"], d["low_tide_24h"])
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


# ---------------------------------------------------------------------------
# Solar (sunrise/sunset) — no external deps
# ---------------------------------------------------------------------------

def solar_times(lat: float, lon: float, d: date) -> tuple:
    """
    Approximate sunrise and sunset in local Pacific time.
    Returns (sunrise_dt, sunset_dt) or (None, None) for polar conditions.
    Typical error under 10 minutes for mid-latitudes.
    """
    doy = d.timetuple().tm_yday
    decl_deg = 23.44 * math.sin(math.radians((360.0 / 365.0) * (doy - 81)))
    lat_r = math.radians(lat)
    decl_r = math.radians(decl_deg)
    cos_H = -math.tan(lat_r) * math.tan(decl_r)
    if cos_H > 1 or cos_H < -1:
        return (None, None)
    H_deg = math.degrees(math.acos(cos_H))

    # Equation of time (minutes)
    B = math.radians((360.0 / 365.0) * (doy - 81))
    eot = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)

    # Adjust for longitude offset from PST meridian (-120°)
    tz_meridian = -120.0
    time_correction_min = 4 * (lon - tz_meridian) + eot
    # Note: during DST the clock is shifted +1 hour, but we're computing in
    # standard time. We'll apply DST offset if the date is in DST.
    noon_min = 12 * 60 - time_correction_min
    half_day_min = H_deg * 4  # 15°/hr = 4 min/°
    sunrise_min = noon_min - half_day_min
    sunset_min = noon_min + half_day_min

    # Rough DST adjustment: second Sunday in March to first Sunday in November.
    dst_start = _nth_weekday(d.year, 3, 6, 2)  # 2nd Sunday of March (weekday 6=Sun)
    dst_end = _nth_weekday(d.year, 11, 6, 1)   # 1st Sunday of November
    if dst_start <= d < dst_end:
        sunrise_min += 60
        sunset_min += 60

    def to_dt(m):
        # Clamp
        m = max(0, min(24 * 60 - 1, m))
        h = int(m // 60)
        mm = int(m % 60)
        return datetime.combine(d, time(h, mm), tzinfo=PACIFIC)

    return (to_dt(sunrise_min), to_dt(sunset_min))


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth occurrence of a weekday (0=Mon…6=Sun) in a given month."""
    first = date(year, month, 1)
    days_ahead = (weekday - first.weekday()) % 7
    return first + timedelta(days=days_ahead + 7 * (n - 1))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_dig(dig: dict) -> dict:
    d = date.fromisoformat(dig["date"])
    sunrise, sunset = solar_times(COAST_LAT, COAST_LON, d)
    dig_dt = datetime.fromisoformat(dig["datetime_iso"])

    if sunrise and sunset:
        civil_dawn = sunrise - timedelta(minutes=30)
        civil_dusk = sunset + timedelta(minutes=30)
        in_daylight = civil_dawn <= dig_dt <= civil_dusk
    else:
        in_daylight = False

    tide = dig["low_tide_ft"]
    # Tide score: -1.5 → 100, 0 → 60, +1 → 0 (clamped)
    tide_score = max(0.0, min(100.0, (1.5 - tide) / 2.5 * 100))

    if tide <= -0.5 and in_daylight:
        priority = "priority"
    elif tide <= 0.5 and in_daylight:
        priority = "standard"
    elif tide <= 0.5:
        priority = "marginal (dark)"
    else:
        priority = "low"

    return {
        "priority": priority,
        "tide_score": round(tide_score, 1),
        "in_daylight": in_daylight,
        "sunrise_local": sunrise.strftime("%I:%M %p").lstrip("0") if sunrise else None,
        "sunset_local": sunset.strftime("%I:%M %p").lstrip("0") if sunset else None,
    }


# ---------------------------------------------------------------------------
# ICS output
# ---------------------------------------------------------------------------

def escape_ics(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _dt_to_utc_ical(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(enriched: list, today_iso: str) -> str:
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Razor Clam Monitor//Shroomer-HQ//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:WA Razor Clam Digs",
        "X-WR-TIMEZONE:America/Los_Angeles",
        "X-WR-CALDESC:WDFW-approved razor clam dig dates with tide + daylight scoring.",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    emoji = {
        "priority": "🦪",
        "standard": "🌊",
        "marginal (dark)": "🌙",
        "low": "·",
    }

    for dig in enriched:
        low_dt = datetime.fromisoformat(dig["datetime_iso"])
        start = low_dt - timedelta(hours=2)
        end = low_dt + timedelta(hours=1)

        uid = hashlib.md5(
            f"razor-{dig['date']}-{dig['low_tide_24h']}".encode()
        ).hexdigest() + "@morel-monitor"

        beaches_str = ", ".join(dig["beaches"]) if dig["beaches"] else "Beach TBD"
        priority = dig["priority"]
        summary = f"{emoji.get(priority, '·')} Razor clams — {beaches_str} [{priority}]"

        desc = [
            f"Low tide: {dig['low_tide_local']} @ {dig['low_tide_ft']:+.1f} ft",
            f"Daylight: {dig['sunrise_local']} — {dig['sunset_local']}",
            f"Dig falls in: {'daylight' if dig['in_daylight'] else 'darkness'}",
            f"Priority: {priority} (tide score {dig['tide_score']}/100)",
            f"Beaches: {beaches_str}",
            f"Recommended window: {start.strftime('%-I:%M %p')}–{end.strftime('%-I:%M %p')}",
            "",
            "Status: WDFW-announced. Final approval depends on marine toxin (domoic acid)",
            "testing — usually confirmed a few days before the dig date.",
            "",
            f"Generated {today_iso} by razor-clam-monitor.",
        ]

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART:{_dt_to_utc_ical(start)}",
            f"DTEND:{_dt_to_utc_ical(end)}",
            f"SUMMARY:{escape_ics(summary)}",
            f"DESCRIPTION:{escape_ics(chr(10).join(desc))}",
            f"LOCATION:{escape_ics(beaches_str + ', Washington coast')}",
            "TRANSP:OPAQUE",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = date.today()
    today_iso = today.isoformat()

    digs = []
    source = None
    error = None
    try:
        html = http_get(WDFW_SEASONS_URL)
        text = strip_html(html)
        digs = parse_digs(text, today.year)
        source = WDFW_SEASONS_URL
        print(f"[ok] Parsed {len(digs)} dig(s) from WDFW seasons page")
    except (URLError, HTTPError) as e:
        error = f"{type(e).__name__}: {e}"
        print(f"[err] WDFW fetch failed: {error}", file=sys.stderr)

    # Keep only upcoming digs
    digs = [d for d in digs if d["date"] >= today_iso]
    digs.sort(key=lambda d: d["datetime_iso"])

    enriched = []
    for dig in digs:
        score = score_dig(dig)
        enriched.append({**dig, **score})

    # Preserve previous data on scrape failure (avoid wiping the calendar)
    if error and OUT_JSON.exists():
        print("[info] Scrape failed — keeping existing data.json and calendar.ics in place.", file=sys.stderr)
        return 0  # Soft success: workflow continues, old data retained

    OUT_JSON.write_text(json.dumps({
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_for_date": today_iso,
        "source": source,
        "error": error,
        "digs": enriched,
    }, indent=2))
    print(f"[ok] Wrote {OUT_JSON}")

    ics = build_ics(enriched, today_iso)
    OUT_ICS.write_text(ics)
    print(f"[ok] Wrote {OUT_ICS}")

    # Summary
    by_priority = {}
    for d in enriched:
        by_priority[d["priority"]] = by_priority.get(d["priority"], 0) + 1
    print(f"\nDigs found: {len(enriched)}")
    for p, n in sorted(by_priority.items()):
        print(f"  {p}: {n}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

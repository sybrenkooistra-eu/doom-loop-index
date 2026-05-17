"""
collect_data.py
================
Wekelijks data-verzamelscript voor de Doom Loop Political Index.

Voor elke leider in data/leaders.json verzamelt dit script:
  1. Tijd in functie (uit took_office datum)
  2. Macro-economische indicatoren (van World Bank API):
     - GDP growth YoY %, current and previous year
     - Inflation YoY %, current and previous year
     - Unemployment %

Consumer confidence en peilingen worden via classify.py opgehaald
(Claude API met web search).

Output: data/inputs.json

Usage:
    python scripts/collect_data.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
LEADERS_FILE = REPO_ROOT / "data" / "leaders.json"
OUTPUT_FILE = REPO_ROOT / "data" / "inputs.json"

HTTP_HEADERS = {
    "User-Agent": "DoomLoopIndex/1.0 (personal project)"
}


def compute_tenure(took_office_str: str) -> dict:
    took_office = datetime.fromisoformat(took_office_str)
    today = datetime.now()
    days = (today - took_office).days
    return {
        "took_office": took_office_str,
        "days_in_office": days,
        "years_in_office": round(days / 365.25, 2),
    }


# World Bank indicators
WB_INDICATORS = {
    "gdp_growth_yoy_pct": "NY.GDP.MKTP.KD.ZG",
    "inflation_yoy_pct": "FP.CPI.TOTL.ZG",
    "unemployment_pct": "SL.UEM.TOTL.ZS",
}


def fetch_world_bank_series(country_code: str, indicator: str) -> list[tuple[int, float]]:
    """
    Returns list of (year, value) tuples, sorted with most recent first.
    Only non-null values.
    """
    url = f"https://api.worldbank.org/v2/country/{country_code}/indicator/{indicator}"
    params = {"format": "json", "per_page": 20}
    try:
        resp = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or len(data) < 2:
            return []
        results = []
        for entry in data[1]:
            if entry.get("value") is not None:
                results.append((int(entry["date"]), round(float(entry["value"]), 2)))
        results.sort(reverse=True)
        return results
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"      ⚠ World Bank fetch failed for {country_code}/{indicator}: {e}", file=sys.stderr)
        return []


def collect_macro(leader: dict) -> dict:
    """Macro-economische data met vorige-jaar-vergelijkingen voor inflation en GDP."""
    cc = leader["country_code"]
    macro = {}
    most_recent_year = None

    for field, indicator in WB_INDICATORS.items():
        series = fetch_world_bank_series(cc, indicator)
        if not series:
            macro[field] = None
            if field in ("inflation_yoy_pct", "gdp_growth_yoy_pct"):
                macro[field.replace("_yoy_pct", "_prev_year_pct")] = None
                macro[field.replace("_yoy_pct", "_data_year")] = None
            continue

        year_current, val_current = series[0]
        macro[field] = val_current

        if most_recent_year is None or year_current > most_recent_year:
            most_recent_year = year_current

        # For inflation and GDP: also track previous year (for YoY delta)
        if field in ("inflation_yoy_pct", "gdp_growth_yoy_pct"):
            prev_field = field.replace("_yoy_pct", "_prev_year_pct")
            year_field = field.replace("_yoy_pct", "_data_year")
            macro[year_field] = year_current
            macro[prev_field] = series[1][1] if len(series) > 1 else None

    macro["data_year"] = most_recent_year
    return macro


def main():
    print(f"Reading leaders from {LEADERS_FILE}")
    with LEADERS_FILE.open() as f:
        leaders_data = json.load(f)

    leaders = leaders_data["leaders"]
    print(f"Found {len(leaders)} leaders\n")

    output = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "leaders": {},
    }

    for i, leader in enumerate(leaders, 1):
        print(f"[{i}/{len(leaders)}] {leader['flag']} {leader['name']} ({leader['country']})")

        leader_output = {
            "name": leader["name"],
            "country": leader["country"],
            "country_code": leader["country_code"],
            "system": leader["system"],
            "role": leader["role"],
            "leader_party": leader["leader_party"],
            "coalition_parties": leader.get("coalition_parties"),
            "far_right_party": leader.get("far_right_party"),
            "tenure": compute_tenure(leader["took_office"]),
            "macro": collect_macro(leader),
        }

        output["leaders"][leader["id"]] = leader_output

        m = leader_output["macro"]
        t = leader_output["tenure"]
        infl_now = m.get("inflation_yoy_pct")
        infl_prev = m.get("inflation_prev_year_pct")
        infl_delta = f"({infl_now - infl_prev:+.1f}pt)" if (infl_now is not None and infl_prev is not None) else ""
        print(
            f"      tenure: {t['years_in_office']}y · "
            f"macro ({m['data_year']}): GDP {m['gdp_growth_yoy_pct']}%, "
            f"infl {infl_now}% {infl_delta}, "
            f"unemp {m['unemployment_pct']}%"
        )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

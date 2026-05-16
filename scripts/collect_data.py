"""
collect_data.py
================
Wekelijks data-verzamelscript voor de Doom Loop Political Index.

Voor elke leider in data/leaders.json verzamelt dit script:
  1. Tijd in functie (uit took_office datum)
  2. Macro-economische indicatoren (van World Bank API)

Peilingen en nieuws worden NIET hier opgehaald — die regelt classify.py
later met Claude API + web search in één call.

Output: data/inputs.json — wordt later door classify.py gelezen.

Usage:
    python scripts/collect_data.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


# ───────────────────────────────────────────────────────────────
# Paden
# ───────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
LEADERS_FILE = REPO_ROOT / "data" / "leaders.json"
OUTPUT_FILE = REPO_ROOT / "data" / "inputs.json"

HTTP_HEADERS = {
    "User-Agent": "DoomLoopIndex/1.0 (personal project)"
}


# ───────────────────────────────────────────────────────────────
# Tijd-in-functie
# ───────────────────────────────────────────────────────────────
def compute_tenure(took_office_str: str) -> dict:
    """Bereken dagen en jaren in functie."""
    took_office = datetime.fromisoformat(took_office_str)
    today = datetime.now()
    days = (today - took_office).days
    return {
        "took_office": took_office_str,
        "days_in_office": days,
        "years_in_office": round(days / 365.25, 2),
    }


# ───────────────────────────────────────────────────────────────
# World Bank macro-indicatoren
# ───────────────────────────────────────────────────────────────
WB_INDICATORS = {
    "gdp_growth_yoy_pct": "NY.GDP.MKTP.KD.ZG",   # GDP growth annual %
    "inflation_yoy_pct": "FP.CPI.TOTL.ZG",       # Inflation, consumer prices annual %
    "unemployment_pct": "SL.UEM.TOTL.ZS",        # Unemployment, total (% labor force)
}


def fetch_world_bank_indicator(country_code: str, indicator: str) -> tuple[float | None, int | None]:
    """Haal meest recente niet-null waarde voor een World Bank indicator."""
    url = f"https://api.worldbank.org/v2/country/{country_code}/indicator/{indicator}"
    params = {"format": "json", "per_page": 10}
    try:
        resp = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or len(data) < 2:
            return None, None
        for entry in data[1]:
            if entry.get("value") is not None:
                return round(float(entry["value"]), 2), int(entry["date"])
        return None, None
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"      ⚠ World Bank fetch failed for {country_code}/{indicator}: {e}", file=sys.stderr)
        return None, None


def collect_macro(leader: dict) -> dict:
    """Verzamel macro-economische indicatoren voor een leider's land."""
    cc = leader["country_code"]
    macro = {}
    most_recent_year = None
    for field, indicator in WB_INDICATORS.items():
        val, year = fetch_world_bank_indicator(cc, indicator)
        macro[field] = val
        if year and (most_recent_year is None or year > most_recent_year):
            most_recent_year = year
    macro["data_year"] = most_recent_year
    return macro


# ───────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────
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
        print(
            f"      tenure: {t['years_in_office']}y · "
            f"macro ({m['data_year']}): "
            f"GDP {m['gdp_growth_yoy_pct']}%, "
            f"infl {m['inflation_yoy_pct']}%, "
            f"unemp {m['unemployment_pct']}%"
        )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

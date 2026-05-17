"""
classify.py — v4
=================
Wekelijks classificatie-script voor de Doom Loop Political Index.

Nieuw t.o.v. v3:
  - Vraagt Claude om coalition/far-right delta vs. laatste nationale verkiezing
  - Voor presidentiëlen: approval delta vs. inauguratie
  - Vraagt om Consumer Confidence Index (huidige + ~12 maanden geleden)
  - Geeft inflation YoY delta mee als context

Output: data/latest.json
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic, RateLimitError, APIError


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
INPUTS_FILE = REPO_ROOT / "data" / "inputs.json"
LATEST_FILE = REPO_ROOT / "data" / "latest.json"
WEEKLY_DIR = REPO_ROOT / "data" / "weekly"

MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 2000

DELAY_BETWEEN_CALLS = 15
RATE_LIMIT_BACKOFF = 60
MAX_RETRIES = 3


SYSTEM_PROMPT = """You assess where a head of government stands on the "doom loop" — a satirical-but-analytical framework for Western political cycles.

═══════════════════════════════════════════════════════════════════
THE FOUR PHASES (angle on a 360° circle)
═══════════════════════════════════════════════════════════════════

PHASE I — CENTRIST DENIAL (0°–90°)
  Centrist/mainstream party in office, clear mandate, far-right NOT yet
  a top-2 polling force (below ~15%, not the leading opposition).
  Sub-positions:
   0–25°  : Fresh, popular, far-right contained. (Albanese, early Martin)
   25–55° : Established, governing competently, far-right ≤15% and not
            the top opposition. (Sánchez actively resisting Vox)
   55–85° : Mid-term, slipping, but far-right still below 15% and below
            opposition leaders.

PHASE II — FAR-RIGHT RISES (90°–180°)
  Far-right is consequential — EITHER in coalition / supporting govt,
  OR leading polls (>20%, ahead of incumbent or top opposition).
  Sub-positions:
   90–115°  : Far-right overtaken government in polls or entered as
              junior coalition partner. (Starmer w/ Reform leading)
   115–145° : Far-right consolidating; centrist losing ground.
              (Macron w/ RN ahead, Merz w/ AfD #1)
   145–175° : Far-right at/near governing majority. Cordon collapsing.
              (Pre-power Meloni; Schoof's last weeks)
   175–180° : Far-right takes premiership.

PHASE III — DESTRUCTION (180°–270°)
  Far-right or far-right-led government in power. Active institutional
  erosion, judicial capture, economic damage, deteriorating public life.
  Sub-positions:
   180–200° : Just took power, dismantling beginning. (Early Meloni)
   200–230° : Active dismantling phase. (Trump, Fico)
   230–260° : Peak destruction or long tenure. (Erdoğan, Netanyahu)
   260–270° : Late-stage, defeat imminent.

PHASE IV — PROMISE OF CHANGE (270°–360°)
  New leader (often centrist, sometimes left, sometimes technocratic)
  defeats the far-right with a promise of change.
  Sub-positions:
   270–290° : Just took power, honeymoon. (Magyar, Carney, Lee)
   290–320° : First year, reality biting. (Sheinbaum one year in)
   320–355° : Late Phase IV, becoming next "uninspiring centrist". (Tusk)
   355–360° : Transition to Phase I.

═══════════════════════════════════════════════════════════════════
PRESIDENTIAL SYSTEMS
═══════════════════════════════════════════════════════════════════
  Trump = Phase III (far-right in office)
  Macron = Phase II (centrist, RN leads polls)
  Milei = Phase III
  Erdoğan = Phase III, late
  Lee = Phase IV (after Yoon impeachment)
  Sheinbaum = Phase IV (continuation of AMLO's change project)

═══════════════════════════════════════════════════════════════════
THE DOOM SCORE (0–100, within the leader's phase)
═══════════════════════════════════════════════════════════════════
NOTE: The frontend rescales doom per phase, so don't worry about absolute
levels across phases. Just rate doom 0-100 within the context of THIS phase:
  0–20   : Stable. Fresh honeymoon (P1/P4) or contained govt (P2/P3 lite)
  20–40  : Mild tension within the phase
  40–60  : Standard mid-cycle pressure
  60–80  : Severe deterioration
  80–100 : Peak intensity (collapse imminent / active destruction)

═══════════════════════════════════════════════════════════════════
WHAT YOU MUST RESEARCH (via web search, up to 6 searches)
═══════════════════════════════════════════════════════════════════
1. Current polling (last 30 days average):
   - For parliamentary: % for governing coalition AND for named far-right party
   - For presidential: % approval rating
2. Comparison baseline:
   - For parliamentary: % each got at the LAST NATIONAL ELECTION
     (so you can compute coalition_delta_vs_election and
      far_right_delta_vs_election)
   - For presidential: approval at INAUGURATION (or first month if
     no inauguration polling)
3. Consumer Confidence Index for the country:
   - Current value (or most recent monthly value)
   - Value from ~12 months ago
   - Source priority (use the first available, in this order):
       a. OECD CCI (covers most OECD countries; ~100-centered, monthly)
       b. European Commission Economic Sentiment Indicator (ESI) for EU countries (~100-centered)
       c. Ipsos Global Consumer Confidence Index (covers many G20 + emerging markets)
       d. Conference Board Global Consumer Confidence Index
       e. National statistical office indicator (INDEC for AR, CBS for IL, TÜİK for TR, etc)
   - In cci_source, state which source you used (e.g. "OECD CCI", "EU ESI", "Ipsos", "INDEC")
   - If genuinely no value is findable, set cci_now and cci_year_ago to null.
     Don't invent. Stick to authoritative indices only.
4. Most important political news of the past 7 days.

═══════════════════════════════════════════════════════════════════
SPREADING REQUIREMENT
═══════════════════════════════════════════════════════════════════
Use the full circle. Differentiate within phases. Don't cluster everyone
at 75-85°. Match the angle to the leader's actual position.

═══════════════════════════════════════════════════════════════════
OUTPUT — CRITICAL
═══════════════════════════════════════════════════════════════════
End your response with EXACTLY ONE LINE of valid JSON, no markdown fences.
Verify: phase matches angle range. Verify: polling fields are filled
appropriately for the system type.

Schema (parliamentary):
{"angle": 105, "doom_score": 60, "phase": 2, "coalition_pct_now": 23.0, "coalition_pct_last_election": 38.5, "far_right_pct_now": 30.5, "far_right_pct_last_election": 25.0, "last_election_year": 2024, "approval_now": null, "approval_at_inauguration": null, "cci_now": 96.5, "cci_year_ago": 99.2, "cci_source": "OECD CCI", "analysis": "60-80 words referencing recent news.", "sources": ["https://url1", "https://url2"]}

Schema (presidential):
{"angle": 215, "doom_score": 78, "phase": 3, "coalition_pct_now": null, "coalition_pct_last_election": null, "far_right_pct_now": null, "far_right_pct_last_election": null, "last_election_year": null, "approval_now": 37.0, "approval_at_inauguration": 49.0, "cci_now": 96.5, "cci_year_ago": 102.1, "cci_source": "OECD CCI", "analysis": "60-80 words referencing recent news.", "sources": ["https://url1", "https://url2"]}

Rules:
  - phase ∈ {1,2,3,4}; angle in correct range
  - For parliamentary: fill coalition_pct_* and far_right_pct_*, plus last_election_year. approval fields = null.
  - For presidential: fill approval fields. coalition/far_right fields = null.
  - cci_now and cci_year_ago: fill if you can find them via one of the
    priority sources above. null if nothing authoritative.
  - cci_source: short name matching the source priority list ("OECD CCI",
    "EU ESI", "Ipsos", "Conference Board", "INDEC", "TÜİK", "CBS", etc).
    null if no value.
  - analysis: 60-80 words exact, reference a specific recent event
  - sources: 2-4 most important URLs"""


def build_user_prompt(leader_data: dict) -> str:
    t = leader_data["tenure"]
    m = leader_data["macro"]
    coalition = leader_data.get("coalition_parties") or []
    far_right = leader_data.get("far_right_party")

    parts = [
        f"Country: {leader_data['country']}",
        f"Role: {leader_data['role']}",
        f"Leader: {leader_data['name']} ({leader_data['leader_party']})",
        f"System: {leader_data['system']}",
        f"Time in office: {t['years_in_office']} years (since {t['took_office']})",
    ]

    if leader_data["system"] == "parliamentary":
        parts.append(f"Coalition parties: {', '.join(coalition) if coalition else '(single-party government)'}")
        parts.append(f"Far-right party to track: {far_right or '(no distinct far-right opposition)'}")
    else:
        parts.append("(Presidential system — look for approval rating)")

    parts.append("")
    parts.append("Macro context (latest year vs previous year, World Bank):")
    infl_now = m.get("inflation_yoy_pct")
    infl_prev = m.get("inflation_prev_year_pct")
    if infl_now is not None and infl_prev is not None:
        infl_delta = infl_now - infl_prev
        parts.append(f"  Inflation YoY: {infl_now}% (vs {infl_prev}% a year ago, delta {infl_delta:+.1f}pt)")
    else:
        parts.append(f"  Inflation YoY: {infl_now}%")

    gdp_now = m.get("gdp_growth_yoy_pct")
    gdp_prev = m.get("gdp_growth_prev_year_pct")
    if gdp_now is not None and gdp_prev is not None:
        parts.append(f"  GDP growth: {gdp_now}% (vs {gdp_prev}% a year ago)")
    else:
        parts.append(f"  GDP growth: {gdp_now}%")

    parts.append(f"  Unemployment: {m.get('unemployment_pct')}%")

    parts.append("")
    parts.append(f"Today is {datetime.now().strftime('%d %B %Y')}.")
    parts.append(
        "Research and classify. Required JSON fields: angle, doom_score, phase, "
        "polling fields (coalition+far-right OR approval depending on system), "
        "last_election_year (parliamentary only), cci_now + cci_year_ago + cci_source, "
        "analysis (60-80 words), sources (2-4 URLs)."
    )

    return "\n".join(parts)


def extract_json_from_text(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    matches = re.findall(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", text, re.DOTALL)
    for candidate in reversed(matches):
        try:
            parsed = json.loads(candidate)
            if "angle" in parsed and "phase" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def classify_leader(client: Anthropic, leader_data: dict, attempt: int = 1) -> dict:
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
            }],
            messages=[{
                "role": "user",
                "content": build_user_prompt(leader_data),
            }],
        )

        text_blocks = [b.text for b in response.content if hasattr(b, "text") and b.type == "text"]
        if not text_blocks:
            return {"error": "no text in response"}

        full_text = "\n".join(text_blocks)
        parsed = extract_json_from_text(full_text)

        if parsed is None:
            return {"error": "no JSON found", "raw_tail": full_text[-500:]}
        return parsed

    except RateLimitError:
        if attempt <= MAX_RETRIES:
            print(f"\n      ⏳ rate limited, waiting {RATE_LIMIT_BACKOFF}s (attempt {attempt}/{MAX_RETRIES})...", end="", flush=True)
            time.sleep(RATE_LIMIT_BACKOFF)
            return classify_leader(client, leader_data, attempt + 1)
        return {"error": f"rate limit after {MAX_RETRIES} retries"}

    except APIError as e:
        if attempt <= MAX_RETRIES:
            print(f"\n      ⏳ API error, retrying in 10s (attempt {attempt}/{MAX_RETRIES})...", end="", flush=True)
            time.sleep(10)
            return classify_leader(client, leader_data, attempt + 1)
        return {"error": f"API error: {e}"}

    except Exception as e:
        return {"error": str(e)}


def get_iso_week_label() -> str:
    today = datetime.now()
    iso_year, iso_week, _ = today.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    client = Anthropic(api_key=api_key)

    print(f"Reading inputs from {INPUTS_FILE}")
    if not INPUTS_FILE.exists():
        print(f"ERROR: {INPUTS_FILE} does not exist. Run collect_data.py first.", file=sys.stderr)
        sys.exit(1)

    with INPUTS_FILE.open() as f:
        inputs = json.load(f)

    leaders = inputs["leaders"]
    print(f"Classifying {len(leaders)} leaders (model: {MODEL})")
    print(f"Pacing: {DELAY_BETWEEN_CALLS}s between calls\n")

    output = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "week_label": get_iso_week_label(),
        "model": MODEL,
        "leaders": {},
        "errors": [],
        "all_sources": [],
    }

    all_sources_set = set()
    successful = 0
    failed = 0

    for i, (leader_id, leader_data) in enumerate(leaders.items(), 1):
        print(f"[{i}/{len(leaders)}] {leader_data['name']} ({leader_data['country']})... ", end="", flush=True)

        result = classify_leader(client, leader_data)

        if "error" in result:
            err = result["error"]
            print(f"✗ error: {err[:80]}")
            output["errors"].append({"id": leader_id, "error": err})
            failed += 1
        else:
            output["leaders"][leader_id] = {
                **leader_data,
                "classification": result,
            }
            angle = result.get("angle")
            doom = result.get("doom_score")
            phase = result.get("phase")
            print(f"✓ phase {phase}, angle {angle}°, doom {doom}")

            for src in result.get("sources", []):
                if src and isinstance(src, str):
                    all_sources_set.add(src)

            successful += 1

        LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LATEST_FILE.open("w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        if i < len(leaders):
            time.sleep(DELAY_BETWEEN_CALLS)

    output["all_sources"] = sorted(all_sources_set)

    with LATEST_FILE.open("w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    weekly_file = WEEKLY_DIR / f"{output['week_label']}.json"
    with weekly_file.open("w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print("CLASSIFICATION SUMMARY")
    print("=" * 60)
    print(f"Successful:  {successful}/{len(leaders)}")
    print(f"Failed:      {failed}")
    print(f"Sources:     {len(all_sources_set)} unique URLs")
    print(f"\nLatest:  {LATEST_FILE}")
    print(f"Archive: {weekly_file}")


if __name__ == "__main__":
    main()

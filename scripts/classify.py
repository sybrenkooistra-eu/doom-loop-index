"""
classify.py — v2
=================
Wekelijks classificatie-script voor de Doom Loop Political Index.

Verbeteringen t.o.v. v1:
  - Sub-fase ankers met concrete voorbeelden (geen clustering meer op 75-88°)
  - Expliciete behandeling van presidentiële systemen (Trump = Phase III)
  - Duidelijker onderscheid tussen angle en doom score
  - Hardere JSON-format eisen (minder "no JSON found" errors)
  - Pacing 25s (i.p.v. 15s) voor minder rate limits
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic, RateLimitError, APIError


# ───────────────────────────────────────────────────────────────
# Paden & config
# ───────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
INPUTS_FILE = REPO_ROOT / "data" / "inputs.json"
LATEST_FILE = REPO_ROOT / "data" / "latest.json"
WEEKLY_DIR = REPO_ROOT / "data" / "weekly"

MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 1500

DELAY_BETWEEN_CALLS = 25
RATE_LIMIT_BACKOFF = 60
MAX_RETRIES = 3


# ───────────────────────────────────────────────────────────────
# Prompt — het hart van het systeem
# ───────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You assess where a head of government stands on the "doom loop" — a satirical-but-analytical framework for Western political cycles.

═══════════════════════════════════════════════════════════════════
THE FOUR PHASES (angle on a 360° circle)
═══════════════════════════════════════════════════════════════════

PHASE I — CENTRIST DENIAL (0°–90°)
  A centrist or mainstream party is in office. They fail to address the
  underlying social/economic problems (housing, inequality, immigration
  anxiety, stagnant wages) that fuel far-right support. The far-right
  is either rising in polls or contained but ever-present.
  
  Sub-positions:
   • 0–15°  : Brand new centrist, just elected, riding high, far-right
              contained. Honeymoon. (early Albanese 2022, Martin 2025)
   • 15–35° : Established and governing. Approval middling but stable.
              Far-right exists but not yet a credible threat.
   • 35–55° : Mid-term. Approval slipping, far-right gaining ground but
              government holds. Some centrists in this band are actively
              trying to address root causes (Sánchez resisting Vox by
              keeping housing/labour policy left); others are coasting.
   • 55–75° : Late-stage. Badly damaged centrist. Far-right surging in
              polls. Coalition under stress. Electoral defeat probable.
   • 75–90° : Exhausted, lame-duck. Centrist has visibly failed.
              Far-right at or above incumbent in polls. (Macron 2026)

PHASE II — FAR-RIGHT RISES (90°–180°)
  The far-right is now consequential in government — either entered as a
  coalition partner, or supports the government externally, or has just
  won an election and is about to take office.
  
  Sub-positions:
   • 90–115°  : Far-right enters coalition as junior partner with limited
                portfolios (Tidö Sweden, NCP-Finns Finland)
   • 115–145° : Far-right has growing influence; centrist cordon weakening
                or already gone. (Schoof NL pre-collapse; Babiš CZ in
                coalition with SPD)
   • 145–175° : Far-right consolidating power, near or holding the
                premiership through coalition. (Stocker AT: cordon by
                exclusion but FPÖ leads polls)
   • 175–180° : Far-right takes the premiership.

PHASE III — DESTRUCTION (180°–270°)
  Far-right or far-right-led government in power. Institutional erosion,
  judicial capture, media pressure, economic damage, deteriorating
  public life. The framework treats this as the bad part of the cycle.
  
  Sub-positions:
   • 180–200° : Just took power, dismantling beginning. (Meloni 2022
                early phase — though Meloni has stayed technocratic)
   • 200–230° : Active dismantling phase. Tariffs, deportations, agency
                hollowing, judicial reform. (Trump 2025-26, Fico SK,
                Babis if he goes hard)
   • 230–260° : Peak destruction. Long tenure, captured institutions,
                deep damage. (Erdoğan TR — 12 years president)
   • 260–270° : Late-stage, electoral defeat or replacement imminent.
                (Late Orbán pre-2026 defeat)

PHASE IV — PROMISE OF CHANGE (270°–360°)
  A new (often centrist, sometimes left, sometimes technocratic) leader
  defeats the far-right with a promise of change. The framework's cynical
  prediction: this promise is usually shallow; they will not address root
  causes; eventually back to Phase I.
  
  Sub-positions:
   • 270–290° : Just took power. Honeymoon. High hopes. (Magyar HU 2026,
                Carney CA 2025, Sheinbaum MX in continuation-of-change role)
   • 290–320° : First year in office. Reality biting. Mixed reviews.
                (Tusk PL 2024-25)
   • 320–355° : Late Phase IV. Becoming the next "uninspiring centrist".
                (Tusk by now)
   • 355–360° : Transition zone — about to become Phase I.

═══════════════════════════════════════════════════════════════════
PRESIDENTIAL SYSTEMS NOTE
═══════════════════════════════════════════════════════════════════
For Presidents (Trump, Macron, Milei, Erdoğan, Lee, Sheinbaum):
  - Trump = far-right in office = Phase III, regardless of how he got there
  - Macron = centrist in office failing on root causes = Phase I (likely late)
  - Milei = libertarian/right-populist in office damaging public services = Phase III
  - Erdoğan = long-running authoritarian-right = Phase III (long-tenure end)
  - Lee = anti-far-right Democrat after Yoon impeachment = Phase IV
  - Sheinbaum = continuation of AMLO's left project, not classic "centrist defeats
    far-right" but functionally Phase IV (popular legitimacy of change government)

═══════════════════════════════════════════════════════════════════
THE DOOM SCORE (0–100, separate from angle)
═══════════════════════════════════════════════════════════════════
Tracks the *intensity* of doom-loop dynamics at this point in the cycle.
NOT the same as angle. A Phase IV leader in honeymoon (Carney) has low
doom even though the loop is "ticking forward". A Phase I leader where
the far-right is at 35% (Reform UK / Starmer) has high doom because the
next phase is visible on the horizon.

  0–20   : Stable. No imminent far-right threat. Healthy approval. Either
           early honeymoon, or genuine resilience. (Carney early, Albanese
           honeymoon, Magyar honeymoon, Lee at high approval)
  20–40  : Mild tension. Government stable but not thriving. Far-right
           exists but constrained. (Martin IE, Frederiksen DK pre-2026)
  40–60  : Significant pressure. Far-right gaining or already in coalition;
           government polling poorly. Real risk of cycle progression.
           (Sánchez ES, Kristersson SE, Luxon NZ)
  60–80  : Serious doom. Late-stage centrist with far-right leading polls,
           OR far-right active in government damaging institutions.
           (Starmer UK if Reform leads, Macron FR, Meloni IT, Babiš CZ,
           Fico SK)
  80–100 : Peak doom. Active destruction phase, long-running authoritarian,
           or imminent collapse. (Trump US, Erdoğan TR, Netanyahu IL
           in current war context)

═══════════════════════════════════════════════════════════════════
SPREADING REQUIREMENT
═══════════════════════════════════════════════════════════════════
The framework's value depends on real differentiation. DO NOT default
to "60-80°, doom 60-75" for every centrist — read the actual data and
distinguish. Albanese after winning an election is very different from
Macron after 9 years. Sánchez actively fighting Vox is different from
Schoof presiding over collapse.

═══════════════════════════════════════════════════════════════════
RESEARCH INSTRUCTIONS
═══════════════════════════════════════════════════════════════════
Do web searches to find:
  1. Current polling average for the named coalition parties (last ~30 days)
  2. Same polling ~3 months ago
  3. For named far-right party: current polling and 3-months-ago polling
  4. For presidents: current approval rating and 3-months-ago approval
  5. The most important political news of the past 7 days

═══════════════════════════════════════════════════════════════════
OUTPUT FORMAT — CRITICAL
═══════════════════════════════════════════════════════════════════
After your reasoning, end your response with EXACTLY ONE LINE of JSON.
No markdown fences. No text after the JSON. The JSON must be valid and
parseable. Verify your angle matches your phase (phase 2 means angle
between 90 and 180).

Schema:
{"angle": 145, "doom_score": 72, "phase": 2, "coalition_pct_now": 38.2, "coalition_pct_3mo_ago": 41.5, "far_right_pct_now": 31.8, "far_right_pct_3mo_ago": 28.4, "approval_now": null, "approval_3mo_ago": null, "analysis": "60-80 words explaining the position with reference to recent news.", "sources": ["https://url1", "https://url2"]}

Rules:
  - phase ∈ {1, 2, 3, 4}; angle must be in that phase's range
  - For parliamentary systems: fill coalition_pct_* and far_right_pct_*; approval_* = null
  - For presidential systems: fill approval_*; coalition_pct_* and far_right_pct_* = null
  - If a number cannot be found, use null (do not invent)
  - analysis: 60-80 words exactly, refer to a specific recent event
  - sources: 2-4 URLs of the most cited search results"""


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
        parts.append(f"Far-right party to track: {far_right or '(no distinct far-right opposition; ruling party may itself be far-right)'}")
    else:
        parts.append("(Presidential system — look for approval rating, not coalition polling)")

    parts.append("")
    parts.append("Macro context (most recent year):")
    parts.append(f"  GDP growth: {m.get('gdp_growth_yoy_pct')}%")
    parts.append(f"  Inflation: {m.get('inflation_yoy_pct')}%")
    parts.append(f"  Unemployment: {m.get('unemployment_pct')}%")
    parts.append("")
    parts.append(f"Today is {datetime.now().strftime('%d %B %Y')}.")
    parts.append("Do web searches for polling, trends, and the past 7 days of news, then classify with angle, doom_score, phase, and analysis. End your response with ONE LINE of JSON in the exact schema above.")

    return "\n".join(parts)


# ───────────────────────────────────────────────────────────────
# JSON extractie
# ───────────────────────────────────────────────────────────────
def extract_json_from_text(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Match nested JSON objects up to 2 levels deep
    matches = re.findall(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", text, re.DOTALL)
    for candidate in reversed(matches):
        try:
            parsed = json.loads(candidate)
            # Only accept if it has the expected schema
            if "angle" in parsed and "phase" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue
    return None


# ───────────────────────────────────────────────────────────────
# Classificatie per leider, met retry
# ───────────────────────────────────────────────────────────────
def classify_leader(client: Anthropic, leader_data: dict, attempt: int = 1) -> dict:
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 4,
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


# ───────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────
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

        # Tussentijds opslaan
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

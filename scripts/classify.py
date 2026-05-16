"""
classify.py — v3
=================
Wekelijks classificatie-script voor de Doom Loop Political Index.

Verbeteringen t.o.v. v2:
  - Phase II definitie verruimd: far-right hoeft niet in de regering te
    zitten; leiden in peilingen is ook Phase II.
  - Daarmee verschuiven leiders als Starmer/Macron/Frederiksen/Merz/
    Stocker van late Phase I naar Phase II.
  - Doom score wordt door de frontend genormaliseerd per phase; in de
    prompt blijft het 0-100 binnen de eigen phase.
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
MAX_TOKENS = 1500

DELAY_BETWEEN_CALLS = 25
RATE_LIMIT_BACKOFF = 60
MAX_RETRIES = 3


SYSTEM_PROMPT = """You assess where a head of government stands on the "doom loop" — a satirical-but-analytical framework for Western political cycles.

═══════════════════════════════════════════════════════════════════
THE FOUR PHASES (angle on a 360° circle)
═══════════════════════════════════════════════════════════════════

PHASE I — CENTRIST DENIAL (0°–90°)
  A centrist/mainstream party is in office, has a clear mandate, and the
  far-right is NOT yet a top-2 polling force. The government is failing
  to address root causes but is still electorally dominant.

  Use Phase I ONLY when ALL of the following hold:
    • The far-right is below ~15% in polls, OR is not the leading
      opposition force
    • The government is not facing imminent electoral collapse
    • The leader has a clear mandate (recent election, stable approval)

  Sub-positions:
   • 0–25°  : New centrist, just elected, riding high. Far-right contained.
              (early Albanese 2022, Martin 2025)
   • 25–55° : Established. Approval middling. Far-right exists but ≤15%
              and not the top opposition. Government clearly in control.
              (Albanese 2025, Martin)
   • 55–85° : Mid-term strain. Approval slipping, but far-right STILL
              below opposition leaders and below 15%. Honest "centrist
              still trying" cases. (Sánchez actively resisting Vox at ~14%)

PHASE II — FAR-RIGHT RISES (90°–180°)
  The far-right is now consequential — EITHER (a) actively governing in
  coalition / supporting government externally, OR (b) leading or
  co-leading the polls without yet holding power. This is the CORE
  doom-loop dynamic and most struggling Western centrists belong here.

  Use Phase II when ANY of these hold:
    • Far-right is in the governing coalition (Schoof era NL,
      Tidö Sweden, Orpo Finland, Babiš Czechia with SPD)
    • Far-right leads polls but isn't in government (Reform UK leads
      → Starmer is Phase II; RN leads → Macron is Phase II;
      AfD leads → Merz is Phase II; FPÖ leads → Stocker is Phase II)
    • Far-right is polling above 20% AND is the top opposition force

  Sub-positions:
   • 90–115°  : Far-right has overtaken the government in polls or just
                entered coalition as junior partner. (Starmer with Reform
                leading; Merz with AfD #1; Macron with RN ahead)
   • 115–145° : Far-right consolidating; centrist visibly losing the
                argument; government polling 15-20 points behind.
                (Late-cycle Macron; Stocker AT where FPÖ leads polls
                AND only an exclusion cordon keeps them out)
   • 145–175° : Far-right at or near governing majority. Cordon collapsing.
                (Schoof NL while Wilders coalition was forming;
                pre-power Meloni)
   • 175–180° : Far-right takes the premiership.

PHASE III — DESTRUCTION (180°–270°)
  Far-right or far-right-led government in power. Institutional erosion,
  judicial capture, media pressure, economic damage.

  Sub-positions:
   • 180–200° : Just took power. (Early Meloni — though she has stayed
                technocratic, which is a judgment call)
   • 200–230° : Active dismantling. (Trump 2025-26, Fico SK active phase)
   • 230–260° : Peak destruction or very long tenure.
                (Erdoğan TR, late Orbán pre-defeat)
   • 260–270° : Late-stage, defeat imminent or just happened.

PHASE IV — PROMISE OF CHANGE (270°–360°)
  A new (often centrist, sometimes left, sometimes technocratic) leader
  defeats the far-right or far-right-adjacent government with a promise
  of change.

  Sub-positions:
   • 270–290° : Just took power. Honeymoon. (Magyar HU 2026,
                Carney CA 2025, Lee KR 2025)
   • 290–320° : First year. Reality biting. (Sheinbaum MX one year in)
   • 320–355° : Late Phase IV — becoming the next "uninspiring centrist".
                (Tusk PL 2025-26)
   • 355–360° : Transition to Phase I.

═══════════════════════════════════════════════════════════════════
PRESIDENTIAL SYSTEMS
═══════════════════════════════════════════════════════════════════
  - Trump = far-right president in office = Phase III
  - Macron = centrist with RN leading polls for 2027 = Phase II
    (not Phase I — the far-right consequence is already present)
  - Milei = libertarian-right in office damaging public services = Phase III
  - Erdoğan = long-running authoritarian-right = Phase III, late
  - Lee = anti-far-right Democrat after Yoon impeachment = Phase IV
  - Sheinbaum = continuation of AMLO's left project = Phase IV (broad sense)

═══════════════════════════════════════════════════════════════════
THE DOOM SCORE (0–100, within the leader's phase)
═══════════════════════════════════════════════════════════════════
NOTE: The frontend re-scales this number per phase, so don't worry that
a Phase I leader with doom 90 might look "worse" than a Phase III leader
with doom 50. Just rate doom 0-100 within the context of THIS phase:

  0–20   : Stable for this phase. Either fresh honeymoon (P1/P4) or
           a contained government (P2/P3 lite).
  20–40  : Mild tension. The phase dynamics are present but not severe.
  40–60  : Standard mid-cycle for this phase. Real pressure but not
           extreme.
  60–80  : Severe. Clear deterioration within the phase.
  80–100 : Peak intensity. Either election-collapse imminent (P1/P2),
           or active institutional destruction (P3), or honeymoon
           rapidly failing (P4).

═══════════════════════════════════════════════════════════════════
SPREADING REQUIREMENT
═══════════════════════════════════════════════════════════════════
Use the full circle. Differentiate leaders within phases. Albanese with
no far-right threat is very different from Sánchez actively fighting
Vox — both Phase I but at very different angles.

═══════════════════════════════════════════════════════════════════
RESEARCH
═══════════════════════════════════════════════════════════════════
Web searches: current polling (30-day avg), 3-months-ago polling,
past-7-days news.

═══════════════════════════════════════════════════════════════════
OUTPUT — CRITICAL
═══════════════════════════════════════════════════════════════════
End your response with EXACTLY ONE LINE of JSON. No markdown fences.
Verify phase matches angle range.

Schema:
{"angle": 105, "doom_score": 60, "phase": 2, "coalition_pct_now": 23.0, "coalition_pct_3mo_ago": 26.0, "far_right_pct_now": 30.5, "far_right_pct_3mo_ago": 27.8, "approval_now": null, "approval_3mo_ago": null, "analysis": "60-80 words referencing a specific recent news event.", "sources": ["https://url1", "https://url2"]}

Rules:
  - phase ∈ {1,2,3,4}; angle must be in that phase's range
  - parliamentary: coalition_pct_* and far_right_pct_* filled; approval_* = null
  - presidential: approval_* filled; coalition_pct_* and far_right_pct_* = null
  - null for unknown values (do not invent)
  - analysis: 60-80 words exact
  - sources: 2-4 URLs"""


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
        parts.append("(Presidential system — look for approval rating, not coalition polling)")

    parts.append("")
    parts.append("Macro context (most recent year):")
    parts.append(f"  GDP growth: {m.get('gdp_growth_yoy_pct')}%")
    parts.append(f"  Inflation: {m.get('inflation_yoy_pct')}%")
    parts.append(f"  Unemployment: {m.get('unemployment_pct')}%")
    parts.append("")
    parts.append(f"Today is {datetime.now().strftime('%d %B %Y')}.")
    parts.append("Search the web for polling, trends, and past-7-days news, then classify. CRITICAL: if the far-right party leads polls or polls above 20%, this is Phase II, not late Phase I. End with ONE LINE of JSON in the schema.")

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

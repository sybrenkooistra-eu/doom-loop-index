"""
detect_changes.py
==================
Leadership sanity check voor de Doom Loop Political Index.

Voor elke leider in data/leaders.json vraagt dit script Claude (via web
search) om te verifiëren of die persoon vandaag nog regeringsleider is.

Het script wijzigt zelf niks; het rapporteert alleen.

Output: stdout (rapport) + data/leadership_check.json (machine-leesbaar)

Usage:
    python scripts/detect_changes.py

Vereist environment variable ANTHROPIC_API_KEY.

Kosten: ~$0.30 per volledige run (30 leiders × web search).
Runtime: ~10 minuten (tragere pace om rate limits te ontwijken).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from anthropic import RateLimitError, APIError


# ───────────────────────────────────────────────────────────────
# Paden & config
# ───────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
LEADERS_FILE = REPO_ROOT / "data" / "leaders.json"
OUTPUT_FILE = REPO_ROOT / "data" / "leadership_check.json"

MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 400

# Rate limiting & retry
DELAY_BETWEEN_CALLS = 15      # seconden — bij 30k input tokens/min en ~5k per call
RATE_LIMIT_BACKOFF = 60       # seconden — wachten bij 429
MAX_RETRIES = 3


# ───────────────────────────────────────────────────────────────
# Prompt-template
# ───────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You verify the current head of government of a country.

For each query, do a web search to check whether the named person is still
in office as the country's current head of government (Prime Minister,
Chancellor, President, or equivalent) as of today.

After your web search, respond with EXACTLY ONE LINE of JSON at the very
end of your response. Do not wrap it in markdown fences. Do not include
any text after the JSON.

Use this exact schema:
{"status": "confirmed" | "outdated", "current_leader": "Name", "current_party": "Party", "took_office": "YYYY-MM-DD", "source_url": "https://..."}

- "confirmed" means the person in our records is still the current leader
- "outdated" means someone else now holds that office
- For "confirmed", repeat the same name and party we asked about
- For "outdated", give the name, party and start date of the actual current leader, plus one source URL"""


def build_user_prompt(leader: dict) -> str:
    return (
        f"Country: {leader['country']}\n"
        f"Role: {leader['role']}\n"
        f"Person in our records: {leader['name']} ({leader['leader_party']})\n"
        f"In office since (per our records): {leader['took_office']}\n\n"
        f"Is {leader['name']} still the current {leader['role']} of {leader['country']} today?"
    )


# ───────────────────────────────────────────────────────────────
# JSON extractie uit response tekst
# ───────────────────────────────────────────────────────────────
def extract_json_from_text(text: str) -> dict | None:
    """Probeer een JSON-object te vinden in de Claude-response tekst."""
    # Strip markdown fences als ze er staan
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # Probeer eerst direct te parsen
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Vind het laatste {...} blok in de tekst — Claude voegt soms commentaar toe
    matches = re.findall(r"\{[^{}]*\}", text, re.DOTALL)
    for candidate in reversed(matches):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


# ───────────────────────────────────────────────────────────────
# Per-leider check, met retry
# ───────────────────────────────────────────────────────────────
def check_leader(client: Anthropic, leader: dict, attempt: int = 1) -> dict:
    """Roep Claude aan voor één leider, met automatische retries bij rate limits."""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
            }],
            messages=[{
                "role": "user",
                "content": build_user_prompt(leader),
            }],
        )

        # Verzamel alle tekst-blokken
        text_blocks = [b.text for b in response.content if hasattr(b, "text") and b.type == "text"]
        if not text_blocks:
            return {"status": "error", "error": "no text in response"}

        full_text = "\n".join(text_blocks)
        parsed = extract_json_from_text(full_text)

        if parsed is None:
            return {"status": "error", "error": "no JSON found", "raw": full_text[:300]}
        return parsed

    except RateLimitError as e:
        if attempt <= MAX_RETRIES:
            print(f"\n      ⏳ rate limited, waiting {RATE_LIMIT_BACKOFF}s (attempt {attempt}/{MAX_RETRIES})...", end="", flush=True)
            time.sleep(RATE_LIMIT_BACKOFF)
            return check_leader(client, leader, attempt + 1)
        return {"status": "error", "error": f"rate limit after {MAX_RETRIES} retries"}

    except APIError as e:
        if attempt <= MAX_RETRIES:
            print(f"\n      ⏳ API error, retrying in 10s (attempt {attempt}/{MAX_RETRIES})...", end="", flush=True)
            time.sleep(10)
            return check_leader(client, leader, attempt + 1)
        return {"status": "error", "error": f"API error: {e}"}

    except Exception as e:
        return {"status": "error", "error": str(e)}


# ───────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────
def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    client = Anthropic(api_key=api_key)

    print(f"Reading leaders from {LEADERS_FILE}")
    with LEADERS_FILE.open() as f:
        leaders_data = json.load(f)

    leaders = leaders_data["leaders"]
    print(f"Checking {len(leaders)} leaders against current news...")
    print(f"(pacing: {DELAY_BETWEEN_CALLS}s between calls to respect rate limits)\n")

    results = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "results": {},
    }

    confirmed = []
    outdated = []
    errors = []

    for i, leader in enumerate(leaders, 1):
        print(f"[{i}/{len(leaders)}] {leader['flag']} {leader['name']} ({leader['country']})... ", end="", flush=True)
        result = check_leader(client, leader)
        results["results"][leader["id"]] = result

        status = result.get("status", "error")
        if status == "confirmed":
            print("✓ confirmed")
            confirmed.append(leader)
        elif status == "outdated":
            current = result.get("current_leader", "?")
            party = result.get("current_party", "?")
            took = result.get("took_office", "?")
            print(f"⚠ OUTDATED → now {current} ({party}), since {took}")
            outdated.append((leader, result))
        else:
            err = result.get("error", "unknown")
            print(f"✗ error: {err[:100]}")
            errors.append((leader, result))

        # Tussentijds opslaan zodat we niet alles kwijt zijn bij een crash
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT_FILE.open("w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        # Pace requests om rate limits te vermijden
        if i < len(leaders):
            time.sleep(DELAY_BETWEEN_CALLS)

    # ─── Rapport ───
    print("\n" + "=" * 60)
    print("LEADERSHIP CHECK SUMMARY")
    print("=" * 60)
    print(f"Confirmed:  {len(confirmed)}/{len(leaders)}")
    print(f"Outdated:   {len(outdated)}")
    print(f"Errors:     {len(errors)}")

    if outdated:
        print("\n⚠️  UPDATES NEEDED IN leaders.json:\n")
        for leader, result in outdated:
            print(f"  {leader['flag']} {leader['country']}")
            print(f"     Records say:  {leader['name']} ({leader['leader_party']}) since {leader['took_office']}")
            print(f"     Actually:     {result.get('current_leader')} ({result.get('current_party')}) since {result.get('took_office')}")
            print(f"     Source:       {result.get('source_url', '(none)')}")
            print()

    if errors:
        print("\n❌ ERRORS:\n")
        for leader, result in errors:
            print(f"  {leader['flag']} {leader['country']}: {result.get('error', '')[:200]}")
        print()

    print(f"✓ Detailed results in {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

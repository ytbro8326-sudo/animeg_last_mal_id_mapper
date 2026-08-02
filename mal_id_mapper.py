"""
MAL ID Mapper for AnimeGG Data (GitHub Actions Edition)
=========================================================
Fetches anime entries from the AnimeGG JSON dataset and maps each one
to a MyAnimeList ID by searching MAL's own search page directly:

    https://myanimelist.net/anime.php?q=<title>&cat=anime

No third-party API (Jikan, etc.) is used — this scrapes MAL's HTML
search results page and parses out the anime titles + MAL IDs.

Matching logic (per entry):
  1. Search MAL using the main `title`.
  2. If no confident hit, try each `alternate_title` entry one by one
     (the field is comma-separated).
  3. Accept a match when a MAL search result's title exactly matches
     (case-insensitive) the title being searched.
  4. If no exact match is found anywhere, fall back to the FIRST result
     of the main-title search (MAL's own relevance ranking) — this can
     be disabled by setting FALLBACK_TO_TOP_RESULT = False (env var
     FALLBACK_TO_TOP_RESULT=false).

Sub/Dub categorization
----------------------
Every entry is additionally tagged with a `sub_dub_status` field, one of:
    "subbed"  - only "sub" is mentioned anywhere on the entry
    "dubbed"  - only "dub" is mentioned anywhere on the entry
    "both"    - both "sub" and "dub" are mentioned
    "neither" - neither word appears anywhere on the entry

This is derived by scanning every plausible text field on the entry
(title, alternate_title, and any sub/dub/type/language/tag-like field
that happens to exist in the source dataset), since AnimeGG-style
datasets typically signal sub/dub through the title itself
(e.g. "Naruto (Dub)") rather than one single fixed field.

Output files (always written to OUTPUT_DIR, default = repo root):
    all.json                            - every processed entry
    all_subbed.json                     - entries tagged "subbed" or "both"
    all_dubbed.json                     - entries tagged "dubbed" or "both"
    neither_sub_or_dub_mentioned.json   - entries tagged "neither"

────────────────────────────────────────────────────────────────────
CONFIGURATION

This script is driven entirely by environment variables so it can be
run unattended from a GitHub Actions workflow. If you run it locally
with no environment variables set, the defaults below are used.

    MODE               "range" or "all"            (default: "range")
    START              start serial_no (range mode) (default: 1282)
    END                end serial_no   (range mode) (default: 1282)
    INPUT_SOURCE        URL or local path to source dataset
    OUTPUT_DIR          where to write the 4 output json files
    REQUEST_DELAY        seconds between MAL requests (default: 2.0)
    FALLBACK_TO_TOP_RESULT  "true"/"false" (default: true)
    VERBOSE              "true"/"false" (default: true)
    COMMIT_EVERY          commit+push after this many NEW entries (100)
    GIT_PUSH              "true"/"false" - actually run git commit/push
    GIT_USER_NAME / GIT_USER_EMAIL   identity used for the commits

Run it locally with:
    python mal_id_mapper.py
Run it in CI via the accompanying .github/workflows/mal_mapper.yml
────────────────────────────────────────────────────────────────────
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Optional

try:
    import requests as _requests  # nicer error handling / session reuse
except ImportError:
    _requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    raise SystemExit(
        "This script requires BeautifulSoup4.\n"
        "Install it with:  pip install beautifulsoup4 requests"
    )


# ═══════════════════════════════ CONFIG ════════════════════════════════

def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    try:
        return int(val) if val not in (None, "") else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    try:
        return float(val) if val not in (None, "") else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# MODE: "range" -> only process serial_no in [START, END] (inclusive)
#       "all"   -> process every entry in the dataset
MODE = os.environ.get("MODE", "range").strip().lower()

START = _env_int("START", 1282)
END = _env_int("END", 1282)

INPUT_SOURCE = os.environ.get(
    "INPUT_SOURCE",
    "https://raw.githubusercontent.com/ytbro8326-sudo/"
    "animeg_main_web_urls_list_extractor/refs/heads/main/"
    "animegg_with_alternate_titles.json",
)

# Directory where output json files live. Defaults to the repo root
# (".") so the GitHub Action can add/commit/push them directly from
# the checkout.
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", ".")

ALL_FILE = os.path.join(OUTPUT_DIR, "all.json")
SUBBED_FILE = os.path.join(OUTPUT_DIR, "all_subbed.json")
DUBBED_FILE = os.path.join(OUTPUT_DIR, "all_dubbed.json")
NEITHER_FILE = os.path.join(OUTPUT_DIR, "neither_sub_or_dub_mentioned.json")

# Seconds to wait between requests to MyAnimeList (be a good citizen —
# MAL will start throwing 429s / captchas if you hammer it).
REQUEST_DELAY = _env_float("REQUEST_DELAY", 2.0)

# If no exact title match is found among any titles tried, fall back
# to accepting MAL's #1 search result for the main title.
FALLBACK_TO_TOP_RESULT = _env_bool("FALLBACK_TO_TOP_RESULT", True)

# Print each individual search query as it happens.
VERBOSE = _env_bool("VERBOSE", True)

# Commit + push to the repo after this many NEWLY processed entries.
COMMIT_EVERY = _env_int("COMMIT_EVERY", 100)

# GitHub Actions sets GITHUB_ACTIONS=true automatically on its runners.
IS_GITHUB_ACTIONS = _env_bool("GITHUB_ACTIONS", False)
GIT_PUSH = _env_bool("GIT_PUSH", IS_GITHUB_ACTIONS)
GIT_USER_NAME = os.environ.get("GIT_USER_NAME", "github-actions[bot]")
GIT_USER_EMAIL = os.environ.get(
    "GIT_USER_EMAIL", "github-actions[bot]@users.noreply.github.com"
)

# ═════════════════════════════ END CONFIG ══════════════════════════════


MAL_SEARCH_URL = "https://myanimelist.net/anime.php"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ─────────────────────────── HTTP helpers ─────────────────────────────

def _http_get(url: str) -> str:
    """Fetch a URL and return its HTML/text body."""
    if _requests is not None:
        resp = _requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.text

    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _load_json_source(source: str):
    if source.startswith(("http://", "https://")):
        text = _http_get(source)
        return json.loads(text)
    with open(source, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_json_file_if_exists(path: str) -> list:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return []
    return []


# ─────────────────────────── Text helpers ─────────────────────────────

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def clean_query(title: str) -> str:
    """Strip characters MAL's search box would ignore/choke on."""
    return re.sub(r"[^\w\s\-:!?'.()/]", " ", title or "").strip()


def split_alternate_titles(raw: str) -> list:
    if not raw or normalize(raw) in ("n/a", ""):
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p and len(p) > 1]


# ───────────────────────── Sub/Dub categorization ───────────────────────

_DUB_RE = re.compile(r"\bdub(?:bed)?\b", re.IGNORECASE)
_SUB_RE = re.compile(r"\bsub(?:bed)?\b", re.IGNORECASE)


def categorize_sub_dub(entry: dict) -> str:
    """
    Decide whether an entry is subbed, dubbed, both, or neither is
    mentioned anywhere. AnimeGG-style datasets usually signal this
    through the title itself (e.g. "Naruto (Dub)") and/or a dedicated
    field, so every plausible text field on the entry is scanned
    rather than assuming one fixed schema.

    Returns one of: "subbed", "dubbed", "both", "neither".
    """
    candidate_fields = [
        "title",
        "alternate_title",
        "sub_dub",
        "type",
        "language",
        "audio",
        "status",
        "tags",
        "category",
        "notes",
        "description",
    ]
    haystack_parts = []
    for field in candidate_fields:
        val = entry.get(field)
        if val is None:
            continue
        if isinstance(val, (list, tuple)):
            haystack_parts.extend(str(v) for v in val)
        else:
            haystack_parts.append(str(val))

    haystack = " ".join(haystack_parts)

    has_dub = bool(_DUB_RE.search(haystack))
    has_sub = bool(_SUB_RE.search(haystack))

    if has_dub and has_sub:
        return "both"
    if has_dub:
        return "dubbed"
    if has_sub:
        return "subbed"
    return "neither"


# ─────────────────────────── MAL search + parse ────────────────────────

def search_mal(query: str) -> list:
    """
    Search MAL's anime.php page for `query` and return a list of
    {"mal_id": int, "title": str, "url": str} dicts in the order MAL
    ranks them (its own relevance ordering).
    """
    encoded = urllib.parse.quote(clean_query(query))
    url = f"{MAL_SEARCH_URL}?q={encoded}&cat=anime"

    try:
        html = _http_get(url)
    except Exception as exc:
        print(f"      ⚠️  Request failed for '{query}': {exc}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    results: list = []
    seen_ids: set = set()

    # MAL search result titles are anchor tags linking to
    # https://myanimelist.net/anime/<id>/<slug> with class "hoverinfo_trigger"
    for a in soup.select("a.hoverinfo_trigger"):
        href = a.get("href", "")
        match = re.search(r"/anime/(\d+)/", href)
        if not match:
            continue
        mal_id = int(match.group(1))
        if mal_id in seen_ids:
            continue

        title_tag = a.find("strong")
        title = title_tag.get_text(strip=True) if title_tag else a.get_text(strip=True)
        if not title:
            continue

        seen_ids.add(mal_id)
        results.append({"mal_id": mal_id, "title": title, "url": href})

    return results


def best_match(query: str, results: list) -> Optional[dict]:
    """Return the exact (case-insensitive) title match, or None."""
    q_norm = normalize(query)
    for r in results:
        if normalize(r["title"]) == q_norm:
            return r
    return None


# ─────────────────────────── Core lookup ───────────────────────────────

def find_mal_entry(main_title: str, alternate_raw: str):
    """
    Try main_title, then each alternate title, searching MAL for each
    until an exact match is found.

    Returns: (mal_id, mal_title, title_used_for_match)
    """
    all_titles = [main_title] + split_alternate_titles(alternate_raw)

    seen: set = set()
    unique_titles: list = []
    for t in all_titles:
        k = normalize(t)
        if k and k not in seen:
            seen.add(k)
            unique_titles.append(t)

    first_search_results: list = []

    for i, title in enumerate(unique_titles):
        if VERBOSE:
            print(f"      🔍 Searching MAL: '{title}'")

        results = search_mal(title)
        time.sleep(REQUEST_DELAY)

        if i == 0:
            first_search_results = results

        match = best_match(title, results)
        if match:
            return match["mal_id"], match["title"], title

    # No exact match anywhere — optionally fall back to the top result
    # of the very first (main title) search.
    if FALLBACK_TO_TOP_RESULT and first_search_results:
        top = first_search_results[0]
        return top["mal_id"], top["title"], f"{main_title} (fallback: top result)"

    return None, None, None


# ─────────────────────────── Git helpers ───────────────────────────────

def _run_git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def git_setup_identity() -> None:
    _run_git("config", "user.name", GIT_USER_NAME)
    _run_git("config", "user.email", GIT_USER_EMAIL)


def git_commit_and_push(message: str) -> None:
    """
    Stage the four output files, commit, and push. Safe no-op if there
    is nothing new to commit, or if GIT_PUSH is disabled. Retries once
    on push failure after a rebase-pull, in case another process moved
    the branch forward in the meantime.
    """
    if not GIT_PUSH:
        return

    _run_git("add", ALL_FILE, SUBBED_FILE, DUBBED_FILE, NEITHER_FILE)

    status = _run_git("status", "--porcelain")
    if not status.stdout.strip():
        print("      ℹ️  Nothing new to commit.")
        return

    commit = _run_git("commit", "-m", message)
    if commit.returncode != 0:
        print(f"      ⚠️  git commit failed:\n{commit.stderr}")
        return

    for attempt in range(2):
        pull = _run_git("pull", "--rebase")
        if pull.returncode != 0:
            print(f"      ⚠️  git pull --rebase failed:\n{pull.stderr}")

        push = _run_git("push")
        if push.returncode == 0:
            print(f"      ✅  Pushed: {message}")
            return

        print(f"      ⚠️  git push failed (attempt {attempt + 1}):\n{push.stderr}")
        time.sleep(3)

    print("      ❌  Giving up on push for this batch; the data is still")
    print("          saved on disk and will be committed at the next checkpoint.")


# ─────────────────────────── Output persistence ────────────────────────

def save_outputs(all_rows: list) -> None:
    subbed = [r for r in all_rows if r.get("sub_dub_status") in ("subbed", "both")]
    dubbed = [r for r in all_rows if r.get("sub_dub_status") in ("dubbed", "both")]
    neither = [r for r in all_rows if r.get("sub_dub_status") == "neither"]

    with open(ALL_FILE, "w", encoding="utf-8") as fh:
        json.dump(all_rows, fh, ensure_ascii=False, indent=2)
    with open(SUBBED_FILE, "w", encoding="utf-8") as fh:
        json.dump(subbed, fh, ensure_ascii=False, indent=2)
    with open(DUBBED_FILE, "w", encoding="utf-8") as fh:
        json.dump(dubbed, fh, ensure_ascii=False, indent=2)
    with open(NEITHER_FILE, "w", encoding="utf-8") as fh:
        json.dump(neither, fh, ensure_ascii=False, indent=2)


# ─────────────────────────── Main routine ──────────────────────────────

def main() -> None:
    print(f"\n📥  Loading dataset: {INPUT_SOURCE}")
    try:
        data = _load_json_source(INPUT_SOURCE)
    except Exception as exc:
        print(f"❌  Failed to load input data: {exc}")
        sys.exit(1)

    print(f"✅  Loaded {len(data)} entries.\n")

    if MODE == "all":
        subset = data
        print(f"📋  MODE=all → processing ALL {len(subset)} entries.\n")
    else:
        subset = [e for e in data if START <= e.get("serial_no", 0) <= END]
        print(f"📋  MODE=range → serial_no {START}-{END} → {len(subset)} entries found.\n")

    if not subset:
        print("⚠️  No entries found for the requested selection. Exiting.")
        return

    # Resume support: load whatever is already in all.json and skip any
    # serial_no already processed, so re-running the workflow (or a job
    # restarting mid-way) never repeats work or duplicates rows.
    existing_rows = _load_json_file_if_exists(ALL_FILE)
    by_serial = {
        r.get("serial_no"): r for r in existing_rows if r.get("serial_no") is not None
    }

    already_done = sum(1 for e in subset if e.get("serial_no") in by_serial)
    if already_done:
        print(
            f"↩️  Resuming: {already_done} of {len(subset)} selected entries "
            "already processed in a previous run.\n"
        )

    if GIT_PUSH:
        git_setup_identity()

    unmatched: list = []
    new_since_commit = 0
    total_new = 0

    for idx, entry in enumerate(subset, start=1):
        serial = entry.get("serial_no", "?")

        if serial in by_serial:
            continue  # already processed in a previous run

        title = entry.get("title", "")
        alt = entry.get("alternate_title", "")

        print(f"[{idx}/{len(subset)}] #{serial} → \"{title}\"")

        mal_id, mal_title, matched_via = find_mal_entry(title, alt)
        mal_url = f"https://myanimelist.net/anime/{mal_id}" if mal_id else None
        sub_dub_status = categorize_sub_dub(entry)

        enriched = dict(entry)
        enriched["mal_id"] = mal_id
        enriched["mal_title"] = mal_title
        enriched["mal_url"] = mal_url
        enriched["mal_matched_via"] = matched_via
        enriched["sub_dub_status"] = sub_dub_status

        if mal_id:
            note = f"  (via: '{matched_via}')" if matched_via != title else ""
            print(f"      ✅  MAL ID={mal_id}  |  MAL title='{mal_title}'  |  {mal_url}{note}")
        else:
            unmatched.append(title)
            print("      ❌  No MAL match found.")
        print(f"      🏷️  sub/dub: {sub_dub_status}")

        by_serial[serial] = enriched
        new_since_commit += 1
        total_new += 1
        print()

        if new_since_commit >= COMMIT_EVERY:
            all_rows = list(by_serial.values())
            all_rows.sort(key=lambda r: (r.get("serial_no") is None, r.get("serial_no", 0)))
            save_outputs(all_rows)
            git_commit_and_push(
                f"MAL mapping: +{new_since_commit} entries (progress checkpoint)"
            )
            new_since_commit = 0

    # Final save + commit for any remainder under COMMIT_EVERY.
    all_rows = list(by_serial.values())
    all_rows.sort(key=lambda r: (r.get("serial_no") is None, r.get("serial_no", 0)))
    save_outputs(all_rows)

    if new_since_commit > 0:
        git_commit_and_push(f"MAL mapping: +{new_since_commit} entries (final)")

    matched_count = sum(1 for r in all_rows if r.get("mal_id"))
    print("=" * 62)
    print(f"  ✅  Matched (all-time)   : {matched_count} / {len(all_rows)}")
    print(f"  🆕  Newly processed run  : {total_new}")
    print(f"  ❌  Unmatched this run   : {len(unmatched)}")
    if unmatched:
        print("\n  Unmatched titles this run:")
        for t in unmatched:
            print(f"    • {t}")
    print("\n  💾  Output files:")
    print(f"      {ALL_FILE}")
    print(f"      {SUBBED_FILE}")
    print(f"      {DUBBED_FILE}")
    print(f"      {NEITHER_FILE}")
    print("=" * 62 + "\n")


if __name__ == "__main__":
    main()

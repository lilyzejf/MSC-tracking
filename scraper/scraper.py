"""
MSC Track-a-Fishery vessel list monitor.

For each fishery in fisheries.json:
  1. Resolve its URL slug on fisheries.msc.org (if not already known).
  2. Find the newest "Vessel List"-style PDF on its documents page.
  3. Download it and extract vessel names.
  4. Compare against the last saved snapshot in docs/data/<slug>.json.
  5. Record any additions/removals into docs/data/changelog.json.

Run manually:  python scraper/scraper.py
Run in CI:     see .github/workflows/monthly-update.yml
"""
import json
import re
import sys
import difflib
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import pdfplumber
import io

ROOT = Path(__file__).resolve().parent.parent
FISHERIES_FILE = ROOT / "scraper" / "fisheries.json"
DATA_DIR = ROOT / "docs" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BASE = "https://fisheries.msc.org/en/fisheries"
# Look like an ordinary browser request — some sites treat bare/non-browser
# User-Agents (or requests missing Accept/Accept-Language) differently.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# One session so cookies set on the first request (if any) carry through to
# later requests, the way a real browser session would behave.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def slugify_candidates(html: str, name: str):
    """Pull every /en/fisheries/<slug>/ link out of a search results page,
    paired with its visible link text, for fuzzy matching against `name`."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a[href*='/en/fisheries/']"):
        href = a.get("href", "")
        m = re.search(r"/en/fisheries/([a-z0-9\-]+)/?", href)
        if m and m.group(1) not in ("@@search",):
            out.append((m.group(1), a.get_text(strip=True)))
    return out


def resolve_slug(name: str) -> str | None:
    """Use the site search to find the slug whose link text best matches `name`."""
    resp = SESSION.get(f"{BASE}/@@search", params={"q": name}, timeout=30)
    resp.raise_for_status()
    candidates = slugify_candidates(resp.text, name)
    if not candidates:
        return None
    best = max(candidates, key=lambda c: difflib.SequenceMatcher(None, c[1].lower(), name.lower()).ratio())
    score = difflib.SequenceMatcher(None, best[1].lower(), name.lower()).ratio()
    if score < 0.5:
        return None
    return best[0]


# Some certifiers label this document type slightly differently on the site.
VESSEL_KEYWORDS = ("vessel", "eligible fisher")


def latest_vessel_list_pdf(slug: str) -> tuple[str, str] | None:
    """Return (pdf_url, version_label) for the newest vessel-list-like document, or None.

    Important: do NOT filter server-side with ?file_type=... — fisheries.msc.org
    returns HTTP 500 for that endpoint when a fishery has zero documents of the
    requested type, instead of an empty page. So instead we fetch the unfiltered
    document list and pick out anything vessel-related ourselves.
    """
    fishery_url = f"{BASE}/{slug}/"
    try:
        SESSION.get(fishery_url, timeout=30)
    except requests.RequestException as e:
        print(f"[DIAG] warm-up request to {fishery_url} failed: {e}", file=sys.stderr)

    doc_url = f"{BASE}/{slug}/@@other-documentsets"
    resp = SESSION.get(doc_url, headers={"Referer": fishery_url}, timeout=30)
    if resp.status_code != 200:
        print(f"[DIAG] {slug}: unfiltered @@other-documentsets -> HTTP {resp.status_code} "
              f"(this usually means the slug itself is wrong/stale on the site)", file=sys.stderr)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Each document is a row/section; look for any block of text containing a
    # vessel-related keyword, then take the first PDF-ish link within it.
    candidates = []
    for el in soup.find_all(["li", "tr", "div"]):
        text = el.get_text(" ", strip=True)
        if text and any(k in text.lower() for k in VESSEL_KEYWORDS):
            link = el.select_one("a[href*='cert.msc.org'], a[href$='.pdf']")
            if link:
                candidates.append((link["href"], text[:80]))

    if candidates:
        # Listings are newest-first on this site; take the first match.
        return candidates[0]

    snippet = re.sub(r"\s+", " ", soup.get_text())[:200]
    print(f"[DIAG] {slug}: page loaded but nothing vessel-related found. Body starts: {snippet!r}", file=sys.stderr)
    return None


def extract_vessel_names(pdf_bytes: bytes) -> list[str]:
    """Best-effort extraction of vessel names from a vessel-list PDF.

    Vessel list PDFs vary by certifier (table vs. plain list), so this pulls every
    table row's first column AND every plausible text line, then de-dupes. Expect
    to tune EXCLUDE_PATTERNS for your specific certifiers after the first run.
    """
    EXCLUDE_PATTERNS = re.compile(
        r"^(page \d|msc|vessel list|certificate|version|effective|issued|prepared|"
        r"client|fishery|ifn|imo|flag|gear|species|date|notes?|total|continued)\b",
        re.IGNORECASE,
    )
    names = set()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    if not row or not row[0]:
                        continue
                    cell = str(row[0]).strip()
                    if cell and not EXCLUDE_PATTERNS.match(cell) and len(cell) > 2:
                        names.add(cell)
            if not page.extract_tables():
                for line in (page.extract_text() or "").splitlines():
                    line = line.strip()
                    if line and not EXCLUDE_PATTERNS.match(line) and 2 < len(line) < 80:
                        names.add(line)
    return sorted(names)


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def main():
    fisheries = json.loads(FISHERIES_FILE.read_text())
    changelog = load_json(DATA_DIR / "changelog.json", [])
    summary = []
    now = datetime.now(timezone.utc).isoformat()
    slugs_dirty = False

    for entry in fisheries:
        name, slug = entry["name"], entry.get("slug")
        if not slug:
            slug = resolve_slug(name)
            if not slug:
                print(f"[WARN] could not resolve slug for: {name}", file=sys.stderr)
                summary.append({"name": name, "status": "slug_not_found"})
                continue
            entry["slug"] = slug
            slugs_dirty = True
            print(f"[INFO] resolved '{name}' -> {slug}")

        found = latest_vessel_list_pdf(slug)
        if not found:
            print(f"[WARN] no vessel list found for: {name} ({slug})", file=sys.stderr)
            summary.append({"name": name, "slug": slug, "status": "no_vessel_list"})
            continue
        pdf_url, version_label = found

        snapshot_path = DATA_DIR / f"{slug}.json"
        prev = load_json(snapshot_path, {"vessels": [], "source_pdf": None})

        if prev.get("source_pdf") == pdf_url:
            summary.append({"name": name, "slug": slug, "status": "unchanged", "vessel_count": len(prev["vessels"])})
            continue

        try:
            pdf_resp = SESSION.get(pdf_url, timeout=60)
            pdf_resp.raise_for_status()
            vessels = extract_vessel_names(pdf_resp.content)
        except Exception as e:
            print(f"[ERROR] fetching/parsing PDF for {name}: {e}", file=sys.stderr)
            summary.append({"name": name, "slug": slug, "status": "error", "error": str(e)})
            continue

        old_set, new_set = set(prev["vessels"]), set(vessels)
        added, removed = sorted(new_set - old_set), sorted(old_set - new_set)

        save_json(snapshot_path, {
            "name": name,
            "slug": slug,
            "vessels": vessels,
            "source_pdf": pdf_url,
            "version_label": version_label,
            "last_updated": now,
        })

        if added or removed:
            changelog.insert(0, {
                "date": now,
                "fishery": name,
                "slug": slug,
                "added": added,
                "removed": removed,
            })
        summary.append({
            "name": name, "slug": slug, "status": "updated",
            "vessel_count": len(vessels), "added": len(added), "removed": len(removed),
        })

    save_json(DATA_DIR / "changelog.json", changelog)
    save_json(DATA_DIR / "summary.json", {"last_run": now, "fisheries": summary})
    if slugs_dirty:
        save_json(FISHERIES_FILE, fisheries)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
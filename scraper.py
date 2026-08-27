"""
Scrape every Bellhaven community from the public website.

The website is our ground truth for who Bellhaven operates today, so this needs
to be complete and precisely parsed -- a mangled street or a dropped zip turns
into a bad match downstream, and a bad match turns into a wrong parent in the
CRM.

Two parsing decisions worth knowing about:

  * Address. "875 Elm Street New Carlisle, OH 45344" cannot be split reliably
    with a regex -- there is no delimiter between street and a multi-word city.
    The markup does have one: <dd>875 Elm Street<br>New Carlisle, OH 45344</dd>.
    We split on the <br> and never guess.

  * Care offerings. Each offering is its own <span class="badge">, so we keep
    them as a list instead of concatenating them into "Assisted Living Memory
    Support" and losing the boundary.

Every record also carries a content hash, so the daily run can tell an unchanged
community from an edited one without re-diffing the whole file.

Usage:  python3 scraper.py [--out data] [--quiet]
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import unescape

BASE = "https://analyst-assessment-production.up.railway.app"
LIST_PATH = "/communities"
MAX_LIST_PAGES = 50          # guard against a pager that never stops
REQUEST_DELAY = 0.15         # be polite; the site is small
USER_AGENT = "bellhaven-ownership-sync/1.0"

# Fields every community must have for the matcher to be able to use it.
REQUIRED = ("name", "street", "city", "state", "zip")


# --------------------------------------------------------------- http

def fetch(path, attempts=3):
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:  # timeouts, connection resets
            last = e
        if i < attempts - 1:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last}")


# --------------------------------------------------------------- parsing

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def text(html):
    """Tags out, entities decoded, whitespace collapsed."""
    return WS_RE.sub(" ", unescape(TAG_RE.sub(" ", html))).strip()


def parse_city_state_zip(line):
    """'New Carlisle, OH 45344' -> ('New Carlisle', 'OH', '45344')"""
    m = re.match(r"^(.+?),\s*([A-Za-z]{2})\.?\s+(\d{5})(?:-\d{4})?$", line.strip())
    if not m:
        return "", "", ""
    city, state, zipcode = m.groups()
    return city.strip(), state.upper(), zipcode


def parse_detail(slug, html):
    rec = {"slug": slug, "url": BASE + slug}

    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html)
    rec["name"] = text(h1.group(1)) if h1 else ""

    for dt, dd in re.findall(r"(?is)<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", html):
        label = text(dt).lower()
        if label == "address":
            # Street and locality are separated by <br>, not by punctuation.
            parts = [text(p) for p in re.split(r"(?i)<br\s*/?>", dd)]
            parts = [p for p in parts if p]
            rec["street"] = parts[0] if parts else ""
            city, state, zipcode = parse_city_state_zip(parts[1] if len(parts) > 1 else "")
            rec["city"], rec["state"], rec["zip"] = city, state, zipcode
            rec["address_raw"] = " ".join(parts)
        elif label == "care offerings":
            badges = re.findall(r'(?is)<span class="badge"[^>]*>(.*?)</span>', dd)
            rec["care_offerings"] = [text(b) for b in badges] or ([text(dd)] if text(dd) else [])
        elif label == "administrator":
            rec["administrator"] = text(dd)
        elif label == "phone":
            rec["phone"] = text(dd)

    # Callout boxes carry ownership news ("recently joined Bellhaven"), which is
    # exactly the kind of evidence a reviewer wants to see. Keep them verbatim.
    rec["notices"] = [text(n) for n in
                      re.findall(r'(?is)<div class="notice"[^>]*>(.*?)</div>', html)]

    rec.setdefault("care_offerings", [])
    for f in ("street", "city", "state", "zip", "administrator", "phone", "address_raw"):
        rec.setdefault(f, "")

    payload = json.dumps({k: rec[k] for k in
                          ("name", "street", "city", "state", "zip",
                           "care_offerings", "administrator", "phone")},
                         sort_keys=True, ensure_ascii=False)
    rec["source_hash"] = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return rec


# --------------------------------------------------------------- crawl

CARD_RE = re.compile(
    r'(?is)<div class="card">\s*<h3><a href="(/communities/[^"]+)">(.*?)</a></h3>'
    r'\s*<div class="city">(.*?)</div>'
)


def discover(log):
    """Walk the pager collecting slugs. Stops when a page yields nothing new."""
    seen, cards, page = [], {}, 1
    while page <= MAX_LIST_PAGES:
        html = fetch(f"{LIST_PATH}?page={page}")
        found = CARD_RE.findall(html)
        if not found:
            found = [(s, "", "") for s in
                     dict.fromkeys(re.findall(r'href="(/communities/[^"]+)"', html))]
        new = [f for f in found if f[0] not in cards]
        if not new:
            break
        for slug, name, city in new:
            cards[slug] = {"name": text(name), "city_line": text(city)}
            seen.append(slug)
        log(f"  page {page}: +{len(new)} (running total {len(seen)})")
        page += 1
        time.sleep(REQUEST_DELAY)
    else:
        log(f"  ! hit the {MAX_LIST_PAGES}-page cap; pager may be misbehaving")
    return seen, cards


def scrape(log=print):
    log("Discovering communities")
    slugs, cards = discover(log)
    log(f"  {len(slugs)} community pages found")

    log("Fetching detail pages")
    rows = []
    for i, slug in enumerate(slugs, 1):
        rec = parse_detail(slug, fetch(slug))

        # The list card independently states the name and city. If the detail
        # page disagrees, our parse drifted -- surface it rather than trust it.
        card = cards.get(slug, {})
        rec["warnings"] = []
        if card.get("name") and rec["name"] and card["name"] != rec["name"]:
            rec["warnings"].append(f"name differs from list card: {card['name']!r}")
        if card.get("city_line") and rec["city"] and rec["state"]:
            if card["city_line"] != f"{rec['city']}, {rec['state']}":
                rec["warnings"].append(f"city differs from list card: {card['city_line']!r}")

        missing = [f for f in REQUIRED if not rec.get(f)]
        if missing:
            rec["warnings"].append("missing required: " + ", ".join(missing))

        rows.append(rec)
        if i % 10 == 0 or i == len(slugs):
            log(f"  {i}/{len(slugs)}")
        time.sleep(REQUEST_DELAY)

    rows.sort(key=lambda r: r["slug"])   # stable order -> clean daily diffs
    return rows


# --------------------------------------------------------------- output

def write(rows, out_dir, log=print):
    os.makedirs(out_dir, exist_ok=True)
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    doc = {
        "source": BASE,
        "scraped_at": scraped_at,
        "count": len(rows),
        "communities": rows,
    }
    with open(os.path.join(out_dir, "website_communities.json"), "w") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)

    cols = ["slug", "name", "street", "city", "state", "zip",
            "care_offerings", "administrator", "phone", "url",
            "source_hash", "warnings"]
    with open(os.path.join(out_dir, "website_communities.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r,
                        "care_offerings": "; ".join(r["care_offerings"]),
                        "warnings": "; ".join(r["warnings"])})

    log(f"\nWrote {len(rows)} communities to {out_dir}/website_communities.{{json,csv}}")
    return doc


def report(rows, log=print):
    flagged = [r for r in rows if r["warnings"]]
    log(f"\n{'-' * 66}")
    log(f"{len(rows)} communities | "
        f"{len({r['state'] for r in rows})} states | "
        f"{sum(len(r['care_offerings']) for r in rows)} care-offering tags")

    offerings = {}
    for r in rows:
        for o in r["care_offerings"]:
            offerings[o] = offerings.get(o, 0) + 1
    for o, n in sorted(offerings.items(), key=lambda kv: -kv[1]):
        log(f"    {n:>3}  {o}")

    noticed = [r for r in rows if r["notices"]]
    if noticed:
        log(f"\n  {len(noticed)} page(s) carry a notice callout:")
        for r in noticed:
            for n in r["notices"]:
                log(f"    {r['name']}: {n}")

    if flagged:
        log(f"\n  ! {len(flagged)} record(s) need attention:")
        for r in flagged:
            log(f"    {r['slug']}: {'; '.join(r['warnings'])}")
    else:
        log("\n  All records parsed cleanly.")
    log("-" * 66)
    return not flagged


def main():
    ap = argparse.ArgumentParser(description="Scrape Bellhaven communities.")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    log = (lambda *a: None) if args.quiet else print

    rows = scrape(log)
    write(rows, args.out, log)
    clean = report(rows, log)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())

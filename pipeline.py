"""
The daily run: refresh both sides of the picture, then re-match.

    python3 pipeline.py

Scrape the website, snapshot the CRM, and regenerate proposals. It deliberately
writes nothing to the CRM -- proposals land in the review queue and a human
decides. Re-running is safe: proposals carry stable fingerprints, and anything
already approved or rejected is filtered out before a reviewer sees it, so a
second run on unchanged data surfaces nothing new.

Exit codes let a scheduler act on the outcome:
    0  ran clean, nothing new to review
    1  ran clean, there are new proposals waiting
    2  something went wrong
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone

import decisions as D
import matcher
import scraper
from crm_client import CRMClient, snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def step(n, total, title):
    print(f"\n[{n}/{total}] {title}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-scrape", action="store_true",
                    help="reuse the last website snapshot")
    ap.add_argument("--skip-crm", action="store_true",
                    help="reuse the last CRM snapshot")
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--ledger", default=D.DB_PATH,
                    help="decision ledger to filter against (for testing)")
    args = ap.parse_args()

    started = datetime.now(timezone.utc)
    print(f"Ownership sync -- {started:%Y-%m-%d %H:%M:%SZ}")

    try:
        step(1, 4, "Website")
        if args.skip_scrape:
            print("  skipped")
        else:
            rows = scraper.scrape(log=lambda *a: None)
            scraper.write(rows, args.data, log=print)
            if not scraper.report(rows, log=print):
                print("  ! scraper raised warnings -- matching may be affected")

        step(2, 4, "CRM snapshot")
        if args.skip_crm:
            print("  skipped")
        else:
            client = CRMClient()
            print(f"  auth: {client.me().get('candidate')}")
            snapshot(args.data, client)

        step(3, 4, "Matching")
        result = matcher.run()
        with open(os.path.join(args.data, "proposals.json"), "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        c = result["counts"]
        for k, v in sorted(c["classified"].items()):
            print(f"  {v:>3}  {k}")
        print(f"  {sum(c['proposals'].values()):>3}  proposals generated")

        step(4, 4, "Review queue")
        conn = D.connect(args.ledger)
        pending = D.pending(conn, result["proposals"])
        decided = len(result["proposals"]) - len(pending)
        print(f"  {decided:>3}  already decided in an earlier run (suppressed)")
        print(f"  {len(pending):>3}  awaiting review")
        if pending:
            from collections import Counter
            for t, n in sorted(Counter(p["type"] for p in pending).items()):
                print(f"         {n:>3}  {t}")

    except Exception:
        traceback.print_exc()
        print("\nFAILED -- the CRM was not modified.", file=sys.stderr)
        return 2

    took = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\nDone in {took:.1f}s. Nothing was written to the CRM.")
    if pending:
        print(f"Review and approve at:  python3 app.py --live")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

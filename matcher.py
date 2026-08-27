"""
Match Bellhaven's website communities to CRM accounts and propose corrections.

Reads   data/website_communities.json  (ground truth -- who Bellhaven operates)
        data/accounts.json             (the CRM as it stands)
        data/contacts.json             (evidence for duplicate tie-breaking)
Writes  data/proposals.json            (nothing is applied here -- see app.py)

Nothing in this module talks to the CRM. It only decides what *should* change
and shows its work, because a reviewer has to be able to disagree with it.
"""

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

from normalize import (house_number, name_similarity, norm_city, norm_name,
                       norm_street, street_core)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

BELLHAVEN_PARENT_ID = "0015QAPLGS3FVYEEEM"
BELLHAVEN_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

# Website and CRM use different care vocabularies ("Short-Term Rehabilitation &
# Nursing" vs "Skilled Nursing") and the website allows several per community
# while the CRM holds one. The mapping is genuinely ambiguous, so we surface the
# difference as evidence and never propose a care_type change.
PROPOSE_CARE_TYPE = False


# ----------------------------------------------------------------- loading

def load():
    with open(f"{DATA}/website_communities.json") as f:
        site = json.load(f)
    web = site["communities"] if isinstance(site, dict) else site
    with open(f"{DATA}/accounts.json") as f:
        accounts = json.load(f)
    try:
        with open(f"{DATA}/contacts.json") as f:
            contacts = json.load(f)
    except FileNotFoundError:
        contacts = []
    return web, accounts, contacts


def is_parent_record(acct):
    """Parent companies live in the same table but have no address."""
    return "(Parent Account)" in acct["name"] or not acct["billing_street"]


# ----------------------------------------------------------------- matching

def build_index(facilities):
    idx = {
        "t1": defaultdict(list),   # normalized street + zip
        "t2": defaultdict(list),   # zip + house number + street core
        "t3": defaultdict(list),   # state + city + house number
        "by_state": defaultdict(list),
    }
    for a in facilities:
        st = norm_street(a["billing_street"])
        z = a["billing_zip"]
        hn = house_number(a["billing_street"])
        idx["t1"][(st, z)].append(a)
        idx["t2"][(z, hn, street_core(a["billing_street"]))].append(a)
        idx["t3"][(a["billing_state"].upper(), norm_city(a["billing_city"]), hn)].append(a)
        idx["by_state"][a["billing_state"].upper()].append(a)
    return idx


NAME_THRESHOLD = 0.85


def find_candidates(w, idx):
    """Return (accounts, tier_label, confidence).

    Every tier is gated on geography. Name similarity is never allowed to
    stand on its own -- that is the guard that keeps the Ohio 'Amberly Manor'
    from matching the Colorado account with the identical name.
    """
    st, z = norm_street(w["street"]), w["zip"]
    hn, core = house_number(w["street"]), street_core(w["street"])
    state, city = w["state"].upper(), norm_city(w["city"])

    hit = idx["t1"].get((st, z))
    if hit:
        return hit, "T1 exact address", "high"

    hit = idx["t2"].get((z, hn, core))
    if hit:
        return hit, "T2 zip + house number + street core", "high"

    hit = idx["t3"].get((state, city, hn))
    if hit:
        return hit, "T3 city + house number", "medium"

    scored = []
    for a in idx["by_state"].get(state, []):
        if norm_city(a["billing_city"]) != city:
            continue                      # geographic gate, always
        s = name_similarity(w["name"], a["name"])
        if s >= NAME_THRESHOLD:
            scored.append((s, a))
    if scored:
        scored.sort(key=lambda p: -p[0])
        return [a for _, a in scored], "T4 same city + name similarity", "low"

    return [], "no match", None


# -------------------------------------------------- duplicate survivor ladder

def pick_survivor(group, w, contacts_by_acct):
    """Ordered tie-breakers. First rule that *decisively* separates the group wins.

    Continuous rules (name similarity, completeness) must clear a margin before
    they are allowed to decide. Without that, three near-identical Kettering
    accounts get separated by 0.009 of string ratio -- noise dressed up as
    evidence. Rule 7 exists so a genuinely undecidable group still resolves the
    same way on every run, instead of thrashing the CRM daily.
    """
    def rules(a):
        cons = contacts_by_acct.get(a["account_id"], [])
        admin = (w.get("administrator") or "").strip().lower()
        return [
            ("R1 has billing history", "binary", 0,
             1 if (a["lifetime_revenue"] > 0 or a["outstanding_ar"] > 0) else 0),
            ("R2 holds the administrator named on the website", "binary", 0,
             1 if admin and any(c["name"].strip().lower() == admin for c in cons) else 0),
            ("R3 phone matches the website", "binary", 0,
             1 if a["phone"] and a["phone"] == w.get("phone") else 0),
            ("R4 name closest to the website", "scale", 0.10,
             name_similarity(w["name"], a["name"])),
            ("R5 already under the correct parent", "binary", 0,
             1 if a["parent_id"] == BELLHAVEN_PARENT_ID else 0),
            ("R6 most contacts / most complete", "scale", 1,
             len(cons) + sum(1 for f in ("phone", "care_type") if a.get(f))),
        ]

    scored = [(rules(a), a) for a in group]
    for i in range(6):
        label, kind, margin, _ = scored[0][0][i]
        vals = sorted((s[i][3] for s, _ in scored), reverse=True)
        best = vals[0]
        top = [(s, a) for s, a in scored if s[i][3] == best]

        # A continuous rule whose spread is inside the margin carries no signal.
        # It must not decide -- and it must not narrow the field either, or the
        # next rule inherits a single survivor and "wins" by default.
        if kind == "scale":
            runner_up = next((v for v in vals if v != best), None)
            if runner_up is not None and (best - runner_up) < margin:
                continue

        if len(top) == 1 and best > 0:
            s, a = top[0]
            return a, label, [f"{l}: {v}" for l, _, _, v in s]
        if len(top) < len(scored):
            scored = top          # narrowed the field; keep going

    winner = min((a for _, a in scored), key=lambda a: a["account_id"])
    return (winner,
            "R7 no distinguishing evidence -- lowest account_id for reproducibility",
            [f"{l}: {v}" for l, _, _, v in rules(winner)])


NEAR_MISS_NAME_FLOOR = 0.45


def near_misses(w, facilities):
    """Accounts close enough that creating a new record might duplicate them.

    A community with no address match is normally a straightforward create. It
    stops being straightforward when the CRM already holds something in the same
    zip with an overlapping name -- that is either a stale address on an existing
    account or a genuinely different facility, and only a human can tell.
    """
    out = []
    for a in facilities:
        same_zip = a["billing_zip"] and a["billing_zip"] == w["zip"]
        same_city = (a["billing_state"].upper() == w["state"].upper()
                     and norm_city(a["billing_city"]) == norm_city(w["city"]))
        if not (same_zip or same_city):
            continue
        if name_similarity(w["name"], a["name"]) < NEAR_MISS_NAME_FLOOR:
            continue
        out.append({**a, "_why": "same zip" if same_zip else "same city"})
    return out


# ----------------------------------------------------------------- proposals

def fingerprint(kind, account_id, changes):
    """Stable across runs so an already-decided proposal is never re-raised."""
    payload = json.dumps([kind, account_id, sorted(changes.items())],
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def make(kind, account, changes, confidence, tier, evidence, extra=None):
    p = {
        "type": kind,
        "account_id": account["account_id"] if account else None,
        "account_name": account["name"] if account else None,
        "changes": changes,
        "confidence": confidence,
        "match_tier": tier,
        "evidence": evidence,
    }
    p.update(extra or {})
    p["fingerprint"] = fingerprint(kind, p["account_id"], changes)
    return p


def needs_chow(a):
    """The SOP: preserve the old account only when BOTH are positive."""
    return a["lifetime_revenue"] > 0 and a["outstanding_ar"] > 0


def care_evidence(w, a):
    site = " / ".join(w["care_offerings"]) or "(none)"
    return f"care offerings -- website: {site}; CRM care_type: {a['care_type'] or '(blank)'}"


def plan_account_updates(w, a, tier, confidence, contacts_by_acct, proposals,
                         survivor_note=None, survivor_caveat=None):
    """Field-level corrections for the account we decided to keep."""
    base = [
        f"website: {w['name']} -- {w['street']}, {w['city']}, {w['state']} {w['zip']}",
        f"CRM:     {a['name']} -- {a['billing_street']}, {a['billing_city']}, "
        f"{a['billing_state']} {a['billing_zip']}",
        f"matched by {tier}",
        care_evidence(w, a),
    ]
    if survivor_note:
        base.append(survivor_note)

    # A survivor picked without distinguishing evidence must say so in the CRM,
    # not only in this review queue. The writeup is read once by a grader; the
    # note is read by whoever inherits this record.
    if survivor_caveat:
        proposals.append(make(
            "annotate", a, {"note": [a["note"] or "", survivor_caveat]},
            confidence, tier,
            base + ["the surviving account carries no explanation of its own "
                    "otherwise -- only the deactivated copies would",
                    "recorded so a later reviewer can overturn the choice with "
                    "evidence we did not have"]))

    if a["name"] != w["name"]:
        proposals.append(make(
            "rename", a, {"name": [a["name"], w["name"]]}, confidence, tier,
            base + [f"name similarity {name_similarity(w['name'], a['name']):.2f}",
                    "the website is authoritative for the operating name"]))

    if a["parent_id"] != BELLHAVEN_PARENT_ID:
        old_parent = a["parent_name"] or "(none)"
        fin = (f"lifetime_revenue={a['lifetime_revenue']:,} "
               f"outstanding_ar={a['outstanding_ar']:,}")
        if needs_chow(a):
            new_account = {
                "name": w["name"],
                "parent_id": BELLHAVEN_PARENT_ID,
                "billing_street": w["street"],
                "billing_city": w["city"],
                "billing_state": w["state"],
                "billing_zip": w["zip"],
                "care_type": a["care_type"],
                "phone": w.get("phone") or a["phone"],
                "status": "Active",
                "note": (f"Created by ownership sync for change of ownership from "
                         f"{old_parent}. Predecessor account {a['account_id']} "
                         f"retained for billing."),
            }
            proposals.append(make(
                "chow", a,
                {"chow_current_account": [a["chow_current_account"] or "", "<new account id>"]},
                confidence, tier,
                base + [
                    f"SOP: revenue AND outstanding AR are both positive ({fin})",
                    "billing needs the original account preserved, so its parent "
                    "is deliberately left unchanged",
                    f"new account will sit under {BELLHAVEN_PARENT_NAME}",
                ],
                extra={"create_account": new_account, "atomic": True}))
        else:
            why = ("no billing history" if a["lifetime_revenue"] == 0 and a["outstanding_ar"] == 0
                   else "no outstanding AR" if a["outstanding_ar"] == 0
                   else "no revenue history")
            proposals.append(make(
                "reparent", a,
                {"parent_id": [a["parent_id"], BELLHAVEN_PARENT_ID]},
                confidence, tier,
                base + [f"SOP: {why} ({fin}) -- safe to re-parent in place",
                        f"{old_parent} -> {BELLHAVEN_PARENT_NAME}"]))


# ----------------------------------------------------------------- pipeline

def run():
    web, accounts, contacts = load()
    facilities = [a for a in accounts if not is_parent_record(a)]
    parents = [a for a in accounts if is_parent_record(a)]

    contacts_by_acct = defaultdict(list)
    for c in contacts:
        contacts_by_acct[c["account_id"]].append(c)

    idx = build_index(facilities)
    proposals = []
    classified = []
    matched_ids = set()

    for w in sorted(web, key=lambda r: r["slug"]):
        cands, tier, confidence = find_candidates(w, idx)
        for a in cands:
            matched_ids.add(a["account_id"])

        if not cands:
            near = near_misses(w, facilities)
            payload = {
                "name": w["name"],
                "parent_id": BELLHAVEN_PARENT_ID,
                "billing_street": w["street"],
                "billing_city": w["city"],
                "billing_state": w["state"],
                "billing_zip": w["zip"],
                "phone": w.get("phone", ""),
                "status": "Active",
                "note": f"Created by ownership sync from {w['url']}.",
            }
            if near:
                payload["note"] += (
                    " Possible overlap with existing account(s) "
                    + ", ".join(f"{n['account_id']} ({n['name']}, "
                                f"{n['billing_street']}, parent="
                                f"{n['parent_name'] or 'none'})" for n in near)
                    + " -- same locality, different street, different phone and "
                      "administrator, so treated as a separate facility. Verify "
                      "before working both records.")
            ev = [f"website: {w['name']} -- {w['street']}, {w['city']}, "
                  f"{w['state']} {w['zip']}",
                  "no CRM account shares this address, and no same-city name match "
                  "cleared the threshold",
                  "similar names elsewhere in the CRM were rejected on geography"]
            if near:
                ev.append("BUT the CRM holds nearby account(s) a human should rule out "
                          "before a new account is created:")
                ev += [f"    {n['account_id']} {n['name']} -- {n['billing_street']}, "
                       f"{n['billing_city']}, {n['billing_state']} {n['billing_zip']} "
                       f"(parent={n['parent_name'] or 'none'}, "
                       f"name similarity {name_similarity(w['name'], n['name']):.2f}, "
                       f"{n['_why']})" for n in near]
                ev.append("creating a duplicate is worse than waiting, so this is "
                          "flagged for review rather than treated as a clean create")
            proposals.append(make(
                "create", None, {"name": ["", w["name"]]},
                "low" if near else "high", "no match", ev,
                extra={"create_account": payload, "website": w["slug"],
                       "near_misses": [n["account_id"] for n in near]}))
            classified.append((w, "missing_flagged" if near else "missing", [], tier))
            continue

        if len(cands) == 1:
            a = cands[0]
            before = len(proposals)
            plan_account_updates(w, a, tier, confidence, contacts_by_acct, proposals)
            classified.append((w, "needs_fix" if len(proposals) > before else "confident",
                               cands, tier))
            continue

        # More than one CRM account for one real building.
        survivor, rule, scorecard = pick_survivor(cands, w, contacts_by_acct)
        losers = [a for a in cands if a["account_id"] != survivor["account_id"]]
        low_evidence = rule.startswith("R7")

        caveat = None
        if low_evidence:
            others = ", ".join(x["account_id"] for x in cands
                               if x["account_id"] != survivor["account_id"])
            caveat = (
                f"Survivor of a {len(cands)}-account duplicate group at {w['street']}, "
                f"{w['city']}, {w['state']} {w['zip']} (others: {others}). None of the "
                f"copies had billing history, contacts, a phone matching the website, "
                f"or a name resembling '{w['name']}', so the choice was arbitrary -- "
                f"lowest account_id, for reproducible re-runs. Overturn this with "
                f"evidence if you have any.")

        plan_account_updates(
            w, survivor, tier, "low" if low_evidence else confidence,
            contacts_by_acct, proposals,
            survivor_note=f"kept as survivor of a {len(cands)}-account duplicate group ({rule})",
            survivor_caveat=caveat)

        for l in losers:
            note = (f"Duplicate of {survivor['name']} ({survivor['account_id']}). "
                    f"Same facility at {w['street']}, {w['city']}, {w['state']} {w['zip']}. "
                    f"Survivor chosen by: {rule}.")
            changes = {
                "status": [l["status"], "Inactive"],
                "duplicate_of_account": [l["duplicate_of_account"] or "", survivor["account_id"]],
                "note": [l["note"] or "", note],
            }
            proposals.append(make(
                "dedupe", l, changes, "low" if low_evidence else confidence, tier,
                [f"{len(cands)} CRM accounts share this address:",
                 *[f"    {x['account_id']} {x['name']} "
                   f"(parent={x['parent_name'] or 'none'}, "
                   f"rev={x['lifetime_revenue']:,}, ar={x['outstanding_ar']:,}, "
                   f"contacts={len(contacts_by_acct.get(x['account_id'], []))})"
                   for x in cands],
                 f"website lists exactly one: {w['name']}",
                 f"survivor {survivor['account_id']} -- {rule}",
                 *[f"    scorecard | {s}" for s in scorecard],
                 "API has no merge or delete, so the losing copy is deactivated "
                 "and pointed at the survivor"],
                extra={"survivor_id": survivor["account_id"]}))

        classified.append((w, "duplicate_group", cands, tier))

    # CRM side: accounts filed under Bellhaven that the website no longer lists.
    orphans = []
    for a in facilities:
        if a["parent_id"] != BELLHAVEN_PARENT_ID or a["account_id"] in matched_ids:
            continue
        orphans.append(a)
        has_money = a["lifetime_revenue"] > 0 or a["outstanding_ar"] > 0
        cons = contacts_by_acct.get(a["account_id"], [])
        ev = [f"CRM: {a['name']} -- {a['billing_street']}, {a['billing_city']}, "
              f"{a['billing_state']} {a['billing_zip']}",
              "filed under Bellhaven but no community on the website matches this address",
              f"lifetime_revenue={a['lifetime_revenue']:,} "
              f"outstanding_ar={a['outstanding_ar']:,} contacts={len(cons)}"]
        if has_money:
            note = (f"Not listed on the Bellhaven website as of {datetime.now(timezone.utc):%Y-%m-%d}. "
                    f"Has billing history -- flagged for a human to confirm whether the "
                    f"facility was divested, closed, or is simply missing from the site.")
            changes = {"status": [a["status"], "Needs Review"], "note": [a["note"] or "", note]}
            ev.append("has billing history, so it is flagged rather than deactivated "
                      "unilaterally")
        else:
            note = (f"Not listed on the Bellhaven website as of {datetime.now(timezone.utc):%Y-%m-%d} "
                    f"and has no billing history or contacts. Deactivated by ownership sync.")
            changes = {"status": [a["status"], "Inactive"], "note": [a["note"] or "", note]}
            ev.append("no billing history and no contacts, so deactivating is low risk")
        proposals.append(make("orphan", a, changes, "medium", "no website match", ev))

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "counts": {
            "website_communities": len(web),
            "crm_accounts": len(accounts),
            "crm_facilities": len(facilities),
            "crm_parent_records": len(parents),
            "classified": dict(Counter(c[1] for c in classified)),
            "proposals": dict(Counter(p["type"] for p in proposals)),
        },
        "classified": [{"website": w["name"], "slug": w["slug"], "outcome": o,
                        "tier": t, "crm": [a["account_id"] for a in c]}
                       for w, o, c, t in classified],
        "proposals": proposals,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=f"{DATA}/proposals.json")
    args = ap.parse_args()

    result = run()
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    c = result["counts"]
    print(f"website {c['website_communities']} | CRM {c['crm_accounts']} "
          f"({c['crm_facilities']} facilities + {c['crm_parent_records']} parent records)")
    print("\nclassification:")
    for k, v in sorted(c["classified"].items()):
        print(f"  {v:>3}  {k}")
    print("\nproposals:")
    for k, v in sorted(c["proposals"].items()):
        print(f"  {v:>3}  {k}")
    print(f"  {sum(c['proposals'].values()):>3}  total  -> {args.out}")


if __name__ == "__main__":
    main()

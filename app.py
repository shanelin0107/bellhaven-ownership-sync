"""
Local review queue for CRM ownership corrections.

Nothing reaches the CRM without a human pressing Approve on this page. The
server starts in dry-run; writing for real requires launching with --live, so
an accidental click can never be the thing that mutates production data.

    python3 app.py            # review safely, no writes
    python3 app.py --live     # approvals actually patch the CRM

Deliberately stdlib-only -- no Flask, no pip install, one command to run.
"""

import argparse
import html
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import decisions as D
from crm_client import CRMClient, CRMError, ValidationError

HERE = os.path.dirname(os.path.abspath(__file__))
PROPOSALS = os.path.join(HERE, "data", "proposals.json")

# Dry-run decisions go to a throwaway ledger. Sharing the real one would let a
# rehearsal permanently mark fingerprints as decided, and those proposals would
# never surface again for a live approval -- testing the UI would quietly
# destroy the ability to actually apply anything.
DRYRUN_DB = os.path.join(HERE, "data", "decisions.dryrun.db")

TYPE_LABEL = {
    "rename": "Rename",
    "reparent": "Re-parent",
    "chow": "Change of ownership",
    "dedupe": "Duplicate",
    "annotate": "Record the reasoning",
    "create": "Create account",
    "orphan": "No longer listed",
}
TYPE_BLURB = {
    "rename": "The CRM name is out of date; the website is authoritative.",
    "reparent": "Ownership moved and the account has no billing that blocks it.",
    "chow": "Ownership moved but billing needs the original account preserved.",
    "dedupe": "Several CRM accounts describe one building.",
    "annotate": "A judgement call that the surviving record should carry itself.",
    "create": "The website lists a community the CRM has never had.",
    "orphan": "Filed under Bellhaven but absent from the website.",
}
TYPE_ORDER = ["chow", "reparent", "dedupe", "annotate", "create", "rename", "orphan"]
CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


# ------------------------------------------------------------------ applying

def apply_proposal(client, proposal):
    """Turn an approved proposal into CRM writes. Returns (status, result)."""
    changes = proposal["changes"]
    fields = {k: v[1] for k, v in changes.items()}

    if proposal["type"] == "create":
        return "applied", client.create_account(proposal["create_account"])

    if proposal["type"] == "chow":
        # One approval, two writes that must not come apart: the successor has
        # to exist before the predecessor can point at it.
        created = client.create_account(proposal["create_account"])
        new_id = created.get("account_id")
        if not new_id or new_id.startswith("<"):
            return "applied", {"created": created,
                               "linked": {"message": "dry-run: link skipped"}}
        linked, _ = client.patch_account(proposal["account_id"],
                                         {"chow_current_account": new_id})
        return "applied", {"created": created, "linked": linked}

    result, applied = client.patch_account(proposal["account_id"], fields)
    return ("skipped" if result.get("message") == "no-op" else "applied"), result


# -------------------------------------------------------------------- render

CSS = """
:root{--bg:#F6F7F9;--card:#fff;--line:#DCE0E8;--soft:#EDEFF3;--ink:#151C28;
--ink2:#3C4757;--muted:#6B7686;--acc:#2D5BA8;--accbg:#E6EDF8;--good:#2A7355;
--goodbg:#E2F0EA;--warn:#8F6410;--warnbg:#F7EEDA;--bad:#9E3B2C;--badbg:#F7E7E3;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "IBM Plex Sans",
-apple-system,"Segoe UI",Helvetica,sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:0 22px 90px;}
header{background:var(--ink);color:#fff;padding:20px 0;margin-bottom:26px;}
header .wrap{padding-bottom:0;display:flex;justify-content:space-between;
align-items:center;gap:18px;flex-wrap:wrap;}
header h1{margin:0;font-size:19px;font-weight:600;letter-spacing:-.01em;}
header .sub{font-size:12.5px;opacity:.72;margin-top:2px;}
.mode{font:500 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;padding:7px 12px;
border-radius:3px;letter-spacing:.04em;}
.mode.dry{background:#F7EEDA;color:#6b4a09;}
.mode.live{background:#F7E7E3;color:#7d2c1e;}
.stats{display:flex;gap:0;background:var(--card);border:1px solid var(--line);
border-radius:5px;overflow:hidden;margin-bottom:24px;}
.stat{flex:1;padding:14px 18px;border-right:1px solid var(--line);}
.stat:last-child{border-right:none}
.stat .n{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;}
.stat .l{font-size:11.5px;color:var(--muted);text-transform:uppercase;
letter-spacing:.07em;}
.stat.ok .n{color:var(--good)}.stat.no .n{color:var(--bad)}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px;align-items:center;}
.bar a{font-size:13px;text-decoration:none;color:var(--ink2);background:var(--card);
border:1px solid var(--line);padding:5px 12px;border-radius:14px;}
.bar a.on{background:var(--acc);border-color:var(--acc);color:#fff;}
.bar .sp{flex:1}
.group{margin-bottom:30px}
.group>h2{font-size:15px;margin:0 0 3px;display:flex;align-items:center;gap:10px;}
.group>p{margin:0 0 12px;font-size:13px;color:var(--muted);}
.card{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--tone);
border-radius:4px;padding:16px 18px;margin-bottom:10px;}
.card.high{--tone:var(--good)}.card.medium{--tone:var(--warn)}.card.low{--tone:var(--bad)}
.card h3{margin:0 0 2px;font-size:15.5px;font-weight:600;}
.meta{font:11.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
margin-bottom:11px;word-break:break-all;}
.badge{display:inline-block;font-size:10.5px;font-weight:500;padding:2px 8px;
border-radius:10px;letter-spacing:.04em;text-transform:uppercase;}
.b-high{background:var(--goodbg);color:var(--good)}
.b-medium{background:var(--warnbg);color:var(--warn)}
.b-low{background:var(--badbg);color:var(--bad)}
.b-type{background:var(--accbg);color:var(--acc)}
.diff{background:var(--soft);border-radius:3px;padding:10px 13px;margin-bottom:11px;
font:12.5px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-x:auto;}
.diff .f{color:var(--muted)}
.diff .o{color:var(--bad);text-decoration:line-through;}
.diff .n{color:var(--good);font-weight:500}
details{margin-bottom:11px}
summary{cursor:pointer;font-size:12.5px;color:var(--acc);user-select:none;}
.ev{margin:9px 0 0;padding-left:17px;font-size:12.5px;color:var(--ink2);}
.ev li{margin-bottom:3px;white-space:pre-wrap;}
form{display:inline}
button{font:500 13px/1 inherit;padding:8px 16px;border-radius:3px;cursor:pointer;
border:1px solid transparent;}
.ap{background:var(--good);color:#fff}
.re{background:var(--card);color:var(--bad);border-color:var(--line);margin-left:7px}
button:hover{filter:brightness(.94)}
.bulk{background:var(--card);border:1px dashed var(--line);border-radius:4px;
padding:9px 13px;margin-bottom:11px;font-size:13px;color:var(--ink2);}
.bulk button{padding:5px 11px;font-size:12px;margin-left:8px}
.empty{background:var(--card);border:1px solid var(--line);border-radius:5px;
padding:40px;text-align:center;color:var(--muted);}
table.hist{width:100%;border-collapse:collapse;font-size:13px;background:var(--card);}
table.hist th{text-align:left;font-size:11px;text-transform:uppercase;
letter-spacing:.06em;color:var(--muted);padding:9px 12px;border-bottom:1px solid var(--line);}
table.hist td{padding:9px 12px;border-bottom:1px solid var(--soft);vertical-align:top;}
.tag{font-size:11px;padding:1px 7px;border-radius:9px}
.tag.approved{background:var(--goodbg);color:var(--good)}
.tag.rejected{background:var(--badbg);color:var(--bad)}
.tag.applied{background:var(--accbg);color:var(--acc)}
.tag.failed{background:var(--badbg);color:var(--bad)}
.tag.skipped{background:var(--soft);color:var(--muted)}
.flash{padding:11px 15px;border-radius:4px;margin-bottom:18px;font-size:13.5px;}
.flash.ok{background:var(--goodbg);color:var(--good)}
.flash.err{background:var(--badbg);color:var(--bad)}
"""


def esc(s):
    return html.escape(str(s if s is not None else ""))


def render_diff(p):
    rows = []
    for f, (old, new) in sorted(p["changes"].items()):
        o = esc(old) if old not in ("", None) else "(empty)"
        rows.append(f'<div><span class="f">{esc(f)}</span> '
                    f'<span class="o">{o}</span> → <span class="n">{esc(new)}</span></div>')
    if p.get("create_account"):
        rows.append('<div class="f" style="margin-top:7px">new account payload</div>')
        for k, v in sorted(p["create_account"].items()):
            rows.append(f'<div><span class="f">  {esc(k)}</span> '
                        f'<span class="n">{esc(v)}</span></div>')
    return '<div class="diff">' + "".join(rows) + "</div>"


def render_card(p):
    conf = p["confidence"]
    title = p.get("account_name") or p["changes"].get("name", ["", ""])[1]
    ident = p.get("account_id") or "new account"
    ev = "".join(f"<li>{esc(e)}</li>" for e in p["evidence"])
    return f"""
<div class="card {conf}">
  <h3>{esc(title)}</h3>
  <div class="meta">{esc(ident)} &middot; {esc(p['match_tier'])} &middot; {esc(p['fingerprint'])}</div>
  <div style="margin-bottom:10px">
    <span class="badge b-type">{esc(TYPE_LABEL.get(p['type'], p['type']))}</span>
    <span class="badge b-{conf}">{esc(conf)} confidence</span>
  </div>
  {render_diff(p)}
  <details open><summary>Evidence ({len(p['evidence'])})</summary>
    <ul class="ev">{ev}</ul></details>
  <form method="post" action="/decide">
    <input type="hidden" name="fingerprint" value="{esc(p['fingerprint'])}">
    <input type="hidden" name="decision" value="approved">
    <button class="ap" type="submit">Approve</button></form>
  <form method="post" action="/decide">
    <input type="hidden" name="fingerprint" value="{esc(p['fingerprint'])}">
    <input type="hidden" name="decision" value="rejected">
    <button class="re" type="submit">Reject</button></form>
</div>"""


def render_page(state, flash=None, view="queue", conf_filter=None):
    conn = state["conn"]
    proposals = state["proposals"]
    pend = D.pending(conn, proposals)
    if conf_filter:
        pend = [p for p in pend if p["confidence"] == conf_filter]

    rows = D.history(conn)
    approved = sum(1 for r in rows if r["decision"] == "approved")
    rejected = sum(1 for r in rows if r["decision"] == "rejected")
    applied = sum(1 for r in rows if r["apply_status"] == "applied")
    failed = sum(1 for r in rows if r["apply_status"] == "failed")

    mode = "live" if state["client"].dry_run is False else "dry"
    mode_txt = "LIVE — approvals write to the CRM" if mode == "live" else \
               "DRY RUN — scratch ledger, nothing written (restart with --live)"

    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {kind}">{esc(msg)}</div>'

    if view == "history":
        trs = "".join(
            f"<tr><td>{esc(r['decided_at'])}</td>"
            f"<td><span class='tag {r['decision']}'>{esc(r['decision'])}</span></td>"
            f"<td>{esc(TYPE_LABEL.get(r['proposal_type'], r['proposal_type']))}</td>"
            f"<td>{esc(r['account_name'] or '(new)')}<br>"
            f"<span style='font-size:11px;color:#6B7686'>{esc(r['account_id'] or '')}</span></td>"
            f"<td>{('<span class=tag-x></span>' if not r['apply_status'] else '')}"
            f"<span class='tag {r['apply_status'] or 'skipped'}'>"
            f"{esc(r['apply_status'] or 'not applied')}</span></td>"
            f"<td style='font-size:11px;color:#6B7686;max-width:260px'>"
            f"{esc((r['apply_result'] or '')[:180])}</td></tr>"
            for r in rows)
        body = ("<table class='hist'><tr><th>When</th><th>Decision</th><th>Type</th>"
                "<th>Account</th><th>Write</th><th>Result</th></tr>"
                + (trs or "<tr><td colspan=6 style='padding:30px;text-align:center;"
                          "color:#6B7686'>Nothing decided yet.</td></tr>")
                + "</table>")
    elif not pend:
        body = ("<div class='empty'><strong>Queue is clear.</strong><br>"
                "Every proposal from the current run has been decided.</div>")
    else:
        by_type = {}
        for p in pend:
            by_type.setdefault(p["type"], []).append(p)
        chunks = []
        ordered = TYPE_ORDER + [t for t in by_type if t not in TYPE_ORDER]
        for t in ordered:
            group = by_type.get(t)
            if not group:
                continue
            group.sort(key=lambda p: (CONF_ORDER[p["confidence"]], p["account_name"] or ""))
            highs = [p for p in group if p["confidence"] == "high"]
            bulk = ""
            if len(highs) > 1:
                bulk = (f"<div class='bulk'>{len(highs)} of these are high confidence."
                        f"<form method='post' action='/decide'>"
                        f"<input type='hidden' name='bulk_type' value='{esc(t)}'>"
                        f"<input type='hidden' name='decision' value='approved'>"
                        f"<button class='ap' type='submit'>Approve all "
                        f"{len(highs)} high-confidence</button></form></div>")
            chunks.append(
                f"<div class='group'><h2>{esc(TYPE_LABEL.get(t, t))} "
                f"<span class='badge b-type'>{len(group)}</span></h2>"
                f"<p>{esc(TYPE_BLURB.get(t, ''))}</p>{bulk}"
                + "".join(render_card(p) for p in group) + "</div>")
        body = "".join(chunks)

    def tab(href, label, on):
        return f"<a href='{href}' class='{'on' if on else ''}'>{label}</a>"

    total_pending = len(D.pending(conn, proposals))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Ownership review queue</title><style>{CSS}</style></head><body>
<header><div class="wrap"><div>
  <h1>Ownership review queue</h1>
  <div class="sub">Bellhaven Senior Living &middot; generated {esc(state['generated_at'])}</div>
</div><span class="mode {mode}">{esc(mode_txt)}</span></div></header>
<div class="wrap">
{flash_html}
<div class="stats">
  <div class="stat"><div class="n">{total_pending}</div><div class="l">Pending</div></div>
  <div class="stat ok"><div class="n">{approved}</div><div class="l">Approved</div></div>
  <div class="stat no"><div class="n">{rejected}</div><div class="l">Rejected</div></div>
  <div class="stat"><div class="n">{applied}</div><div class="l">Written to CRM</div></div>
  <div class="stat"><div class="n">{failed}</div><div class="l">Failed</div></div>
</div>
<div class="bar">
  {tab('/', 'Queue', view == 'queue' and not conf_filter)}
  {tab('/?confidence=low', 'Low confidence', conf_filter == 'low')}
  {tab('/?confidence=medium', 'Medium', conf_filter == 'medium')}
  {tab('/?confidence=high', 'High', conf_filter == 'high')}
  <span class="sp"></span>
  {tab('/history', 'Decision log', view == 'history')}
</div>
{body}
</div></body></html>"""


# -------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    state = None
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _send(self, body, code=200, headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        flash = None
        if "msg" in q:
            flash = (q.get("kind", ["ok"])[0], q["msg"][0])
        if u.path == "/history":
            self._send(render_page(self.state, flash, view="history"))
        elif u.path == "/":
            self._send(render_page(self.state, flash,
                                   conf_filter=q.get("confidence", [None])[0]))
        else:
            self._send("<h1>404</h1>", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        decision = form.get("decision", ["rejected"])[0]
        state = self.state

        with self.lock:
            conn = state["conn"]
            pend = {p["fingerprint"]: p for p in D.pending(conn, state["proposals"])}

            if form.get("bulk_type"):
                targets = [p for p in pend.values()
                           if p["type"] == form["bulk_type"][0] and p["confidence"] == "high"]
            else:
                fp = form.get("fingerprint", [""])[0]
                targets = [pend[fp]] if fp in pend else []

            if not targets:
                return self._redirect("Already decided — nothing to do.", "err")

            ok = err = 0
            problems = []
            for p in targets:
                try:
                    D.record(conn, p, decision)
                except ValueError:
                    continue
                if decision != "approved":
                    ok += 1
                    continue
                try:
                    status, result = apply_proposal(state["client"], p)
                    D.record_apply(conn, p["fingerprint"], status, result)
                    ok += 1
                except (CRMError, ValidationError) as e:
                    D.record_apply(conn, p["fingerprint"], "failed", {"error": str(e)})
                    err += 1
                    problems.append(f"{p.get('account_name') or 'new'}: {e}")

            if err:
                return self._redirect(
                    f"{ok} succeeded, {err} failed — " + "; ".join(problems[:2]), "err")
            verb = "approved and written" if decision == "approved" else "rejected"
            if state["client"].dry_run and decision == "approved":
                verb = "approved (dry run — nothing written)"
            return self._redirect(f"{ok} proposal(s) {verb}.")

    def _redirect(self, msg, kind="ok"):
        q = urllib.parse.urlencode({"msg": msg, "kind": kind})
        self.send_response(303)
        self.send_header("Location", f"/?{q}")
        self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--live", action="store_true",
                    help="actually write approved changes to the CRM")
    ap.add_argument("--proposals", default=PROPOSALS)
    ap.add_argument("--reset-dryrun", action="store_true",
                    help="clear the dry-run ledger before starting")
    args = ap.parse_args()

    if args.reset_dryrun and os.path.exists(DRYRUN_DB):
        os.remove(DRYRUN_DB)

    with open(args.proposals) as f:
        doc = json.load(f)

    db = D.DB_PATH if args.live else DRYRUN_DB
    Handler.state = {
        "proposals": doc["proposals"],
        "generated_at": doc["generated_at"],
        "conn": D.connect(db),
        "client": CRMClient(dry_run=not args.live),
    }

    mode = "LIVE — approvals will write to the CRM" if args.live else \
           "DRY RUN — no writes (restart with --live to apply)"
    pend = len(D.pending(Handler.state["conn"], doc["proposals"]))
    print(f"Review queue on http://localhost:{args.port}", flush=True)
    print(f"  {mode}", flush=True)
    print(f"  ledger: {db}", flush=True)
    print(f"  {pend} pending of {len(doc['proposals'])} proposals", flush=True)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

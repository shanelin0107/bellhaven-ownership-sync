"""
Thin CRM API wrapper with the guards the server does not provide.

Probing the sandbox turned up three things the API accepts that it should not:

  * duplicate_of_account may point at the account's own id (a self-referencing
    loop -- discovered the hard way, by writing one)
  * duplicate_of_account is not checked for existence, unlike parent_id
  * chow_current_account is not checked either

Anything the server declines to validate, we validate here. Every write also
drops fields whose value already matches the CRM, so a re-run is a no-op rather
than a pointless update -- and an all-no-op patch is skipped entirely, since the
API rejects an empty body with a 422.
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://analyst-assessment-production.up.railway.app/api/v1"

# The token maps to one candidate's private copy of the CRM, so it stays out of
# the repository. Export it before running anything that talks to the API:
#     export BELLHAVEN_TOKEN=bh_...
TOKEN = os.environ.get("BELLHAVEN_TOKEN", "")

# Exactly what PATCH /accounts/{id} will accept, per the server's own 422 body.
MUTABLE = frozenset({
    "name", "parent_id", "status", "note", "care_type", "phone",
    "billing_street", "billing_city", "billing_state", "billing_zip",
    "chow_current_account", "duplicate_of_account",
})

STATUSES = frozenset({"Active", "Inactive", "Needs Review"})

# Fields that must name a real account when they are non-empty.
REFERENCE_FIELDS = ("parent_id", "duplicate_of_account", "chow_current_account")


class CRMError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class ValidationError(CRMError):
    """Refused before the request left the machine."""


class CRMClient:
    def __init__(self, token=TOKEN, base=BASE, dry_run=True, timeout=30):
        if not token:
            raise CRMError(
                "No API token. Export it before running:\n"
                "    export BELLHAVEN_TOKEN=bh_...")
        self.base = base
        self.timeout = timeout
        self.dry_run = dry_run
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "bellhaven-ownership-sync/1.0",
        }
        self._known_ids = None      # lazily built, for reference checking

    # ------------------------------------------------------------- transport

    def _request(self, method, path, payload=None, attempts=3):
        url = self.base + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers)
        last = None
        for i in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                # 4xx means the request itself is wrong; retrying changes nothing.
                if 400 <= e.code < 500:
                    try:
                        detail = json.loads(body).get("detail", body)
                    except json.JSONDecodeError:
                        detail = body
                    raise CRMError(f"{method} {path} -> {e.code}: {detail}",
                                   status=e.code, body=body)
                last = e
            except Exception as e:
                last = e
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
        raise CRMError(f"{method} {path} failed after {attempts} attempts: {last}")

    # ------------------------------------------------------------------ reads

    def me(self):
        return self._request("GET", "/me")

    def _paged(self, resource, page_size=200):
        rows, page = [], 1
        while True:
            d = self._request("GET", f"/{resource}?page={page}&page_size={page_size}")
            rows.extend(d["data"])
            if not d["data"] or len(rows) >= d["total"]:
                return rows
            page += 1

    def accounts(self):
        return self._paged("accounts")

    def contacts(self):
        return self._paged("contacts")

    def account(self, account_id):
        return self._request("GET", f"/accounts/{account_id}")

    def known_ids(self, refresh=False):
        if self._known_ids is None or refresh:
            self._known_ids = {a["account_id"] for a in self.accounts()}
        return self._known_ids

    # ------------------------------------------------------------- validation

    def _validate(self, fields, account_id=None):
        unknown = set(fields) - MUTABLE
        if unknown:
            raise ValidationError(
                f"not writable by this API: {sorted(unknown)}; "
                f"mutable fields are {sorted(MUTABLE)}")

        if "status" in fields and fields["status"] not in STATUSES:
            raise ValidationError(
                f"status {fields['status']!r} is invalid (case-sensitive); "
                f"must be one of {sorted(STATUSES)}")

        # The server rejects a self-referencing parent_id but happily accepts a
        # self-referencing duplicate_of_account. Block both here.
        for f in ("duplicate_of_account", "chow_current_account", "parent_id"):
            if account_id and fields.get(f) == account_id:
                raise ValidationError(
                    f"{f} would point at its own account ({account_id})")

        known = None
        for f in REFERENCE_FIELDS:
            target = fields.get(f)
            if not target:
                continue
            if known is None:
                known = self.known_ids()
            if target not in known:
                raise ValidationError(f"{f}={target!r} is not an existing account")

    # ----------------------------------------------------------------- writes

    def patch_account(self, account_id, fields, current=None):
        """Apply only the fields that actually differ from what the CRM holds.

        Returns (result, applied_fields). An unchanged account is reported as
        'no-op' rather than sent -- the API 422s on an empty body, and re-running
        the pipeline should be quiet, not noisy.
        """
        current = current or self.account(account_id)
        changed = {k: v for k, v in fields.items() if current.get(k) != v}
        if not changed:
            return {"message": "no-op", "account_id": account_id, "fields": []}, {}

        self._validate(changed, account_id=account_id)

        if self.dry_run:
            return {"message": "dry-run", "account_id": account_id,
                    "fields": sorted(changed)}, changed

        return self._request("PATCH", f"/accounts/{account_id}", changed), changed

    def create_account(self, payload):
        fields = {k: v for k, v in payload.items() if k in MUTABLE}
        if not fields.get("name"):
            raise ValidationError("a new account needs a name")
        self._validate(fields)

        if self.dry_run:
            return {"message": "dry-run", "account_id": "<would be assigned>",
                    "fields": sorted(fields)}

        result = self._request("POST", "/accounts", fields)
        if self._known_ids is not None and result.get("account_id"):
            self._known_ids.add(result["account_id"])
        return result


def snapshot(out_dir, client=None, log=print):
    """Refresh the local CRM snapshots the matcher reads."""
    client = client or CRMClient()
    os.makedirs(out_dir, exist_ok=True)
    for name, rows in (("accounts", client.accounts()), ("contacts", client.contacts())):
        with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        log(f"  {name}: {len(rows)}")
    return client


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="CRM snapshot / connectivity check.")
    ap.add_argument("--snapshot", action="store_true", help="refresh data/*.json")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    a = ap.parse_args()

    c = CRMClient()
    print("auth:", c.me())
    if a.snapshot:
        print("snapshot:")
        snapshot(a.out, c)

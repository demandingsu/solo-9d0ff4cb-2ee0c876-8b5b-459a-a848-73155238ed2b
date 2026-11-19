"""Live end-to-end smoke test for the QC contamination ledger.

Runs against a started uvicorn instance and exercises the whole field
procedure in one script: equipment/jobs registration, two positive blank
sources, cross-contamination propagation, a cleaning, a late-reported job,
and retroactive corrections of both a blank and a cleaning.  It only uses
the public HTTP API, exactly like a field operator would through /docs.

Usage:
    python scripts/qc_smoke.py --base-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def call(base, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def must(status, body, expected, label):
    assert status == expected, f"{label}: expected {expected}, got {status} {body}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base_url
    ts = "2026-11-19"

    def sample(code, hour):
        status, body = call(base, "POST", "/samples", {
            "location": f"well-{code}",
            "collector": "Li Wei",
            "sampled_at_utc": f"{ts}T01:00:00Z",
            "sample_code": code,
        })
        must(status, body, 201, f"sample {code}")
        return body["id"]

    def equip(code, kind="pump"):
        status, body = call(base, "POST", "/equipment",
                            {"code": code, "kind": kind, "description": f"{kind} {code}"})
        must(status, body, 201, f"equipment {code}")
        return body["id"]

    def job(sid, at, codes, blank=False, blank_code=None):
        payload = {"sample_id": sid, "worked_at_utc": at,
                   "equipment_codes": codes, "blank_positive": blank}
        if blank_code:
            payload["blank_sample_code"] = blank_code
        status, body = call(base, "POST", "/sampling-jobs", payload)
        must(status, body, 201, f"job for {sid}")
        return body

    def review():
        status, body = call(base, "GET", "/qc/review")
        must(status, body, 200, "review")
        return body

    def pending_samples(r):
        return {s["sample_id"] for s in r["pending_samples"]}

    def equip_state(r, code):
        return next(e for e in r["pending_equipment"] if e["code"] == code)

    # --- facts -------------------------------------------------------------
    s1, s2, s3, s4, s5 = (sample(c, 1) for c in ("SMK-1", "SMK-2", "SMK-3", "SMK-4", "SMK-5"))
    p1 = equip("PUMP-S1")
    p2 = equip("PUMP-S2")
    t1 = equip("TUBE-S1", "tubing")
    t2 = equip("TUBE-S2", "tubing")

    # 02:00 source blank A: P1 + T1
    blank_a = job(s1, f"{ts}T02:00:00Z", ["PUMP-S1", "TUBE-S1"], blank=True, blank_code="BLANK-A")
    # 03:00 ordinary job reuses P1, picks up T2
    job(s2, f"{ts}T03:00:00Z", ["PUMP-S1", "TUBE-S2"])
    # 04:00 another job via the now-pending T2
    job(s3, f"{ts}T04:00:00Z", ["TUBE-S2", "PUMP-S2"])

    r = review()
    assert r["summary"]["source_blank_count"] == 1, r["summary"]
    assert pending_samples(r) == {s1, s2, s3}
    assert {e["code"] for e in r["pending_equipment"]} == {"PUMP-S1", "PUMP-S2", "TUBE-S1", "TUBE-S2"}
    print("step 1 (propagation):", r["summary"])

    # 05:00 second source blank B on P2 (independent of A's chain)
    blank_b = job(s4, f"{ts}T05:00:00Z", ["PUMP-S2"], blank=True, blank_code="BLANK-B")
    r = review()
    assert r["summary"]["source_blank_count"] == 2
    # B cannot travel backwards to s3 (04:00): s3 only carries source A
    s3_entry = next(s for s in r["pending_samples"] if s["sample_id"] == s3)
    assert {src["source_blank_job_id"] for src in s3_entry["sources"]} == {blank_a["id"]}
    print("step 2 (second source registered; propagation stays forward):", r["summary"])

    # wash T2 at 05:30: its active chains close, history stays
    status, body = call(base, "POST", f"/equipment/{t2}/cleanings",
                        {"cleaned_at_utc": f"{ts}T05:30:00Z"})
    must(status, body, 201, "cleaning T2")
    r = review()
    t2_state = equip_state(r, "TUBE-S2")
    assert t2_state["active"] is False
    assert len(t2_state["sources"]) == 1  # source A's historical path kept
    print("step 3 (cleaning closes active risk, keeps history):",
          {"active": t2_state["active"], "historical_sources": len(t2_state["sources"])})

    # late job at 06:00: P1 still carries A and P2 now carries B, so the job
    # inherits both independent source chains at once
    job(s5, f"{ts}T06:00:00Z", ["PUMP-S1", "PUMP-S2"])
    r = review()
    assert s5 in pending_samples(r)
    s5_entry = next(s for s in r["pending_samples"] if s["sample_id"] == s5)
    assert {src["source_blank_job_id"] for src in s5_entry["sources"]} == {
        blank_a["id"], blank_b["id"],
    }
    print("step 4 (late job inherits both live chains):", r["summary"])

    # correct blank A to negative: source B's paths must survive untouched
    status, body = call(base, "PATCH", f"/sampling-jobs/{blank_a['id']}", {
        "sample_id": s1,
        "worked_at_utc": f"{ts}T02:00:00Z",
        "equipment_codes": ["PUMP-S1", "TUBE-S1"],
        "blank_positive": False,
        "reason": "lab re-test: blank A negative, false positive withdrawn",
    })
    must(status, body, 200, "correct blank A")
    r = review()
    assert r["summary"]["source_blank_count"] == 1
    surviving = {s["sample_id"] for s in r["pending_samples"]}
    # A's whole chain disappears; B's source (s4) and the B leg into s5 remain
    assert surviving == {s4, s5}, surviving
    s5_after = next(s for s in r["pending_samples"] if s["sample_id"] == s5)
    assert {src["source_blank_job_id"] for src in s5_after["sources"]} == {blank_b["id"]}
    print("step 5 (blank A corrected away; B intact):", r["summary"])

    # move B's cleaning-equivalent: correct the earlier T2 cleaning earlier ->
    # here correct it later (07:00); no job between, so no re-propagation, but
    # the audit trail must show it
    status, body = call(base, "GET", f"/equipment/{t2}/cleanings/revisions")
    must(status, body, 200, "cleaning revisions")
    cleaning_id = body["revisions"][0]["cleaning_id"]
    status, body = call(base, "PATCH", f"/equipment/{t2}/cleanings/{cleaning_id}", {
        "cleaned_at_utc": f"{ts}T07:00:00Z",
        "reason": "wash bay ledger corrected: completion at 07:00",
    })
    must(status, body, 200, "correct cleaning")
    print("step 6 (cleaning correction audited):",
          {"actions": [x["action"] for x in call(base, "GET", f"/equipment/{t2}/cleanings/revisions")[1]["revisions"]]})

    # failed write leaves no half ledger: duplicate equipment on a job
    before = review()
    status, body = call(base, "PATCH", "/sampling-jobs/2", {
        "sample_id": s2,
        "worked_at_utc": f"{ts}T03:00:00Z",
        "equipment_codes": ["PUMP-S1", "GHOST-PUMP"],
        "reason": "should be rejected",
    })
    assert status == 422, (status, body)
    after = review()
    before.pop("generated_at_utc")
    after.pop("generated_at_utc")
    assert before == after, "failed correction changed the ledger"
    print("step 7 (rejected correction rolled back cleanly):", body["detail"]["code"])

    # concentrations delivered through the original workflow stay untouched
    status, meas = call(base, "POST", f"/samples/{s4}/measurements", {
        "lab": "Central Water Laboratory", "analyte": "nitrate-N",
        "concentration": 3.7, "unit": "mg/L",
    })
    must(status, meas, 201, "measurement")
    status, got = call(base, "GET", f"/samples/{s4}/measurements")
    must(status, got, 200, "read measurements")
    assert got["measurements"][0]["concentration"] == 3.7
    print("step 8 (concentration delivery unchanged):", got["measurements"][0])

    print(json.dumps({"ok": True, "final_summary": review()["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

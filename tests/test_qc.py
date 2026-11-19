"""End-to-end tests for the QC equipment-blank cross-contamination ledger.

Covers the field procedure end to end:

* a positive blank flags the source sample and all shared equipment;
* risk propagates through pending equipment to later jobs, their samples and
  the other equipment shared on those jobs;
* cleaning stops future propagation but keeps historical findings;
* two blank sources coexist and correcting one / cleaning one implement never
  erases the other source's paths;
* late-reported jobs and retroactive corrections recompute everything;
* failed writes leave no partial ledger; the concentration/delivery workflow
  is untouched.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GSB_DB_PATH", str(tmp_path / "test.sqlite3"))
    from app import db

    if hasattr(db, "_initialised"):
        db._initialised = False
    importlib.reload(db)
    from app import domain, main, qc, qc_routes

    importlib.reload(domain)
    importlib.reload(qc)
    importlib.reload(qc_routes)
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def register_sample(client, code, at="2026-11-18T01:00:00Z"):
    r = client.post(
        "/samples",
        json={"location": f"loc-{code}", "collector": "Li Wei",
              "sampled_at_utc": at, "sample_code": code},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_equipment(client, code, kind="pump"):
    r = client.post("/equipment", json={"code": code, "kind": kind})
    assert r.status_code == 201, r.text
    return r.json()


def job(client, sample_id, worked_at, codes, *, blank_positive=False, blank_sample_code=None):
    payload = {
        "sample_id": sample_id,
        "worked_at_utc": worked_at,
        "equipment_codes": codes,
        "blank_positive": blank_positive,
    }
    if blank_sample_code:
        payload["blank_sample_code"] = blank_sample_code
    r = client.post("/sampling-jobs", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def pending_sample_ids(review):
    return {s["sample_id"] for s in review["pending_samples"]}


def pending_equipment_codes(review):
    return {e["code"] for e in review["pending_equipment"]}


def active_equipment_codes(review):
    return {e["code"] for e in review["pending_equipment"] if e["active"]}


def sample_entry(review, sample_id):
    return next(s for s in review["pending_samples"] if s["sample_id"] == sample_id)


def equip_entry(review, code):
    return next(e for e in review["pending_equipment"] if e["code"] == code)


# ---------------------------------------------------------------------------
# Equipment basics
# ---------------------------------------------------------------------------

def test_equipment_registration_and_conflict(client):
    add_equipment(client, "PUMP-1")
    r = client.post("/equipment", json={"code": "PUMP-1", "kind": "pump"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "equipment_code_conflict"
    blank = client.post("/equipment", json={"code": "  ", "kind": "pump"})
    assert blank.status_code == 422
    listing = client.get("/equipment").json()
    assert listing["total"] == 1 and listing["items"][0]["status"] == "clean"


def test_unknown_equipment_rejected(client):
    sid = register_sample(client, "S1")
    r = client.post(
        "/sampling-jobs",
        json={"sample_id": sid, "worked_at_utc": "2026-11-18T02:00:00Z",
              "equipment_codes": ["GHOST"]},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_equipment"


def test_job_requires_blank_code_when_positive(client):
    sid = register_sample(client, "S1")
    add_equipment(client, "P1")
    r = client.post(
        "/sampling-jobs",
        json={"sample_id": sid, "worked_at_utc": "2026-11-18T02:00:00Z",
              "equipment_codes": ["P1"], "blank_positive": True},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "blank_code_required"
    # blank code without positive flag is also refused
    r2 = client.post(
        "/sampling-jobs",
        json={"sample_id": sid, "worked_at_utc": "2026-11-18T02:00:00Z",
              "equipment_codes": ["P1"], "blank_sample_code": "B9"},
    )
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "blank_code_without_positive"


def test_job_time_not_before_sampling(client):
    sid = register_sample(client, "S1")
    add_equipment(client, "P1")
    r = client.post(
        "/sampling-jobs",
        json={"sample_id": sid, "worked_at_utc": "2026-11-18T00:30:00Z",
              "equipment_codes": ["P1"]},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "job_not_after_sampling"


def test_one_job_per_sample_and_idempotent_cleaning(client):
    sid = register_sample(client, "S1")
    add_equipment(client, "P1")
    job(client, sid, "2026-11-18T02:00:00Z", ["P1"])
    dup = client.post(
        "/sampling-jobs",
        json={"sample_id": sid, "worked_at_utc": "2026-11-18T03:00:00Z",
              "equipment_codes": ["P1"]},
    )
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "sampling_job_already_exists"
    # identical cleaning retry is idempotent
    c1 = client.post("/equipment/1/cleanings", json={"cleaned_at_utc": "2026-11-18T05:00:00Z"})
    assert c1.status_code == 201
    c2 = client.post("/equipment/1/cleanings", json={"cleaned_at_utc": "2026-11-18T05:00:00Z"})
    assert c2.status_code == 200 and c2.json()["idempotent"] is True
    assert client.get("/equipment/1").json()["cleaning_count"] == 1


# ---------------------------------------------------------------------------
# Propagation
# ---------------------------------------------------------------------------

def test_positive_blank_flags_source_and_propagates_through_shared_equipment(client):
    # Timeline:
    #  02:00 J1 (S1): P1 + T1, blank positive          -> source
    #  03:00 J2 (S2): P1 + T2                           -> inherits via P1; T2 tainted
    #  04:00 J3 (S3): T2 + P2                           -> inherits via T2; P2 tainted
    #  05:00 J4 (S4): P9 (never near a blank)           -> clear
    s1 = register_sample(client, "S1")
    s2 = register_sample(client, "S2")
    s3 = register_sample(client, "S3")
    s4 = register_sample(client, "S4")
    for code, kind in [("P1", "pump"), ("P2", "pump"), ("P9", "pump"),
                       ("T1", "tubing"), ("T2", "tubing")]:
        add_equipment(client, code, kind)

    job(client, s1, "2026-11-18T02:00:00Z", ["P1", "T1"],
        blank_positive=True, blank_sample_code="BLANK-1")
    job(client, s2, "2026-11-18T03:00:00Z", ["P1", "T2"])
    job(client, s3, "2026-11-18T04:00:00Z", ["T2", "P2"])
    job(client, s4, "2026-11-18T05:00:00Z", ["P9"])

    review = client.get("/qc/review").json()
    assert review["summary"]["source_blank_count"] == 1
    assert pending_sample_ids(review) == {s1, s2, s3}
    assert pending_equipment_codes(review) == {"P1", "T1", "T2", "P2"}
    assert "P9" not in pending_equipment_codes(review)
    # nothing cleaned yet: every flagged implement is still active
    assert active_equipment_codes(review) == {"P1", "T1", "T2", "P2"}

    # evidence: S3's full chain runs from the source job through J2 into J3
    s3_entry = sample_entry(review, s3)
    assert len(s3_entry["sources"]) == 1
    path = s3_entry["sources"][0]["paths"][0]
    assert [hop["sample_id"] for hop in path] == [s1, s2, s3]
    assert path[0]["equipment_ids"] and path[-1]["equipment_ids"]

    # the source sample is marked as the blank origin
    s1_entry = sample_entry(review, s1)
    assert s1_entry["is_positive_blank_source"] is True
    assert s1_entry["sources"][0]["is_source"] is True

    # sample detail carries the QC summary; concentrations are unaffected
    detail = client.get(f"/samples/{s3}").json()
    assert detail["qc"]["qc_status"] == "pending_review"
    assert detail["qc"]["risk_source_job_ids"] == [1]
    assert client.get(f"/samples/{s4}").json()["qc"]["qc_status"] == "clear"

    # job view
    j3 = client.get(f"/samples/{s3}/sampling-job").json()
    assert j3["qc_status"] == "pending_review"
    assert {e["code"] for e in j3["equipment"]} == {"T2", "P2"}


def test_cleaning_stops_future_propagation_but_keeps_history(client):
    s1 = register_sample(client, "S1")
    s2 = register_sample(client, "S2")
    s3 = register_sample(client, "S3")
    add_equipment(client, "P1")
    add_equipment(client, "T1")
    add_equipment(client, "T2")

    job(client, s1, "2026-11-18T02:00:00Z", ["P1", "T1"],
        blank_positive=True, blank_sample_code="BLANK-1")
    job(client, s2, "2026-11-18T03:00:00Z", ["P1", "T2"])  # inherits, taints T2
    # clean P1 before the next job
    cleaned = client.post("/equipment/1/cleanings",
                          json={"cleaned_at_utc": "2026-11-18T03:30:00Z"})
    assert cleaned.status_code == 201
    job(client, s3, "2026-11-18T04:00:00Z", ["P1"])  # P1 clean -> no risk

    review = client.get("/qc/review").json()
    assert pending_sample_ids(review) == {s1, s2}
    # P1 stays listed historically but is no longer active; T2 was never cleaned
    assert "P1" in pending_equipment_codes(review)
    assert active_equipment_codes(review) == {"T1", "T2"}
    p1 = equip_entry(review, "P1")
    assert p1["active"] is False
    assert p1["last_cleaned_at_utc"] == "2026-11-18T03:30:00.000000Z"
    # status filter on the equipment list
    active = client.get("/equipment", params={"status": "pending_review"}).json()["items"]
    assert {e["code"] for e in active} == {"T1", "T2"}


def test_recontamination_after_cleaning_adds_path_without_erasing_first(client):
    s1 = register_sample(client, "S1")
    s2 = register_sample(client, "S2")
    s3 = register_sample(client, "S3")
    add_equipment(client, "P1")
    add_equipment(client, "T1")
    add_equipment(client, "T2")

    job(client, s1, "2026-11-18T02:00:00Z", ["P1", "T1"],
        blank_positive=True, blank_sample_code="BLANK-1")
    job(client, s2, "2026-11-18T03:00:00Z", ["P1", "T2"])
    client.post("/equipment/1/cleanings", json={"cleaned_at_utc": "2026-11-18T03:30:00Z"})
    # T2 is still pending; a job using T2 + P1 re-contaminates the clean P1
    job(client, s3, "2026-11-18T04:00:00Z", ["T2", "P1"])

    review = client.get("/qc/review").json()
    p1 = equip_entry(review, "P1")
    assert p1["active"] is True  # re-contaminated after the cleaning
    paths = p1["sources"][0]["paths"]
    # source exposure (J1), the J2 hop and the post-cleaning re-contamination (J3)
    assert len(paths) == 3
    chain_samples = sorted([hop["sample_id"] for hop in chain] for chain in paths)
    assert chain_samples == [[s1], [s1, s2], [s1, s2, s3]]


def test_multiple_sources_keep_independent_paths(client):
    # Source A at 02:00 (P1/T1), source B at 05:00 (P2/T3).
    s_a = register_sample(client, "SA")
    s_b = register_sample(client, "SB")
    s_mid = register_sample(client, "SM")
    s_late = register_sample(client, "SL")
    for code in ["P1", "P2", "T1", "T2", "T3"]:
        add_equipment(client, code, "tubing" if code.startswith("T") else "pump")

    job(client, s_a, "2026-11-18T02:00:00Z", ["P1", "T1"],
        blank_positive=True, blank_sample_code="BLANK-A")
    job(client, s_mid, "2026-11-18T03:00:00Z", ["P1", "T2"])
    job(client, s_b, "2026-11-18T05:00:00Z", ["P2", "T3"],
        blank_positive=True, blank_sample_code="BLANK-B")
    job(client, s_late, "2026-11-18T06:00:00Z", ["T2", "T3"])  # hits both source chains

    review = client.get("/qc/review").json()
    assert review["summary"]["source_blank_count"] == 2
    assert {s["source_blank_job_id"] for s in review["source_blanks"]} == {1, 3}

    late = sample_entry(review, s_late)
    source_ids = {src["source_blank_job_id"] for src in late["sources"]}
    assert source_ids == {1, 3}  # two independent paths to the same sample

    mid = sample_entry(review, s_mid)
    assert {src["source_blank_job_id"] for src in mid["sources"]} == {1}

    # Correct source A to negative: source B's paths must survive untouched.
    corr = client.patch(
        "/sampling-jobs/1",
        json={
            "sample_id": s_a,
            "worked_at_utc": "2026-11-18T02:00:00Z",
            "equipment_codes": ["P1", "T1"],
            "blank_positive": False,
            "reason": "lab re-ran the blank: negative, false positive withdrawn",
        },
    )
    assert corr.status_code == 200, corr.text
    review2 = client.get("/qc/review").json()
    assert review2["summary"]["source_blank_count"] == 1
    assert pending_sample_ids(review2) == {s_b, s_late}
    late2 = sample_entry(review2, s_late)
    assert {src["source_blank_job_id"] for src in late2["sources"]} == {3}
    # the correction is audited
    audit = client.get("/sampling-jobs/revisions/audit").json()
    actions = [(i["job_id"], i["action"]) for i in audit["items"]]
    assert (1, "correct") in actions
    assert audit["items"][-1]["reason"].startswith("lab re-ran")


def test_cleaning_one_implement_does_not_erase_other_source(client):
    s_a = register_sample(client, "SA")
    s_b = register_sample(client, "SB")
    s_x = register_sample(client, "SX")
    add_equipment(client, "P1")
    add_equipment(client, "P2")
    add_equipment(client, "T1")

    job(client, s_a, "2026-11-18T02:00:00Z", ["P1"],
        blank_positive=True, blank_sample_code="BLANK-A")
    job(client, s_b, "2026-11-18T03:00:00Z", ["P2"],
        blank_positive=True, blank_sample_code="BLANK-B")
    job(client, s_x, "2026-11-18T04:00:00Z", ["P1", "P2", "T1"])

    # clean P1 only: P1 stops carrying every source's risk, but its historical
    # paths from both sources remain and source B is still active on T1/P2
    client.post("/equipment/1/cleanings", json={"cleaned_at_utc": "2026-11-18T04:30:00Z"})
    review = client.get("/qc/review").json()
    p1 = equip_entry(review, "P1")
    assert {src["source_blank_job_id"] for src in p1["sources"]} == {1, 2}
    assert p1["active"] is False  # cleaning closed P1 for all sources
    assert all(src["active"] is False for src in p1["sources"])
    t1 = equip_entry(review, "T1")
    assert {src["source_blank_job_id"] for src in t1["sources"]} == {1, 2}
    assert t1["active"] is True  # T1 was never washed; B's chain is live there

    # a later job using T1 still inherits BOTH sources, proving B survived
    s_y = register_sample(client, "SY")
    job(client, s_y, "2026-11-18T05:00:00Z", ["T1"])
    review2 = client.get("/qc/review").json()
    sy = sample_entry(review2, s_y)
    assert {src["source_blank_job_id"] for src in sy["sources"]} == {1, 2}


# ---------------------------------------------------------------------------
# Late reports and retroactive corrections
# ---------------------------------------------------------------------------

def test_late_job_inserted_into_timeline_recomputes_propagation(client):
    # Report out of chronological order: J3 first, then J1, then the missing J2.
    s1 = register_sample(client, "S1")
    s2 = register_sample(client, "S2")
    s3 = register_sample(client, "S3")
    add_equipment(client, "P1")
    add_equipment(client, "T1")

    job(client, s3, "2026-11-18T04:00:00Z", ["T1"])
    job(client, s1, "2026-11-18T02:00:00Z", ["P1"],
        blank_positive=True, blank_sample_code="BLANK-1")
    assert client.get("/qc/review").json()["summary"]["pending_sample_count"] == 1

    # the late middle job bridges P1 -> T1 and pulls S3 under review
    job(client, s2, "2026-11-18T03:00:00Z", ["P1", "T1"])
    review = client.get("/qc/review").json()
    assert pending_sample_ids(review) == {s1, s2, s3}
    s3_entry = sample_entry(review, s3)
    assert [hop["sample_id"] for hop in s3_entry["sources"][0]["paths"][0]] == [s1, s2, s3]


def test_retroactive_blank_correction_and_job_time_correction(client):
    s1 = register_sample(client, "S1")
    s2 = register_sample(client, "S2")
    add_equipment(client, "P1")
    add_equipment(client, "T1")
    created = job(client, s1, "2026-11-18T02:00:00Z", ["P1", "T1"])
    job(client, s2, "2026-11-18T03:00:00Z", ["P1"])
    assert client.get("/qc/review").json()["summary"]["source_blank_count"] == 0

    # lab result arrives late: correct J1 into a positive blank -> recompute
    corr = client.patch(
        f"/sampling-jobs/{created['id']}",
        json={
            "sample_id": s1,
            "worked_at_utc": "2026-11-18T02:00:00Z",
            "equipment_codes": ["P1", "T1"],
            "blank_positive": True,
            "blank_sample_code": "BLANK-9",
            "reason": "positive blank result received from lab",
        },
    )
    assert corr.status_code == 200, corr.text
    review = client.get("/qc/review").json()
    assert pending_sample_ids(review) == {s1, s2}

    # moving the source job AFTER s2's job removes s2 from the path
    move = client.patch(
        f"/sampling-jobs/{created['id']}",
        json={
            "sample_id": s1,
            "worked_at_utc": "2026-11-18T03:30:00Z",
            "equipment_codes": ["P1", "T1"],
            "blank_positive": True,
            "blank_sample_code": "BLANK-9",
            "reason": "field log shows the blank run happened at 03:30",
        },
    )
    assert move.status_code == 200, move.text
    review2 = client.get("/qc/review").json()
    assert pending_sample_ids(review2) == {s1}


def test_cleaning_correction_reopens_and_closes_propagation(client):
    s1 = register_sample(client, "S1")
    s2 = register_sample(client, "S2")
    add_equipment(client, "P1")
    job(client, s1, "2026-11-18T02:00:00Z", ["P1"],
        blank_positive=True, blank_sample_code="BLANK-1")
    # cleaning wrongly reported at 02:30, before the 03:00 job
    cleaning = client.post("/equipment/1/cleanings",
                           json={"cleaned_at_utc": "2026-11-18T02:30:00Z"}).json()
    job(client, s2, "2026-11-18T03:00:00Z", ["P1"])
    review = client.get("/qc/review").json()
    assert pending_sample_ids(review) == {s1}  # cleaning protected the later job

    # correct the cleaning to 03:30: S2 must now be pulled under review
    corr = client.patch(
        f"/equipment/1/cleanings/{cleaning['cleaning_id']}",
        json={"cleaned_at_utc": "2026-11-18T03:30:00Z",
              "reason": "wash bay log shows 03:30 completion"},
    )
    assert corr.status_code == 200, corr.text
    review2 = client.get("/qc/review").json()
    assert pending_sample_ids(review2) == {s1, s2}
    revs = client.get("/equipment/1/cleanings/revisions").json()["revisions"]
    assert [r["action"] for r in revs] == ["create", "correct"]
    assert revs[-1]["reason"].startswith("wash bay log")


def test_noop_correction_is_not_substantive(client):
    s1 = register_sample(client, "S1")
    add_equipment(client, "P1")
    created = job(client, s1, "2026-11-18T02:00:00Z", ["P1"])
    r = client.patch(
        f"/sampling-jobs/{created['id']}",
        json={"sample_id": s1, "worked_at_utc": "2026-11-18T02:00:00Z",
              "equipment_codes": ["P1"], "reason": "resubmission with same content"},
    )
    assert r.status_code == 200
    assert r.json()["substantive"] is False


def test_job_cannot_be_moved_to_another_sample(client):
    s1 = register_sample(client, "S1")
    s2 = register_sample(client, "S2")
    add_equipment(client, "P1")
    created = job(client, s1, "2026-11-18T02:00:00Z", ["P1"])
    r = client.patch(
        f"/sampling-jobs/{created['id']}",
        json={"sample_id": s2, "worked_at_utc": "2026-11-18T02:00:00Z",
              "equipment_codes": ["P1"], "reason": "attempted reassignment"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "job_sample_mismatch"


def test_failed_correction_leaves_no_partial_ledger(client):
    s1 = register_sample(client, "S1")
    s2 = register_sample(client, "S2")
    add_equipment(client, "P1")
    created = job(client, s1, "2026-11-18T02:00:00Z", ["P1"],
                  blank_positive=True, blank_sample_code="BLANK-1")
    job(client, s2, "2026-11-18T03:00:00Z", ["P1"])
    before = client.get("/qc/review").json()

    # invalid correction: unknown equipment code -> 422, whole tx rolls back
    bad = client.patch(
        f"/sampling-jobs/{created['id']}",
        json={"sample_id": s1, "worked_at_utc": "2026-11-18T02:00:00Z",
              "equipment_codes": ["P1", "MISSING"], "blank_positive": True,
              "blank_sample_code": "BLANK-1", "reason": "attempted bad edit"},
    )
    assert bad.status_code == 422
    after = client.get("/qc/review").json()
    before.pop("generated_at_utc")
    after.pop("generated_at_utc")
    assert after == before
    # no audit row for the failed correction
    audit = client.get("/sampling-jobs/revisions/audit").json()
    assert all(i["action"] != "correct" for i in audit["items"])
    # equipment link table unchanged: still one implement on job 1
    job_view = client.get(f"/sampling-jobs/{created['id']}").json()
    assert [e["code"] for e in job_view["equipment"]] == ["P1"]


def test_delivery_and_concentration_workflow_unchanged(client):
    # QC data must not alter delivery eligibility or delivered concentrations.
    s1 = register_sample(client, "S1")
    add_equipment(client, "P1")
    job(client, s1, "2026-11-18T02:00:00Z", ["P1"],
        blank_positive=True, blank_sample_code="BLANK-1")
    m = client.post(
        f"/samples/{s1}/measurements",
        json={"lab": "Central Water Laboratory", "analyte": "nitrate-N",
              "concentration": 4.2, "unit": "mg/L"},
    )
    assert m.status_code == 201
    # sample without custody chain stays ineligible, same as before
    d = client.post("/deliveries", json={"sample_date": "2026-11-18"})
    assert d.status_code == 409
    assert d.json()["detail"]["code"] == "no_eligible_samples"


def test_docs_and_openapi_expose_qc_endpoints(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    for path in ["/equipment", "/equipment/{equipment_id}/cleanings",
                 "/sampling-jobs", "/sampling-jobs/{job_id}", "/qc/review"]:
        assert path in schema["paths"]

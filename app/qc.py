"""Quality-control domain: equipment-blank cross-contamination tracing.

The QC ledger stores only reported *facts*:

* reusable equipment (pumps, tubing lines);
* cleanings, each with a completion time;
* sampling jobs, each tied to one registered sample, a UTC working time, the
  equipment shared on that job and an optional positive equipment blank;
* revision rows for every correction.

Risk itself is never hand-maintained.  After every accepted write,
:func:`recompute_qc_findings` rebuilds the derived ``qc_findings`` table from
the full fact ledger, inside the same SQLite transaction as the write.  This
gives three properties the field procedure requires:

1. **Propagation through shared equipment.**  A positive blank marks the
   source job's sample and every piece of its equipment for review.  A later
   job that uses still-pending equipment inherits the risk for its own sample
   and for every other piece of equipment shared on that job.  Risk flows
   forward in working time until each affected implement is cleaned.
2. **History is retained.**  Cleaning an implement only stops *later* jobs
   from inheriting that source's risk; findings already propagated stay in the
   ledger.  Re-contamination after a cleaning adds a new path instead of
   overwriting the old one.
3. **Multiple sources never erase each other.**  Each positive blank is
   simulated independently, so correcting one blank or cleaning one
   implement cannot remove another blank's paths.

Tie handling: events are processed in (event time, event kind, row id) order.
Cleanings at exactly a job's working time protect that job (a cleaning at t
covers jobs worked at t), while jobs at the same instant are processed in
registration order.  A full chronological sweep means late-reported jobs and
retroactive corrections are handled by exactly the same code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import domain


# ---------------------------------------------------------------------------
# Fact loading
# ---------------------------------------------------------------------------

def row_to_equipment(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "kind": row["kind"],
        "description": row["description"],
        "created_at_utc": row["created_at_utc"],
    }


def get_equipment_by_id(conn, equipment_id: int):
    row = conn.execute("SELECT * FROM equipment WHERE id = ?", (equipment_id,)).fetchone()
    if row is None:
        raise domain.ApiError(404, "equipment_not_found", f"equipment {equipment_id} does not exist")
    return row


def get_equipment_by_code(conn, code: str):
    return conn.execute("SELECT * FROM equipment WHERE code = ?", (code,)).fetchone()


def get_job(conn, job_id: int):
    row = conn.execute("SELECT * FROM sampling_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise domain.ApiError(404, "sampling_job_not_found", f"sampling job {job_id} does not exist")
    return row


def get_job_for_sample(conn, sample_id: int):
    return conn.execute(
        "SELECT * FROM sampling_jobs WHERE sample_id = ?", (sample_id,)
    ).fetchone()


def job_equipment(conn, job_id: int) -> list:
    return conn.execute(
        "SELECT e.* FROM sampling_job_equipment je JOIN equipment e ON e.id = je.equipment_id"
        " WHERE je.job_id = ? ORDER BY je.position ASC, je.id ASC",
        (job_id,),
    ).fetchall()


def cleaning_hash(cleaned_at_iso: str) -> str:
    return domain.sha256_text(domain.canonical_json({"cleaned_at_utc": cleaned_at_iso}))


def job_hash(worked_at_iso: str, equipment_ids: list[int], blank_positive: bool,
             blank_sample_code: str | None) -> str:
    return domain.sha256_text(
        domain.canonical_json(
            {
                "worked_at_utc": worked_at_iso,
                "equipment_ids": sorted(equipment_ids),
                "blank_positive": bool(blank_positive),
                "blank_sample_code": blank_sample_code,
            }
        )
    )


# ---------------------------------------------------------------------------
# Derived risk computation
# ---------------------------------------------------------------------------

@dataclass
class _Event:
    kind: str                 # 'job' | 'cleaning'
    time: str                 # canonical ISO UTC, lexicographically sortable
    order_id: int
    job_id: int | None = None
    sample_id: int | None = None
    equipment_ids: tuple[int, ...] = ()
    blank_positive: bool = False


def _load_events(conn) -> list[_Event]:
    events: list[_Event] = []
    jobs = conn.execute("SELECT * FROM sampling_jobs ORDER BY id ASC").fetchall()
    for job in jobs:
        equip = tuple(
            r["equipment_id"]
            for r in conn.execute(
                "SELECT equipment_id FROM sampling_job_equipment WHERE job_id = ?"
                " ORDER BY position ASC, id ASC",
                (job["id"],),
            ).fetchall()
        )
        events.append(
            _Event(
                kind="job",
                time=job["worked_at_utc"],
                order_id=job["id"],
                job_id=job["id"],
                sample_id=job["sample_id"],
                equipment_ids=equip,
                blank_positive=bool(job["blank_positive"]),
            )
        )
    for c in conn.execute("SELECT * FROM cleanings ORDER BY id ASC").fetchall():
        events.append(
            _Event(
                kind="cleaning",
                time=c["cleaned_at_utc"],
                order_id=c["id"],
                equipment_ids=(c["equipment_id"],),
            )
        )
    # (time, kind, id): 'cleaning' < 'job' so a cleaning at t is applied before
    # any job worked at t.  Canonical UTC strings sort chronologically.
    events.sort(key=lambda e: (e.time, e.kind, e.order_id))
    return events


def _hop(event: _Event, equipment_ids: tuple[int, ...]) -> dict[str, Any]:
    return {
        "job_id": event.job_id,
        "sample_id": event.sample_id,
        "worked_at_utc": event.time,
        "equipment_ids": list(equipment_ids),
    }


def recompute_qc_findings(conn) -> dict[str, Any]:
    """Rebuild ``qc_findings`` from the complete fact ledger.

    Runs inside the caller's open write transaction; the caller commits or
    rolls back atomically.  Returns a small summary of what was derived.
    """
    events = _load_events(conn)
    sources = [e for e in events if e.kind == "job" and e.blank_positive]

    conn.execute("DELETE FROM qc_findings")

    finding_count = 0
    for source in sources:
        # Every subject keeps the full chain(s) of hops reaching it from this
        # source.  ``risky`` holds the chains still active on each implement
        # (cleaning drops them); equip_paths retains every chain ever formed so
        # incurred risk survives washing.  Chains are stored as canonical JSON
        # strings so they hash and dedupe cleanly.
        sample_paths: dict[int, set[str]] = {}
        equip_paths: dict[int, set[str]] = {}
        sample_source: set[int] = set()
        equip_source: set[int] = set()
        risky: dict[int, set[str]] = {}

        def add(target: dict[int, set[str]], key: int, chain: list[dict]) -> None:
            target.setdefault(key, set()).add(domain.canonical_json(chain))

        started = False
        for event in events:
            if not started:
                if event.kind == "job" and event.job_id == source.job_id:
                    started = True
                else:
                    continue

            if event.kind == "cleaning":
                # Washing removes the implement's *active* chains for every
                # source; chains already recorded in history are untouched.
                risky.pop(event.equipment_ids[0], None)
                continue

            equip = event.equipment_ids
            if event.job_id == source.job_id:
                # The positive blank itself: the one-hop chain starts here.
                chain = [_hop(event, equip)]
                sample_source.add(event.sample_id)
                add(sample_paths, event.sample_id, chain)
                for eq in equip:
                    equip_source.add(eq)
                    add(equip_paths, eq, chain)
                    risky.setdefault(eq, set()).add(domain.canonical_json(chain))
                continue

            incoming: set[str] = set()
            for eq in equip:
                incoming.update(risky.get(eq, ()))
            if incoming:
                # Each active chain extends through this job.  The job's sample
                # and every co-used implement inherit every extended chain.
                for chain_json in incoming:
                    chain = domain.json.loads(chain_json)
                    chain.append(_hop(event, equip))
                    extended = domain.canonical_json(chain)
                    add(sample_paths, event.sample_id, chain)
                    for shared in equip:
                        add(equip_paths, shared, chain)
                        risky.setdefault(shared, set()).add(extended)

        for sample_id, chain_set in sample_paths.items():
            paths = [domain.json.loads(p) for p in sorted(chain_set)]
            conn.execute(
                "INSERT INTO qc_findings (source_blank_job_id, subject_type, sample_id, equipment_id,"
                " is_source, active, paths_json) VALUES (?, 'sample', ?, NULL, ?, 1, ?)",
                (
                    source.job_id,
                    sample_id,
                    int(sample_id in sample_source),
                    domain.canonical_json(paths),
                ),
            )
            finding_count += 1
        for equip_id, chain_set in equip_paths.items():
            paths = [domain.json.loads(p) for p in sorted(chain_set)]
            conn.execute(
                "INSERT INTO qc_findings (source_blank_job_id, subject_type, sample_id, equipment_id,"
                " is_source, active, paths_json) VALUES (?, 'equipment', NULL, ?, ?, ?, ?)",
                (
                    source.job_id,
                    equip_id,
                    int(equip_id in equip_source),
                    int(bool(risky.get(equip_id))),
                    domain.canonical_json(paths),
                ),
            )
            finding_count += 1

    return {"source_blank_count": len(sources), "finding_count": finding_count}


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------

def _finding_paths(row) -> list[list[dict[str, Any]]]:
    return domain.json.loads(row["paths_json"])


def _annotate_equipment(conn, equipment_id: int) -> dict[str, Any]:
    eq = get_equipment_by_id(conn, equipment_id)
    findings = conn.execute(
        "SELECT * FROM qc_findings WHERE subject_type = 'equipment' AND equipment_id = ?"
        " ORDER BY source_blank_job_id ASC",
        (equipment_id,),
    ).fetchall()
    cleanings = conn.execute(
        "SELECT * FROM cleanings WHERE equipment_id = ? ORDER BY cleaned_at_utc ASC, id ASC",
        (equipment_id,),
    ).fetchall()
    active_sources = [f["source_blank_job_id"] for f in findings if f["active"]]
    return {
        **row_to_equipment(eq),
        "status": "pending_review" if active_sources else "clean",
        "active_risk_source_job_ids": active_sources,
        "finding_count": len(findings),
        "cleaning_count": len(cleanings),
        "last_cleaned_at_utc": cleanings[-1]["cleaned_at_utc"] if cleanings else None,
    }


def job_public(conn, job_row) -> dict[str, Any]:
    equip_rows = job_equipment(conn, job_row["id"])
    sample = domain.get_sample(conn, job_row["sample_id"])
    findings = conn.execute(
        "SELECT * FROM qc_findings WHERE subject_type = 'sample' AND sample_id = ?",
        (job_row["sample_id"],),
    ).fetchall()
    source_for = conn.execute(
        "SELECT id FROM sampling_jobs WHERE sample_id = ? AND blank_positive = 1",
        (job_row["sample_id"],),
    ).fetchall()
    return {
        "id": job_row["id"],
        "sample_id": job_row["sample_id"],
        "sample_code": sample["sample_code"],
        "worked_at_utc": job_row["worked_at_utc"],
        "blank_positive": bool(job_row["blank_positive"]),
        "blank_sample_code": job_row["blank_sample_code"],
        "equipment": [row_to_equipment(e) for e in equip_rows],
        "qc_status": "pending_review" if findings else "clear",
        "is_positive_blank_source": bool(source_for),
        "risk_source_job_ids": sorted({f["source_blank_job_id"] for f in findings}),
        "created_at_utc": job_row["created_at_utc"],
    }


def sample_qc_summary(conn, sample_id: int) -> dict[str, Any]:
    findings = conn.execute(
        "SELECT * FROM qc_findings WHERE subject_type = 'sample' AND sample_id = ?"
        " ORDER BY source_blank_job_id ASC",
        (sample_id,),
    ).fetchall()
    return {
        "qc_status": "pending_review" if findings else "clear",
        "risk_source_job_ids": sorted({f["source_blank_job_id"] for f in findings}),
        "finding_count": len(findings),
    }


def review_report(conn) -> dict[str, Any]:
    """Full review listing: source blanks, pending samples, pending equipment.

    Every row is read in one snapshot transaction, so the report and the
    evidence it cites always come from the same committed ledger state.
    """
    source_jobs = conn.execute(
        "SELECT * FROM sampling_jobs WHERE blank_positive = 1 ORDER BY worked_at_utc ASC, id ASC"
    ).fetchall()

    sources: list[dict[str, Any]] = []
    for job in source_jobs:
        sample = domain.get_sample(conn, job["sample_id"])
        sources.append(
            {
                "source_blank_job_id": job["id"],
                "sample_id": job["sample_id"],
                "sample_code": sample["sample_code"],
                "blank_sample_code": job["blank_sample_code"],
                "worked_at_utc": job["worked_at_utc"],
                "equipment": [
                    row_to_equipment(e) for e in job_equipment(conn, job["id"])
                ],
            }
        )

    pending_samples: list[dict[str, Any]] = []
    sample_rows = conn.execute(
        "SELECT f.* FROM qc_findings f WHERE f.subject_type = 'sample'"
        " ORDER BY f.sample_id ASC, f.source_blank_job_id ASC"
    ).fetchall()
    by_sample: dict[int, list] = {}
    for row in sample_rows:
        by_sample.setdefault(row["sample_id"], []).append(row)
    for sample_id, rows in by_sample.items():
        sample = domain.get_sample(conn, sample_id)
        job = get_job_for_sample(conn, sample_id)
        pending_samples.append(
            {
                "sample_id": sample_id,
                "sample_code": sample["sample_code"],
                "location": sample["location"],
                "sampled_at_utc": sample["sampled_at_utc"],
                "worked_at_utc": job["worked_at_utc"] if job else None,
                "is_positive_blank_source": any(r["is_source"] for r in rows),
                "active": True,
                "sources": [
                    {
                        "source_blank_job_id": r["source_blank_job_id"],
                        "is_source": bool(r["is_source"]),
                        "paths": _finding_paths(r),
                    }
                    for r in rows
                ],
            }
        )

    pending_equipment: list[dict[str, Any]] = []
    equip_rows = conn.execute(
        "SELECT f.* FROM qc_findings f WHERE f.subject_type = 'equipment'"
        " ORDER BY f.equipment_id ASC, f.source_blank_job_id ASC"
    ).fetchall()
    by_equip: dict[int, list] = {}
    for row in equip_rows:
        by_equip.setdefault(row["equipment_id"], []).append(row)
    for equipment_id, rows in by_equip.items():
        annotated = _annotate_equipment(conn, equipment_id)
        pending_equipment.append(
            {
                **annotated,
                "active": annotated["status"] == "pending_review",
                "sources": [
                    {
                        "source_blank_job_id": r["source_blank_job_id"],
                        "is_source": bool(r["is_source"]),
                        "active": bool(r["active"]),
                        "paths": _finding_paths(r),
                    }
                    for r in rows
                ],
            }
        )
    # Active (still uncleaned) first, then equipment whose risk is historical.
    pending_equipment.sort(key=lambda e: (not e["active"], e["code"]))

    return {
        "generated_at_utc": domain.utc_now_iso(),
        "summary": {
            "source_blank_count": len(sources),
            "pending_sample_count": len(pending_samples),
            "pending_equipment_count": len(pending_equipment),
            "active_equipment_count": sum(1 for e in pending_equipment if e["active"]),
        },
        "source_blanks": sources,
        "pending_samples": pending_samples,
        "pending_equipment": pending_equipment,
    }

"""HTTP routes for the QC equipment-blank cross-contamination ledger.

All writes follow the same discipline as the rest of the service: validate
inside one ``BEGIN IMMEDIATE`` transaction, mutate the fact ledger, rebuild the
derived ``qc_findings`` table in the same transaction and commit once.  Any
error rolls the whole request back, so a rejected late report or correction
never leaves a half-written ledger.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from . import db, domain, qc
from .domain import ApiError

router = APIRouter(tags=["quality-control"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EquipmentCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"code": "PUMP-07", "kind": "pump", "description": "peristaltic pump 07"}
        }
    )
    code: str = Field(min_length=1, max_length=100)
    kind: str = Field(min_length=1, max_length=50, description="e.g. pump or tubing")
    description: str | None = Field(default=None, max_length=500)


class CleaningCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"cleaned_at_utc": "2026-11-18T09:30:00Z"}}
    )
    cleaned_at_utc: str = Field(description="ISO-8601 timestamp with explicit UTC offset")


class CleaningCorrect(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "cleaned_at_utc": "2026-11-18T09:45:00Z",
                "reason": "wash log shows completion at 09:45, not 09:30",
            }
        }
    )
    cleaned_at_utc: str
    reason: str = Field(min_length=1, max_length=1000)


class JobCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "sample_id": 1,
                "worked_at_utc": "2026-11-18T02:15:00Z",
                "equipment_codes": ["PUMP-07", "TUBE-03"],
                "blank_positive": False,
            }
        }
    )
    sample_id: int = Field(ge=1)
    worked_at_utc: str = Field(description="UTC time the sampling job was performed")
    equipment_codes: list[str] = Field(min_length=1, description="Pump and tubing used, in use order")
    blank_positive: bool = Field(default=False)
    blank_sample_code: str | None = Field(
        default=None,
        max_length=100,
        description="Code of the positive equipment blank; required (and only allowed) when blank_positive is true",
    )


class JobCorrect(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "sample_id": 1,
                "worked_at_utc": "2026-11-18T02:20:00Z",
                "equipment_codes": ["PUMP-07", "TUBE-03"],
                "blank_positive": True,
                "blank_sample_code": "BLANK-20261118-02",
                "reason": "blank result arrived from the lab and the job time was misread",
            }
        }
    )
    sample_id: int = Field(ge=1, description="Must equal the sample the job is already attached to")
    worked_at_utc: str
    equipment_codes: list[str] = Field(min_length=1)
    blank_positive: bool = False
    blank_sample_code: str | None = Field(default=None, max_length=100)
    reason: str = Field(min_length=1, max_length=1000, description="Mandatory: corrections are explicit and auditable")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _clean(value: str, field_name: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ApiError(422, "invalid_field", f"{field_name} must not be blank")
    return trimmed


def _resolve_equipment(conn, codes: list[str]) -> list[int]:
    cleaned = [_clean(c, "equipment_codes") for c in codes]
    if len(set(cleaned)) != len(cleaned):
        raise ApiError(422, "duplicate_equipment", "equipment_codes must not contain duplicates")
    ids: list[int] = []
    for code in cleaned:
        row = qc.get_equipment_by_code(conn, code)
        if row is None:
            raise ApiError(422, "unknown_equipment", f"equipment code '{code}' is not registered")
        ids.append(int(row["id"]))
    return ids


def _validate_blank_fields(blank_positive: bool, blank_sample_code: str | None) -> str | None:
    code = _clean(blank_sample_code, "blank_sample_code") if blank_sample_code else None
    if blank_positive and not code:
        raise ApiError(
            422,
            "blank_code_required",
            "blank_sample_code is required when blank_positive is true, so the source blank stays identifiable",
        )
    if not blank_positive and code is not None:
        raise ApiError(
            422,
            "blank_code_without_positive",
            "blank_sample_code can only be supplied together with blank_positive=true",
        )
    return code


# ---------------------------------------------------------------------------
# Equipment
# ---------------------------------------------------------------------------

@router.post("/equipment", status_code=status.HTTP_201_CREATED, summary="Register a pump or tubing line")
def create_equipment(body: EquipmentCreate) -> dict[str, Any]:
    code = _clean(body.code, "code")
    kind = _clean(body.kind, "kind")
    description = _clean(body.description, "description") if body.description else None
    now = domain.utc_now_iso()
    with db.write_tx() as conn:
        if qc.get_equipment_by_code(conn, code) is not None:
            raise ApiError(409, "equipment_code_conflict", f"equipment code '{code}' already exists")
        cur = conn.execute(
            "INSERT INTO equipment (code, kind, description, created_at_utc) VALUES (?, ?, ?, ?)",
            (code, kind, description, now),
        )
        row = qc.get_equipment_by_id(conn, int(cur.lastrowid))
        result = qc._annotate_equipment(conn, int(cur.lastrowid))
    return result


@router.get("/equipment", summary="List registered equipment with current QC status")
def list_equipment(
    status_filter: str | None = Query(default=None, alias="status", description="pending_review | clean"),
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if kind is not None:
        where.append("kind = ?")
        params.append(_clean(kind, "kind"))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db.read_conn() as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) AS n FROM equipment{clause}", params).fetchone()["n"]
        )
        rows = conn.execute(
            f"SELECT * FROM equipment{clause} ORDER BY id ASC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        items = [qc._annotate_equipment(conn, r["id"]) for r in rows]
        if status_filter is not None:
            if status_filter not in ("pending_review", "clean"):
                raise ApiError(422, "invalid_status", "status must be 'pending_review' or 'clean'")
            items = [e for e in items if e["status"] == status_filter]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/equipment/{equipment_id}", summary="Get one piece of equipment, its cleanings and risk paths")
def get_equipment(equipment_id: int = Path(ge=1)) -> dict[str, Any]:
    with db.read_conn() as conn:
        annotated = qc._annotate_equipment(conn, equipment_id)
        cleanings = conn.execute(
            "SELECT * FROM cleanings WHERE equipment_id = ? ORDER BY cleaned_at_utc ASC, id ASC",
            (equipment_id,),
        ).fetchall()
        annotated["cleanings"] = [
            {
                "id": r["id"],
                "cleaned_at_utc": r["cleaned_at_utc"],
                "created_at_utc": r["created_at_utc"],
            }
            for r in cleanings
        ]
        findings = conn.execute(
            "SELECT * FROM qc_findings WHERE subject_type = 'equipment' AND equipment_id = ?"
            " ORDER BY source_blank_job_id ASC",
            (equipment_id,),
        ).fetchall()
        annotated["risk_sources"] = [
            {
                "source_blank_job_id": r["source_blank_job_id"],
                "is_source": bool(r["is_source"]),
                "active": bool(r["active"]),
                "paths": domain.json.loads(r["paths_json"]),
            }
            for r in findings
        ]
    return annotated


# ---------------------------------------------------------------------------
# Cleanings
# ---------------------------------------------------------------------------

@router.post(
    "/equipment/{equipment_id}/cleanings",
    status_code=status.HTTP_201_CREATED,
    summary="Record a completed cleaning (late reports allowed, idempotent)",
)
def add_cleaning(body: CleaningCreate, equipment_id: int = Path(ge=1), response: Response = None) -> dict[str, Any]:
    cleaned_dt = domain.parse_utc(body.cleaned_at_utc, "cleaned_at_utc")
    cleaned_iso = domain.format_utc(cleaned_dt)
    new_hash = qc.cleaning_hash(cleaned_iso)
    now = domain.utc_now_iso()
    with db.write_tx() as conn:
        equipment = qc.get_equipment_by_id(conn, equipment_id)
        existing = conn.execute(
            "SELECT * FROM cleanings WHERE equipment_id = ? AND cleaned_at_utc = ?",
            (equipment_id, cleaned_iso),
        ).fetchone()
        if existing is not None:
            if response is not None:
                response.status_code = status.HTTP_200_OK
            return {
                "idempotent": True,
                "cleaning": {
                    "id": existing["id"],
                    "equipment_id": equipment_id,
                    "equipment_code": equipment["code"],
                    "cleaned_at_utc": existing["cleaned_at_utc"],
                },
            }
        cur = conn.execute(
            "INSERT INTO cleanings (equipment_id, cleaned_at_utc, content_hash, created_at_utc)"
            " VALUES (?, ?, ?, ?)",
            (equipment_id, cleaned_iso, new_hash, now),
        )
        cleaning_id = int(cur.lastrowid)
        payload = domain.canonical_json({"cleaned_at_utc": cleaned_iso})
        conn.execute(
            "INSERT INTO cleaning_revisions (cleaning_id, equipment_id, action, prev_json, new_json, reason, created_at_utc)"
            " VALUES (?, ?, 'create', NULL, ?, NULL, ?)",
            (cleaning_id, equipment_id, payload, now),
        )
        summary = qc.recompute_qc_findings(conn)
        result = qc._annotate_equipment(conn, equipment_id)
        result["cleaning_id"] = cleaning_id
        result["idempotent"] = False
        result["recomputed"] = summary
    return result


@router.patch(
    "/equipment/{equipment_id}/cleanings/{cleaning_id}",
    summary="Correct a cleaning completion time (audited; risk is recomputed)",
)
def correct_cleaning(
    body: CleaningCorrect,
    equipment_id: int = Path(ge=1),
    cleaning_id: int = Path(ge=1),
) -> dict[str, Any]:
    cleaned_dt = domain.parse_utc(body.cleaned_at_utc, "cleaned_at_utc")
    cleaned_iso = domain.format_utc(cleaned_dt)
    reason = _clean(body.reason, "reason")
    new_hash = qc.cleaning_hash(cleaned_iso)
    now = domain.utc_now_iso()
    with db.write_tx() as conn:
        equipment = qc.get_equipment_by_id(conn, equipment_id)
        existing = conn.execute(
            "SELECT * FROM cleanings WHERE id = ? AND equipment_id = ?", (cleaning_id, equipment_id)
        ).fetchone()
        if existing is None:
            raise ApiError(404, "cleaning_not_found", f"cleaning {cleaning_id} does not exist for equipment {equipment_id}")
        if new_hash == existing["content_hash"]:
            return {
                "corrected": False,
                "substantive": False,
                "reason": "content unchanged; no correction recorded",
                "cleaning": {
                    "id": existing["id"],
                    "equipment_id": equipment_id,
                    "equipment_code": equipment["code"],
                    "cleaned_at_utc": existing["cleaned_at_utc"],
                },
            }
        clash = conn.execute(
            "SELECT 1 FROM cleanings WHERE equipment_id = ? AND cleaned_at_utc = ? AND id != ?",
            (equipment_id, cleaned_iso, cleaning_id),
        ).fetchone()
        if clash:
            raise ApiError(
                409,
                "cleaning_already_exists",
                f"equipment {equipment_id} already has a cleaning recorded at {cleaned_iso}",
            )
        prev_payload = domain.canonical_json({"cleaned_at_utc": existing["cleaned_at_utc"]})
        new_payload = domain.canonical_json({"cleaned_at_utc": cleaned_iso})
        conn.execute(
            "UPDATE cleanings SET cleaned_at_utc = ?, content_hash = ? WHERE id = ?",
            (cleaned_iso, new_hash, cleaning_id),
        )
        conn.execute(
            "INSERT INTO cleaning_revisions (cleaning_id, equipment_id, action, prev_json, new_json, reason, created_at_utc)"
            " VALUES (?, ?, 'correct', ?, ?, ?, ?)",
            (cleaning_id, equipment_id, prev_payload, new_payload, reason, now),
        )
        summary = qc.recompute_qc_findings(conn)
        result = qc._annotate_equipment(conn, equipment_id)
        result["corrected"] = True
        result["substantive"] = True
        result["recomputed"] = summary
    return result


@router.get(
    "/equipment/{equipment_id}/cleanings/revisions",
    summary="Audit trail of cleaning reports and corrections",
)
def cleaning_revisions(equipment_id: int = Path(ge=1)) -> dict[str, Any]:
    with db.read_conn() as conn:
        qc.get_equipment_by_id(conn, equipment_id)
        rows = conn.execute(
            "SELECT * FROM cleaning_revisions WHERE equipment_id = ? ORDER BY id ASC", (equipment_id,)
        ).fetchall()
    return {
        "equipment_id": equipment_id,
        "revisions": [
            {
                "cleaning_id": r["cleaning_id"],
                "action": r["action"],
                "prev_json": domain.json.loads(r["prev_json"]) if r["prev_json"] else None,
                "new_json": domain.json.loads(r["new_json"]),
                "reason": r["reason"],
                "created_at_utc": r["created_at_utc"],
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Sampling jobs
# ---------------------------------------------------------------------------

@router.post(
    "/sampling-jobs",
    status_code=status.HTTP_201_CREATED,
    summary="Register a sampling job (sample, UTC time, pump/tubing, optional positive blank)",
)
def create_job(body: JobCreate, response: Response = None) -> dict[str, Any]:
    worked_dt = domain.parse_utc(body.worked_at_utc, "worked_at_utc")
    worked_iso = domain.format_utc(worked_dt)
    blank_code = _validate_blank_fields(body.blank_positive, body.blank_sample_code)
    now = domain.utc_now_iso()
    with db.write_tx() as conn:
        sample = domain.get_sample(conn, body.sample_id)
        sampled_at = domain.parse_utc(sample["sampled_at_utc"], "sampled_at_utc")
        if worked_dt < sampled_at:
            raise ApiError(
                422,
                "job_not_after_sampling",
                "worked_at_utc must not be earlier than the sample's sampled_at_utc",
            )
        existing = qc.get_job_for_sample(conn, body.sample_id)
        if existing is not None:
            raise ApiError(
                409,
                "sampling_job_already_exists",
                f"sample {body.sample_id} already has sampling job {existing['id']}; use PATCH to correct it",
            )
        equipment_ids = _resolve_equipment(conn, body.equipment_codes)
        new_hash = qc.job_hash(worked_iso, equipment_ids, body.blank_positive, blank_code)
        cur = conn.execute(
            "INSERT INTO sampling_jobs (sample_id, worked_at_utc, blank_positive, blank_sample_code, content_hash, created_at_utc)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (body.sample_id, worked_iso, int(body.blank_positive), blank_code, new_hash, now),
        )
        job_id = int(cur.lastrowid)
        for position, equipment_id in enumerate(equipment_ids, start=1):
            conn.execute(
                "INSERT INTO sampling_job_equipment (job_id, equipment_id, position) VALUES (?, ?, ?)",
                (job_id, equipment_id, position),
            )
        payload = domain.canonical_json(
            {
                "worked_at_utc": worked_iso,
                "equipment_ids": sorted(equipment_ids),
                "blank_positive": body.blank_positive,
                "blank_sample_code": blank_code,
            }
        )
        conn.execute(
            "INSERT INTO sampling_job_revisions (job_id, sample_id, action, prev_json, new_json, reason, created_at_utc)"
            " VALUES (?, ?, 'create', NULL, ?, NULL, ?)",
            (job_id, body.sample_id, payload, now),
        )
        summary = qc.recompute_qc_findings(conn)
        job = qc.get_job(conn, job_id)
        result = qc.job_public(conn, job)
        result["idempotent"] = False
        result["recomputed"] = summary
    return result


@router.get("/sampling-jobs", summary="List sampling jobs (oldest working time first)")
def list_jobs(
    blank_only: bool = Query(default=False, description="Only jobs carrying a positive equipment blank"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    where = " WHERE blank_positive = 1" if blank_only else ""
    with db.read_conn() as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) AS n FROM sampling_jobs{where}").fetchone()["n"]
        )
        rows = conn.execute(
            f"SELECT * FROM sampling_jobs{where} ORDER BY worked_at_utc ASC, id ASC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        items = [qc.job_public(conn, r) for r in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# Registered before the {job_id} route so "revisions" is not parsed as an id.
@router.get("/sampling-jobs/revisions/audit", summary="Audit trail of all sampling job reports and corrections")
def job_revisions_audit(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    with db.read_conn() as conn:
        total = int(
            conn.execute("SELECT COUNT(*) AS n FROM sampling_job_revisions").fetchone()["n"]
        )
        rows = conn.execute(
            "SELECT * FROM sampling_job_revisions ORDER BY id ASC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        items = [
            {
                "job_id": r["job_id"],
                "sample_id": r["sample_id"],
                "action": r["action"],
                "prev_json": domain.json.loads(r["prev_json"]) if r["prev_json"] else None,
                "new_json": domain.json.loads(r["new_json"]),
                "reason": r["reason"],
                "created_at_utc": r["created_at_utc"],
            }
            for r in rows
        ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/sampling-jobs/{job_id}", summary="Get one sampling job with equipment and QC status")
def get_job(job_id: int = Path(ge=1)) -> dict[str, Any]:
    with db.read_conn() as conn:
        job = qc.get_job(conn, job_id)
        return qc.job_public(conn, job)


@router.get(
    "/samples/{sample_id}/sampling-job",
    summary="Get the sampling job registered for a sample (404 if none yet)",
)
def get_sample_job(sample_id: int = Path(ge=1)) -> dict[str, Any]:
    with db.read_conn() as conn:
        domain.get_sample(conn, sample_id)
        job = qc.get_job_for_sample(conn, sample_id)
        if job is None:
            raise ApiError(404, "sampling_job_not_found", f"no sampling job registered for sample {sample_id} yet")
        return qc.job_public(conn, job)


@router.patch(
    "/sampling-jobs/{job_id}",
    summary="Correct a sampling job (time, equipment, blank result); audited and recomputed",
)
def correct_job(body: JobCorrect, job_id: int = Path(ge=1)) -> dict[str, Any]:
    worked_dt = domain.parse_utc(body.worked_at_utc, "worked_at_utc")
    worked_iso = domain.format_utc(worked_dt)
    reason = _clean(body.reason, "reason")
    blank_code = _validate_blank_fields(body.blank_positive, body.blank_sample_code)
    now = domain.utc_now_iso()
    with db.write_tx() as conn:
        existing = qc.get_job(conn, job_id)
        if body.sample_id != existing["sample_id"]:
            raise ApiError(
                422,
                "job_sample_mismatch",
                f"job {job_id} belongs to sample {existing['sample_id']}; a job cannot be moved to another sample",
            )
        sample = domain.get_sample(conn, existing["sample_id"])
        sampled_at = domain.parse_utc(sample["sampled_at_utc"], "sampled_at_utc")
        if worked_dt < sampled_at:
            raise ApiError(
                422,
                "job_not_after_sampling",
                "worked_at_utc must not be earlier than the sample's sampled_at_utc",
            )
        equipment_ids = _resolve_equipment(conn, body.equipment_codes)
        new_hash = qc.job_hash(worked_iso, equipment_ids, body.blank_positive, blank_code)
        if new_hash == existing["content_hash"]:
            return {
                "corrected": False,
                "substantive": False,
                "reason": "content unchanged; no correction recorded",
                "job": qc.job_public(conn, existing),
            }
        prev_payload = domain.canonical_json(
            {
                "worked_at_utc": existing["worked_at_utc"],
                "equipment_ids": sorted(
                    r["equipment_id"]
                    for r in conn.execute(
                        "SELECT equipment_id FROM sampling_job_equipment WHERE job_id = ?", (job_id,)
                    ).fetchall()
                ),
                "blank_positive": bool(existing["blank_positive"]),
                "blank_sample_code": existing["blank_sample_code"],
            }
        )
        new_payload = domain.canonical_json(
            {
                "worked_at_utc": worked_iso,
                "equipment_ids": sorted(equipment_ids),
                "blank_positive": body.blank_positive,
                "blank_sample_code": blank_code,
            }
        )
        conn.execute(
            "UPDATE sampling_jobs SET worked_at_utc = ?, blank_positive = ?, blank_sample_code = ?,"
            " content_hash = ? WHERE id = ?",
            (worked_iso, int(body.blank_positive), blank_code, new_hash, job_id),
        )
        conn.execute("DELETE FROM sampling_job_equipment WHERE job_id = ?", (job_id,))
        for position, equipment_id in enumerate(equipment_ids, start=1):
            conn.execute(
                "INSERT INTO sampling_job_equipment (job_id, equipment_id, position) VALUES (?, ?, ?)",
                (job_id, equipment_id, position),
            )
        conn.execute(
            "INSERT INTO sampling_job_revisions (job_id, sample_id, action, prev_json, new_json, reason, created_at_utc)"
            " VALUES (?, ?, 'correct', ?, ?, ?, ?)",
            (job_id, existing["sample_id"], prev_payload, new_payload, reason, now),
        )
        summary = qc.recompute_qc_findings(conn)
        job = qc.get_job(conn, job_id)
        result = qc.job_public(conn, job)
        result["corrected"] = True
        result["substantive"] = True
        result["recomputed"] = summary
    return result


# ---------------------------------------------------------------------------
# Review report
# ---------------------------------------------------------------------------

@router.get(
    "/qc/review",
    summary="Pending-review samples, source blanks and the jobs/equipment each path passes through",
)
def qc_review() -> dict[str, Any]:
    with db.read_conn() as conn:
        return qc.review_report(conn)

# GSB Groundwater Sample Delivery API

Data-supply API for handing groundwater sample concentrations to the drawing
team with an auditable chain of custody. FastAPI + SQLite, interactive docs at
`/docs`.

## Run with Docker Compose

```bash
docker compose up --build
# API:  http://localhost:8000
# Docs: http://localhost:8000/docs
```

The SQLite file lives in the named volume `gsb-data` (`/data/gsb.sqlite3`).
Set `GSB_PORT` to change the published port. The container runs a single
uvicorn worker by design: SQLite plus the in-process write lock serialise all
writes. Do not scale to multiple replicas against one database file.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest -q
```

`GSB_DB_PATH` overrides the database path (default `data/gsb.sqlite3`).

## Model

| Concept | Description |
| --- | --- |
| Sample | `location`, `collector`, UTC `sampled_at_utc` (offset required), optional `sample_code` and `reporting_lab` |
| Handover | One custody link per sample and `seq`, with `from_party`, `to_party`, UTC `handed_at_utc` |
| Measurement | `lab`, `analyte`, `concentration`, `unit`; unique per sample+lab+analyte |
| Delivery | A numbered snapshot of all *eligible* samples for one UTC sampling date |
| Equipment | A reusable pump or tubing line (`/equipment`) with a ledger of cleaning completion times |
| Sampling job | One field operation per sample: UTC `worked_at_utc`, the equipment used and an optional positive equipment blank |
| QC finding | A derived, per-blank-source contamination path; rebuilt atomically after every QC write, never edited by hand |

### Custody rules

* Sequence numbers start at 1 and must be contiguous.
* Handover 1 leaves from the collector; handover *n* leaves from the recipient
  of handover *n-1*.
* Handover times are later than sampling time and strictly increase.
* The chain's terminal party is the reporting laboratory: it must equal the
  sample's declared `reporting_lab`, or be one of the labs that reported a
  measurement. A multi-lab sample must declare `reporting_lab`.
* Handovers may be reported out of order and are validated against whichever
  neighbours already exist; a gap keeps the sample out of deliveries until it
  is filled.
* Retrying an identical handover (`POST`) is idempotent (`200` with
  `idempotent: true`). Different content on an existing `seq` is rejected with
  `409`; use `PATCH .../handovers/{seq}` with a mandatory `reason`.
* Corrections may temporarily break the chain (so it can be repaired link by
  link); the sample simply stays ineligible until the chain is whole again.

### Deliveries

* `POST /deliveries {"sample_date": "YYYY-MM-DD"}` assigns
  `DLV-YYYYMMDD-NNN` per sampling date and includes only samples that are fully
  eligible at that instant. Ineligible samples are reported in
  `excluded_samples`; if none are eligible the request fails with `409` and no
  delivery number is consumed.
* Each delivery stores a full snapshot plus a `revision_token` (hash of the
  exact committed state). Optionally pass `expected_revision_token` to refuse
  publishing if anything changed since you last inspected the data.
* When a sample's handovers or measurements are later added or substantively
  corrected, every active delivery immediately stops serving **only that
  sample**; the item remains visible in `revoked_samples` with a reason.
  Other samples in the delivery keep being served.
* The withdrawal is permanent for that delivery even if the chain is repaired.
  Publish a new delivery to supply the sample again.
* `GET /samples/{id}/handovers/revisions` gives the creation/correction audit
  trail.

### Equipment-blank quality control

When a field QC officer finds a positive equipment blank, the contaminated
water samples must be traceable through every pump and tubing line that
shared work afterwards.

1. Register equipment once: `POST /equipment` (`code`, `kind`, optional
   `description`).
2. Record each completed cleaning:
   `POST /equipment/{id}/cleanings {"cleaned_at_utc": ...}`. Cleanings may be
   reported late and corrected with `PATCH` (mandatory reason, audited).
3. Register one sampling job per sample: `POST /sampling-jobs` with
   `sample_id`, UTC `worked_at_utc`, the ordered `equipment_codes` and, for a
   positive blank, `blank_positive: true` plus the blank's code. Jobs may be
   reported late and corrected via `PATCH /sampling-jobs/{id}`.

Propagation rules, recomputed from the full ledger after every write:

* A positive blank puts the source job's **sample and every piece of its
  equipment** under review (`pending_review`).
* A later job using still-pending equipment inherits the risk: its own sample
  and **all other equipment shared on that job** take the risk too, so it
  carries forward through subsequent jobs.
* A cleaning only stops *later* jobs from inheriting that implement's risk.
  Findings already incurred stay in the ledger forever; re-contamination
  after a cleaning adds a new path instead of overwriting the old one.
* Each positive blank is simulated independently. Correcting one blank to
  negative, or washing one implement, never erases another blank's paths.
* Late jobs and retroactive corrections (blank result, job time, equipment
  set, cleaning time) trigger a full chronological recompute, so the outcome
  is identical regardless of report arrival order.
* `GET /qc/review` lists every source blank, every sample pending review and
  every involved implement, each with the explicit chain of jobs/equipment
  the risk passed through. Sample details (`GET /samples/{id}`) carry a `qc`
  summary as well.

The QC ledger is read-consistent with the rest of the service: each write is
one `BEGIN IMMEDIATE` transaction that mutates the facts, rebuilds the derived
findings and commits once, so a rejected report leaves no partial ledger and a
review always cites one committed state. QC data never changes delivery
eligibility or delivered concentrations.

### Concurrency and atomicity

Each write runs inside one SQLite `BEGIN IMMEDIATE` transaction guarded by a
process-wide lock, so a failed correction, measurement insert or publish
leaves no partial rows, and a delivery can never mix pre- and post-correction
state. Reads use a single snapshot transaction.

## Endpoints

```
POST   /samples
GET    /samples                      ?sample_date=&limit=&offset=
GET    /samples/{id}
GET    /samples/{id}/handovers
POST   /samples/{id}/handovers
PATCH  /samples/{id}/handovers/{seq}
GET    /samples/{id}/handovers/revisions
GET    /samples/{id}/measurements
POST   /samples/{id}/measurements
PATCH  /samples/{id}/measurements/{measurement_id}
POST   /deliveries
GET    /deliveries                   ?sample_date=&limit=&offset=
GET    /deliveries/{delivery_no}
POST   /equipment
GET    /equipment                    ?status=&kind=&limit=&offset=
GET    /equipment/{id}
POST   /equipment/{id}/cleanings
PATCH  /equipment/{id}/cleanings/{cleaning_id}
GET    /equipment/{id}/cleanings/revisions
POST   /sampling-jobs
GET    /sampling-jobs                ?blank_only=&limit=&offset=
GET    /sampling-jobs/revisions/audit
GET    /sampling-jobs/{id}
PATCH  /sampling-jobs/{id}
GET    /samples/{id}/sampling-job
GET    /qc/review
GET    /healthz
```

Errors use `{"detail": {"code": ..., "message": ..., ...}}`.

## Self-test

With the server running:

```bash
python scripts/interleave_selftest.py --base-url http://127.0.0.1:8000
```

It creates six eligible samples, publishes a delivery, then races handover
corrections, measurement entry and further publishes. It asserts no 5xx
responses, that the old delivery still serves untouched samples while the
corrected one is withdrawn with a reason, that no active delivery serves a
stale snapshot, and that a fresh delivery can be issued afterwards.

A second script exercises the QC ledger over HTTP — two independent positive
blanks, propagation through shared pumps/tubing, a cleaning, a late job,
retroactive blank/cleaning corrections and rollback of a rejected edit:

```bash
python scripts/qc_smoke.py --base-url http://127.0.0.1:8000
```

"""SQLite access helpers.

The service is intentionally a small, single-process FastAPI app backed by
SQLite.  Every write request opens its own ``BEGIN IMMEDIATE`` transaction so
that concurrent requests are serialised by SQLite instead of interleaving
partial state.  A ``threading.RLock`` also serialises writers inside the
process (uvicorn default deployment), which keeps "read the current committed
state, validate, write" sequences atomic even though the app uses threads.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.environ.get("GSB_DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "gsb.sqlite3"))

_write_lock = threading.RLock()
_initialised = False
_init_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_code     TEXT NOT NULL,
    location        TEXT NOT NULL,
    collector       TEXT NOT NULL,
    sampled_at_utc  TEXT NOT NULL,           -- ISO-8601, canonical UTC with Z
    sample_date     TEXT NOT NULL,           -- YYYY-MM-DD (UTC date of sampling)
    reporting_lab   TEXT,                    -- optional declared reporting laboratory
    created_at_utc  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_date ON samples(sample_date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_samples_code ON samples(sample_code);

CREATE TABLE IF NOT EXISTS measurements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    lab             TEXT NOT NULL,
    analyte         TEXT NOT NULL,
    concentration   REAL NOT NULL,
    unit            TEXT NOT NULL,
    entered_at_utc  TEXT NOT NULL,
    UNIQUE(sample_id, lab, analyte)
);

CREATE TABLE IF NOT EXISTS measurement_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    measurement_id  INTEGER NOT NULL REFERENCES measurements(id),
    action          TEXT NOT NULL,           -- create | correct
    prev_json       TEXT,
    new_json        TEXT NOT NULL,
    reason          TEXT,
    created_at_utc  TEXT NOT NULL
);

-- Handover links.  A link is identified by (sample_id, seq).  ``content`` is
-- the canonical JSON of the *business* content (handover parties + timestamp),
-- so an identical retry can be recognised and a real edit detected.
CREATE TABLE IF NOT EXISTS handovers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    seq             INTEGER NOT NULL,
    from_party      TEXT NOT NULL,
    to_party        TEXT NOT NULL,
    handed_at_utc   TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    created_at_utc  TEXT NOT NULL,
    UNIQUE(sample_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_handovers_sample ON handovers(sample_id, seq);

-- Every accepted *change* to a handover link is recorded here, including the
-- initial registration (action='create').  This is the audit trail that proves
-- why an old delivery was invalidated and whether it can be republished.
CREATE TABLE IF NOT EXISTS handover_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    seq             INTEGER NOT NULL,
    action          TEXT NOT NULL,           -- create | correct
    prev_hash       TEXT,
    new_hash        TEXT NOT NULL,
    reason          TEXT,
    created_at_utc  TEXT NOT NULL
);

-- Delivery numbers are per sample_date.  ``revision_token`` hashes the exact
-- committed sample set + the exact committed handover/measurement state the
-- delivery was built from, which lets us detect "substantive" changes.
CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_no     TEXT NOT NULL UNIQUE,
    sample_date     TEXT NOT NULL,
    revision_token  TEXT NOT NULL,
    created_at_utc  TEXT NOT NULL,
    revoked_at_utc  TEXT,
    revoke_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_deliveries_date ON deliveries(sample_date);

CREATE TABLE IF NOT EXISTS delivery_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id     INTEGER NOT NULL REFERENCES deliveries(id),
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    snapshot_json   TEXT NOT NULL,
    revoked_at_utc  TEXT,
    revoke_reason   TEXT,
    UNIQUE(delivery_id, sample_id)
);

CREATE TABLE IF NOT EXISTS sequence_counters (
    name            TEXT PRIMARY KEY,
    value           INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- Quality-control: sampling equipment, cleaning ledger, sampling jobs and the
-- derived cross-contamination findings.
--
-- The first three groups are *facts* reported by field QC.  qc_findings is a
-- fully derived table: every QC write rebuilds it inside the same transaction
-- from the facts, so late jobs, retroactive corrections and cleanings always
-- produce a result consistent with one committed ledger state.
-- ---------------------------------------------------------------------------

-- Reusable sampling implements: pumps and tubing lines, identified by a stable
-- business code.  Identity is immutable; only cleanings/jobs reference it.
CREATE TABLE IF NOT EXISTS equipment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,           -- e.g. pump | tubing
    description     TEXT,
    created_at_utc  TEXT NOT NULL
);

-- One row per completed cleaning of one piece of equipment.  A cleaning at
-- time t protects jobs whose worked_at is strictly later than t.  Cleanings
-- may be reported late and corrected afterwards (audited).
CREATE TABLE IF NOT EXISTS cleanings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id    INTEGER NOT NULL REFERENCES equipment(id),
    cleaned_at_utc  TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    created_at_utc  TEXT NOT NULL,
    UNIQUE(equipment_id, cleaned_at_utc)
);
CREATE INDEX IF NOT EXISTS idx_cleanings_equipment ON cleanings(equipment_id, cleaned_at_utc);

CREATE TABLE IF NOT EXISTS cleaning_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cleaning_id     INTEGER NOT NULL REFERENCES cleanings(id),
    equipment_id    INTEGER NOT NULL REFERENCES equipment(id),
    action          TEXT NOT NULL,           -- create | correct
    prev_json       TEXT,
    new_json        TEXT NOT NULL,
    reason          TEXT,
    created_at_utc  TEXT NOT NULL
);

-- A sampling job is the field operation that produced one registered sample.
-- It records the UTC working time and the pump/tubing used.  A positive
-- equipment blank (blank_positive = 1) makes it a contamination *source*.
CREATE TABLE IF NOT EXISTS sampling_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id           INTEGER NOT NULL UNIQUE REFERENCES samples(id),
    worked_at_utc       TEXT NOT NULL,
    blank_positive      INTEGER NOT NULL DEFAULT 0,
    blank_sample_code   TEXT,
    content_hash        TEXT NOT NULL,
    created_at_utc      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_worked ON sampling_jobs(worked_at_utc);

CREATE TABLE IF NOT EXISTS sampling_job_equipment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES sampling_jobs(id) ON DELETE CASCADE,
    equipment_id    INTEGER NOT NULL REFERENCES equipment(id),
    position        INTEGER NOT NULL,       -- order as reported, 1-based
    UNIQUE(job_id, equipment_id)
);
CREATE INDEX IF NOT EXISTS idx_job_equipment_equipment ON sampling_job_equipment(equipment_id);

CREATE TABLE IF NOT EXISTS sampling_job_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES sampling_jobs(id),
    sample_id       INTEGER NOT NULL REFERENCES samples(id),
    action          TEXT NOT NULL,           -- create | correct
    prev_json       TEXT,
    new_json        TEXT NOT NULL,
    reason          TEXT,
    created_at_utc  TEXT NOT NULL
);

-- Derived risk paths.  One row per (source blank, subject).  paths_json holds
-- every distinct propagation path (a subject can be re-contaminated after a
-- cleaning, which adds a path without erasing the first).  active = 1 means
-- the equipment is still uncleaned at the timeline end for this source;
-- sample findings stay active forever once the risk has occurred.
CREATE TABLE IF NOT EXISTS qc_findings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_blank_job_id     INTEGER NOT NULL REFERENCES sampling_jobs(id),
    subject_type            TEXT NOT NULL,   -- sample | equipment
    sample_id               INTEGER REFERENCES samples(id),
    equipment_id            INTEGER REFERENCES equipment(id),
    is_source               INTEGER NOT NULL DEFAULT 0,
    active                  INTEGER NOT NULL DEFAULT 1,
    paths_json              TEXT NOT NULL,
    UNIQUE(source_blank_job_id, subject_type, sample_id, equipment_id)
);
CREATE INDEX IF NOT EXISTS idx_qc_sample ON qc_findings(subject_type, sample_id);
CREATE INDEX IF NOT EXISTS idx_qc_equipment ON qc_findings(subject_type, equipment_id);
CREATE INDEX IF NOT EXISTS idx_qc_source ON qc_findings(source_blank_job_id);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    global _initialised
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        _initialised = True


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """Open a serialised immediate transaction; commit on success, rollback on error."""
    init_db()
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    init_db()
    conn = _connect()
    try:
        # A read transaction gives the whole request one consistent snapshot.
        conn.execute("BEGIN")
        yield conn
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()

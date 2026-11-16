"""SQLite 本地存储：原始表、不可变快照、任务与发布记录。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .util import digest, stable_json, utc_now

SCHEMA_VERSION = "1"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    recorded_on TEXT NOT NULL,
    product TEXT NOT NULL,
    channel TEXT NOT NULL,
    amount INTEGER NOT NULL,
    term_months INTEGER NOT NULL,
    income_band TEXT NOT NULL,
    employment_years REAL NOT NULL,
    age INTEGER NOT NULL,
    existing_debt INTEGER NOT NULL,
    bureau_grade TEXT NOT NULL,
    city_tier TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_applied ON applications(applied_at);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    occurred_on TEXT NOT NULL,
    recorded_on TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_application ON events(application_id);
CREATE INDEX IF NOT EXISTS idx_events_recorded ON events(recorded_on);

CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    cutoff TEXT,
    params_json TEXT NOT NULL,
    policy_id TEXT,
    sample_run_id TEXT,
    label_batch_id TEXT,
    report_id TEXT,
    steps_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_kind_created ON runs(kind, created_at);

CREATE TABLE IF NOT EXISTS samples (
    sample_run_id TEXT PRIMARY KEY,
    cutoff TEXT NOT NULL,
    mode TEXT NOT NULL,
    label_config_json TEXT NOT NULL,
    label_config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    sealed_hash TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    application_count INTEGER NOT NULL,
    eligible_count INTEGER NOT NULL,
    matured_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_samples_cutoff ON samples(cutoff);

CREATE TABLE IF NOT EXISTS sample_rows (
    sample_run_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    applied_on TEXT NOT NULL,
    decision_on TEXT,
    disburse_on TEXT,
    week TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    features_json TEXT NOT NULL,
    visibility_json TEXT NOT NULL,
    PRIMARY KEY (sample_run_id, application_id)
);
CREATE INDEX IF NOT EXISTS idx_sample_rows_eligible ON sample_rows(sample_run_id, eligible);

CREATE TABLE IF NOT EXISTS label_batches (
    label_batch_id TEXT PRIMARY KEY,
    sample_run_id TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    label_config_json TEXT NOT NULL,
    label_config_hash TEXT NOT NULL,
    sealed_hash TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_label_batches_sample ON label_batches(sample_run_id, cutoff);

CREATE TABLE IF NOT EXISTS labels (
    label_batch_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    status TEXT NOT NULL,
    label INTEGER,
    matured_on TEXT,
    window_start TEXT,
    window_end TEXT,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (label_batch_id, application_id)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    sample_run_id TEXT NOT NULL,
    label_batch_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    mode TEXT NOT NULL,
    report_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_cutoff ON reports(cutoff);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 原始数据 ----------

    def seeded(self) -> bool:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='dataset_hash'").fetchone()
            return row is not None
        finally:
            conn.close()

    def meta(self, key: str) -> str | None:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def set_meta(self, key: str, value: str) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def ingest(
        self,
        applications: list[dict[str, Any]],
        events: list[dict[str, Any]],
        policies: list[dict[str, Any]],
        *,
        seed: int,
        snapshot: str,
        replace: bool = False,
    ) -> str:
        """写入虚构原始数据与不可变规则版本，返回数据集摘要。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if replace:
                for table in ("events", "applications", "policies"):
                    conn.execute(f"DELETE FROM {table}")
            existing = conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"]
            dataset_hash = digest({"applications": applications, "events": events, "policies": policies}, 24)
            if existing and not replace:
                conn.rollback()
                return dataset_hash
            now = utc_now()
            conn.executemany(
                """INSERT OR REPLACE INTO applications(
                    application_id, customer_id, applied_at, recorded_on, product, channel, amount,
                    term_months, income_band, employment_years, age, existing_debt, bureau_grade, city_tier
                ) VALUES(:application_id,:customer_id,:applied_at,:recorded_on,:product,:channel,:amount,
                    :term_months,:income_band,:employment_years,:age,:existing_debt,:bureau_grade,:city_tier)""",
                applications,
            )
            conn.executemany(
                """INSERT OR REPLACE INTO events(
                    event_id, application_id, kind, occurred_on, recorded_on, amount, detail
                ) VALUES(:event_id,:application_id,:kind,:occurred_on,:recorded_on,:amount,:detail)""",
                events,
            )
            for p in policies:
                conn.execute(
                    """INSERT OR REPLACE INTO policies(
                        policy_id, name, version, definition_json, definition_hash, created_at, active
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        p["policy_id"],
                        p["name"],
                        p["version"],
                        stable_json(p["definition"]),
                        digest(p["definition"], 20),
                        now,
                        1 if p.get("active") else 0,
                    ),
                )
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                [
                    ("seed", str(seed)),
                    ("snapshot_date", snapshot),
                    ("dataset_hash", dataset_hash),
                    ("seeded_at", now),
                ],
            )
            conn.commit()
            return dataset_hash
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def dataset_hash(self) -> str:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='dataset_hash'").fetchone()
            return row["value"] if row else ""
        finally:
            conn.close()

    # ---------- 规则 ----------

    def policies(self) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM policies ORDER BY policy_id").fetchall()
            return [
                {
                    "policy_id": r["policy_id"],
                    "name": r["name"],
                    "version": r["version"],
                    "definition": json.loads(r["definition_json"]),
                    "definition_hash": r["definition_hash"],
                    "active": bool(r["active"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def policy(self, policy_id: str) -> dict[str, Any]:
        conn = self.connect()
        try:
            r = conn.execute("SELECT * FROM policies WHERE policy_id=?", (policy_id,)).fetchone()
            if not r:
                raise KeyError(f"未知规则版本: {policy_id}")
            return {
                "policy_id": r["policy_id"],
                "name": r["name"],
                "version": r["version"],
                "definition": json.loads(r["definition_json"]),
                "definition_hash": r["definition_hash"],
                "active": bool(r["active"]),
            }
        finally:
            conn.close()

    def active_policy_id(self) -> str | None:
        conn = self.connect()
        try:
            r = conn.execute("SELECT policy_id FROM policies WHERE active=1 ORDER BY policy_id LIMIT 1").fetchone()
            return r["policy_id"] if r else None
        finally:
            conn.close()

    def activate_policy(self, policy_id: str) -> None:
        conn = self.connect()
        try:
            conn.execute("UPDATE policies SET active=0")
            cur = conn.execute("UPDATE policies SET active=1 WHERE policy_id=?", (policy_id,))
            if cur.rowcount != 1:
                raise KeyError(f"未知规则版本: {policy_id}")
            conn.commit()
        finally:
            conn.close()

    # ---------- 任务 ----------

    def create_run(
        self,
        kind: str,
        *,
        cutoff: str | None,
        params: dict[str, Any],
        policy_id: str | None = None,
        sample_run_id: str | None = None,
        label_batch_id: str | None = None,
    ) -> str:
        run_id = f"RUN-{kind[:3].upper()}-{uuid4().hex[:12]}"
        now = utc_now()
        conn = self.connect()
        try:
            conn.execute(
                """INSERT INTO runs(run_id, kind, status, cutoff, params_json, policy_id,
                    sample_run_id, label_batch_id, steps_json, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, kind, "running", cutoff, stable_json(params), policy_id,
                 sample_run_id, label_batch_id, "{}", now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return run_id

    def set_step(self, run_id: str, step: str, status: str, note: str = "") -> None:
        conn = self.connect()
        try:
            row = conn.execute("SELECT steps_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            steps = json.loads(row["steps_json"])
            entry = steps.setdefault(step, {"attempts": 0})
            entry["status"] = status
            entry["at"] = utc_now()
            if status == "done":
                entry["attempts"] = entry.get("attempts", 0) + 1
            if note:
                entry["note"] = note
            conn.execute(
                "UPDATE runs SET steps_json=?, status='running', error=NULL, updated_at=? WHERE run_id=?",
                (stable_json(steps), utc_now(), run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def run(self, run_id: str) -> dict[str, Any]:
        conn = self.connect()
        try:
            r = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not r:
                raise KeyError(run_id)
            return {
                "run_id": r["run_id"],
                "kind": r["kind"],
                "status": r["status"],
                "cutoff": r["cutoff"],
                "params": json.loads(r["params_json"]),
                "policy_id": r["policy_id"],
                "sample_run_id": r["sample_run_id"],
                "label_batch_id": r["label_batch_id"],
                "report_id": r["report_id"],
                "steps": json.loads(r["steps_json"]),
                "result": json.loads(r["result_json"]) if r["result_json"] else None,
                "error": r["error"],
                "attempts": r["attempts"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
        finally:
            conn.close()

    def runs(self, limit: int = 40) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for r in rows:
                out.append(
                    {
                        "run_id": r["run_id"],
                        "kind": r["kind"],
                        "status": r["status"],
                        "cutoff": r["cutoff"],
                        "policy_id": r["policy_id"],
                        "sample_run_id": r["sample_run_id"],
                        "label_batch_id": r["label_batch_id"],
                        "report_id": r["report_id"],
                        "steps": json.loads(r["steps_json"]),
                        "error": r["error"],
                        "attempts": r["attempts"],
                        "created_at": r["created_at"],
                        "updated_at": r["updated_at"],
                    }
                )
            return out
        finally:
            conn.close()

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        report_id: str | None = None,
    ) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """UPDATE runs SET status=?, result_json=COALESCE(?, result_json), error=?,
                   report_id=COALESCE(?, report_id), updated_at=? WHERE run_id=?""",
                (status, stable_json(result) if result is not None else None, error, report_id, utc_now(), run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def audit(self, actor: str, action: str, detail: dict[str, Any]) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO audit_log(at, actor, action, detail_json) VALUES(?,?,?,?)",
                (utc_now(), actor, action, stable_json(detail)),
            )
            conn.commit()
        finally:
            conn.close()

    def audits(self, limit: int = 30) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [
                {"at": r["at"], "actor": r["actor"], "action": r["action"], "detail": json.loads(r["detail_json"])}
                for r in rows
            ]
        finally:
            conn.close()

    # ---------- 样本 / 标签 / 报告 ----------

    def latest_sample(self, cutoff: str, mode: str | None = None) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            if mode:
                r = conn.execute(
                    "SELECT * FROM samples WHERE cutoff=? AND mode=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (cutoff, mode),
                ).fetchone()
            else:
                r = conn.execute(
                    "SELECT * FROM samples WHERE cutoff=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (cutoff,),
                ).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def sample(self, sample_run_id: str) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            r = conn.execute("SELECT * FROM samples WHERE sample_run_id=?", (sample_run_id,)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def sample_rows(self, sample_run_id: str) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sample_rows WHERE sample_run_id=? ORDER BY decision_on, application_id",
                (sample_run_id,),
            ).fetchall()
            out = []
            for r in rows:
                out.append(
                    {
                        "sample_run_id": r["sample_run_id"],
                        "application_id": r["application_id"],
                        "customer_id": r["customer_id"],
                        "applied_on": r["applied_on"],
                        "decision_on": r["decision_on"],
                        "disburse_on": r["disburse_on"],
                        "week": r["week"],
                        "eligible": bool(r["eligible"]),
                        "reason_code": r["reason_code"],
                        "reason_text": r["reason_text"],
                        "features": json.loads(r["features_json"]),
                        "visibility": json.loads(r["visibility_json"]),
                    }
                )
            return out
        finally:
            conn.close()

    def latest_label_batch(self, sample_run_id: str, cutoff: str | None = None) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            if cutoff:
                r = conn.execute(
                    "SELECT * FROM label_batches WHERE sample_run_id=? AND cutoff=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (sample_run_id, cutoff),
                ).fetchone()
            else:
                r = conn.execute(
                    "SELECT * FROM label_batches WHERE sample_run_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (sample_run_id,),
                ).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def label_batch(self, label_batch_id: str) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            r = conn.execute("SELECT * FROM label_batches WHERE label_batch_id=?", (label_batch_id,)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def labels(self, label_batch_id: str) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM labels WHERE label_batch_id=? ORDER BY application_id", (label_batch_id,)
            ).fetchall()
            return [
                {
                    "label_batch_id": r["label_batch_id"],
                    "application_id": r["application_id"],
                    "status": r["status"],
                    "label": r["label"],
                    "matured_on": r["matured_on"],
                    "window_start": r["window_start"],
                    "window_end": r["window_end"],
                    "evidence": json.loads(r["evidence_json"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def report(self, report_id: str) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            r = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["report"] = json.loads(d.pop("report_json"))
            return d
        finally:
            conn.close()

    def reports(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT report_id, sample_run_id, label_batch_id, policy_id, cutoff, mode, status, created_at, manifest_hash FROM reports ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def latest_report(self) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            r = conn.execute(
                "SELECT * FROM reports WHERE status='published' ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            if not r:
                return None
            d = dict(r)
            d["report"] = json.loads(d.pop("report_json"))
            return d
        finally:
            conn.close()

    def reports_for_sample(self, sample_run_id: str) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT report_id, sample_run_id, label_batch_id, policy_id, cutoff, mode, created_at FROM reports WHERE sample_run_id=? ORDER BY created_at DESC",
                (sample_run_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def find_report(
        self,
        sample_run_id: str,
        label_batch_id: str,
        policy_id: str,
        mode: str,
        manifest_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """按输入组合查找报告；给出 manifest_hash 时只复用内容完全一致的报告。"""
        conn = self.connect()
        try:
            if manifest_hash:
                r = conn.execute(
                    """SELECT * FROM reports WHERE sample_run_id=? AND label_batch_id=? AND policy_id=? AND mode=?
                       AND manifest_hash=? LIMIT 1""",
                    (sample_run_id, label_batch_id, policy_id, mode, manifest_hash),
                ).fetchone()
            else:
                r = conn.execute(
                    "SELECT * FROM reports WHERE sample_run_id=? AND label_batch_id=? AND policy_id=? AND mode=? LIMIT 1",
                    (sample_run_id, label_batch_id, policy_id, mode),
                ).fetchone()
            if not r:
                return None
            d = dict(r)
            d["report"] = json.loads(d.pop("report_json"))
            return d
        finally:
            conn.close()

    def all_events(self, application_id: str) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE application_id=? ORDER BY occurred_on, recorded_on, event_id",
                (application_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        conn = self.connect()
        try:
            return {
                t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                for t in ("applications", "events", "policies", "samples", "label_batches", "reports", "runs")
            }
        finally:
            conn.close()

    def storage_stats(self) -> dict[str, Any]:
        conn = self.connect()
        try:
            return {
                "db_bytes": os.path.getsize(self.path) if os.path.exists(self.path) else 0,
                "dataset_hash": self.dataset_hash(),
                "snapshot_date": self.meta("snapshot_date"),
                "seeded_at": self.meta("seeded_at"),
                "counts": self.counts(),
            }
        finally:
            conn.close()


def d(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])

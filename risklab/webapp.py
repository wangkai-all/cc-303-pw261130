"""本地网页服务：标准库 HTTP，无外部依赖。"""

from __future__ import annotations

import json
import threading
import traceback
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import policy as policy_mod
from . import reporting, synth
from .engine import DEFAULT_CUTOFF, DEFAULT_START, build_labels, build_sample, label_config, sample_config
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
JOBS: dict[str, threading.Thread] = {}
JOB_LOCK = threading.Lock()


class App:
    def __init__(self, db: str):
        self.db = db
        self.store = Store(db)
        self.store.initialize()

    # ---------- 读取 ----------

    def state(self, q: dict[str, list[str]]) -> dict[str, Any]:
        store = self.store
        cutoff = (q.get("cutoff") or [DEFAULT_CUTOFF.isoformat()])[0]
        mode = (q.get("mode") or ["batch_holdout"])[0]
        sample = store.latest_sample(cutoff, mode)
        label_batch = None
        report = None
        policy_id = (q.get("policy") or [store.active_policy_id() or ""])[0]
        if sample:
            label_batch = store.latest_label_batch(sample["sample_run_id"])
            if label_batch:
                for rep in store.reports(100):
                    if (
                        rep["sample_run_id"] == sample["sample_run_id"]
                        and rep["mode"] == mode
                        and rep["policy_id"] == policy_id
                    ):
                        report = store.report(rep["report_id"])
                        break
                if not report:
                    for rep in store.reports(100):
                        if rep["sample_run_id"] == sample["sample_run_id"] and rep["mode"] == mode:
                            report = store.report(rep["report_id"])
                            break
        stats = json.loads(sample["stats_json"]) if sample else None
        leakage = None
        if sample:
            rows = store.sample_rows(sample["sample_run_id"])
            blocked_rows = [r for r in rows if r["visibility"].get("blocked_late_records")]
            examples = [
                {"application_id": r["application_id"], **ex}
                for r in blocked_rows
                for ex in r["visibility"].get("blocked_examples", [])[:1]
            ][:6]
            leakage = {
                "rows_with_blocked": len(blocked_rows),
                "blocked_total": sum(r["visibility"].get("blocked_late_records", 0) for r in rows),
                "late_records_after_cutoff": stats.get("late_records_after_cutoff"),
                "feature_late": stats.get("reasons", {}).get("feature_late", 0),
                "examples": examples,
            }
        return {
            "cutoffs": self._cutoffs(),
            "modes": [
                {"id": "batch_holdout", "label": "按申请批次留出"},
                {"id": "daily_rolling", "label": "按日滚动回测"},
            ],
            "cutoff": cutoff,
            "mode": mode,
            "policies": [{k: p[k] for k in ("policy_id", "name", "version", "definition_hash", "active")} for p in store.policies()],
            "policy_id": policy_id,
            "sample": (
                {
                    "sample_run_id": sample["sample_run_id"],
                    "cutoff": sample["cutoff"],
                    "config": json.loads(sample["config_json"]),
                    "stats": stats,
                    "sealed_hash": sample["sealed_hash"],
                    "created_at": sample["created_at"],
                }
                if sample
                else None
            ),
            "labels": (
                {
                    "label_batch_id": label_batch["label_batch_id"],
                    "stats": json.loads(label_batch["stats_json"]),
                    "config": json.loads(label_batch["label_config_json"]),
                    "sealed_hash": label_batch["sealed_hash"],
                    "created_at": label_batch["created_at"],
                }
                if label_batch
                else None
            ),
            "report": report,
            "leakage": leakage,
            "reports": store.reports(30),
            "runs": store.runs(30),
            "audits": store.audits(15),
            "storage": store.storage_stats(),
            "disclaimer": "全部为虚构客户与合成数据，仅用于方法演示，不构成任何真实授信建议。",
        }

    def _cutoffs(self) -> list[str]:
        store = self.store
        rows = store.reports(100)
        candidates = {DEFAULT_CUTOFF.isoformat()}
        conn = store.connect()
        try:
            for r in conn.execute("SELECT DISTINCT cutoff FROM samples"):
                candidates.add(r["cutoff"])
        finally:
            conn.close()
        for r in rows:
            candidates.add(r["cutoff"])
        snapshot = store.meta("snapshot_date") or DEFAULT_CUTOFF.isoformat()
        candidates.add(snapshot)
        return sorted(c for c in candidates if c and c <= snapshot)

    def trace(self, sample_run_id: str, application_id: str) -> dict[str, Any]:
        store = self.store
        sample = store.sample(sample_run_id)
        if not sample:
            raise KeyError("样本不存在")
        row = next((r for r in store.sample_rows(sample_run_id) if r["application_id"] == application_id), None)
        if not row:
            raise KeyError("该申请不在本样本中")
        batch = store.latest_label_batch(sample_run_id)
        label = None
        if batch:
            label = next((x for x in store.labels(batch["label_batch_id"]) if x["application_id"] == application_id), None)
        events = store.all_events(application_id)
        decision = row["decision_on"]
        cutoff = sample["cutoff"]
        visible, late, afterward = [], [], []
        for e in events:
            rec = {
                "event_id": e["event_id"],
                "kind": e["kind"],
                "kind_label": {
                    "approval": "审批",
                    "disbursement": "放款",
                    "repayment": "还款",
                    "delinquency": "逾期",
                    "correction": "贷后更正",
                }.get(e["kind"], e["kind"]),
                "occurred_on": e["occurred_on"],
                "recorded_on": e["recorded_on"],
                "amount": e["amount"],
                "detail": e["detail"],
                "dpd": int(e["detail"]) if e["kind"] == "delinquency" and str(e["detail"]).isdigit() else None,
            }
            if decision and e["occurred_on"] <= decision and e["recorded_on"] <= decision:
                rec["visibility"] = "visible"
                visible.append(rec)
            elif decision and e["occurred_on"] <= decision and e["recorded_on"] > decision and e["recorded_on"] <= cutoff:
                # 业务发生在前、入库在审批后，但在本样本快照内可见：属于必须隔离的晚到记录。
                rec["visibility"] = "blocked"
                late.append(rec)
            elif decision and e["occurred_on"] <= decision and e["recorded_on"] > cutoff:
                rec["visibility"] = "future"
                afterward.append(rec)
            else:
                rec["visibility"] = "after"
                afterward.append(rec)
        pol_id = store.active_policy_id()
        evaluation = None
        if pol_id and row["features"]:
            evaluation = policy_mod.evaluate(store.policy(pol_id)["definition"], row["features"])
        customer = None
        conn = store.connect()
        try:
            cr = conn.execute("SELECT * FROM applications WHERE application_id=?", (application_id,)).fetchone()
            if cr:
                customer = dict(cr)
        finally:
            conn.close()
        return {
            "application": customer,
            "sample_row": row,
            "label": label,
            "label_batch": (
                {"label_batch_id": batch["label_batch_id"], "as_of": batch["cutoff"]} if batch else None
            ),
            "events": events,
            "visible_events": visible,
            "blocked_events": late,
            "after_decision_events": afterward[:20],
            "evaluation": evaluation,
            "policy": store.policy(pol_id) if pol_id else None,
        }

    def applications(self, sample_run_id: str, q: str) -> list[dict[str, Any]]:
        rows = self.store.sample_rows(sample_run_id)
        q = (q or "").strip().upper()
        out = []
        for r in rows:
            if not q or q in r["application_id"].upper() or q in r["customer_id"].upper():
                out.append(
                    {
                        "application_id": r["application_id"],
                        "customer_id": r["customer_id"],
                        "eligible": r["eligible"],
                        "reason_code": r["reason_code"],
                        "decision_on": r["decision_on"],
                        "blocked": bool(r["visibility"].get("blocked_late_records")),
                    }
                )
        return out[:1200]

    # ---------- 任务 ----------

    def start_job(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        with JOB_LOCK:
            if any(t.is_alive() for t in JOBS.values()):
                raise ValueError("已有任务在运行，请等待完成或从中断处恢复")
        run_id, fn, args = self._prepare(kind, payload)
        self._launch(run_id, fn, args)
        return {"run_id": run_id, "status": "running"}

    def resume_job(self, run_id: str) -> dict[str, Any]:
        run = self.store.run(run_id)
        if run["status"] == "done":
            return {"run_id": run_id, "status": "done", "note": "任务已完成，无需恢复"}
        if run["status"] == "running":
            with JOB_LOCK:
                alive = run_id in JOBS and JOBS[run_id].is_alive()
            if alive:
                return {"run_id": run_id, "status": "running", "note": "任务仍在运行"}
            # 进程重启会留下“运行中”但实际已停止的任务，按中断处理。
            self.store.finish_run(run_id, "interrupted", error="任务进程已丢失，按中断处理")
        if run["kind"] == "sample":
            cfg = run["params"]
            return self.start_job(
                "sample",
                {
                    "cutoff": cfg["cutoff"],
                    "start": cfg.get("start"),
                    "mode": cfg.get("mode"),
                    "performance_months": cfg.get("performance_months"),
                    "settlement_lag_days": cfg.get("settlement_lag_days"),
                    "holdout_batches": cfg.get("holdout_batches"),
                    "rolling_windows": cfg.get("rolling_windows"),
                    "resume_from": run_id,
                },
            )
        if run["kind"] == "labels":
            lcfg = run["params"].get("config", {})
            return self.start_job(
                "labels",
                {
                    "sample_id": run["sample_run_id"],
                    "as_of": run["cutoff"],
                    "performance_months": lcfg.get("performance_months"),
                    "settlement_lag_days": lcfg.get("settlement_lag_days"),
                    "bad_dpd": lcfg.get("bad_dpd"),
                    "resume_from": run_id,
                },
            )
        if run["kind"] == "backtest":
            return self.start_job(
                "backtest",
                {
                    "sample_id": run["sample_run_id"],
                    "label_id": run["label_batch_id"],
                    "policy_id": run["policy_id"],
                    "mode": run["params"].get("mode", "batch_holdout"),
                    "cutoff": run["cutoff"],
                    "rolling_windows": run["params"].get("rolling_windows"),
                    "holdout_batches": run["params"].get("holdout_batches"),
                    "resume_from": run_id,
                },
            )
        if run["kind"] == "publish":
            return self.start_job("publish", {"report_id": run["params"]["report_id"]})
        raise ValueError(f"不支持恢复的任务类型: {run['kind']}")

    def _prepare(self, kind: str, payload: dict[str, Any]):
        store = self.store
        fail_after = payload.get("fail_after")
        if kind == "sample":
            cutoff = date.fromisoformat(payload["cutoff"])
            mode = payload.get("mode") or "batch_holdout"
            cfg = sample_config(
                cutoff,
                start=date.fromisoformat(payload.get("start") or DEFAULT_START.isoformat()),
                mode=mode,
                performance_months=int(payload.get("performance_months") or 6),
                settlement_lag_days=int(payload.get("settlement_lag_days") or 30),
                holdout_batches=int(payload.get("holdout_batches") or 2),
                rolling_windows=int(payload.get("rolling_windows") or 6),
                dataset_hash=store.dataset_hash(),
            )
            run_id = store.create_run("sample", cutoff=cfg["cutoff"], params=cfg)
            return run_id, build_sample, {"store": store, "config": cfg, "fail_after": fail_after, "run_id": run_id}
        if kind == "labels":
            sample = store.sample(payload["sample_id"])
            if not sample:
                raise KeyError("样本不存在")
            lcfg = label_config(
                performance_months=int(payload.get("performance_months") or 6),
                settlement_lag_days=int(payload.get("settlement_lag_days") or 30),
                bad_dpd=int(payload.get("bad_dpd") or 30),
            )
            as_of = date.fromisoformat(payload.get("as_of") or synth.SNAPSHOT.isoformat())
            batch_key = {"sample_run_id": sample["sample_run_id"], "config": lcfg, "as_of": as_of.isoformat()}
            run_id = store.create_run(
                "labels",
                cutoff=as_of.isoformat(),
                params=batch_key,
                sample_run_id=sample["sample_run_id"],
            )
            return (
                run_id,
                build_labels,
                {
                    "store": store,
                    "sample_run_id": sample["sample_run_id"],
                    "config": lcfg,
                    "as_of": as_of,
                    "fail_after": fail_after,
                    "run_id": run_id,
                },
            )
        if kind == "backtest":
            sample = store.sample(payload["sample_id"])
            batch = store.label_batch(payload["label_id"])
            if not sample or not batch:
                raise KeyError("样本或标签批次不存在")
            if sample["sample_run_id"] != batch["sample_run_id"]:
                raise ValueError("标签批次不属于该样本")
            cutoff = date.fromisoformat(payload.get("cutoff") or sample["cutoff"])
            run_id = store.create_run(
                "backtest",
                cutoff=cutoff.isoformat(),
                params={
                    "mode": payload.get("mode", "batch_holdout"),
                    "rolling_windows": int(payload.get("rolling_windows") or 6),
                    "holdout_batches": int(payload.get("holdout_batches") or 2),
                },
                policy_id=payload["policy_id"],
                sample_run_id=sample["sample_run_id"],
                label_batch_id=batch["label_batch_id"],
            )
            return (
                run_id,
                reporting.run_backtest,
                {
                    "store": store,
                    "sample_run_id": sample["sample_run_id"],
                    "label_batch_id": batch["label_batch_id"],
                    "policy_id": payload["policy_id"],
                    "mode": payload.get("mode", "batch_holdout"),
                    "cutoff": cutoff,
                    "rolling_windows": int(payload.get("rolling_windows") or 6),
                    "holdout_batches": int(payload.get("holdout_batches") or 2),
                    "fail_after": fail_after,
                    "run_id": run_id,
                },
            )
        if kind == "publish":
            run_id = store.create_run("publish", cutoff=None, params={"report_id": payload["report_id"]})
            return (
                run_id,
                reporting.publish_report,
                {"store": store, "report_id": payload["report_id"], "run_id": run_id},
            )
        raise ValueError(f"未知任务类型: {kind}")

    def _launch(self, run_id: str, fn, args: dict[str, Any]) -> None:
        def target() -> None:
            store = args.pop("store")
            try:
                fn(store, **args)
            except Exception as exc:  # 失败原因写入 runs.error，由前端展示
                store.audit(
                    "web",
                    "job.failed",
                    {"run_id": run_id, "error": str(exc), "trace": traceback.format_exc(limit=4)},
                )
            finally:
                with JOB_LOCK:
                    JOBS.pop(run_id, None)

        with JOB_LOCK:
            if run_id in JOBS and JOBS[run_id].is_alive():
                return
            t = threading.Thread(target=target, name=run_id, daemon=True)
            JOBS[run_id] = t
            t.start()

    def compare(self, left: str, right: str) -> dict[str, Any]:
        return reporting.compare_reports(self.store, left, right)


APP: App | None = None


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RiskLabLocal/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
            print(f"[web] {self.address_string()} {fmt % args}")

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: object) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            q = parse_qs(parsed.query)
            try:
                if parsed.path in ("/", "/index.html"):
                    body = (WEB / "index.html").read_bytes()
                    return self._send(200, body, "text/html; charset=utf-8")
                if parsed.path == "/styles.css":
                    return self._send(200, (WEB / "styles.css").read_bytes(), "text/css; charset=utf-8")
                if parsed.path == "/app.js":
                    return self._send(200, (WEB / "app.js").read_bytes(), "application/javascript; charset=utf-8")
                if parsed.path == "/api/state":
                    return self._json(200, app.state(q))
                if parsed.path.startswith("/api/sample/") and "/application/" in parsed.path:
                    parts = parsed.path.split("/")
                    sample_id, app_id = parts[3], parts[5]
                    return self._json(200, app.trace(sample_id, app_id))
                if parsed.path == "/api/applications":
                    sid = q.get("sample_id", [""])[0]
                    return self._json(200, {"items": app.applications(sid, q.get("q", [""])[0])})
                if parsed.path == "/api/compare":
                    return self._json(200, app.compare(q.get("left", [""])[0], q.get("right", [""])[0]))
                if parsed.path == "/api/jobs":
                    return self._json(200, {"runs": app.store.runs(30)})
                return self._json(404, {"error": "not found"})
            except KeyError as exc:
                return self._json(404, {"error": str(exc)})
            except Exception as exc:
                return self._json(500, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "请求体不是合法 JSON"})
            try:
                if parsed.path == "/api/actions/sample":
                    return self._json(200, app.start_job("sample", payload))
                if parsed.path == "/api/actions/labels":
                    return self._json(200, app.start_job("labels", payload))
                if parsed.path == "/api/actions/backtest":
                    return self._json(200, app.start_job("backtest", payload))
                if parsed.path == "/api/actions/publish":
                    return self._json(200, app.start_job("publish", payload))
                if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/resume"):
                    run_id = parsed.path.split("/")[3]
                    return self._json(200, app.resume_job(run_id))
                return self._json(404, {"error": "not found"})
            except (KeyError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:
                return self._json(500, {"error": str(exc)})

    return Handler


def serve(db: str = "var/risklab.sqlite3", host: str = "127.0.0.1", port: int = 8765) -> int:
    global APP
    APP = App(db)
    handler = make_handler(APP)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"RiskLab 已启动: http://{host}:{port}  (数据库 {Path(db).resolve()})")
    print("全部数据为虚构客户，仅用于方法演示。Ctrl+C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0

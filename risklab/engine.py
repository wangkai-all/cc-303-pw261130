"""样本构建、标签回补与回测报告的时点隔离核心。

三条硬约束：
1. 特征只使用审批决策日（含）之前“已入库且已发生”的记录；
2. 观察期未满（含结算缓冲）的样本只能记为删失，绝不能记为 good；
3. 样本、标签批次、报告分别带内容摘要，报告只能引用创建时校验过的版本。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from .store import Store
from .util import digest, stable_json, utc_now

DEFAULT_START = date(2024, 1, 1)
DEFAULT_CUTOFF = date(2026, 6, 30)
DEFAULT_PERFORMANCE_MONTHS = 6
DEFAULT_SETTLEMENT_LAG_DAYS = 30
DEFAULT_BAD_DPD = 30


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def sample_config(
    cutoff: date,
    *,
    start: date = DEFAULT_START,
    mode: str = "batch_holdout",
    performance_months: int = DEFAULT_PERFORMANCE_MONTHS,
    settlement_lag_days: int = DEFAULT_SETTLEMENT_LAG_DAYS,
    holdout_batches: int = 2,
    rolling_windows: int = 6,
    dataset_hash: str = "",
) -> dict[str, Any]:
    if mode not in ("batch_holdout", "daily_rolling"):
        raise ValueError("mode 必须是 batch_holdout 或 daily_rolling")
    if start >= cutoff:
        raise ValueError("样本起点必须早于截止日")
    return {
        "start": start.isoformat(),
        "cutoff": cutoff.isoformat(),
        "mode": mode,
        "performance_months": performance_months,
        "settlement_lag_days": settlement_lag_days,
        "holdout_batches": holdout_batches,
        "rolling_windows": rolling_windows,
        "dataset_hash": dataset_hash,
    }


def label_config(
    *,
    performance_months: int = DEFAULT_PERFORMANCE_MONTHS,
    settlement_lag_days: int = DEFAULT_SETTLEMENT_LAG_DAYS,
    bad_dpd: int = DEFAULT_BAD_DPD,
    include_early_bad: bool = True,
) -> dict[str, Any]:
    return {
        "performance_months": performance_months,
        "settlement_lag_days": settlement_lag_days,
        "bad_dpd": bad_dpd,
        "include_early_bad": include_early_bad,
        "version": "label-v1",
    }


def _window_end(disburse: date, months: int) -> date:
    return disburse + timedelta(days=30 * months)


# ---------------------------------------------------------------------------
# 样本构建
# ---------------------------------------------------------------------------


def _load_cutoff(store: Store, cutoff: date) -> tuple[dict[str, dict], list[dict], dict[str, list[dict]]]:
    conn = store.connect()
    try:
        apps = {
            r["application_id"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM applications WHERE applied_at<=? AND recorded_on<=?",
                (cutoff.isoformat(), cutoff.isoformat()),
            )
        }
        events = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM events WHERE recorded_on<=? AND occurred_on<=?",
                (cutoff.isoformat(), cutoff.isoformat()),
            )
        ]
    finally:
        conn.close()
    by_app: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_app[e["application_id"]].append(e)
    return apps, events, by_app


def _first_event(events: list[dict], kind: str) -> dict | None:
    candidates = [e for e in events if e["kind"] == kind]
    if not candidates:
        return None
    return sorted(candidates, key=lambda e: (e["occurred_on"], e["recorded_on"], e["event_id"]))[0]


def build_sample(
    store: Store,
    config: dict[str, Any],
    *,
    fail_after: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """构建并封存样本；同一配置幂等复用已封存样本。"""
    config = dict(config)
    sample_run_id = "SMP-" + digest(config, 12)
    run_id = run_id or store.create_run("sample", cutoff=config["cutoff"], params=config)

    def step(name: str) -> None:
        store.set_step(run_id, name, "done")
        if fail_after == name:
            raise RuntimeError(f"模拟中断：已完成 {name} 后停止")

    try:
        cutoff = date.fromisoformat(config["cutoff"])
        start = date.fromisoformat(config["start"])
        existing = store.sample(sample_run_id)
        if existing:
            result = {"sample_run_id": sample_run_id, "reused": True, "stats": json.loads(existing["stats_json"])}
            step("load_raw")
            step("cohort")
            step("features")
            step("persist_sample")
            store.finish_run(run_id, "done", result=result)
            store.audit("job", "sample.reuse", {"run_id": run_id, "sample_run_id": sample_run_id})
            return result

        step("load_raw")
        apps, all_events, by_app = _load_cutoff(store, cutoff)
        # 全量事件（含截止日之后才入库的）只用于识别“被隔离的晚到记录”，绝不进入特征。
        conn = store.connect()
        try:
            every_event = [dict(r) for r in conn.execute("SELECT * FROM events")]
        finally:
            conn.close()
        all_by_app: dict[str, list[dict]] = defaultdict(list)
        for e in every_event:
            all_by_app[e["application_id"]].append(e)
        later_records = [
            e for e in every_event if e["recorded_on"] > cutoff.isoformat() and e["occurred_on"] <= cutoff.isoformat()
        ]

        step("cohort")
        rows: list[dict[str, Any]] = []
        cohort_ids = [aid for aid, a in apps.items() if start <= date.fromisoformat(a["applied_at"]) <= cutoff]
        # 客户历史（仅审批时点可见）用于解释，不参与基础评分。
        by_customer: dict[str, list[dict]] = defaultdict(list)
        for a in apps.values():
            by_customer[a["customer_id"]].append(a)
        bad_by_customer: dict[str, list[dict]] = defaultdict(list)
        for e in all_events:
            if e["kind"] == "delinquency" and int(e["detail"] or 0) >= DEFAULT_BAD_DPD:
                app = apps.get(e["application_id"])
                if app:
                    bad_by_customer[app["customer_id"]].append(e)

        for aid in sorted(cohort_ids):
            a = apps[aid]
            evs = by_app.get(aid, [])
            approval = _first_event(evs, "approval")
            reason_code, reason_text, eligible = "eligible", "入选：审批通过且已放款，时点可见", True
            decision_on: date | None = None
            disburse: dict | None = None
            if not approval:
                reason_code, reason_text, eligible = "decision_pending", "排除：截止日前没有可见的审批结果", False
            else:
                decision_on = date.fromisoformat(approval["occurred_on"])
                if approval["detail"] == "declined":
                    reason_code, reason_text, eligible = "declined", "排除：审批拒绝，无贷后表现", False
                else:
                    disburse = _first_event(evs, "disbursement")
                    if not disburse:
                        reason_code, reason_text, eligible = "disburse_pending", "排除：审批通过但截止日前无可见放款", False
                    elif date.fromisoformat(a["recorded_on"]) > decision_on:
                        reason_code, reason_text, eligible = (
                            "feature_late",
                            "排除：申请主档入库晚于审批决策日，审批时不可见",
                            False,
                        )

            features: dict[str, Any] = {}
            visibility: dict[str, Any] = {}
            if decision_on:
                # 历史申请也必须在决策日已入库，否则会用到审批后才补录的主档。
                prior_apps = [
                    x
                    for x in by_customer.get(a["customer_id"], [])
                    if x["application_id"] != aid
                    and date.fromisoformat(x["applied_at"]) < date.fromisoformat(a["applied_at"])
                    and date.fromisoformat(x["recorded_on"]) <= decision_on
                ]
                prior_bad = [
                    e
                    for e in bad_by_customer.get(a["customer_id"], [])
                    if e["application_id"] != aid
                    and date.fromisoformat(e["recorded_on"]) <= decision_on
                    and date.fromisoformat(e["occurred_on"]) <= decision_on
                ]
                # 晚到记录：业务发生不晚于决策日，但入库晚于决策日。
                # 它们可能早于“样本截止日”，因此不能只查截止日之后的记录。
                # 只看贷后/历史类事件；审批与放款本身不进入特征，单列不算泄漏。
                # 严格在截止日快照之内识别：业务日不晚于决策日、入库晚于决策日。
                blocked = [
                    e
                    for e in all_by_app.get(aid, [])
                    if e["kind"] in ("delinquency", "correction", "repayment")
                    and date.fromisoformat(e["occurred_on"]) <= decision_on
                    and date.fromisoformat(e["recorded_on"]) > decision_on
                    and date.fromisoformat(e["recorded_on"]) <= cutoff
                ]
                # 这些记录在本次样本截止日快照里还不可见，只作为“未来才到”的计数，不参与任何判断。
                not_in_snapshot = [
                    e
                    for e in all_by_app.get(aid, [])
                    if date.fromisoformat(e["recorded_on"]) > cutoff
                    and date.fromisoformat(e["occurred_on"]) <= decision_on
                ]
                workflow_late = [
                    e
                    for e in all_by_app.get(aid, [])
                    if e["kind"] in ("approval", "disbursement")
                    and date.fromisoformat(e["recorded_on"]) > decision_on
                    and date.fromisoformat(e["recorded_on"]) <= cutoff
                ]
                features = {
                    "income_band": a["income_band"],
                    "bureau_grade": a["bureau_grade"],
                    "employment_years": a["employment_years"],
                    "existing_debt": a["existing_debt"],
                    "term_months": a["term_months"],
                    "product": a["product"],
                    "channel": a["channel"],
                    "city_tier": a["city_tier"],
                    "amount": a["amount"],
                    "prior_loans": len(prior_apps),
                    "prior_bad_events": len(prior_bad),
                    "prior_max_dpd": max((int(e["detail"] or 0) for e in prior_bad), default=0),
                }
                visibility = {
                    "as_of_decision": decision_on.isoformat(),
                    "rule": "recorded_on<=决策日 且 occurred_on<=决策日",
                    "master_recorded_on": a["recorded_on"],
                    "blocked_late_records": len(blocked),
                    "not_in_snapshot_records": len(not_in_snapshot),
                    "workflow_late_records": len(workflow_late),
                    "blocked_examples": [
                        {"event_id": e["event_id"], "kind": e["kind"], "occurred_on": e["occurred_on"], "recorded_on": e["recorded_on"]}
                        for e in blocked[:3]
                    ],
                    "history_events_used": len(prior_bad),
                }

            rows.append(
                {
                    "application_id": aid,
                    "customer_id": a["customer_id"],
                    "applied_on": a["applied_at"],
                    "decision_on": decision_on.isoformat() if decision_on else None,
                    "disburse_on": disburse["occurred_on"] if disburse else None,
                    "week": (date.fromisoformat(a["applied_at"]) - timedelta(days=date.fromisoformat(a["applied_at"]).weekday())).isoformat(),
                    "eligible": eligible,
                    "reason_code": reason_code,
                    "reason_text": reason_text,
                    "features": features,
                    "visibility": visibility,
                }
            )
        rows.sort(key=lambda r: r["application_id"])
        step("features")
        store.set_step(run_id, "features", "done", f"{sum(1 for r in rows if r['eligible'])}/{len(rows)} 笔入选")

        stats = _sample_stats(rows, later_records)
        sealed = digest({"config": config, "rows": rows}, 32)
        conn = store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO samples(sample_run_id, cutoff, mode, label_config_json, label_config_hash, config_json,
                    sealed_hash, stats_json, created_at, application_count, eligible_count, matured_count)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sample_run_id,
                    config["cutoff"],
                    config["mode"],
                    "{}",
                    "",
                    stable_json(config),
                    sealed,
                    stable_json(stats),
                    utc_now(),
                    len(rows),
                    stats["eligible"],
                    0,
                ),
            )
            conn.executemany(
                """INSERT INTO sample_rows(sample_run_id, application_id, customer_id, applied_on, decision_on,
                    disburse_on, week, eligible, reason_code, reason_text, features_json, visibility_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        sample_run_id,
                        r["application_id"],
                        r["customer_id"],
                        r["applied_on"],
                        r["decision_on"],
                        r["disburse_on"],
                        r["week"],
                        1 if r["eligible"] else 0,
                        r["reason_code"],
                        r["reason_text"],
                        stable_json(r["features"]),
                        stable_json(r["visibility"]),
                    )
                    for r in rows
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        step("persist_sample")
        result = {"sample_run_id": sample_run_id, "reused": False, "stats": stats, "run_id": run_id}
        store.finish_run(run_id, "done", result=result)
        store.audit("job", "sample.build", {"run_id": run_id, "sample_run_id": sample_run_id, "stats": stats})
        return result
    except Exception as exc:
        store.finish_run(run_id, "interrupted", error=str(exc))
        store.audit("job", "sample.error", {"run_id": run_id, "error": str(exc)})
        raise


def _sample_stats(rows: list[dict], later_records: list[dict]) -> dict[str, Any]:
    reasons = Counter(r["reason_code"] for r in rows)
    eligible = [r for r in rows if r["eligible"]]
    months: Counter[str] = Counter(r["decision_on"][:7] for r in eligible if r["decision_on"])
    blocked = sum(r["visibility"].get("blocked_late_records", 0) for r in rows)
    workflow_late = sum(r["visibility"].get("workflow_late_records", 0) for r in rows)
    not_in_snapshot = sum(r["visibility"].get("not_in_snapshot_records", 0) for r in rows)
    return {
        "cohort": len(rows),
        "eligible": len(eligible),
        "reasons": dict(sorted(reasons.items())),
        "reason_labels": {
            "eligible": "入选",
            "decision_pending": "审批未入库/未完成",
            "declined": "审批拒绝",
            "disburse_pending": "放款未入库",
            "feature_late": "申请主档晚于审批入库",
        },
        "decision_months": dict(sorted(months.items())),
        "late_records_excluded": blocked,
        "workflow_late_records": workflow_late,
        "not_in_snapshot_records": not_in_snapshot,
        "late_records_after_cutoff": len(later_records),
    }


# ---------------------------------------------------------------------------
# 标签回补
# ---------------------------------------------------------------------------


def build_labels(
    store: Store,
    sample_run_id: str,
    config: dict[str, Any],
    *,
    as_of: date,
    fail_after: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    batch_key = {"sample_run_id": sample_run_id, "config": config, "as_of": as_of.isoformat()}
    label_batch_id = "LBL-" + digest(batch_key, 12)
    sample = store.sample(sample_run_id)
    if not sample:
        raise KeyError(f"未知样本: {sample_run_id}")
    run_id = run_id or store.create_run(
        "labels", cutoff=as_of.isoformat(), params=batch_key, sample_run_id=sample_run_id
    )

    def step(name: str) -> None:
        store.set_step(run_id, name, "done")
        if fail_after == name:
            raise RuntimeError(f"模拟中断：已完成 {name} 后停止")

    try:
        existing = store.label_batch(label_batch_id)
        if existing:
            step("load_sample")
            step("locate_events")
            step("classify")
            step("persist_labels")
            result = {"label_batch_id": label_batch_id, "reused": True, "stats": json.loads(existing["stats_json"])}
            store.finish_run(run_id, "done", result=result)
            return result

        step("load_sample")
        rows = store.sample_rows(sample_run_id)
        eligible = [r for r in rows if r["eligible"]]
        conn = store.connect()
        try:
            all_events = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM events WHERE recorded_on<=? AND occurred_on<=?",
                    (as_of.isoformat(), as_of.isoformat()),
                )
            ]
        finally:
            conn.close()
        step("locate_events")
        by_app: dict[str, list[dict]] = defaultdict(list)
        for e in all_events:
            by_app[e["application_id"]].append(e)

        months = int(config["performance_months"])
        lag = int(config["settlement_lag_days"])
        dpd_threshold = int(config["bad_dpd"])
        labels: list[dict[str, Any]] = []
        for r in eligible:
            disburse = date.fromisoformat(r["disburse_on"])
            window_end = _window_end(disburse, months)
            relevant = [
                e
                for e in by_app.get(r["application_id"], [])
                if e["kind"] == "delinquency"
                and disburse <= date.fromisoformat(e["occurred_on"]) <= window_end
            ]
            bads = [e for e in relevant if int(e["detail"] or 0) >= dpd_threshold]
            max_dpd = max((int(e["detail"] or 0) for e in relevant), default=0)
            evidence = {
                "window_start": disburse.isoformat(),
                "window_end": window_end.isoformat(),
                "as_of": as_of.isoformat(),
                "max_dpd": max_dpd,
                "bad_events": [
                    {"event_id": e["event_id"], "occurred_on": e["occurred_on"], "recorded_on": e["recorded_on"], "dpd": int(e["detail"] or 0)}
                    for e in bads
                ],
                "delinquency_events": len(relevant),
                "policy_rule": f"放款日后 {months} 个月内 DPD>={dpd_threshold} 记为坏；坏账可提前确认，好客户必须满观察期+{lag}天结算缓冲",
            }
            if bads:
                status = "bad"
                maturity = "matured" if window_end <= as_of else "early_bad"
                # 确认日取最早“已入库”的坏事件日：发生早但晚入库的信息在 as_of 不可见。
                matured_on = min(date.fromisoformat(e["recorded_on"]) for e in bads).isoformat()
                if maturity == "early_bad" and not config.get("include_early_bad", True):
                    status = "censored"
                    maturity = "early_bad_excluded"
                label = 1 if status == "bad" else None
            elif window_end + timedelta(days=lag) <= as_of:
                status, maturity, label = "good", "matured", 0
                matured_on = (window_end + timedelta(days=lag)).isoformat()
            else:
                days_left = (window_end + timedelta(days=lag) - as_of).days
                status, maturity, label = "censored", "immature", None
                matured_on = (window_end + timedelta(days=lag)).isoformat()
                evidence["days_to_maturity"] = days_left
                evidence["censored_reason"] = "观察期或结算缓冲未满，不能计为好客户"
            evidence["maturity"] = maturity
            labels.append(
                {
                    "application_id": r["application_id"],
                    "status": status,
                    "label": label,
                    "matured_on": matured_on,
                    "window_start": disburse.isoformat(),
                    "window_end": window_end.isoformat(),
                    "evidence": evidence,
                }
            )

        stats = {
            "good": sum(1 for x in labels if x["status"] == "good"),
            "bad": sum(1 for x in labels if x["status"] == "bad"),
            "censored": sum(1 for x in labels if x["status"] == "censored"),
            "eligible_for_metrics": sum(1 for x in labels if x["status"] in ("good", "bad")),
            "as_of": as_of.isoformat(),
            "config": config,
            "window_end_max": max((x["window_end"] for x in labels), default=None),
            "early_bad": sum(1 for x in labels if x["evidence"].get("maturity") == "early_bad"),
        }
        labels.sort(key=lambda x: x["application_id"])
        step("classify")
        sealed = digest({"sample_run_id": sample_run_id, "config": config, "labels": labels}, 32)
        conn = store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO label_batches(label_batch_id, sample_run_id, cutoff, label_config_json,
                    label_config_hash, sealed_hash, stats_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    label_batch_id,
                    sample_run_id,
                    as_of.isoformat(),
                    stable_json(config),
                    digest(config, 20),
                    sealed,
                    stable_json(stats),
                    utc_now(),
                ),
            )
            conn.executemany(
                """INSERT INTO labels(label_batch_id, application_id, status, label, matured_on,
                    window_start, window_end, evidence_json) VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (
                        label_batch_id,
                        x["application_id"],
                        x["status"],
                        x["label"],
                        x["matured_on"],
                        x["window_start"],
                        x["window_end"],
                        stable_json(x["evidence"]),
                    )
                    for x in labels
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        step("persist_labels")
        result = {"label_batch_id": label_batch_id, "reused": False, "stats": stats, "run_id": run_id}
        store.finish_run(run_id, "done", result=result)
        store.audit("job", "labels.build", {"run_id": run_id, "label_batch_id": label_batch_id, "stats": stats})
        return result
    except Exception as exc:
        store.finish_run(run_id, "interrupted", error=str(exc))
        store.audit("job", "labels.error", {"run_id": run_id, "error": str(exc)})
        raise

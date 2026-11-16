"""时点回测（申请批次留出 / 按日滚动）与不可变报告。

报告先构建为带摘要的工件，再单独发布。构建时会重新校验样本与标签批次的
封存摘要，并同时校验报告引用的规则版本摘要，三者任一不匹配即拒绝构建。
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from . import policy as policy_mod
from .store import Store
from .util import digest, month_ends, pct, stable_json, utc_now


# ---------------------------------------------------------------------------
# 完整性
# ---------------------------------------------------------------------------


def verify_sample(store: Store, sample_run_id: str) -> dict[str, Any]:
    sample = store.sample(sample_run_id)
    if not sample:
        raise ValueError(f"样本不存在: {sample_run_id}")
    rows = sorted(store.sample_rows(sample_run_id), key=lambda r: r["application_id"])
    expected = digest(
        {
            "config": json.loads(sample["config_json"]),
            "rows": [
                {
                    "application_id": r["application_id"],
                    "customer_id": r["customer_id"],
                    "applied_on": r["applied_on"],
                    "decision_on": r["decision_on"],
                    "disburse_on": r["disburse_on"],
                    "week": r["week"],
                    "eligible": r["eligible"],
                    "reason_code": r["reason_code"],
                    "reason_text": r["reason_text"],
                    "features": r["features"],
                    "visibility": r["visibility"],
                }
                for r in rows
            ],
        },
        32,
    )
    ok = expected == sample["sealed_hash"]
    return {"ok": ok, "expected": expected, "stored": sample["sealed_hash"], "row_count": len(rows)}


def verify_labels(store: Store, label_batch_id: str) -> dict[str, Any]:
    batch = store.label_batch(label_batch_id)
    if not batch:
        raise ValueError(f"标签批次不存在: {label_batch_id}")
    labels = store.labels(label_batch_id)
    expected = digest(
        {
            "sample_run_id": batch["sample_run_id"],
            "config": json.loads(batch["label_config_json"]),
            "labels": [
                {
                    "application_id": x["application_id"],
                    "status": x["status"],
                    "label": x["label"],
                    "matured_on": x["matured_on"],
                    "window_start": x["window_start"],
                    "window_end": x["window_end"],
                    "evidence": x["evidence"],
                }
                for x in labels
            ],
        },
        32,
    )
    return {
        "ok": expected == batch["sealed_hash"],
        "expected": expected,
        "stored": batch["sealed_hash"],
        "row_count": len(labels),
        "config_hash": batch["label_config_hash"],
    }


def _verify_policy(policy_row: dict[str, Any]) -> dict[str, Any]:
    expected = policy_mod.definition_hash(policy_row["definition"])
    return {
        "ok": expected == policy_row["definition_hash"],
        "expected": expected,
        "stored": policy_row["definition_hash"],
    }


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


def _auc(pairs: list[tuple[float, int]]) -> float | None:
    """pairs = (score, 1=好客户/0=坏客户)，分数越高风险越低。

    采用 Mann-Whitney 秩和公式，计算 P(好客户得分 > 坏客户得分)；
    仅含已成熟样本，无第三方依赖。
    """
    n_pos = sum(1 for _, y in pairs if y == 1)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    ranked = sorted(pairs, key=lambda p: p[0])
    ranks: dict[int, float] = {}
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        avg = (i + j + 2) / 2
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    rank_sum = sum(ranks[i] for i, (_, y) in enumerate(ranked) if y == 1)
    return round((rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg), 4)


def _ks(pairs: list[tuple[float, int]]) -> float | None:
    """按分数阈值扫描累计好/坏占比的最大差；分数越高应为越好。"""
    pos = [s for s, y in pairs if y == 1]
    neg = [s for s, y in pairs if y == 0]
    if not pos or not neg:
        return None
    thresholds = sorted(set(pos + neg), reverse=True)
    best = 0.0
    above_pos = above_neg = 0
    pos_at = {s: pos.count(s) for s in thresholds}
    neg_at = {s: neg.count(s) for s in thresholds}
    for t in thresholds:
        above_pos += pos_at[t]
        above_neg += neg_at[t]
        best = max(best, above_pos / len(pos) - above_neg / len(neg))
    return round(best, 4)


def _score_band(score: int) -> str:
    if score >= 750:
        return "750+"
    if score >= 700:
        return "700-749"
    if score >= 650:
        return "650-699"
    if score >= 600:
        return "600-649"
    return "<600"


def _date(value: str) -> date:
    return date.fromisoformat(value[:10])


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------


def run_backtest(
    store: Store,
    *,
    sample_run_id: str,
    label_batch_id: str,
    policy_id: str,
    mode: str,
    cutoff: date,
    fail_after: str | None = None,
    rolling_windows: int = 6,
    holdout_batches: int = 2,
    run_id: str | None = None,
) -> dict[str, Any]:
    sample = store.sample(sample_run_id)
    batch = store.label_batch(label_batch_id)
    if not sample or not batch:
        raise ValueError("样本或标签批次不存在")
    if sample["sample_run_id"] != batch["sample_run_id"]:
        raise ValueError("标签批次不属于该样本，拒绝混用")
    sample_cfg = json.loads(sample["config_json"])
    if sample_cfg["cutoff"] != cutoff.isoformat():
        raise ValueError("截止日与样本配置不一致")
    # 样本模式与回测模式必须一致：批次留出的样本被拿去滚动评估会改变窗口口径。
    if sample_cfg.get("mode") != mode:
        raise ValueError(
            f"样本模式 {sample_cfg.get('mode')} 与回测模式 {mode} 不一致，请按模式分别构建样本"
        )
    run_id = run_id or store.create_run(
        "backtest",
        cutoff=cutoff.isoformat(),
        params={"mode": mode, "rolling_windows": rolling_windows, "holdout_batches": holdout_batches},
        policy_id=policy_id,
        sample_run_id=sample_run_id,
        label_batch_id=label_batch_id,
    )

    def step(name: str) -> None:
        store.set_step(run_id, name, "done")
        if fail_after == name:
            raise RuntimeError(f"模拟中断：已完成 {name} 后停止")

    try:
        step("verify_inputs")
        checks = {
            "sample": verify_sample(store, sample_run_id),
            "labels": verify_labels(store, label_batch_id),
            "policy": _verify_policy(store.policy(policy_id)),
        }
        if not all(c["ok"] for c in checks.values()):
            raise ValueError("输入完整性校验失败：" + json.dumps({k: v["ok"] for k, v in checks.items()}))
        step("score")
        pol = store.policy(policy_id)
        rows = store.sample_rows(sample_run_id)
        labels = {x["application_id"]: x for x in store.labels(label_batch_id)}
        scored = []
        for r in rows:
            if not r["eligible"]:
                continue
            ev = policy_mod.evaluate(pol["definition"], r["features"])
            lab = labels.get(r["application_id"])
            if lab is None:
                raise ValueError(f"标签缺失: {r['application_id']}")
            scored.append({"row": r, "eval": ev, "label": lab})
        step("windows")
        segments, windows, headline = _segment(scored, mode, cutoff, rolling_windows, holdout_batches)
        step("metrics")
        headline["rule_bad_capture"] = _capture(scored)
        headline["all_eligible"] = _metrics(scored)
        headline["eval_scope"] = "holdout" if mode == "batch_holdout" else "rolling_eval"
        step("maturity")
        maturity = _maturity(store, sample_run_id, label_batch_id, cutoff)
        step("report")
        report = {
            "schema": "risklab.report/1",
            "created_at": utc_now(),
            "dataset_hash": store.dataset_hash(),
            "snapshot_date": store.meta("snapshot_date"),
            "mode": mode,
            "cutoff": cutoff.isoformat(),
            "sample": {
                "sample_run_id": sample_run_id,
                "sealed_hash": sample["sealed_hash"],
                "config": sample_cfg,
                "stats": json.loads(sample["stats_json"]),
            },
            "labels": {
                "label_batch_id": label_batch_id,
                "sealed_hash": batch["sealed_hash"],
                "config": json.loads(batch["label_config_json"]),
                "stats": json.loads(batch["stats_json"]),
            },
            "policy": {
                "policy_id": policy_id,
                "name": pol["name"],
                "version": pol["version"],
                "definition_hash": pol["definition_hash"],
                "approval_threshold": pol["definition"]["approval_threshold"],
            },
            "integrity": checks,
            "headline": headline,
            "windows": windows,
            "segments": segments,
            "maturity": maturity,
            "method": _method_note(mode, holdout_batches, rolling_windows),
            "findings": _findings(mode, headline, segments, maturity, windows),
            "disclaimer": "全部为虚构客户与合成行为，仅用于方法演示，不构成任何真实授信建议。",
        }
        manifest_hash = digest(
            {
                "sample": sample["sealed_hash"],
                "labels": batch["sealed_hash"],
                "policy": pol["definition_hash"],
                "mode": mode,
                "cutoff": cutoff.isoformat(),
                "headline": headline,
                "windows": windows,
                "segments": segments,
            },
            32,
        )
        report["manifest_hash"] = manifest_hash
        existing = store.find_report(sample_run_id, label_batch_id, policy_id, mode, manifest_hash)
        if existing:
            store.finish_run(run_id, "done", result={"report_id": existing["report_id"], "reused": True})
            return {"report_id": existing["report_id"], "reused": True, "report": existing["report"]}

        report_id = "RPT-" + digest(
            {"sample": sample_run_id, "labels": label_batch_id, "policy": policy_id, "mode": mode, "manifest": manifest_hash},
            10,
        )
        report["report_id"] = report_id
        artifact_dir = Path(os.environ.get("RISKLAB_ARTIFACT_DIR", "var/artifacts"))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"{report_id}.json"
        artifact_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        conn = store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO reports(report_id, sample_run_id, label_batch_id, policy_id, cutoff, mode,
                    report_json, manifest_hash, artifact_path, status, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    report_id,
                    sample_run_id,
                    label_batch_id,
                    policy_id,
                    cutoff.isoformat(),
                    mode,
                    stable_json(report),
                    manifest_hash,
                    str(artifact_path),
                    "built",
                    utc_now(),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        store.finish_run(run_id, "done", result={"report_id": report_id, "reused": False}, report_id=report_id)
        store.audit(
            "job",
            "backtest.build",
            {
                "run_id": run_id,
                "report_id": report_id,
                "sample_run_id": sample_run_id,
                "label_batch_id": label_batch_id,
                "policy_id": policy_id,
                "mode": mode,
            },
        )
        return {"report_id": report_id, "reused": False, "report": report}
    except Exception as exc:
        store.finish_run(run_id, "interrupted", error=str(exc))
        store.audit("job", "backtest.error", {"run_id": run_id, "error": str(exc)})
        raise


def _segment(
    scored: list[dict[str, Any]],
    mode: str,
    cutoff: date,
    rolling_windows: int,
    holdout_batches: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """划分评估窗口并返回 (segments, windows, eval_metrics)。

    规则评分本身不做训练，因此“训练/留出”只决定评估口径：
    - 批次留出：只把标签已成熟的最近 N 个申请月作为评估集；
      截止日附近批次尚未成熟，必须推迟，不能为了凑最新而使用删失样本。
    - 按日滚动：最近 N 个申请月逐月评估，同时展示各月成熟率。
    """
    eligible = [s for s in scored if s["row"]["decision_on"]]
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in eligible:
        by_month[s["row"]["decision_on"][:7]].append(s)
    months = sorted(by_month)
    month_metrics = {m: _metrics(by_month[m]) for m in months}
    matured_months = [m for m in months if (month_metrics[m]["maturity_rate"] or 0) >= 90]

    if mode == "batch_holdout":
        holdout_months = matured_months[-holdout_batches:]
        if not holdout_months:
            # 没有任何成熟批次时不硬凑留出集；报告会明确标记退化并给出警告。
            holdout_months = []
        start = holdout_months[0] if holdout_months else None
        train_months = [m for m in months if start and m < start]
        deferred_months = [m for m in months if start and m > holdout_months[-1]]
        train = [s for m in train_months for s in by_month[m]]
        holdout = [s for m in holdout_months for s in by_month[m]]
        eval_items = holdout
        windows = {
            "kind": "batch_holdout",
            "train_months": train_months,
            "holdout_months": holdout_months,
            "deferred_months": deferred_months,
            "degraded": not holdout_months,
            "degraded_reason": (
                "截止日之前没有成熟率≥90% 的申请批次，未生成留出集；请推迟截止日或延长观察期。"
                if not holdout_months
                else None
            ),
            "deferred_detail": {m: month_metrics[m] for m in deferred_months},
            "in_sample": _metrics(train),
            "holdout": _metrics(holdout),
            "by_holdout_month": {m: month_metrics[m] for m in holdout_months},
        }
    else:
        eval_months = months[-rolling_windows:] if len(months) > rolling_windows else months[:]
        eval_set = set(eval_months)
        train = [s for m in months if m not in eval_set for s in by_month[m]]
        eval_items = [s for m in eval_months for s in by_month[m]]
        deferred_months: list[str] = []
        windows = {
            "kind": "daily_rolling",
            "eval_months": eval_months,
            "train_months": [m for m in months if m not in eval_set],
            "by_month": {m: month_metrics[m] for m in eval_months},
            "aggregate": _metrics(eval_items),
            "deferred_months": deferred_months,
        }

    segments: dict[str, Any] = {
        "by_month": month_metrics,
        "by_product": {},
        "by_channel": {},
        "by_income_band": {},
        "by_bureau_grade": {},
        "by_score_band": {},
        "by_selection_reason": {k: v for k, v in Counter(s["row"]["reason_code"] for s in scored).items()},
    }
    for field in ("product", "channel", "income_band", "bureau_grade"):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for s in eval_items:
            groups[str(s["row"]["features"].get(field))].append(s)
        segments[f"by_{field}"] = {k: _metrics(v) for k, v in sorted(groups.items())}
    bands: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in eval_items:
        bands[_score_band(s["eval"]["score"])].append(s)
    segments["by_score_band"] = {k: _metrics(bands[k]) for k in ["750+", "700-749", "650-699", "600-649", "<600"] if k in bands}
    segments["_segment_sets"] = {
        "eval": {"count": len(eval_items), "metrics": _metrics(eval_items)},
        "reference": {"count": len(train), "metrics": _metrics(train)},
    }
    return segments, windows, _metrics(eval_items)


def _metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    matured = [s for s in items if s["label"]["status"] in ("good", "bad")]
    bad = sum(1 for s in matured if s["label"]["status"] == "bad")
    good = len(matured) - bad
    approved = [s for s in items if s["eval"]["decision"] == "approve"]
    approved_matured = [s for s in approved if s["label"]["status"] in ("good", "bad")]
    approved_bad = sum(1 for s in approved_matured if s["label"]["status"] == "bad")
    pairs = [(float(s["eval"]["score"]), 1 if s["label"]["status"] == "good" else 0) for s in matured]
    thresholds: dict[str, Any] = {}
    for t in (560, 600, 620, 640, 660, 680, 700, 720, 750):
        appr = [s for s in matured if s["eval"]["score"] >= t]
        rej = [s for s in matured if s["eval"]["score"] < t]
        thresholds[str(t)] = {
            "approve_n": len(appr),
            "approve_bad_rate": pct(sum(1 for s in appr if s["label"]["status"] == "bad"), len(appr)),
            "reject_n": len(rej),
            "reject_bad_n": sum(1 for s in rej if s["label"]["status"] == "bad"),
            "reject_bad_rate": pct(sum(1 for s in rej if s["label"]["status"] == "bad"), len(rej)),
        }
    return {
        "n": len(items),
        "matured": len(matured),
        "good": good,
        "bad": bad,
        "censored": len(items) - len(matured),
        "maturity_rate": pct(len(matured), len(items)),
        "bad_rate": pct(bad, len(matured)),
        "approved_n": len(approved),
        "approved_matured": len(approved_matured),
        "approved_bad": approved_bad,
        "approved_bad_rate": pct(approved_bad, len(approved_matured)),
        "approval_rate": pct(len(approved), len(items)),
        "auc": _auc(pairs),
        "ks": _ks(pairs),
        "score_mean": round(sum(s["eval"]["score"] for s in items) / len(items), 1) if items else None,
        "thresholds": thresholds,
    }


def _capture(scored: list[dict[str, Any]]) -> float | None:
    matured = [s for s in scored if s["label"]["status"] in ("good", "bad")]
    bad = [s for s in matured if s["label"]["status"] == "bad"]
    captured = [s for s in bad if s["eval"]["decision"] == "decline"]
    return pct(len(captured), len(bad))


def _maturity(store: Store, sample_run_id: str, label_batch_id: str, cutoff: date) -> dict[str, Any]:
    """在若干观察时点重算成熟度，展示“等得越久、可用样本越多但时效越差”。"""
    labels = store.labels(label_batch_id)
    snapshot = date.fromisoformat(store.meta("snapshot_date") or cutoff.isoformat())
    points = month_ends(cutoff, snapshot) or [cutoff]
    # 至少包含 cutoff 与之后每第 2 个月末，避免图表过密。
    points = points[::2] or [cutoff]
    if points[-1] != snapshot:
        points.append(snapshot)
    curve = []
    for as_of in points:
        matured_rows = [x for x in labels if x["matured_on"] and _date(x["matured_on"]) <= as_of]
        bad = sum(1 for x in matured_rows if x["status"] == "bad")
        curve.append(
            {
                "as_of": as_of.isoformat(),
                "matured": len(matured_rows),
                "bad": bad,
                "good": len(matured_rows) - bad,
                "coverage_pct": pct(len(matured_rows), len(labels)),
                "bad_rate": pct(bad, len(matured_rows)),
            }
        )
    return {
        "points": curve,
        "note": "“可用”指已满观察期+结算缓冲，或坏账已确认入库；观察期未满一律删失，不计为好客户。",
    }


def _method_note(mode: str, holdout_batches: int, rolling_windows: int) -> dict[str, Any]:
    if mode == "batch_holdout":
        return {
            "summary": f"按申请批次留出：最后 {holdout_batches} 个申请月作为留出集，其余月份只用于校准/参考。",
            "why": "同批次客户的宏观环境与流程版本一致，留出边界清楚；但留出批次需要等满观察期，时效落后。",
            "tradeoff": "批次留出适合版本验收与稳定性判断，不适合每日监控。",
        }
    return {
        "summary": f"按日滚动：逐月向前滚动 {rolling_windows} 个评估月，评估月之前的数据作为历史参考。",
        "why": "每月重估，能更快反映近期表现；但每个评估月的成熟度不同，早期窗口可用标签少。",
        "tradeoff": "滚动窗口适合监控漂移，报告必须同时展示未成熟比例，避免拿删失样本当负样本。",
    }


def _findings(
    mode: str,
    headline: dict[str, Any],
    segments: dict[str, Any],
    maturity: dict[str, Any],
    windows: dict[str, Any],
) -> list[str]:
    out = []
    usable = headline.get("matured") or 0
    total = headline.get("n") or 0
    scope = "留出批次" if mode == "batch_holdout" else "最近滚动窗口"
    if mode == "batch_holdout" and windows.get("degraded"):
        out.append(windows["degraded_reason"])
    if usable == total:
        out.append(f"{scope}共 {total} 笔，标签全部成熟；未成熟批次已整体推迟，不用删失样本充数。")
    else:
        out.append(
            f"{scope}共 {total} 笔，其中 {usable} 笔标签已成熟（{headline.get('maturity_rate')}%）；"
            f"{total - usable} 笔观察期未满，全部删失而非记为好人。"
        )
    if headline.get("approved_bad_rate") is not None:
        out.append(
            f"规则通过 {headline['approved_n']} 笔，已成熟 {headline['approved_matured']} 笔，"
            f"其中坏账率 {headline['approved_bad_rate']}%。"
        )
    bands = segments.get("by_score_band", {})
    if len(bands) >= 2:
        ordered = [b for b in ["750+", "700-749", "650-699", "600-649", "<600"] if b in bands]
        rates = [(b, bands[b]["bad_rate"]) for b in ordered if bands[b]["bad_rate"] is not None]
        if rates:
            out.append("分层单调性：" + "，".join(f"{b} 坏账率 {r}%" for b, r in rates) + "。")
    deferred = windows.get("deferred_months") or []
    if mode == "batch_holdout" and deferred:
        detail = windows.get("deferred_detail", {})
        weakest = min(detail.items(), key=lambda kv: kv[1].get("maturity_rate") or 0)
        out.append(
            f"截止日前最后 {len(deferred)} 个申请月（{deferred[0]}—{deferred[-1]}）成熟率不足 90%，"
            f"未纳入留出评估；其中 {weakest[0]} 仅 {weakest[1].get('maturity_rate')}% 成熟。"
            "批次留出必须用“已成熟批次”，代价是报告时效落后。"
        )
    elif mode == "daily_rolling":
        evals = windows.get("by_month", {})
        if evals:
            weakest = min(evals.items(), key=lambda kv: kv[1].get("maturity_rate") or 0)
            out.append(
                f"滚动窗口逐月展示，最不成熟的评估月 {weakest[0]} 仅 {weakest[1].get('maturity_rate')}% 可用；"
                "未成熟样本不进入坏账率分母，避免月初窗口被系统性低估。"
            )
    points = maturity.get("points", [])
    if len(points) >= 2:
        out.append(
            f"截止 {points[0]['as_of']} 可用 {points[0]['matured']} 笔（覆盖 {points[0]['coverage_pct']}%），"
            f"到 {points[-1]['as_of']} 升至 {points[-1]['matured']} 笔（{points[-1]['coverage_pct']}%）；"
            "等得越久样本越全，但报告时效越差。"
        )
    return out


# ---------------------------------------------------------------------------
# 发布与对比
# ---------------------------------------------------------------------------


def publish_report(store: Store, report_id: str, run_id: str | None = None) -> dict[str, Any]:
    record = store.report(report_id)
    if not record:
        raise KeyError(f"未知报告: {report_id}")
    run_id = run_id or store.create_run("publish", cutoff=record["cutoff"], params={"report_id": report_id})
    try:
        report = record["report"]
        if report.get("manifest_hash") != record["manifest_hash"]:
            raise ValueError("报告清单摘要不一致，拒绝发布")
        sample = store.sample(record["sample_run_id"])
        batch = store.label_batch(record["label_batch_id"])
        pol = store.policy(record["policy_id"])
        if not sample or not batch:
            raise ValueError("报告引用的样本/标签已丢失")
        if sample["sealed_hash"] != report["sample"]["sealed_hash"]:
            raise ValueError("样本封存摘要与报告不一致")
        if batch["sealed_hash"] != report["labels"]["sealed_hash"]:
            raise ValueError("标签批次封存摘要与报告不一致")
        if pol["definition_hash"] != report["policy"]["definition_hash"]:
            raise ValueError("规则版本摘要与报告不一致，拒绝发布")
        store.set_step(run_id, "verify", "done")
        conn = store.connect()
        try:
            conn.execute("UPDATE reports SET status='published' WHERE report_id=? AND status='built'", (report_id,))
            conn.execute(
                "UPDATE reports SET status='superseded' WHERE status='published' AND report_id<>? AND mode=? AND cutoff=? AND policy_id=?",
                (report_id, record["mode"], record["cutoff"], record["policy_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        store.set_step(run_id, "publish", "done")
        store.finish_run(run_id, "done", result={"report_id": report_id, "status": "published"}, report_id=report_id)
        store.audit("job", "report.publish", {"run_id": run_id, "report_id": report_id, "manifest_hash": record["manifest_hash"]})
        return {"report_id": report_id, "status": "published"}
    except Exception as exc:
        store.finish_run(run_id, "failed", error=str(exc))
        raise


def compare_reports(store: Store, left_id: str, right_id: str) -> dict[str, Any]:
    left = store.report(left_id)
    right = store.report(right_id)
    if not left or not right:
        raise KeyError("报告不存在")
    lr = left["report"]
    rr = right["report"]

    def row(_key: str, label: str, a: Any, b: Any) -> dict[str, Any]:
        return {"label": label, "left": a, "right": b}

    keys = [
        ("sample_run_id", "样本", lr["sample"]["sample_run_id"], rr["sample"]["sample_run_id"]),
        ("sample_hash", "样本摘要", lr["sample"]["sealed_hash"][:12], rr["sample"]["sealed_hash"][:12]),
        ("label_batch_id", "标签批次", lr["labels"]["label_batch_id"], rr["labels"]["label_batch_id"]),
        ("policy", "规则版本", f"{lr['policy']['policy_id']} / {lr['policy']['version']}", f"{rr['policy']['policy_id']} / {rr['policy']['version']}"),
        ("mode", "回测方式", lr["mode"], rr["mode"]),
        ("cutoff", "截止日", lr["cutoff"], rr["cutoff"]),
        ("eligible", "入选", lr["headline"]["n"], rr["headline"]["n"]),
        ("matured", "成熟", lr["headline"]["matured"], rr["headline"]["matured"]),
        ("coverage", "成熟覆盖 %", lr["headline"]["maturity_rate"], rr["headline"]["maturity_rate"]),
        ("bad_rate", "整体坏账率 %", lr["headline"]["bad_rate"], rr["headline"]["bad_rate"]),
        ("approval", "通过率 %", lr["headline"]["approval_rate"], rr["headline"]["approval_rate"]),
        ("approved_bad", "通过件坏账率 %", lr["headline"]["approved_bad_rate"], rr["headline"]["approved_bad_rate"]),
        ("auc", "AUC（成熟样本）", lr["headline"]["auc"], rr["headline"]["auc"]),
        ("ks", "KS（成熟样本）", lr["headline"]["ks"], rr["headline"]["ks"]),
        ("manifest", "清单摘要", lr["manifest_hash"][:12], rr["manifest_hash"][:12]),
    ]
    rows = [row(k, label, l, r) for k, label, l, r in keys]
    warnings = []
    if lr["sample"]["sealed_hash"] != rr["sample"]["sealed_hash"]:
        warnings.append("两份报告使用不同样本快照，指标差异同时包含样本变化。")
    if lr["labels"]["label_batch_id"] != rr["labels"]["label_batch_id"]:
        warnings.append("两份报告使用不同标签批次，不能直接归因于规则版本。")
    if lr["mode"] != rr["mode"]:
        warnings.append("两份报告的回测方式不同，口径不可直接横比。")
    l_bands = set(lr["segments"].get("by_score_band", {}))
    r_bands = set(rr["segments"].get("by_score_band", {}))
    return {
        "left": {"report_id": left_id, "mode": lr["mode"], "policy": lr["policy"], "cutoff": lr["cutoff"]},
        "right": {"report_id": right_id, "mode": rr["mode"], "policy": rr["policy"], "cutoff": rr["cutoff"]},
        "rows": rows,
        "warnings": warnings,
        "band_presence": {"left": sorted(l_bands), "right": sorted(r_bands)},
    }

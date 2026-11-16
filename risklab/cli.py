"""命令行入口：样本构建、标签回补、回测、发布与本地网页。

示例：
    python3 -m risklab.cli init --seed
    python3 -m risklab.cli sample --cutoff 2026-06-30 --mode batch_holdout
    python3 -m risklab.cli labels --sample <id> --as-of 2026-09-30
    python3 -m risklab.cli backtest --sample <id> --labels <id> --policy policy-base-v1 --mode batch_holdout
    python3 -m risklab.cli publish --report <id>
    python3 -m risklab.cli serve --port 8765
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from . import policy as policy_mod
from . import reporting, synth
from .engine import (
    DEFAULT_BAD_DPD,
    DEFAULT_CUTOFF,
    DEFAULT_PERFORMANCE_MONTHS,
    DEFAULT_SETTLEMENT_LAG_DAYS,
    DEFAULT_START,
    build_labels,
    build_sample,
    label_config,
    sample_config,
)
from .store import Store

DEFAULT_DB = "var/risklab.sqlite3"


def _store(args: argparse.Namespace) -> Store:
    store = Store(args.db)
    store.initialize()
    return store


def _seed(args: argparse.Namespace, store: Store) -> str:
    apps, events = synth.generate(seed=args.seed_value, n_apps=args.rows)
    app_rows, event_rows = synth.rows(apps, events)
    dataset_hash = store.ingest(
        app_rows,
        event_rows,
        policy_mod.policy_rows(),
        seed=args.seed_value,
        snapshot=synth.SNAPSHOT.isoformat(),
        replace=True,
    )
    print(f"已写入 {len(app_rows)} 笔虚构申请、{len(event_rows)} 条事件；数据集摘要 {dataset_hash}")
    return dataset_hash


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def cmd_init(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.seed:
        _seed(args, store)
    print(json.dumps(store.storage_stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    store = _store(args)
    cfg = sample_config(
        date.fromisoformat(args.cutoff),
        start=date.fromisoformat(args.start),
        mode=args.mode,
        performance_months=args.performance_months,
        settlement_lag_days=args.settlement_lag_days,
        holdout_batches=args.holdout_batches,
        rolling_windows=args.rolling_windows,
        dataset_hash=store.dataset_hash(),
    )
    result = build_sample(store, cfg, fail_after=args.fail_after)
    _print(result)
    return 0


def cmd_labels(args: argparse.Namespace) -> int:
    store = _store(args)
    sample = store.sample(args.sample)
    if not sample:
        raise SystemExit(f"未知样本 {args.sample}")
    lcfg = label_config(
        performance_months=args.performance_months,
        settlement_lag_days=args.settlement_lag_days,
        bad_dpd=args.bad_dpd,
    )
    result = build_labels(store, args.sample, lcfg, as_of=date.fromisoformat(args.as_of), fail_after=args.fail_after)
    _print(result)
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    store = _store(args)
    result = reporting.run_backtest(
        store,
        sample_run_id=args.sample,
        label_batch_id=args.labels,
        policy_id=args.policy,
        mode=args.mode,
        cutoff=date.fromisoformat(args.cutoff),
        fail_after=args.fail_after,
        rolling_windows=args.rolling_windows,
        holdout_batches=args.holdout_batches,
    )
    _print({"report_id": result["report_id"], "reused": result["reused"], "headline": result["report"]["headline"]})
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    store = _store(args)
    _print(reporting.publish_report(store, args.report))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    store = _store(args)
    record = store.report(args.report) if args.report else store.latest_report()
    if not record:
        raise SystemExit("没有报告")
    _print(record["report"])
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .webapp import serve

    return serve(args.db, host=args.host, port=args.port)


def cmd_demo(args: argparse.Namespace) -> int:
    """一次性构建两版规则、两种回测口径的演示数据。"""
    store = _store(args)
    if args.seed:
        _seed(args, store)
    cutoff = date.fromisoformat(args.cutoff)
    as_of = date.fromisoformat(args.as_of)
    results = []
    for mode in ("batch_holdout", "daily_rolling"):
        cfg = sample_config(cutoff, start=date.fromisoformat(args.start), mode=mode, dataset_hash=store.dataset_hash())
        sample = build_sample(store, cfg)
        lcfg = label_config()
        labels = build_labels(store, sample["sample_run_id"], lcfg, as_of=as_of)
        for pid in args.policies:
            bt = reporting.run_backtest(
                store,
                sample_run_id=sample["sample_run_id"],
                label_batch_id=labels["label_batch_id"],
                policy_id=pid,
                mode=mode,
                cutoff=cutoff,
            )
            results.append({"mode": mode, "policy": pid, "report_id": bt["report_id"]})
    last = results[-1]["report_id"]
    reporting.publish_report(store, last)
    _print({"built": results, "published": last})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="risklab", description="虚构贷款样本与回测演示")
    parser.add_argument("--db", default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化数据库，可选写入虚构数据")
    p.add_argument("--seed", action="store_true")
    p.add_argument("--seed-value", type=int, default=20261116)
    p.add_argument("--rows", type=int, default=2200)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("sample", help="构建样本")
    p.add_argument("--cutoff", default=DEFAULT_CUTOFF.isoformat())
    p.add_argument("--start", default=DEFAULT_START.isoformat())
    p.add_argument("--mode", choices=["batch_holdout", "daily_rolling"], default="batch_holdout")
    p.add_argument("--performance-months", type=int, default=DEFAULT_PERFORMANCE_MONTHS)
    p.add_argument("--settlement-lag-days", type=int, default=DEFAULT_SETTLEMENT_LAG_DAYS)
    p.add_argument("--holdout-batches", type=int, default=2)
    p.add_argument("--rolling-windows", type=int, default=6)
    p.add_argument("--fail-after", default=None, help="调试用：完成某步后模拟中断")
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("labels", help="回补标签")
    p.add_argument("--sample", required=True)
    p.add_argument("--as-of", default=synth.SNAPSHOT.isoformat())
    p.add_argument("--performance-months", type=int, default=DEFAULT_PERFORMANCE_MONTHS)
    p.add_argument("--settlement-lag-days", type=int, default=DEFAULT_SETTLEMENT_LAG_DAYS)
    p.add_argument("--bad-dpd", type=int, default=DEFAULT_BAD_DPD)
    p.add_argument("--fail-after", default=None)
    p.set_defaults(func=cmd_labels)

    p = sub.add_parser("backtest", help="生成回测报告（不发布）")
    p.add_argument("--sample", required=True)
    p.add_argument("--labels", required=True)
    p.add_argument("--policy", required=True)
    p.add_argument("--mode", choices=["batch_holdout", "daily_rolling"], default="batch_holdout")
    p.add_argument("--cutoff", default=DEFAULT_CUTOFF.isoformat())
    p.add_argument("--rolling-windows", type=int, default=6)
    p.add_argument("--holdout-batches", type=int, default=2)
    p.add_argument("--fail-after", default=None)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("publish", help="发布已构建报告")
    p.add_argument("--report", required=True)
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("report", help="打印报告 JSON")
    p.add_argument("--report", default=None)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("demo", help="构建演示数据")
    p.add_argument("--seed", action="store_true")
    p.add_argument("--seed-value", type=int, default=20261116)
    p.add_argument("--rows", type=int, default=2200)
    p.add_argument("--cutoff", default=DEFAULT_CUTOFF.isoformat())
    p.add_argument("--as-of", default=synth.SNAPSHOT.isoformat())
    p.add_argument("--start", default=DEFAULT_START.isoformat())
    p.add_argument("--policies", nargs="+", default=["policy-base-v1", "policy-base-v2"])
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("serve", help="启动本地网页")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

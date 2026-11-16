"""端到端测试：只依赖标准库 unittest，可直接运行。

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risklab import policy as policy_mod  # noqa: E402
from risklab import reporting, synth  # noqa: E402
from risklab.engine import (  # noqa: E402
    build_labels,
    build_sample,
    label_config,
    sample_config,
    DEFAULT_CUTOFF,
    DEFAULT_START,
)
from risklab.store import Store  # noqa: E402
from risklab.util import digest  # noqa: E402


class TempStore(unittest.TestCase):
    rows = 900
    seed = 424242

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.sqlite3")
        self.artifacts = os.path.join(self.tmp.name, "artifacts")
        os.environ["RISKLAB_ARTIFACT_DIR"] = self.artifacts
        self.store = Store(self.db_path)
        self.store.initialize()
        apps, events = synth.generate(seed=self.seed, n_apps=self.rows)
        app_rows, event_rows = synth.rows(apps, events)
        self.dataset_hash = self.store.ingest(
            app_rows,
            event_rows,
            policy_mod.policy_rows(),
            seed=self.seed,
            snapshot=synth.SNAPSHOT.isoformat(),
            replace=True,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("RISKLAB_ARTIFACT_DIR", None)

    def make_sample(self, mode: str = "batch_holdout", cutoff: date = DEFAULT_CUTOFF):
        cfg = sample_config(cutoff, start=DEFAULT_START, mode=mode, dataset_hash=self.store.dataset_hash())
        return build_sample(self.store, cfg)

    def make_labels(self, sample_run_id: str, as_of: date | None = None):
        lcfg = label_config()
        return build_labels(
            self.store,
            sample_run_id,
            lcfg,
            as_of=as_of or date.fromisoformat(synth.SNAPSHOT.isoformat()),
        )

    def make_report(self, sample_id: str, label_id: str, policy_id: str = "policy-base-v1", mode: str = "batch_holdout"):
        return reporting.run_backtest(
            self.store,
            sample_run_id=sample_id,
            label_batch_id=label_id,
            policy_id=policy_id,
            mode=mode,
            cutoff=DEFAULT_CUTOFF,
        )


class TestDeterminism(TempStore):
    def test_same_seed_gives_same_dataset_hash(self) -> None:
        other_path = os.path.join(self.tmp.name, "other.sqlite3")
        other = Store(other_path)
        other.initialize()
        apps, events = synth.generate(seed=self.seed, n_apps=self.rows)
        app_rows, event_rows = synth.rows(apps, events)
        h = other.ingest(
            app_rows,
            event_rows,
            policy_mod.policy_rows(),
            seed=self.seed,
            snapshot=synth.SNAPSHOT.isoformat(),
            replace=True,
        )
        self.assertEqual(h, self.dataset_hash)

    def test_sample_is_idempotent(self) -> None:
        a = self.make_sample()
        b = self.make_sample()
        self.assertEqual(a["sample_run_id"], b["sample_run_id"])
        self.assertTrue(b["reused"])


class TestPointInTimeIsolation(TempStore):
    def test_features_never_use_later_recorded_events(self) -> None:
        sample = self.make_sample()
        rows = self.store.sample_rows(sample["sample_run_id"])
        eligible = [r for r in rows if r["eligible"]]
        self.assertTrue(eligible)
        conn = self.store.connect()
        try:
            all_events = [dict(r) for r in conn.execute("SELECT * FROM events")]
        finally:
            conn.close()
        for row in eligible:
            decision = row["decision_on"]
            self.assertIsNotNone(decision)
            # 事件级：不得有在决策日之后才入库的事件出现在可见集合中。
            for ev in row["visibility"].get("blocked_examples", []):
                self.assertGreater(ev["recorded_on"], decision)
            # 历史坏事件特征必须都能在决策日前入库。
            if row["features"]["prior_bad_events"]:
                usable = [
                    e
                    for e in all_events
                    if e["kind"] == "delinquency"
                    and e["recorded_on"] <= decision
                    and e["occurred_on"] <= decision
                ]
                self.assertGreaterEqual(len(usable), 1)

    def test_master_recorded_after_decision_is_excluded(self) -> None:
        sample = self.make_sample()
        rows = self.store.sample_rows(sample["sample_run_id"])
        late = [r for r in rows if r["reason_code"] == "feature_late"]
        self.assertTrue(late, "应存在主档晚入库的排除样本")
        for r in late:
            self.assertFalse(r["eligible"])
            self.assertTrue(r["visibility"]["master_recorded_on"] > r["decision_on"])

    def test_later_correction_not_in_decision_features(self) -> None:
        sample = self.make_sample()
        rows = self.store.sample_rows(sample["sample_run_id"])
        blocked_rows = [r for r in rows if r["visibility"].get("blocked_late_records")]
        self.assertTrue(blocked_rows, "合成数据应包含审批后补录的更正事件")
        row = blocked_rows[0]
        # 更正事件业务日早于审批，但不应产生任何可见的坏事件计数。
        self.assertEqual(row["features"]["prior_bad_events"], 0)
        self.assertEqual(row["features"]["prior_max_dpd"], 0)


class TestLabels(TempStore):
    def test_immature_never_good(self) -> None:
        sample = self.make_sample()
        labels = self.make_labels(sample["sample_run_id"], as_of=date(2025, 1, 31))
        items = self.store.labels(labels["label_batch_id"])
        self.assertTrue(items)
        for x in items:
            if x["status"] == "good":
                self.assertLessEqual(x["matured_on"], "2025-01-31")
            if x["status"] == "censored":
                self.assertIsNone(x["label"])
                self.assertIn("未满", x["evidence"]["censored_reason"])

    def test_good_label_respects_settlement_lag(self) -> None:
        sample = self.make_sample()
        labels = self.make_labels(sample["sample_run_id"])
        items = self.store.labels(labels["label_batch_id"])
        for x in items:
            if x["status"] == "good":
                window_end = date.fromisoformat(x["window_end"])
                self.assertGreaterEqual(date.fromisoformat(x["matured_on"]), window_end + timedelta(days=30))

    def test_bad_label_has_evidence(self) -> None:
        sample = self.make_sample()
        labels = self.make_labels(sample["sample_run_id"])
        items = self.store.labels(labels["label_batch_id"])
        for x in items:
            if x["status"] == "bad":
                self.assertTrue(x["evidence"]["bad_events"])
                self.assertIsNotNone(x["label"])
                self.assertEqual(x["label"], 1)


class TestIntegrity(TempStore):
    def test_tampered_sample_blocks_backtest(self) -> None:
        sample = self.make_sample()
        labels = self.make_labels(sample["sample_run_id"])
        conn = self.store.connect()
        try:
            conn.execute(
                "UPDATE sample_rows SET features_json=? WHERE sample_run_id=? AND eligible=1 AND rowid=(SELECT MIN(rowid) FROM sample_rows WHERE sample_run_id=? AND eligible=1)",
                (json.dumps({"income_band": "A", "bureau_grade": "A", "employment_years": 9, "existing_debt": 0, "term_months": 6}), sample["sample_run_id"], sample["sample_run_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ValueError) as ctx:
            self.make_report(sample["sample_run_id"], labels["label_batch_id"])
        self.assertIn("完整性校验失败", str(ctx.exception))

    def test_label_batch_from_other_sample_rejected(self) -> None:
        sample_a = self.make_sample("batch_holdout")
        sample_b = self.make_sample("daily_rolling")
        labels_b = self.make_labels(sample_b["sample_run_id"])
        with self.assertRaises(ValueError) as ctx:
            self.make_report(sample_a["sample_run_id"], labels_b["label_batch_id"])
        self.assertIn("不属于该样本", str(ctx.exception))

    def test_mode_mismatch_rejected(self) -> None:
        sample = self.make_sample("batch_holdout")
        labels = self.make_labels(sample["sample_run_id"])
        with self.assertRaises(ValueError):
            self.make_report(sample["sample_run_id"], labels["label_batch_id"], mode="daily_rolling")

    def test_report_cannot_mix_two_label_versions(self) -> None:
        sample = self.make_sample()
        labels = self.make_labels(sample["sample_run_id"])
        report = self.make_report(sample["sample_run_id"], labels["label_batch_id"])
        record = self.store.report(report["report_id"])
        self.assertEqual(record["report"]["labels"]["label_batch_id"], labels["label_batch_id"])
        # 标签配置变化会生成新批次；旧报告必须保持旧批次引用。
        alt = build_labels(
            self.store,
            sample["sample_run_id"],
            label_config(performance_months=3),
            as_of=date.fromisoformat(synth.SNAPSHOT.isoformat()),
        )
        self.assertNotEqual(alt["label_batch_id"], labels["label_batch_id"])
        again = self.store.report(report["report_id"])
        self.assertEqual(again["report"]["labels"]["label_batch_id"], labels["label_batch_id"])
        self.assertEqual(again["report"]["labels"]["sealed_hash"], record["report"]["labels"]["sealed_hash"])

    def test_publish_verifies_manifest(self) -> None:
        sample = self.make_sample()
        labels = self.make_labels(sample["sample_run_id"])
        report = self.make_report(sample["sample_run_id"], labels["label_batch_id"])
        result = reporting.publish_report(self.store, report["report_id"])
        self.assertEqual(result["status"], "published")
        conn = self.store.connect()
        try:
            conn.execute("UPDATE reports SET manifest_hash='tampered' WHERE report_id=?", (report["report_id"],))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(ValueError):
            reporting.publish_report(self.store, report["report_id"])


class TestResume(TempStore):
    def test_interrupted_sample_resumes_idempotently(self) -> None:
        cfg = sample_config(
            DEFAULT_CUTOFF, start=DEFAULT_START, mode="batch_holdout", dataset_hash=self.store.dataset_hash()
        )
        with self.assertRaises(RuntimeError):
            build_sample(self.store, cfg, fail_after="features")
        runs = self.store.runs(10)
        self.assertEqual(runs[0]["status"], "interrupted")
        resumed = build_sample(self.store, cfg)
        self.assertFalse(resumed["reused"])
        self.assertTrue(self.store.sample(resumed["sample_run_id"]))

    def test_interrupted_labels_resume(self) -> None:
        sample = self.make_sample()
        lcfg = label_config()
        with self.assertRaises(RuntimeError):
            build_labels(
                self.store,
                sample["sample_run_id"],
                lcfg,
                as_of=date.fromisoformat(synth.SNAPSHOT.isoformat()),
                fail_after="classify",
            )
        resumed = build_labels(
            self.store, sample["sample_run_id"], lcfg, as_of=date.fromisoformat(synth.SNAPSHOT.isoformat())
        )
        self.assertFalse(resumed["reused"])
        self.assertGreater(resumed["stats"]["eligible_for_metrics"], 0)

    def test_interrupted_backtest_then_resume(self) -> None:
        sample = self.make_sample()
        labels = self.make_labels(sample["sample_run_id"])
        with self.assertRaises(RuntimeError):
            reporting.run_backtest(
                self.store,
                sample_run_id=sample["sample_run_id"],
                label_batch_id=labels["label_batch_id"],
                policy_id="policy-base-v1",
                mode="batch_holdout",
                cutoff=DEFAULT_CUTOFF,
                fail_after="metrics",
            )
        self.assertFalse(self.store.reports())
        result = self.make_report(sample["sample_run_id"], labels["label_batch_id"])
        self.assertTrue(self.store.report(result["report_id"]))


class TestMaturityEdges(TempStore):
    def test_all_immature_does_not_fake_holdout(self) -> None:
        cfg = sample_config(
            date(2026, 8, 1), start=date(2026, 7, 1), mode="batch_holdout", dataset_hash=self.store.dataset_hash()
        )
        sample = build_sample(self.store, cfg)
        labels = build_labels(
            self.store,
            sample["sample_run_id"],
            label_config(),
            as_of=date.fromisoformat(synth.SNAPSHOT.isoformat()),
        )
        self.assertEqual(labels["stats"]["good"], 0)
        self.assertEqual(labels["stats"]["bad"], 0)
        report = reporting.run_backtest(
            self.store,
            sample_run_id=sample["sample_run_id"],
            label_batch_id=labels["label_batch_id"],
            policy_id="policy-base-v1",
            mode="batch_holdout",
            cutoff=date(2026, 8, 1),
        )["report"]
        self.assertTrue(report["windows"]["degraded"])
        self.assertEqual(report["windows"]["holdout_months"], [])
        self.assertEqual(report["headline"]["matured"], 0)
        self.assertIsNone(report["headline"]["bad_rate"])


class TestReporting(TempStore):
    def test_windows_respect_maturity(self) -> None:
        sample = self.make_sample("batch_holdout")
        labels = self.make_labels(sample["sample_run_id"])
        report = self.make_report(sample["sample_run_id"], labels["label_batch_id"])["report"]
        holdout = report["windows"]["holdout"]
        self.assertGreaterEqual(holdout["maturity_rate"] or 0, 90)
        self.assertTrue(report["windows"]["deferred_months"], "截止日附近未成熟批次应被推迟")
        for m in report["windows"]["deferred_months"]:
            self.assertLess(report["segments"]["by_month"][m]["maturity_rate"], 90)

    def test_metrics_denominator_excludes_censored(self) -> None:
        sample = self.make_sample("daily_rolling")
        labels = self.make_labels(sample["sample_run_id"])
        report = self.make_report(sample["sample_run_id"], labels["label_batch_id"], mode="daily_rolling")["report"]
        h = report["headline"]
        self.assertEqual(h["good"] + h["bad"], h["matured"])
        self.assertEqual(h["censored"], h["n"] - h["matured"])
        self.assertLess(h["maturity_rate"], 100)

    def test_policy_versions_are_pinned(self) -> None:
        sample = self.make_sample()
        labels = self.make_labels(sample["sample_run_id"])
        r1 = self.make_report(sample["sample_run_id"], labels["label_batch_id"], "policy-base-v1")
        r2 = self.make_report(sample["sample_run_id"], labels["label_batch_id"], "policy-base-v2")
        self.assertNotEqual(r1["report"]["policy"]["definition_hash"], r2["report"]["policy"]["definition_hash"])
        cmp = reporting.compare_reports(self.store, r1["report_id"], r2["report_id"])
        self.assertEqual(cmp["warnings"], [])
        self.assertEqual(cmp["left"]["policy"]["policy_id"], "policy-base-v1")

    def test_rebuild_after_config_change_creates_new_report(self) -> None:
        sample = self.make_sample()
        labels = self.make_labels(sample["sample_run_id"])
        first = self.make_report(sample["sample_run_id"], labels["label_batch_id"])
        self.assertFalse(first["reused"])
        same = self.make_report(sample["sample_run_id"], labels["label_batch_id"])
        self.assertTrue(same["reused"])
        self.assertEqual(same["report_id"], first["report_id"])
        # 相同样本与标签、但窗口参数不同：必须生成新报告，不能静默复用旧指标。
        changed = reporting.run_backtest(
            self.store,
            sample_run_id=sample["sample_run_id"],
            label_batch_id=labels["label_batch_id"],
            policy_id="policy-base-v1",
            mode="batch_holdout",
            cutoff=DEFAULT_CUTOFF,
            holdout_batches=1,
        )
        self.assertFalse(changed["reused"])
        self.assertNotEqual(changed["report_id"], first["report_id"])
        self.assertNotEqual(
            changed["report"]["windows"]["holdout_months"],
            first["report"]["windows"]["holdout_months"],
        )

    def test_compare_flags_mixed_samples(self) -> None:
        sample_a = self.make_sample("batch_holdout")
        labels_a = self.make_labels(sample_a["sample_run_id"])
        ra = self.make_report(sample_a["sample_run_id"], labels_a["label_batch_id"])
        sample_b = self.make_sample("daily_rolling")
        labels_b = self.make_labels(sample_b["sample_run_id"])
        rb = self.make_report(sample_b["sample_run_id"], labels_b["label_batch_id"], mode="daily_rolling")
        cmp = reporting.compare_reports(self.store, ra["report_id"], rb["report_id"])
        self.assertTrue(cmp["warnings"])

    def test_sample_seal_detects_row_change(self) -> None:
        sample = self.make_sample()
        before = reporting.verify_sample(self.store, sample["sample_run_id"])
        self.assertTrue(before["ok"])
        conn = self.store.connect()
        try:
            conn.execute(
                "UPDATE sample_rows SET reason_text='篡改' WHERE sample_run_id=? AND rowid=(SELECT MIN(rowid) FROM sample_rows WHERE sample_run_id=?)",
                (sample["sample_run_id"], sample["sample_run_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        after = reporting.verify_sample(self.store, sample["sample_run_id"])
        self.assertFalse(after["ok"])


class TestScoring(TempStore):
    def test_score_is_deterministic_and_bounded(self) -> None:
        features = {
            "income_band": "B",
            "bureau_grade": "A",
            "employment_years": 5.0,
            "existing_debt": 9000,
            "term_months": 12,
        }
        pol = self.store.policy("policy-base-v1")["definition"]
        a = policy_mod.evaluate(pol, features)
        b = policy_mod.evaluate(pol, features)
        self.assertEqual(a["score"], b["score"])
        self.assertLessEqual(a["score"], pol["score_ceiling"])
        self.assertGreaterEqual(a["score"], pol["score_floor"])
        self.assertAlmostEqual(a["score"], sum(p["weighted"] for p in a["parts"]) + pol["base"], delta=1)

    def test_worse_customer_scores_lower(self) -> None:
        pol = self.store.policy("policy-base-v1")["definition"]
        good = {"income_band": "A", "bureau_grade": "A", "employment_years": 9, "existing_debt": 0, "term_months": 6}
        bad = {"income_band": "E", "bureau_grade": "D", "employment_years": 0.5, "existing_debt": 30000, "term_months": 36}
        self.assertGreater(policy_mod.evaluate(pol, good)["score"], policy_mod.evaluate(pol, bad)["score"])

    def test_auc_ks_orientation(self) -> None:
        pairs = [(800, 1), (780, 1), (700, 1), (650, 0), (600, 0), (500, 0)]
        self.assertGreater(reporting._auc(pairs), 0.9)
        self.assertGreater(reporting._ks(pairs), 0.9)
        shuffled = [(800, 0), (780, 1), (700, 0), (650, 1), (600, 0), (500, 1)]
        self.assertLessEqual(reporting._auc(shuffled), 0.6)


if __name__ == "__main__":
    unittest.main()

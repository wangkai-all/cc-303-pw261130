"""确定性虚构数据生成，所有客户、订单与还款行为均为程序合成。"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, asdict
from datetime import date, timedelta

EPOCH = date(2024, 1, 1)
SIM_END = date(2026, 8, 31)
SNAPSHOT = date(2026, 9, 30)  # 数据快照日：此日之后发生/入库的事件不在本地库中


@dataclass(frozen=True)
class RawApplication:
    application_id: str
    customer_id: str
    applied_at: date
    recorded_on: date
    product: str
    channel: str
    amount: int
    term_months: int
    income_band: str
    employment_years: float
    age: int
    existing_debt: int
    bureau_grade: str
    city_tier: str


@dataclass(frozen=True)
class RawEvent:
    event_id: str
    application_id: str
    kind: str  # approval / disbursement / repayment / delinquency / correction
    occurred_on: date
    recorded_on: date
    amount: int = 0
    detail: str = ""


def _rng(seed: int, *parts: object) -> random.Random:
    key = "|".join([str(seed), *(str(p) for p in parts)])
    digest = hashlib.sha256(key.encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


INCOME_BANDS = [
    ("A", 0.06, 22_000, 30_000),
    ("B", 0.20, 14_000, 22_000),
    ("C", 0.34, 9_000, 14_000),
    ("D", 0.25, 6_000, 9_000),
    ("E", 0.15, 3_500, 6_000),
]
BUREAU_GRADES = ["A", "B", "C", "D"]
CHANNELS = [("app", 0.42), ("partner", 0.23), ("branch", 0.20), ("referral", 0.15)]
PRODUCTS = [("cash", 0.62), ("installment", 0.38)]


def _pick(rng: random.Random, weighted: list[tuple[str, float]]) -> str:
    total = sum(w for _, w in weighted)
    x = rng.random() * total
    for name, weight in weighted:
        x -= weight
        if x <= 0:
            return name
    return weighted[-1][0]


def _in_range(rng: random.Random, lo: int, hi: int) -> int:
    return rng.randint(lo, hi)


def generate(seed: int = 20261116, n_apps: int = 2200) -> tuple[list[RawApplication], list[RawEvent]]:
    """生成申请与事件；occurred_on 是业务发生日，recorded_on 是入库/可见日。"""
    apps: list[RawApplication] = []
    events: list[RawEvent] = []
    start = EPOCH
    span_days = (SIM_END - start).days - 1

    # 申请按日分布：工作日多、周末少，并留出一个批次空档，便于检验批次留出。
    day_weights: dict[date, float] = {}
    for i in range(span_days + 1):
        d = start + timedelta(days=i)
        if date(2024, 8, 5) <= d <= date(2024, 8, 11):
            continue
        day_weights[d] = 0.16 if d.weekday() >= 5 else 1.0
    active_days = sorted(day_weights)
    active_weights = [day_weights[d] for d in active_days]

    for i in range(n_apps):
        rng = _rng(seed, "app", i)
        applied = rng.choices(active_days, weights=active_weights, k=1)[0]
        customer_id = f"C{seed % 1000:03d}{i // 3:05d}"
        channel = _pick(rng, CHANNELS)
        product = _pick(rng, PRODUCTS)
        income_band = _pick(rng, [(b, w) for b, w, _, _ in INCOME_BANDS])
        employment_years = round(rng.triangular(0.2, 18, 4), 1)
        age = _in_range(rng, 21, 63)
        existing_debt = _in_range(rng, 0, 8) * 1000
        bureau = rng.choices(BUREAU_GRADES, weights=[0.30, 0.38, 0.24, 0.08])[0]
        amount = _in_range(rng, 3, 30) * 1000
        if product == "installment":
            amount = _in_range(rng, 2, 20) * 1000
        term = rng.choice([6, 12, 18, 24, 36])
        # 申请主档也有入库时延；极少数申请会晚入库，用于检验“截止日可见性”。
        app_lag = rng.choices([0, 1, 2, 5, 30], weights=[0.82, 0.10, 0.05, 0.02, 0.01])[0]
        app_recorded = applied + timedelta(days=app_lag)
        app = RawApplication(
            application_id=f"APP-{applied.strftime('%Y%m%d')}-{i:05d}",
            customer_id=customer_id,
            applied_at=applied,
            recorded_on=app_recorded,
            product=product,
            channel=channel,
            amount=amount,
            term_months=term,
            income_band=income_band,
            employment_years=employment_years,
            age=age,
            existing_debt=existing_debt,
            bureau_grade=bureau,
            city_tier=rng.choices(["一线", "新一线", "二线", "三线"], weights=[0.18, 0.27, 0.30, 0.25])[0],
        )
        apps.append(app)

        decision_day = applied + timedelta(days=0 if rng.random() < 0.55 else rng.choice([1, 1, 2, 3]))
        if decision_day.weekday() >= 5:
            decision_day += timedelta(days=2)
        approved = rng.random() < _approval_probability(app, seed)
        record_lag = rng.choices([0, 1, 2, 4, 9], weights=[0.68, 0.17, 0.08, 0.05, 0.02])[0]
        approval_recorded = decision_day + timedelta(days=record_lag)
        events.append(
            RawEvent(
                event_id=f"EV-{app.application_id}-AP",
                application_id=app.application_id,
                kind="approval",
                occurred_on=decision_day,
                recorded_on=max(approval_recorded, decision_day),
                amount=amount if approved else 0,
                detail="approved" if approved else "declined",
            )
        )
        if not approved:
            continue

        disburse_day = decision_day + timedelta(days=rng.choice([0, 1, 1, 2]))
        if disburse_day.weekday() >= 5:
            disburse_day += timedelta(days=2)
        disburse_recorded = disburse_day + timedelta(days=rng.choices([0, 1, 2, 6], weights=[0.76, 0.15, 0.06, 0.03])[0])
        events.append(
            RawEvent(
                event_id=f"EV-{app.application_id}-DB",
                application_id=app.application_id,
                kind="disbursement",
                occurred_on=disburse_day,
                recorded_on=disburse_recorded,
                amount=amount,
            )
        )

        # 虚构违约驱动：静态特征 + 随机噪声；不用于训练，仅用于制造可比较的标签。
        # 记账口径：每期到期日只生成一条状态事件，DPD 是可观察的最大值；
        # 30/60/90 天的跨越由后续到期日自然推进，避免同月多条互相覆盖。
        risk = _risk_score(app, seed)
        first_due = disburse_day + timedelta(days=30)
        current_dpd = 0
        for k in range(1, app.term_months + 1):
            due = first_due + timedelta(days=30 * (k - 1))
            hazard = min(0.75, 0.010 + risk * 0.16 * (0.6 + k / app.term_months))
            if rng.random() < hazard:
                if current_dpd == 0:
                    current_dpd = rng.choices([3, 7, 15, 31, 45], weights=[0.24, 0.24, 0.20, 0.19, 0.13])[0]
                else:
                    current_dpd = min(120, current_dpd + rng.choice([15, 30, 30, 31]))
            elif current_dpd > 0 and rng.random() < 0.28:
                current_dpd = 0
            if current_dpd == 0:
                pay_day = max(
                    due + timedelta(days=rng.choices([-3, -1, 0, 0, 0, 1, 2], weights=[0.05, 0.15, 0.45, 0.15, 0.10, 0.06, 0.04])[0]),
                    disburse_day + timedelta(days=1),
                )
                recorded = pay_day + timedelta(days=rng.choices([0, 1, 2], weights=[0.82, 0.13, 0.05])[0])
                events.append(
                    RawEvent(
                        event_id=f"EV-{app.application_id}-RP{k:02d}",
                        application_id=app.application_id,
                        kind="repayment",
                        occurred_on=pay_day,
                        recorded_on=recorded,
                        amount=amount // app.term_months,
                    )
                )
            else:
                delinq_day = due + timedelta(days=current_dpd)
                # 约 12% 的逾期事件晚到 30~120 天，模拟贷后补录/更正。
                lag = rng.choices([0, 1, 3, 7, 30, 60, 120], weights=[0.52, 0.20, 0.10, 0.06, 0.06, 0.04, 0.02])[0]
                recorded = delinq_day + timedelta(days=lag)
                events.append(
                    RawEvent(
                        event_id=f"EV-{app.application_id}-DL{k:02d}",
                        application_id=app.application_id,
                        kind="delinquency",
                        occurred_on=delinq_day,
                        recorded_on=recorded,
                        amount=amount // app.term_months,
                        detail=str(current_dpd),
                    )
                )
            if current_dpd >= 90:
                break

        # 贷后更正：业务发生日早于审批，但入库晚于审批；用于验证审批时特征隔离。
        if rng.random() < 0.07:
            correction_day = decision_day + timedelta(days=rng.randint(2, 45))
            events.append(
                RawEvent(
                    event_id=f"EV-{app.application_id}-CR",
                    application_id=app.application_id,
                    kind="correction",
                    # 有 1/3 的更正业务日也落在申请之后、审批之前，构成“审批后才知道”的泄漏陷阱；
                    # 其余落在申请前，构成“历史信息晚到”的隔离样本。
                    occurred_on=applied - timedelta(days=rng.randint(1, 20))
                    if rng.random() < 0.34
                    else applied + timedelta(days=rng.randint(0, max(1, (decision_day - applied).days))),
                    recorded_on=correction_day,
                    amount=rng.choice([0, 1000]),
                    detail="bureau_backfill",
                )
            )
    # 数据快照：未来才发生的还款/逾期不存在，晚于快照的入库也不可见（即右删失）。
    events = [
        RawEvent(
            event_id=e.event_id,
            application_id=e.application_id,
            kind=e.kind,
            occurred_on=e.occurred_on,
            recorded_on=min(e.recorded_on, SNAPSHOT),
            amount=e.amount,
            detail=e.detail,
        )
        for e in events
        if e.occurred_on <= SNAPSHOT
    ]
    apps = [a for a in apps if a.applied_at <= SNAPSHOT and a.recorded_on <= SNAPSHOT]
    return apps, events


def _approval_probability(app: RawApplication, seed: int) -> float:
    r = _rng(seed, "approval", app.application_id)
    p = 0.80
    p -= {"A": 0.0, "B": 0.02, "C": 0.12, "D": 0.28, "E": 0.42}[app.income_band]
    p -= {"A": 0.0, "B": 0.02, "C": 0.10, "D": 0.22}[app.bureau_grade]
    if app.amount > 20_000:
        p -= 0.06
    if app.age < 25:
        p -= 0.05
    if app.channel == "partner":
        p += 0.02
    return max(0.08, min(0.96, p + (r.random() - 0.5) * 0.04))


def _risk_score(app: RawApplication, seed: int) -> float:
    r = _rng(seed, "risk", app.application_id)
    base = (r.random() - 0.5) * 0.30
    base += {"A": 0.0, "B": 0.10, "C": 0.30, "D": 0.65, "E": 1.0}[app.income_band] * 0.55
    base += {"A": 0.0, "B": 0.15, "C": 0.40, "D": 0.80}[app.bureau_grade] * 0.45
    base += min(0.35, app.existing_debt / 60_000)
    base -= min(0.18, app.employment_years / 60)
    return min(1.0, max(0.0, base))


def rows(apps: list[RawApplication], events: list[RawEvent]) -> tuple[list[dict], list[dict]]:
    app_rows = []
    for a in apps:
        d = asdict(a)
        for k in ("applied_at", "recorded_on"):
            d[k] = d[k].isoformat()
        app_rows.append(d)
    event_rows = []
    for e in events:
        d = asdict(e)
        for k in ("occurred_on", "recorded_on"):
            d[k] = d[k].isoformat()
        event_rows.append(d)
    return app_rows, event_rows

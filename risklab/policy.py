"""可复现的规则版本与基础评分规则。

规则是贷款准入演示用的透明加权分箱，不是真实授信模型；所有输入均来自
申请时点可见的静态字段。版本号用于把同一批样本、同一版标签与同一版规则绑定。
"""

from __future__ import annotations

import json
from typing import Any

from .util import digest

POLICY_V1: dict[str, Any] = {
    "id": "policy-base-v1",
    "name": "基础评分 v1（收入/征信/负债）",
    "version": "1.0.0",
    "approval_threshold": 620,
    "score_floor": 300,
    "score_ceiling": 850,
    "bands": [
        {"field": "income_band", "weight": 0.30, "points": {"A": 130, "B": 100, "C": 60, "D": 10, "E": -50}},
        {"field": "bureau_grade", "weight": 0.35, "points": {"A": 160, "B": 105, "C": 35, "D": -90}},
        {"field": "employment_years", "weight": 0.10, "points": {"0-1": -40, "1-3": 0, "3-7": 45, "7+": 80}},
        {"field": "existing_debt", "weight": 0.15, "points": {"0-4k": 40, "4k-12k": 10, "12k-25k": -35, "25k+": -75}},
        {"field": "term_months", "weight": 0.10, "points": {"6": 25, "12": 15, "18": 5, "24": -10, "36": -30}},
    ],
    "base": 560,
    "segments": {"product": ["cash", "installment"], "channel": ["app", "partner", "branch", "referral"]},
    "notes": "仅用于演示分层与版本对比；不是真实授信建议。",
}

POLICY_V2: dict[str, Any] = {
    "id": "policy-base-v2",
    "name": "基础评分 v2（提高征信权重，收紧长分期）",
    "version": "2.0.0",
    "approval_threshold": 640,
    "score_floor": 300,
    "score_ceiling": 850,
    "bands": [
        {"field": "income_band", "weight": 0.25, "points": {"A": 120, "B": 90, "C": 50, "D": 0, "E": -55}},
        {"field": "bureau_grade", "weight": 0.40, "points": {"A": 170, "B": 110, "C": 25, "D": -110}},
        {"field": "employment_years", "weight": 0.10, "points": {"0-1": -50, "1-3": -5, "3-7": 50, "7+": 85}},
        {"field": "existing_debt", "weight": 0.15, "points": {"0-4k": 45, "4k-12k": 5, "12k-25k": -45, "25k+": -85}},
        {"field": "term_months", "weight": 0.10, "points": {"6": 30, "12": 20, "18": 5, "24": -20, "36": -45}},
    ],
    "base": 570,
    "segments": {"product": ["cash", "installment"], "channel": ["app", "partner", "branch", "referral"]},
    "notes": "仅用于演示分层与版本对比；不是真实授信建议。",
}

POLICY_V3_CANDIDATE: dict[str, Any] = {
    "id": "policy-candidate-v3",
    "name": "候选评分 v3（渠道校准、尚未启用）",
    "version": "3.0.0-rc1",
    "approval_threshold": 650,
    "score_floor": 300,
    "score_ceiling": 850,
    "bands": [
        {"field": "income_band", "weight": 0.24, "points": {"A": 125, "B": 92, "C": 55, "D": 5, "E": -60}},
        {"field": "bureau_grade", "weight": 0.40, "points": {"A": 175, "B": 112, "C": 30, "D": -105}},
        {"field": "employment_years", "weight": 0.09, "points": {"0-1": -45, "1-3": -2, "3-7": 48, "7+": 82}},
        {"field": "existing_debt", "weight": 0.17, "points": {"0-4k": 40, "4k-12k": 8, "12k-25k": -40, "25k+": -80}},
        {"field": "term_months", "weight": 0.10, "points": {"6": 28, "12": 18, "18": 5, "24": -18, "36": -40}},
    ],
    "base": 565,
    "segments": {"product": ["cash", "installment"], "channel": ["app", "partner", "branch", "referral"]},
    "notes": "仅用于演示迁移与版本对比；不是真实授信建议。",
}

POLICIES = [POLICY_V1, POLICY_V2, POLICY_V3_CANDIDATE]


def policy_rows() -> list[dict[str, Any]]:
    return [
        {"policy_id": p["id"], "name": p["name"], "version": p["version"], "definition": p, "active": i == 0}
        for i, p in enumerate(POLICIES)
    ]


def _bucket(field: str, value: Any) -> str:
    if field == "employment_years":
        v = float(value)
        if v < 1:
            return "0-1"
        if v < 3:
            return "1-3"
        if v < 7:
            return "3-7"
        return "7+"
    if field == "existing_debt":
        v = int(value)
        if v <= 4000:
            return "0-4k"
        if v <= 12000:
            return "4k-12k"
        if v <= 25000:
            return "12k-25k"
        return "25k+"
    if field == "term_months":
        return str(int(value))
    return str(value)


def evaluate(policy: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
    """对一笔申请打分并返回可解释的分项，不读取任何标签或贷后数据。"""
    raw = float(policy["base"])
    parts: list[dict[str, Any]] = []
    for band in policy["bands"]:
        field = band["field"]
        bucket = _bucket(field, features[field])
        points = band["points"].get(bucket)
        if points is None:
            raise ValueError(f"规则 {policy['id']} 未覆盖 {field}={bucket}")
        weighted = round(points * float(band["weight"]), 2)
        raw += weighted
        parts.append(
            {
                "field": field,
                "value": features[field],
                "bucket": bucket,
                "points": points,
                "weight": band["weight"],
                "weighted": weighted,
            }
        )
    score = int(round(max(policy["score_floor"], min(policy["score_ceiling"], raw))))
    decision = "approve" if score >= policy["approval_threshold"] else "decline"
    return {
        "policy_id": policy["id"],
        "policy_version": policy["version"],
        "score": score,
        "threshold": policy["approval_threshold"],
        "decision": decision,
        "raw": round(raw, 2),
        "parts": parts,
    }


def definition_hash(policy: dict[str, Any]) -> str:
    return digest(policy, 20)


def from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "policy_id": row["policy_id"],
        "name": row["name"],
        "version": row["version"],
        "definition": json.loads(row["definition_json"]) if isinstance(row.get("definition_json"), str) else row["definition"],
        "definition_hash": row["definition_hash"],
        "active": bool(row.get("active")),
    }

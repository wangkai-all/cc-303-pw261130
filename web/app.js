"use strict";

const state = {
  data: null,
  segment: "by_score_band",
  busy: false,
  poll: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const STATUS_LABEL = {
  running: "运行中",
  done: "已完成",
  interrupted: "已中断",
  failed: "失败",
};

const REASON_LABEL = {
  eligible: "入选",
  decision_pending: "审批未完成",
  declined: "审批拒绝",
  disburse_pending: "放款未入库",
  feature_late: "主档晚入库",
};

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmt(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "—";
  return `${value}${suffix}`;
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast show${isError ? " err" : ""}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.className = "toast"), 4200);
}

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `请求失败 (${res.status})`);
  return body;
}

function currentQuery() {
  return new URLSearchParams({
    cutoff: $("#cutoff").value || "",
    mode: $("#mode").value || "batch_holdout",
    policy: $("#policy").value || "",
  });
}

async function load() {
  try {
    const data = await api(`/api/state?${currentQuery()}`);
    state.data = data;
    render();
    primeApplicationList();
    if (data.sample && !state.tracedFor) {
      state.tracedFor = data.sample.sample_run_id;
      traceSampleExample();
    }
    if (state.tracedFor && state.tracedFor !== data.sample?.sample_run_id) {
      state.tracedFor = data.sample?.sample_run_id || null;
      traceSampleExample();
    }
  } catch (err) {
    toast(err.message, true);
  }
}

function render() {
  const d = state.data;
  if (!d) return;
  renderPickers(d);
  renderHero(d);
  renderKpis(d);
  renderMaturity(d);
  renderMethod(d);
  renderSegments(d);
  renderLeakage(d);
  renderCompareControls(d);
  renderRuns(d);
  renderStorage(d);
  renderAudits(d);
  if (!$("#appSearch").dataset.loaded) {
    $("#appSearch").dataset.loaded = "1";
  }
}

function renderPickers(d) {
  const cutoff = $("#cutoff");
  if (!cutoff.dataset.init || cutoff.options.length !== d.cutoffs.length) {
    cutoff.innerHTML = d.cutoffs.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
    cutoff.dataset.init = "1";
  }
  cutoff.value = d.cutoff;

  const mode = $("#mode");
  mode.value = d.mode;
  $("#modeHint").textContent =
    d.mode === "batch_holdout"
      ? "最后若干申请月留出，适合版本验收；需等满观察期。"
      : "逐月滚动评估，适合监控；各窗口成熟度不同。";

  const policy = $("#policy");
  const pkey = d.policies.map((p) => p.policy_id + p.version).join("|");
  if (policy.dataset.key !== pkey) {
    policy.innerHTML = d.policies
      .map((p) => `<option value="${esc(p.policy_id)}">${esc(p.name)} · v${esc(p.version)}</option>`)
      .join("");
    policy.dataset.key = pkey;
  }
  policy.value = d.policy_id;
}

function renderHero(d) {
  $("#cutoffLabel").textContent = d.cutoff;
  const sample = d.sample;
  const report = d.report;
  if (!sample) {
    $("#heroTitle").textContent = "该截止日还没有样本";
    $("#heroNote").textContent = "点击左侧“1 · 构建样本”生成，样本会按截止日封存，后续标签与报告不可回写。";
    $("#integrity").innerHTML = "";
    return;
  }
  $("#heroTitle").textContent = `样本 ${sample.sample_run_id}`;
  const stats = sample.stats || {};
  $("#heroNote").textContent =
    `队列 ${stats.cohort ?? "—"} 笔 · 入选 ${stats.eligible ?? "—"} 笔 · ` +
    (d.labels ? `标签批次 ${d.labels.label_batch_id} · 截至 ${d.labels.stats?.as_of}` : "尚未回补标签");
  const chips = [];
  const integrity = report?.report?.integrity;
  const sOk = integrity?.sample?.ok;
  const lOk = integrity?.labels?.ok;
  const pOk = integrity?.policy?.ok;
  chips.push(
    `<span class="chip ${sOk ? "ok" : "bad"}">样本封存 ${sOk ? "校验通过" : report ? "摘要不符" : "待回测校验"}</span>`,
    `<span class="chip ${lOk ? "ok" : "bad"}">标签封存 ${lOk ? "校验通过" : report ? "摘要不符" : d.labels ? "待回测校验" : "未生成"}</span>`,
    `<span class="chip ${pOk ? "ok" : "bad"}">规则版本 ${pOk ? "校验通过" : report ? "摘要不符" : "待创建"}</span>`,
  );
  if (report) {
    chips.push(`<span class="chip">清单 ${esc(report.manifest_hash.slice(0, 12))}…</span>`);
    chips.push(`<span class="chip ${report.status === "published" ? "ok" : ""}">${report.status === "published" ? "已发布" : "已构建·未发布"}</span>`);
  }
  $("#integrity").innerHTML = chips.join("");
}

function renderKpis(d) {
  const rep = d.report?.report;
  const stats = d.sample?.stats || {};
  const labels = d.labels?.stats || {};
  const scope = rep ? (rep.mode === "batch_holdout" ? "留出批次" : "滚动评估窗口") : "";
  const base = rep ? rep.headline : null;
  const items = [
    {
      k: "评估样本",
      v: fmt(base ? base.n : stats.eligible),
      s: base ? `${scope} · 全部入选 ${fmt(stats.eligible)}` : `全部入选 ${fmt(stats.eligible)}`,
      cls: "accent",
    },
    {
      k: "标签已成熟",
      v: fmt(base ? base.matured : labels.eligible_for_metrics),
      s: base
        ? `好 ${fmt(base.good)} · 坏 ${fmt(base.bad)} · 删失 ${fmt(base.censored)}`
        : `好 ${fmt(labels.good)} · 坏 ${fmt(labels.bad)} · 删失 ${fmt(labels.censored)}`,
    },
    {
      k: "坏账率（成熟样本）",
      v: base ? fmt(base.bad_rate, "%") : "—",
      s: base ? `${base.bad} / ${base.matured}` : "需先运行回测",
      cls: "warn",
    },
    {
      k: "规则通过率",
      v: base ? fmt(base.approval_rate, "%") : "—",
      s: base ? `通过 ${base.approved_n} 笔` : "—",
    },
    {
      k: "通过件坏账率",
      v: base ? fmt(base.approved_bad_rate, "%") : "—",
      s: base ? `${base.approved_bad} / ${base.approved_matured} 成熟` : "—",
    },
    {
      k: "AUC / KS",
      v: base ? `${fmt(base.auc)} / ${fmt(base.ks)}` : "—",
      s: "成熟样本，分数越高越好",
    },
  ];
  $("#kpis").innerHTML = items
    .map(
      (x) => `<div class="kpi ${x.cls || ""}">
        <div class="k">${esc(x.k)}</div>
        <div class="v">${esc(x.v)}</div>
        <div class="s">${esc(x.s)}</div>
      </div>`,
    )
    .join("");
}

function renderMaturity(d) {
  const points = d.report?.report?.maturity?.points || [];
  const box = $("#maturityChart");
  if (!points.length) {
    box.innerHTML = `<div class="empty">运行回测后展示成熟度曲线：红色虚线为观察期截止，曲线为当前已可用样本数。</div>`;
    $("#maturityNote").textContent = "";
    return;
  }
  const width = 560;
  const height = 180;
  const pad = { l: 34, r: 12, t: 12, b: 24 };
  const maxY = Math.max(...points.map((p) => p.matured), 1);
  const xs = points.map((_, i) => pad.l + (i * (width - pad.l - pad.r)) / Math.max(points.length - 1, 1));
  const ys = points.map((p) => height - pad.b - (p.matured / maxY) * (height - pad.t - pad.b));
  const line = xs.map((x, i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(" ");
  const area = `${line} L${xs[xs.length - 1].toFixed(1)},${height - pad.b} L${xs[0].toFixed(1)},${height - pad.b} Z`;
  const grid = [0.25, 0.5, 0.75, 1]
    .map((f) => {
      const y = height - pad.b - f * (height - pad.t - pad.b);
      return `<line class="grid-line" x1="${pad.l}" y1="${y.toFixed(1)}" x2="${width - pad.r}" y2="${y.toFixed(1)}" />
        <text x="4" y="${(y + 3).toFixed(1)}">${Math.round(maxY * f)}</text>`;
    })
    .join("");
  const dots = points
    .map(
      (p, i) => `<circle class="dot" cx="${xs[i].toFixed(1)}" cy="${ys[i].toFixed(1)}" r="3.2">
        <title>${esc(p.as_of)} · 可用 ${p.matured} 笔 · 覆盖 ${esc(p.coverage_pct)}% · 坏账率 ${esc(p.bad_rate)}%</title>
      </circle>
      <text x="${xs[i].toFixed(1)}" y="${height - 7}" text-anchor="middle">${esc(p.as_of.slice(5))}</text>`,
    )
    .join("");
  box.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="标签成熟度曲线">
      <defs><linearGradient id="maturityFill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#59d1a8" stop-opacity="0.30" />
        <stop offset="100%" stop-color="#59d1a8" stop-opacity="0" />
      </linearGradient></defs>
      ${grid}
      <path class="area" d="${area}" />
      <path class="line" d="${line}" />
      ${dots}
    </svg>
    <div class="legend"><span><i style="background:var(--accent)"></i>已可用标签</span><span>悬停查看各时点覆盖与坏账率</span></div>`;
  const snap = points[points.length - 1];
  $("#maturityRange").textContent = `${points[0].as_of} → ${snap.as_of}（快照日）`;
  $("#maturityNote").textContent = d.report?.report?.maturity?.note || "";
  const rep = d.report?.report;
  if (rep) {
    const total = rep.headline.n || 1;
    const g = (rep.headline.good / total) * 100;
    const b = (rep.headline.bad / total) * 100;
    const c = (rep.headline.censored / total) * 100;
    $("#maturityRange").innerHTML =
      `<span class="maturity-split" style="width:180px;display:inline-flex;vertical-align:middle;margin:0 8px">` +
      `<i class="good" style="width:${g}%"></i><i class="bad" style="width:${b}%"></i><i class="censored" style="width:${c}%"></i></span>` +
      `${points[0].as_of} → ${snap.as_of}（快照日）`;
  }
}

function renderMethod(d) {
  const rep = d.report?.report;
  if (!rep) {
    $("#methodNote").innerHTML = `<div class="empty">选择规则版本并运行回测后，这里展示两种回测口径的取舍。</div>`;
    $("#findings").innerHTML = "";
    $("#modeTag").textContent = "";
    return;
  }
  const m = rep.method;
  $("#modeTag").textContent = rep.mode === "batch_holdout" ? "批次留出" : "滚动回测";
  const windows = rep.windows;
  let extra = "";
  if (windows.kind === "batch_holdout") {
    extra = `<div class="box"><h3>窗口划分</h3><p>留出月：${esc(windows.holdout_months.join("、")) || "—"}；参考月 ${windows.train_months.length} 个。</p></div>`;
  } else {
    extra = `<div class="box"><h3>窗口划分</h3><p>评估月：${esc(windows.eval_months.join("、")) || "—"}；历史参考月 ${windows.train_months.length} 个。</p></div>`;
  }
  $("#methodNote").innerHTML = `<div class="method-box">
      <div class="box"><h3>做法</h3><p>${esc(m.summary)}</p></div>
      <div class="box"><h3>适用</h3><p>${esc(m.why)}</p></div>
      ${extra}
      <div class="box"><h3>取舍</h3><p>${esc(m.tradeoff)}</p></div>
    </div>`;
  $("#findings").innerHTML = (rep.findings || []).map((f) => `<div class="finding">${esc(f)}</div>`).join("");
}

const SEGMENT_TABS = [
  ["by_score_band", "评分带"],
  ["by_income_band", "收入带"],
  ["by_bureau_grade", "征信等级"],
  ["by_channel", "渠道"],
  ["by_product", "产品"],
  ["by_month", "申请月"],
];

function renderSegments(d) {
  const rep = d.report?.report;
  const tabs = $("#segmentTabs");
  tabs.innerHTML = SEGMENT_TABS.map(
    ([key, label]) =>
      `<button class="tab ${state.segment === key ? "active" : ""}" data-segment="${key}">${label}</button>`,
  ).join("");
  tabs.querySelectorAll("[data-segment]").forEach((btn) =>
    btn.addEventListener("click", () => {
      state.segment = btn.dataset.segment;
      renderSegments(state.data);
    }),
  );
  const box = $("#segmentTable");
  if (!rep) {
    box.innerHTML = `<div class="empty">暂无回测结果。</div>`;
    return;
  }
  if (rep.windows?.degraded) {
    box.innerHTML = `<div class="empty">${esc(rep.windows.degraded_reason)}</div>`;
    return;
  }
  const order = ["750+", "700-749", "650-699", "600-649", "<600"];
  const segment = rep.segments[state.segment] || {};
  const keys = state.segment === "by_score_band" ? order.filter((k) => segment[k]) : Object.keys(segment).sort();
  if (!keys.length) {
    box.innerHTML = `<div class="empty">当前窗口没有可用分层。</div>`;
    return;
  }
  const total = keys.reduce((acc, k) => acc + (segment[k].matured || 0), 0) || 1;
  const rows = keys
    .map((k) => {
      const r = segment[k];
      const share = ((r.matured / total) * 100).toFixed(1);
      return `<tr>
        <td>${esc(k)}</td>
        <td class="num">${fmt(r.n)}</td>
        <td class="num">${fmt(r.matured)}</td>
        <td class="num">${fmt(r.maturity_rate, "%")}</td>
        <td class="num">${fmt(r.good)}</td>
        <td class="num">${fmt(r.bad)}</td>
        <td class="num">${badge(r.bad_rate)}</td>
        <td class="num">${fmt(r.approval_rate, "%")}</td>
        <td class="num">${fmt(r.approved_bad_rate, "%")}</td>
        <td class="num">${fmt(r.auc)}</td>
        <td class="num">${share}%</td>
      </tr>`;
    })
    .join("");
  const caption =
    state.segment === "by_month"
      ? "按申请月展示成熟覆盖：越靠近截止日，可用标签越少，坏账率的分母越小。"
      : "分组仅在成熟样本上计算坏账率；未成熟样本单独列示，不进入分母。";
  box.innerHTML = `<table>
      <thead><tr>
        <th>分组</th><th>样本</th><th>成熟</th><th>覆盖</th><th>好</th><th>坏</th><th>坏账率</th>
        <th>通过率</th><th>通过件坏账率</th><th>AUC</th><th>成熟占比</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="muted small" style="margin-top:8px">${esc(caption)}</p>`;
}

function badge(rate) {
  if (rate === null || rate === undefined) return "—";
  const cls = rate >= 10 ? "bad" : rate >= 5 ? "mid" : "good";
  return `<span class="pill ${cls}">${esc(rate)}%</span>`;
}

function renderLeakage(d) {
  const leak = d.leakage;
  const stats = d.sample?.stats || {};
  if (!leak) {
    $("#leakTag").textContent = "";
    $("#leakage").innerHTML = `<div class="empty">选择已有样本的截止日。</div>`;
    return;
  }
  $("#leakTag").textContent = `${leak.rows_with_blocked} 笔含晚到记录`;
  const reasons = stats.reasons || {};
  const total = stats.cohort || 1;
  const bars = Object.entries(reasons)
    .sort((a, b) => b[1] - a[1])
    .map(
      ([code, n]) => `<div class="reason-bar">
        <span>${esc(REASON_LABEL[code] || code)}</span>
        <span class="bar"><i style="width:${((n / total) * 100).toFixed(1)}%"></i></span>
        <span style="text-align:right">${n} 笔</span>
      </div>`,
    )
    .join("");
  const examples = leak.examples.length
    ? leak.examples
        .map(
          (e) => `<div class="leak-item">
            <b>${esc(e.application_id)}</b>：${esc(e.kind)} 发生于 ${esc(e.occurred_on)}，
            但 ${esc(e.recorded_on)} 才入库，晚于审批日，已从审批时特征中剔除。
          </div>`,
        )
        .join("")
    : `<div class="leak-item">本截止日没有“业务发生早于审批、入库晚于审批”的记录。</div>`;
  $("#leakage").innerHTML = `
    <div class="reason-bars" style="margin-bottom:12px">${bars}</div>
    <div class="leak-list">
      <div class="leak-item">
        <b>主档晚入库</b> ${leak.feature_late} 笔申请在审批时不可见，直接排除，不用未来信息补特征。
      </div>
      <div class="leak-item">
        <b>晚到记录（快照内）</b> ${leak.blocked_total} 条事件“业务日不晚于审批日、入库晚于审批日、且在截止日快照内”，
        全部从审批时特征隔离；快照日之后才入库的 ${fmt(leak.late_records_after_cutoff)} 条事件不在本次样本视野内。
      </div>
      ${examples}
    </div>`;
}

function reportOptions(d) {
  return (d.reports || [])
    .map(
      (r) =>
        `<option value="${esc(r.report_id)}">${esc(r.report_id)} · ${esc(r.mode === "batch_holdout" ? "批次留出" : "滚动")} · ${esc(r.policy_id)} · ${esc(r.cutoff)}</option>`,
    )
    .join("");
}

function renderCompareControls(d) {
  const left = $("#leftReport");
  const right = $("#rightReport");
  const prevL = left.value;
  const prevR = right.value;
  const opts = reportOptions(d);
  left.innerHTML = opts;
  right.innerHTML = opts;
  const reports = d.reports || [];
  // 默认优先同一截止日、同一样本快照下的另一规则版本，避免把样本变化误读成版本差异。
  const peers = reports.filter((r) => r.cutoff === d.cutoff);
  const same = peers.find((r) => r.mode === d.mode) || peers[0];
  const pickLeft = prevL || same?.report_id || reports[0]?.report_id || "";
  const leftRep = reports.find((r) => r.report_id === pickLeft);
  const pickRight =
    prevR && prevR !== pickLeft
      ? prevR
      : reports.find(
          (r) =>
            r.report_id !== pickLeft &&
            leftRep &&
            r.sample_run_id === leftRep.sample_run_id &&
            r.policy_id !== leftRep.policy_id,
        )?.report_id ||
        reports.find((r) => r.report_id !== pickLeft && r.cutoff === d.cutoff)?.report_id ||
        reports.find((r) => r.report_id !== pickLeft)?.report_id ||
        pickLeft;
  left.value = pickLeft;
  right.value = pickRight;
}

$("#compareBtn")?.addEventListener("click", async () => {
  const left = $("#leftReport").value;
  const right = $("#rightReport").value;
  if (!left || !right) return toast("至少需要两份报告", true);
  if (left === right) return toast("请选择两份不同的报告", true);
  try {
    const cmp = await api(`/api/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);
    const rows = cmp.rows
      .map(
        (r) => `<tr><td>${esc(r.label)}</td><td class="num">${esc(fmt(r.left))}</td><td class="num">${esc(fmt(r.right))}</td></tr>`,
      )
      .join("");
    const warnings = cmp.warnings.length
      ? cmp.warnings.map((w) => `<div class="warn-item">${esc(w)}</div>`).join("")
      : `<div class="muted small">样本、标签批次与回测口径一致，可直接比较规则版本。</div>`;
    $("#compareOut").innerHTML = `
      <div class="warn-list">${warnings}</div>
      <table><thead><tr><th>指标</th><th>左</th><th>右</th></tr></thead><tbody>${rows}</tbody></table>`;
  } catch (err) {
    toast(err.message, true);
  }
});

function renderRuns(d) {
  const runs = d.runs || [];
  const box = $("#runs");
  if (!runs.length) {
    box.innerHTML = `<div class="empty">还没有任务记录。</div>`;
    $("#jobStatus").innerHTML = "";
    return;
  }
  const current = runs.slice(0, 6);
  $("#jobStatus").innerHTML = current
    .map((r) => {
      const steps = Object.entries(r.steps || {})
        .map(([name, s]) => `<span class="step-chip ${s.status === "done" ? "done" : ""}">${esc(stepLabel(name))}</span>`)
        .join("");
      const resumable = ["interrupted", "failed", "running"].includes(r.status);
      return `<div class="job">
        <div class="row">
          <b>${esc(kindLabel(r.kind))}</b>
          <span class="status-pill status-${esc(r.status)}">${esc(STATUS_LABEL[r.status] || r.status)}</span>
        </div>
        <div class="tl-meta">${esc(r.run_id)} · ${esc(r.cutoff || "—")}</div>
        <div class="step-list">${steps || '<span class="step-chip">尚未开始</span>'}</div>
        ${r.error ? `<div class="tl-meta" style="color:var(--bad);margin-top:4px">${esc(r.error)}</div>` : ""}
        ${resumable && r.status !== "running" ? `<button class="btn small" style="margin-top:6px" data-resume="${esc(r.run_id)}">继续此任务（断点恢复）</button>` : ""}
      </div>`;
    })
    .join("");
  $("#jobStatus").querySelectorAll("[data-resume]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/jobs/${btn.dataset.resume}/resume`, { method: "POST", body: "{}" });
        toast("已从断点继续；已封存的样本/标签会复用，不会混用两版。");
        schedulePoll();
        load();
      } catch (err) {
        toast(err.message, true);
      }
    }),
  );
  box.innerHTML = runs
    .slice(0, 14)
    .map(
      (r) => `<div class="audit">
        <span>${esc((r.created_at || "").slice(5, 16).replace("T", " "))}</span>
        <span><b>${esc(kindLabel(r.kind))}</b> · ${esc(STATUS_LABEL[r.status] || r.status)} · ${esc(r.run_id.slice(-6))}</span>
      </div>`,
    )
    .join("");
}

function kindLabel(kind) {
  return { sample: "样本构建", labels: "标签回补", backtest: "回测", publish: "发布" }[kind] || kind;
}

function stepLabel(step) {
  return (
    {
      load_raw: "读原始数据",
      cohort: "筛选队列",
      features: "时点特征",
      persist_sample: "封存样本",
      load_sample: "读样本",
      locate_events: "定位事件",
      classify: "判定标签",
      persist_labels: "封存标签",
      verify_inputs: "校验输入",
      score: "规则评分",
      windows: "窗口划分",
      metrics: "指标",
      maturity: "成熟度",
      report: "生成报告",
      verify: "校验",
      publish: "发布",
    }[step] || step
  );
}

function renderStorage(d) {
  const s = d.storage || {};
  const kv = [
    ["数据库大小", `${((s.db_bytes || 0) / 1024 / 1024).toFixed(2)} MB`],
    ["数据集摘要", s.dataset_hash || "—"],
    ["合成数据快照", s.snapshot_date || "—"],
    ["样本 / 标签 / 报告", `${s.counts?.samples ?? 0} / ${s.counts?.label_batches ?? 0} / ${s.counts?.reports ?? 0}`],
    ["申请 / 事件", `${s.counts?.applications ?? 0} / ${s.counts?.events ?? 0}`],
  ];
  $("#storage").innerHTML = kv.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

function renderAudits(d) {
  const items = d.audits || [];
  $("#audits").innerHTML = items.length
    ? items
        .map(
          (a) => `<div class="audit">
            <span>${esc((a.at || "").slice(5, 19).replace("T", " "))}</span>
            <span><b>${esc(a.action)}</b> · ${esc(summarizeAudit(a.detail))}</span>
          </div>`,
        )
        .join("")
    : `<div class="empty">暂无审计事件。</div>`;
}

function summarizeAudit(detail) {
  if (!detail) return "";
  if (detail.report_id) return `报告 ${detail.report_id}`;
  if (detail.label_batch_id) return `标签批次 ${detail.label_batch_id}`;
  if (detail.sample_run_id) return `样本 ${detail.sample_run_id}`;
  if (detail.error) return detail.error.slice(0, 60);
  return JSON.stringify(detail).slice(0, 60);
}

// ---------- 流水线操作 ----------

$$("[data-action]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    const action = btn.dataset.action;
    if (state.busy) return toast("已有任务在运行", true);
    const d = state.data;
    const payload = { fail_after: null };
    if ($("#simulateFail").checked) {
      payload.fail_after = { sample: "features", labels: "classify", backtest: "metrics", publish: "verify" }[action];
    }
    try {
      if (action === "sample") {
        payload.cutoff = d.cutoff;
        payload.mode = d.mode;
      }
      if (action === "labels") {
        if (!d.sample) return toast("请先构建样本", true);
        payload.sample_id = d.sample.sample_run_id;
        payload.as_of = d.storage?.snapshot_date || d.cutoff;
      }
      if (action === "backtest") {
        if (!d.sample || !d.labels) return toast("请先构建样本并回补标签", true);
        payload.sample_id = d.sample.sample_run_id;
        payload.label_id = d.labels.label_batch_id;
        payload.policy_id = d.policy_id;
        payload.mode = d.mode;
        payload.cutoff = d.cutoff;
      }
      if (action === "publish") {
        if (!d.report) return toast("请先运行回测", true);
        payload.report_id = d.report.report_id;
      }
      setBusy(true);
      const res = await api(`/api/actions/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast(`${kindLabel(action)}已启动（${res.run_id}）`);
      schedulePoll();
    } catch (err) {
      setBusy(false);
      toast(err.message, true);
    }
  }),
);

function setBusy(v) {
  state.busy = v;
  $$("[data-action]").forEach((b) => (b.disabled = v));
}

function schedulePoll() {
  clearInterval(state.poll);
  let ticks = 0;
  state.poll = setInterval(async () => {
    ticks += 1;
    try {
      const { runs } = await api("/api/jobs");
      const active = runs.some((r) => r.status === "running");
      await load();
      if (!active) {
        setBusy(false);
        clearInterval(state.poll);
        toast("任务结束，可继续下一步或切换版本对比。");
      }
    } catch (_) {
      /* 轮询失败不打断页面 */
    }
    if (ticks > 80) {
      clearInterval(state.poll);
      setBusy(false);
    }
  }, 1200);
}

// ---------- 样本追溯 ----------

$("#traceBtn").addEventListener("click", runTrace);
$("#randomBtn").addEventListener("click", () => {
  const d = state.data;
  if (!d?.sample) return toast("请先构建样本", true);
  const idx = Math.floor(Math.random() * Math.max(1, d.sample.stats?.eligible || 1));
  const candidates = getCandidates();
  if (!candidates.length) return toast("没有可追溯的申请", true);
  $("#appSearch").value = candidates[Math.min(idx, candidates.length - 1)];
  runTrace();
});

$("#appSearch").addEventListener("change", () => {
  if ($("#appSearch").value.trim()) runTrace();
});

function getCandidates() {
  return state.data?._candidates || [];
}

async function runTrace() {
  const d = state.data;
  const query = $("#appSearch").value.trim();
  if (!d?.sample) return toast("请先构建样本", true);
  if (!query) return toast("请输入申请号或客户号", true);
  try {
    let appId = query.toUpperCase();
    if (!appId.startsWith("APP-")) {
      const found = await api(
        `/api/applications?sample_id=${encodeURIComponent(d.sample.sample_run_id)}&q=${encodeURIComponent(query)}`,
      );
      if (!found.items.length) return toast("没有匹配的申请", true);
      appId = found.items[0].application_id;
      $("#appSearch").value = appId;
    }
    const trace = await api(`/api/sample/${d.sample.sample_run_id}/application/${encodeURIComponent(appId)}`);
    renderTrace(trace);
  } catch (err) {
    toast(err.message, true);
  }
}

async function traceSampleExample() {
  const d = state.data;
  if (!d?.sample) return;
  try {
    const res = await api(`/api/applications?sample_id=${encodeURIComponent(d.sample.sample_run_id)}&q=`);
    // 优先展示一条“审批后才入库”的更正记录，直观说明隔离规则。
    const pick =
      res.items.find((x) => x.eligible && x.blocked) ||
      res.items.find((x) => x.eligible) ||
      res.items[0];
    if (!pick) return;
    $("#appSearch").value = pick.application_id;
    const trace = await api(
      `/api/sample/${d.sample.sample_run_id}/application/${encodeURIComponent(pick.application_id)}`,
    );
    renderTrace(trace);
  } catch (_) {
    /* 首次自动追溯失败不打扰用户 */
  }
}

async function primeApplicationList() {
  const d = state.data;
  if (!d?.sample) return;
  try {
    const res = await api(`/api/applications?sample_id=${encodeURIComponent(d.sample.sample_run_id)}&q=`);
    state.data._candidates = res.items.map((x) => x.application_id);
    $("#appList").innerHTML = res.items
      .slice(0, 60)
      .map((x) => `<option value="${esc(x.application_id)}">${esc(x.customer_id)} · ${esc(REASON_LABEL[x.reason_code] || x.reason_code)}</option>`)
      .join("");
  } catch (_) {
    /* 忽略 */
  }
}

function renderTrace(t) {
  const row = t.sample_row;
  const lab = t.label;
  const ev = t.application;
  const evalData = t.evaluation;
  const features = row.features || {};
  const vis = row.visibility || {};
  const reasonClass = row.eligible ? "" : " excluded";
  const labelText = lab
    ? { good: "好客户（已满观察期）", bad: "坏客户（确认逾期）", censored: "删失（观察期未满）" }[lab.status] || lab.status
    : "未回补标签";
  const labelClass = lab?.status === "bad" ? "bad" : lab?.status === "good" ? "good" : "mid";

  const parts = (evalData?.parts || [])
    .map((p) => {
      const w = Math.max(-90, Math.min(90, p.weighted * 4));
      const pos = p.weighted >= 0;
      return `<div class="scorebar">
        <span>${esc(fieldLabel(p.field))} · ${esc(p.bucket)}</span>
        <span class="track"><i class="${pos ? "pos" : "neg"}" style="width:${Math.abs(w)}%"></i></span>
        <span style="text-align:right">${p.weighted > 0 ? "+" : ""}${esc(p.weighted)}</span>
      </div>`;
    })
    .join("");

  const timeline = [...t.visible_events, ...t.blocked_events, ...t.after_decision_events.slice(0, 6)]
    .slice(0, 18)
    .map((e) => {
      const cls =
        e.visibility === "visible"
          ? "visible"
          : e.visibility === "blocked"
            ? "blocked bad-vis"
            : e.visibility === "future"
              ? "blocked"
              : "after";
      const note =
        e.visibility === "visible"
          ? "审批前可见"
          : e.visibility === "blocked"
            ? "发生在前、快照内入库在审批后 → 已隔离"
            : e.visibility === "future"
              ? "快照日之后才入库 → 本样本不可见"
              : "审批后";
      return `<div class="tl-item ${cls}">
        <span class="tl-dot"></span>
        <div>
          <div>${esc(e.kind_label)} · ${e.dpd ? `DPD ${esc(e.dpd)}` : esc(e.detail || "—")}</div>
          <div class="tl-meta">发生 ${esc(e.occurred_on)} · 入库 ${esc(e.recorded_on)} · ${esc(note)}</div>
        </div>
      </div>`;
    })
    .join("");

  $("#traceOut").innerHTML = `
    <div class="reason-callout${reasonClass}">
      <b>${esc(row.application_id)}</b> · ${esc(row.eligible ? "入选样本" : "未入选")}：${esc(row.reason_text)}
      <div class="tl-meta" style="margin-top:4px">决策日 ${esc(row.decision_on || "—")} · 放款日 ${esc(row.disburse_on || "—")} · 申请日 ${esc(row.applied_on)} · 快照入库 ${esc(vis.master_recorded_on || "—")}</div>
    </div>
    <div class="trace-grid">
      <div class="trace-card">
        <h3>标签与观察窗</h3>
        <dl class="kv">
          <dt>状态</dt><dd><span class="pill ${labelClass}">${esc(labelText)}</span></dd>
          <dt>观察窗</dt><dd>${esc(lab?.window_start || "—")} → ${esc(lab?.window_end || "—")}</dd>
          <dt>成熟/确认日</dt><dd>${esc(lab?.matured_on || "—")}</dd>
          <dt>窗口内最大 DPD</dt><dd>${esc(lab?.evidence?.max_dpd ?? "—")}</dd>
          <dt>坏事件数</dt><dd>${esc(lab?.evidence?.bad_events?.length ?? 0)}</dd>
          <dt>标签批次</dt><dd>${esc(t.label_batch?.label_batch_id || "—")}</dd>
        </dl>
        <p class="muted small" style="margin-top:8px">${esc(lab?.evidence?.policy_rule || "运行标签回补后展示规则。")}</p>
      </div>
      <div class="trace-card">
        <h3>审批时特征（仅可见信息）</h3>
        <dl class="kv">
          <dt>收入带</dt><dd>${esc(features.income_band || "—")}</dd>
          <dt>征信等级</dt><dd>${esc(features.bureau_grade || "—")}</dd>
          <dt>就业年限</dt><dd>${esc(features.employment_years ?? "—")}</dd>
          <dt>存量负债</dt><dd>${esc(features.existing_debt ?? "—")}</dd>
          <dt>期限</dt><dd>${esc(features.term_months ?? "—")} 月</dd>
          <dt>金额</dt><dd>${esc(features.amount ?? "—")}</dd>
          <dt>历史申请</dt><dd>${esc(features.prior_loans ?? "—")}</dd>
          <dt>历史坏事件</dt><dd>${esc(features.prior_bad_events ?? "—")}（最大 DPD ${esc(features.prior_max_dpd ?? 0)}）</dd>
        </dl>
        <p class="muted small" style="margin-top:8px">${esc(vis.rule || "")} · 隔离晚到记录 ${esc(vis.blocked_late_records ?? 0)} 条</p>
      </div>
    </div>
    <div class="trace-grid">
      <div class="trace-card">
        <h3>基础评分拆解 ${evalData ? `（${esc(evalData.policy_id)}，${evalData.score} 分 vs 阈值 ${evalData.threshold}）` : ""}</h3>
        ${
          evalData
            ? `<div class="bars">${parts}</div>
               <p class="muted small" style="margin-top:8px">规则结论：<b style="color:${evalData.decision === "approve" ? "var(--good)" : "var(--bad)"}">${evalData.decision === "approve" ? "通过" : "拒绝"}</b>；仅演示用，不构成授信建议。</p>`
            : `<div class="empty">该申请未入选，或没有启用规则版本。</div>`
        }
      </div>
      <div class="trace-card">
        <h3>事件时间线</h3>
        <div class="timeline">${timeline || '<div class="empty">没有事件记录。</div>'}</div>
      </div>
    </div>`;
}

function fieldLabel(field) {
  return (
    {
      income_band: "收入带",
      bureau_grade: "征信",
      employment_years: "就业年限",
      existing_debt: "存量负债",
      term_months: "期限",
      amount: "金额",
      product: "产品",
      channel: "渠道",
    }[field] || field
  );
}

// ---------- 事件绑定 ----------

$("#cutoff").addEventListener("change", load);
$("#mode").addEventListener("change", load);
$("#policy").addEventListener("change", load);

load();
setInterval(async () => {
  if (state.busy) return;
  try {
    const { runs } = await api("/api/jobs");
    if (runs.some((r) => r.status === "running")) {
      setBusy(true);
      schedulePoll();
    }
  } catch (_) {
    /* 忽略 */
  }
}, 5000);

/* 偏差练习处方与复测 —— 原生 JS（与 app.js / compare.js / synth.js 同页）
 *
 * 数据流：
 *   GET  /api/prescriptions                     处方列表
 *   POST /api/prescriptions                     创建（request_id 幂等）
 *   GET  /api/prescriptions/{id}                详情（固化/时间线/复测摘要）
 *   GET  /api/prescriptions/{id}/candidates     同曲录音兼容状态
 *   POST /api/prescriptions/{id}/retests        复测（request_id 幂等；
 *                                               来源过期/指纹不符/无已确认
 *                                               对齐/已作废 返回 409）
 *   GET  /api/prescriptions/{id}/retests        复测历史
 *   GET  /api/retests/{id}                      单次复测完整结果
 *   POST /api/prescriptions/{id}/rebind         显式重绑（保留旧结果）
 *   POST /api/prescriptions/{id}/void           作废
 */
"use strict";

const rxState = {
  list: [],
  current: null,          // 处方详情
  candidates: [],
  retests: [],            // 完整复测历史（含 result）
  selectedRetest: null,   // 当前展示的复测 id
  diffA: null,
  diffB: null,
  builder: null,          // {source_type, source_id, title, segments, notes}
};

const RX_CATEGORIES = [
  ["wrong_pitch", "错音"], ["missing", "漏音"], ["extra", "多音"],
  ["onset_offset", "起音偏移"], ["duration_deviation", "时值偏差"],
];
const RX_CAT_LABEL = Object.fromEntries(RX_CATEGORIES);
const RX_VERDICT_LABEL = {
  pass: "通过", fail: "未通过", undeterminable: "不可判定",
};
const RX_UND_LABEL = {
  pending_confirmation: "未处置待确认/歧义",
  excluded: "用户已排除该谱音",
  low_confidence: "置信度不足",
  no_snapshot_note: "快照缺该谱音",
};
const RX_DEFAULT_MAX_ONSET = 0.12;
const RX_DEFAULT_DUR_DEV = 0.2;
const RX_COMPAT_LABEL = {
  source_stale: "处方来源已过期：固化成员的对齐版本/音频已变化，须先显式重绑",
  member_stale: "该录音是处方固化成员且已过期：请先显式重绑来源",
  no_confirmed_alignment: "尚无已确认对齐版本：请先完成分析并确认对齐",
};
const RX_STALE_REASON_LABEL = {
  alignment_version_changed: "对齐版本已变化",
  alignment_missing: "已确认对齐版本缺失",
  audio_changed: "音频哈希已变化",
  score_changed: "谱文件已变化",
  removed_from_comparison: "已被移出对比",
  piece_removed: "录音已删除",
  comparison_removed: "对比已删除",
};

function rid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return "rid-" + Date.now().toString(36) + "-"
    + Math.random().toString(36).slice(2, 10);
}

// ---------------------------------------------------------------- 入口

async function rxOnViewEnter() {
  await loadRxList();
  if (rxState.current) {
    await openRx(rxState.current.id);
  } else {
    $("#rx-empty").classList.remove("hidden");
  }
}

$("#btn-new-rx-piece").addEventListener("click", () => openRxBuilder("piece"));
$("#btn-new-rx-cmp").addEventListener("click", () => openRxBuilder("comparison"));
$("#btn-rx-cancel").addEventListener("click", () => closeRxBuilder());
$("#btn-rx-create").addEventListener("click", submitRxCreate);
$("#btn-rx-retest").addEventListener("click", submitRetest);
$("#btn-rx-rebind").addEventListener("click", submitRebind);
$("#btn-rx-void").addEventListener("click", submitVoid);

$("#btn-review-rx").addEventListener("click", async () => {
  if (!state.pieceId) { alert("请先打开一支已分析的录音"); return; }
  await switchView("rx");
  openRxBuilder("piece", state.pieceId);
});
$("#btn-compare-rx").addEventListener("click", async () => {
  if (!cmpState.data) { alert("请先打开一次对比"); return; }
  if (cmpState.data.stale) {
    alert("该对比已过期：请先“显式刷新”固化最新只读快照，再开处方。");
    return;
  }
  await switchView("rx");
  openRxBuilder("comparison", cmpState.data.id);
});

// ---------------------------------------------------------------- 列表

async function loadRxList() {
  rxState.list = await api("/api/prescriptions");
  const ul = $("#rx-list");
  ul.innerHTML = "";
  if (!rxState.list.length) {
    ul.appendChild(htmlEl("li", { className: "muted-li" }, "（暂无处方）"));
    return;
  }
  for (const p of rxState.list) {
    const li = htmlEl("li", {
      className: rxState.current && p.id === rxState.current.id
        ? "active" : "",
      onclick: () => openRx(p.id),
    });
    const name = document.createElement("span");
    name.className = "rx-name";
    name.innerHTML =
      `${escapeHtml(p.title)}<span class="rx-sub">`
      + `${p.source_type === "piece" ? "乐曲" : "对比"} · ${p.segment_count} 段`
      + ` · 复测 ${p.retest_count} 次</span>`;
    li.appendChild(name);
    const dot = htmlEl("span", { className: `rx-status-dot ${p.status}` },
      ({ created: "待复测", retested: "已复测", voided: "已作废" })[p.status]);
    li.appendChild(dot);
    if (p.source_stale) {
      li.appendChild(htmlEl("span", {
        className: "stale-dot",
        title: "来源对齐版本/音频已变化，复测前须显式重绑",
      }, "过期"));
    }
    ul.appendChild(li);
  }
}

// ---------------------------------------------------------------- 打开详情

async function openRx(id) {
  try {
    rxState.current = await api(`/api/prescriptions/${id}`);
  } catch (err) {
    alert("加载处方失败：" + err.message);
    return;
  }
  closeRxBuilder(true);
  $("#rx-empty").classList.add("hidden");
  $("#rx-detail").classList.remove("hidden");
  await Promise.all([loadCandidates(), loadRetests()]);
  renderRx();
  loadRxList();
}

async function loadCandidates() {
  rxState.candidates = await api(
    `/api/prescriptions/${rxState.current.id}/candidates`);
}

async function loadRetests() {
  const briefs = await api(
    `/api/prescriptions/${rxState.current.id}/retests`);
  rxState.retests = await Promise.all(
    briefs.map((b) => api(`/api/retests/${b.id}`)));
  rxState.retests.sort((a, b) => a.ord - b.ord);
  if (rxState.retests.length) {
    const keep = rxState.retests.some((r) => r.id === rxState.selectedRetest);
    if (!keep) rxState.selectedRetest = rxState.retests.at(-1).id;
  } else {
    rxState.selectedRetest = null;
  }
  // 默认差异对比：最近两次
  if (rxState.retests.length >= 2) {
    rxState.diffA = rxState.diffA && rxState.retests.some(
      (r) => r.id === rxState.diffA)
      ? rxState.diffA : rxState.retests.at(-2).id;
    rxState.diffB = rxState.retests.at(-1).id;
  } else if (rxState.retests.length === 1) {
    rxState.diffA = null;
    rxState.diffB = rxState.retests[0].id;
  } else {
    rxState.diffA = rxState.diffB = null;
  }
}

function selectedRetest() {
  return rxState.retests.find((r) => r.id === rxState.selectedRetest) || null;
}

// ---------------------------------------------------------------- 渲染详情

function renderRx() {
  const p = rxState.current;
  $("#rx-detail-title").textContent = p.title;
  const statusLabel = { created: "待复测", retested: "已复测",
                       voided: "已作废" }[p.status];
  $("#rx-detail-meta").textContent =
    `创建 ${p.created_at}${p.voided_at ? ` · 作废 ${p.voided_at}` : ""}`
    + ` · 状态 ${statusLabel} · 来源：`
    + (p.source_type === "piece" ? "单次乐曲" : "多次演奏对比");
  renderStaleBanner(p);
  renderFrozen(p);
  renderCurrentOverall();
  renderTargetSelect(p);
  renderSegmentRules(p, selectedRetest());
  renderTimeline(p);
  renderDiffControls();
  renderDiff();
}

function renderCurrentOverall() {
  const box = $("#rx-current-overall");
  const rt = selectedRetest();
  if (!rt) { box.innerHTML = ""; return; }
  const cls = rt.overall === "pass" ? "rx-pass"
    : rt.overall === "fail" ? "rx-fail" : "rx-und";
  box.innerHTML = `当前展示：第 ${rt.ord} 次复测《${escapeHtml(rt.target_title)}》`
    + `（对齐 v${rt.target_version} · 音频 <code>${
      (rt.target_wav_sha256 || "").slice(0, 12)}</code>）总判定 `
    + `<span class="${cls}">${RX_VERDICT_LABEL[rt.overall]}</span>`
    + ` · ${rt.created_at}`
    + (rt.result && rt.result.source_basis
      ? `<div class="rx-sub">依据来源固化版本：第 ${
        rt.result.source_basis.source_version_ord} 版（来源 `
        + `${rt.result.source_basis.source_type === "piece" ? "单曲" : "对比"}`
        + `）；历史结果只读，来源/目标之后变化不改写本结果。</div>`
      : "");
}

function renderStaleBanner(p) {
  const b = $("#rx-stale-banner");
  if (!p.source_stale) { b.classList.add("hidden"); return; }
  const names = p.source_stale_members.map((s) =>
    `${s.piece_id}（${s.reasons.map(staleReasonLabel).join("、")}）`).join("；");
  b.textContent = "⚠ 来源已过期：" + names
    + "。旧复测结果保留；请点“显式重绑来源”后再提交复测。";
  b.classList.remove("hidden");
}

function renderFrozen(p) {
  const box = $("#rx-frozen");
  const members = p.frozen.members.map((m) => {
    const stale = p.source_stale_members.some(
      (s) => s.piece_id === m.piece_id);
    return `<span class="rx-frozen-member${stale ? " stale" : ""}">`
      + `${m.is_baseline ? "★ " : ""}${escapeHtml(m.title || m.piece_id)}`
      + ` · 固化对齐 v${m.alignment_version}</span>`;
  }).join("");
  const srcRef = p.source_type === "comparison"
    ? `对比《${escapeHtml(p.frozen.comparison_title || p.comparison_id)}》`
    : "单次乐曲";
  box.innerHTML =
    `只读固化：来源 ${srcRef} · 乐谱指纹 `
    + `<code>${p.fingerprint.slice(0, 16)}…</code>`
    + ` · 算法 ${p.params.rx_algorithm_version}`
    + ` · 置信阈值 ≥ ${p.params.min_confidence}`
    + `<div>${members}</div>`
    + `<div>默认起音误差 ≤ ${p.params.default_max_onset_error_s}s · `
    + `默认时值比例误差 ≤ ${p.params.default_duration_ratio_dev}</div>`;
}

function renderTargetSelect(p) {
  const sel = $("#rx-target-select");
  sel.innerHTML = "";
  sel.onchange = () => updateRetestHint(sel.value);
  const btn = $("#btn-rx-retest");
  if (!rxState.candidates.length) {
    sel.innerHTML = '<option value="">（没有谱指纹一致的已分析录音）</option>';
    sel.disabled = true;
    btn.disabled = true;
    $("#rx-retest-msg").textContent = "";
    return;
  }
  sel.disabled = p.status === "voided";
  for (const c of rxState.candidates) {
    const opt = document.createElement("option");
    opt.value = c.piece_id;
    const vTxt = c.current_version == null
      ? "无已确认对齐" : `当前 v${c.current_version}`;
    const tag = c.compatible
      ? (c.in_frozen_source
        ? "固化成员，可复测"
        : "同曲新录音，无需重绑即可复测")
      : (RX_COMPAT_LABEL[c.reason] || c.reason);
    opt.textContent = `${c.title}（${vTxt}，${tag}）`;
    if (!c.compatible) opt.style.color = "var(--missing)";
    sel.appendChild(opt);
  }
  updateRetestHint(sel.value);
}

function updateRetestHint(pieceId) {
  const msg = $("#rx-retest-msg");
  const btn = $("#btn-rx-retest");
  const p = rxState.current;
  const chosen = rxState.candidates.find((c) => c.piece_id === pieceId);
  if (!chosen) { msg.textContent = ""; btn.disabled = true; return; }
  btn.disabled = p.status === "voided" || !chosen.compatible;
  if (chosen.compatible) {
    msg.textContent = chosen.in_frozen_source
      ? "该录音为来源固化成员，对齐版本与固化一致，可复测。"
      : "同曲新目标：复测时当场固化它的当前对齐版本与音频哈希，无需重绑。"
        + "相同 request_id 的重复提交返回首次结果。";
    return;
  }
  let text = "该录音暂不可复测："
    + (RX_COMPAT_LABEL[chosen.reason] || chosen.reason);
  if (chosen.member_stale_reasons) {
    text += "（" + chosen.member_stale_reasons
      .map((r) => RX_STALE_REASON_LABEL[r] || r).join("、") + "）";
  }
  msg.textContent = text;
}

// ---------------------------------------------------------------- 谱段规则 + 复测逐项

function renderSegmentRules(p, retest) {
  const box = $("#rx-segments-rules");
  box.innerHTML = "";
  p.segments.forEach((ruleSeg, i) => {
    const rtSeg = retest
      ? retest.result.segments.find(
        (s) => s.score_index_start === ruleSeg.score_index_start
          && s.score_index_end === ruleSeg.score_index_end)
      : null;
    const verdict = rtSeg ? rtSeg.verdict : null;
    const card = document.createElement("div");
    card.className = `rx-seg-result ${verdict ? "verdict-" + verdict : ""}`;
    const head = document.createElement("div");
    head.className = "rx-seg-head";
    head.innerHTML =
      `谱段 ${i + 1}：#${ruleSeg.score_index_start}–#`
      + `${ruleSeg.score_index_end}`
      + (rtSeg && rtSeg.measure_start != null
        ? `（小节 ${rtSeg.measure_start}`
          + (rtSeg.measure_end && rtSeg.measure_end !== rtSeg.measure_start
            ? `–${rtSeg.measure_end}` : "") + "）" : "")
      + ` <span class="rx-sub">规则：${
        ruleSeg.deviation_categories.map(
          (c) => RX_CAT_LABEL[c] || c).join(" / ")}</span>`
      + (verdict
        ? ` <span class="rx-${
          verdict === "undeterminable" ? "und"
          : verdict === "fail" ? "fail" : "pass"}">`
          + `判定：${RX_VERDICT_LABEL[verdict]}</span>`
        : ' <span class="rx-sub">（尚未复测）</span>');
    card.appendChild(head);

    if (rtSeg) {
      card.appendChild(ruleTable(rtSeg));
      if (rtSeg.undeterminable.length) {
        const ul = document.createElement("ul");
        ul.className = "rx-und-list";
        ul.innerHTML = "不可判定谱音：" + rtSeg.undeterminable.map((u) =>
          `#${u.score_index}（${RX_UND_LABEL[u.reason] || u.reason}）`).join("、");
        card.appendChild(ul);
      }
      card.appendChild(evidenceBlock(rtSeg));
    }
    box.appendChild(card);
  });
}

function ruleTable(seg) {
  const table = document.createElement("table");
  table.className = "rx-rule-table";
  table.innerHTML =
    "<thead><tr><th>规则</th><th>实际值</th><th>阈值</th>"
    + "<th>结果</th><th>证据音符</th><th>失败原因</th></tr></thead>";
  const tbody = document.createElement("tbody");
  for (const r of seg.rules) {
    const tr = document.createElement("tr");
    if (!r.passed) tr.className = "fail";
    const actual = formatActual(r.rule, r.actual);
    const threshold = formatThreshold(r.threshold);
    const ev = (r.evidence_score_indexes || []).length
      ? r.evidence_score_indexes.map((i) => `#${i}`).join("、") : "—";
    tr.innerHTML =
      `<td>${escapeHtml(r.label)}</td>`
      + `<td>${escapeHtml(actual)}</td>`
      + `<td>${escapeHtml(threshold)}</td>`
      + `<td class="${r.passed ? "rx-pass" : "rx-fail"}">`
      + `${r.passed ? "满足" : "违反"}</td>`
      + `<td>${ev}</td>`
      + `<td>${r.failure_reason ? escapeHtml(r.failure_reason) : "—"}</td>`;
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

function formatActual(rule, a) {
  if (rule.startsWith("eliminate:")) {
    return `${a.count} 处` + (a.extra_observations
      ? `（含额外音 ${a.extra_observations}）` : "");
  }
  if (rule === "max_onset_error") {
    return `最大 |Δ起音| ${a.max_abs_onset_offset_s ?? "—"}s，`
      + `超阈 ${a.over_count} 处`;
  }
  if (rule === "max_duration_error") {
    return `最大 |时值比−1| ${a.max_abs_ratio_dev ?? "—"}，`
      + `超阈 ${a.over_count} 处`;
  }
  if (rule === "max_abnormal_notes") {
    const by = Object.entries(a.by_category || {})
      .map(([k, v]) => `${RX_CAT_LABEL[k] || k} ${v}`).join("、");
    return `${a.count} 个（${by || "无"}）`;
  }
  if (rule === "forbid_pending") return `待确认 ${a.pending_count} 处`;
  return JSON.stringify(a);
}

function formatThreshold(t) {
  if ("max_count" in t) return `≤ ${t.max_count}`;
  if ("max_abs_onset_offset_s" in t)
    return `|Δ起音| ≤ ${t.max_abs_onset_offset_s}s`;
  if ("max_abs_ratio_dev" in t)
    return `|时值比−1| ≤ ${t.max_abs_ratio_dev}`;
  if ("max_pending" in t) return `待确认 = ${t.max_pending}`;
  return JSON.stringify(t);
}

function evidenceBlock(seg) {
  const wrap = document.createElement("div");
  const btn = htmlEl("button", { className: "rx-ev-toggle" },
    "▸ 证据音符明细（实际起音/偏差/置信度）");
  const table = document.createElement("table");
  table.className = "rx-ev-table hidden";
  table.innerHTML =
    "<thead><tr><th>谱音</th><th>坐标</th><th>音高</th>"
    + "<th>状态/类别</th><th>实际起音(s)</th><th>Δ起音(s)</th>"
    + "<th>时值比</th><th>Δ半音</th><th>置信度</th><th>观测#</th></tr></thead>";
  const tbody = document.createElement("tbody");
  const undSet = new Map(seg.undeterminable.map((u) =>
    [u.score_index, u.reason]));
  for (const e of seg.evidence) {
    const tr = document.createElement("tr");
    if (undSet.has(e.score_index)) tr.className = "und";
    else if (e.categories.length) tr.className = "bad";
    const cats = e.categories.map((c) => RX_CAT_LABEL[c] || c).join("/");
    const coord = `小节 ${e.measure ?? "?"}`
      + (e.occurrence ? `'${e.occurrence + 1}` : "")
      + ` 拍${e.beat_in_measure ?? "?"}`;
    tr.innerHTML =
      `<td>#${e.score_index}</td><td>${coord}</td>`
      + `<td>${midiName(e.midi)}</td>`
      + `<td>${e.status || "—"}${cats ? " / " + cats : ""}`
      + (undSet.has(e.score_index)
        ? ` <span class="rx-und">[${RX_UND_LABEL[undSet.get(e.score_index)]
            || undSet.get(e.score_index)}]</span>` : "") + "</td>"
      + `<td>${e.onset_s ?? "—"}</td>`
      + `<td>${e.onset_offset_s ?? "—"}</td>`
      + `<td>${e.duration_ratio ?? "—"}</td>`
      + `<td>${e.pitch_delta ?? "—"}</td>`
      + `<td>${e.confidence ?? "—"}</td>`
      + `<td>${e.obs_index ?? "—"}</td>`;
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  if (seg.extra_observations && seg.extra_observations.length) {
    const note = document.createElement("div");
    note.className = "rx-und-list";
    note.textContent = "段内额外音（多音）："
      + seg.extra_observations.map((x) =>
        `观测#${x.obs_index}（≈#${x.near_score_index}，${midiName(x.midi)}，`
        + `${x.onset_s}s）`).join("、");
    wrap.appendChild(note);
  }
  btn.onclick = () => {
    table.classList.toggle("hidden");
    btn.textContent = table.classList.contains("hidden")
      ? "▸ 证据音符明细（实际起音/偏差/置信度）"
      : "▾ 收起证据音符明细";
  };
  wrap.appendChild(btn);
  wrap.appendChild(table);
  return wrap;
}

// ---------------------------------------------------------------- 时间线

function renderTimeline(p) {
  const ul = $("#rx-timeline");
  ul.innerHTML = "";
  for (const ev of p.timeline) {
    const li = document.createElement("li");
    li.className = `${ev.type}${ev.overall ? " overall-" + ev.overall : ""}`
      + (ev.retest_id && ev.retest_id === rxState.selectedRetest
        ? " active" : "");
    li.innerHTML = `<span class="rx-time">${ev.at}</span>`
      + escapeHtml(ev.text);
    if (ev.retest_id) {
      li.style.cursor = "pointer";
      li.title = "点击查看该次复测";
      li.onclick = () => {
        rxState.selectedRetest = ev.retest_id;
        rxState.diffB = ev.retest_id;
        renderRx();
      };
    }
    ul.appendChild(li);
  }
}

// ---------------------------------------------------------------- 复测差异

function renderDiffControls() {
  const box = $("#rx-diff-controls");
  if (rxState.retests.length === 0) {
    box.innerHTML = "尚无复测记录。";
    return;
  }
  const opts = rxState.retests.map((r) =>
    `<option value="${r.id}"${r.id === rxState.diffA ? " selected" : ""}>`
    + `第${r.ord}次 · ${escapeHtml(r.target_title)} · `
    + RX_VERDICT_LABEL[r.overall] + "</option>").join("");
  const optsB = rxState.retests.map((r) =>
    `<option value="${r.id}"${r.id === rxState.diffB ? " selected" : ""}>`
    + `第${r.ord}次 · ${escapeHtml(r.target_title)} · `
    + RX_VERDICT_LABEL[r.overall] + "</option>").join("");
  box.innerHTML = '对比 <select id="rx-diff-a">'
    + '<option value="">（不选 = 无基线）</option>' + opts
    + "</select> → <select id=\"rx-diff-b\">" + optsB + "</select>";
  $("#rx-diff-a").onchange = (e) => {
    rxState.diffA = e.target.value || null; renderDiff(); };
  $("#rx-diff-b").onchange = (e) => {
    rxState.diffB = e.target.value; renderDiff(); };
}

function renderDiff() {
  const box = $("#rx-diff");
  box.innerHTML = "";
  const a = rxState.retests.find((r) => r.id === rxState.diffA) || null;
  const b = rxState.retests.find((r) => r.id === rxState.diffB) || null;
  if (!b) { box.textContent = "选择一次复测查看差异。"; return; }

  const table = document.createElement("table");
  const head = document.createElement("tr");
  head.innerHTML = "<th>谱段 / 规则</th><th>判定变化</th><th>证据谱音变化</th>";
  table.appendChild(head);

  b.result.segments.forEach((segB, i) => {
    const segA = a
      ? a.result.segments.find((s) => s.score_index_start
          === segB.score_index_start
          && s.score_index_end === segB.score_index_end) : null;
    const trSeg = document.createElement("tr");
    trSeg.innerHTML =
      `<td><b>谱段 ${i + 1} #${segB.score_index_start}`
      + `–#${segB.score_index_end}</b></td>`
      + `<td>${verdictArrow(segA ? segA.verdict : null, segB.verdict)}</td>`
      + `<td></td>`;
    table.appendChild(trSeg);

    for (const rb of segB.rules) {
      const ra = segA ? segA.rules.find((x) => x.rule === rb.rule) : null;
      const changed = !ra || ra.passed !== rb.passed
        || JSON.stringify(ra.actual) !== JSON.stringify(rb.actual);
      const evA = new Set(ra ? ra.evidence_score_indexes : []);
      const evB = new Set(rb.evidence_score_indexes);
      const added = [...evB].filter((x) => !evA.has(x));
      const removed = [...evA].filter((x) => !evB.has(x));
      const evTxt = [
        added.length && `新增 ${added.map((x) => "#" + x).join("、")}`,
        removed.length && `消除 ${removed.map((x) => "#" + x).join("、")}`,
      ].filter(Boolean).join("；") || (evB.size ? "证据谱音无变化" : "—");
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>　${escapeHtml(rb.label)}</td>`
        + `<td>${ruleArrow(ra ? ra.passed : null, rb.passed)}</td>`
        + `<td>${changed ? evTxt : "无变化"}</td>`;
      table.appendChild(tr);
    }
    const undA = new Set((segA ? segA.undeterminable : [])
      .map((u) => u.score_index));
    const undB = new Set(segB.undeterminable.map((u) => u.score_index));
    const undAdded = [...undB].filter((x) => !undA.has(x));
    const undGone = [...undA].filter((x) => !undB.has(x));
    if (undAdded.length || undGone.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td>　不可判定谱音</td><td></td><td>"
        + [undAdded.length && `新增 ${undAdded.map((x) => "#" + x).join("、")}`,
           undGone.length && `转为可判 ${undGone.map((x) => "#" + x).join("、")}`]
          .filter(Boolean).join("；") + "</td>";
      table.appendChild(tr);
    }
  });
  box.appendChild(table);
}

function verdictArrow(a, b) {
  const la = a ? RX_VERDICT_LABEL[a] : "无基线";
  const cls = b === "pass" ? "arrow-pass"
    : b === "fail" ? "arrow-fail" : "arrow-und";
  return `${la} → <span class="${cls}">${RX_VERDICT_LABEL[b]}</span>`;
}

function ruleArrow(aPass, bPass) {
  if (aPass === null) {
    return bPass
      ? '<span class="arrow-pass">— → 满足</span>'
      : '<span class="arrow-fail">— → 违反</span>';
  }
  if (aPass === bPass) {
    return bPass
      ? '<span class="arrow-pass">满足（无变化）</span>'
      : '<span class="arrow-fail">违反（无变化）</span>';
  }
  return bPass
    ? '<span class="arrow-pass">违反 → 满足 ✓</span>'
    : '<span class="arrow-fail">满足 → 违反 ✗</span>';
}

// ---------------------------------------------------------------- 复测提交

async function submitRetest() {
  const p = rxState.current;
  const pid = $("#rx-target-select").value;
  if (!pid) return;
  try {
    const data = await api(`/api/prescriptions/${p.id}/retests`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: rid(), target_piece_id: pid }),
    });
    rxState.selectedRetest = data.id;
    rxState.diffA = rxState.retests.length ? rxState.retests.at(-1).id : null;
    await openRx(p.id);
  } catch (err) {
    await rxHandleConflict(err);
  }
}

async function rxHandleConflict(err) {
  let detail = err.message;
  try {
    const body = JSON.parse(detail);
    if (body && body.reason) {
      detail = body.detail;
      if (body.reason === "fingerprint_mismatch")
        detail += "（谱指纹不一致：非同曲或反复展开不同）";
      if (body.reason === "no_confirmed_alignment")
        detail += "（目标尚无已确认对齐版本，请先完成分析并确认对齐）";
      if (body.reason === "source_stale")
        detail += "（处方来源固化成员已过期：请先显式重绑，旧结果保留）";
      if (body.reason === "prescription_voided")
        detail += "（处方已作废）";
    }
  } catch { /* 普通字符串 */ }
  alert("复测被拒绝：" + detail);
  if (rxState.current) {
    await Promise.all([loadCandidates(), openRx(rxState.current.id)]);
  }
}

async function submitRebind() {
  const p = rxState.current;
  const sourceType = p.source_type;
  const sourceId = sourceType === "piece"
    ? p.source_piece_id : p.comparison_id;
  if (sourceType === "comparison" && cmpState.data
      && cmpState.data.id === sourceId && cmpState.data.stale) {
    alert("对比已过期：请先在“多次演奏对比”页点“显式刷新”，"
        + "再回来重绑处方。");
    return;
  }
  if (!confirm("显式重绑将重新固化来源的最新对齐版本（谱指纹必须一致）；"
      + "已有复测结果全部保留。继续？")) return;
  try {
    await api(`/api/prescriptions/${p.id}/rebind`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_type: sourceType, source_id: sourceId }),
    });
    await openRx(p.id);
  } catch (err) {
    alert("重绑失败：" + err.message);
  }
}

async function submitVoid() {
  const p = rxState.current;
  if (!confirm("作废后处方进入终态，不能再复测或重绑（历史结果保留）。继续？"))
    return;
  await api(`/api/prescriptions/${p.id}/void`, { method: "POST" })
    .catch(handleErr);
  await openRx(p.id);
}

// ---------------------------------------------------------------- 新建构造器

async function openRxBuilder(sourceType, sourceId) {
  $("#rx-empty").classList.add("hidden");
  $("#rx-detail").classList.add("hidden");
  $("#rx-builder").classList.remove("hidden");

  if (!sourceId) {
    // 弹选择器
    sourceId = await pickSource(sourceType);
    if (!sourceId) { closeRxBuilder(); return; }
  }
  let notes, sourceLabel;
  try {
    if (sourceType === "piece") {
      const bundle = await api(`/api/pieces/${sourceId}/bundle`);
      if (bundle.piece.status !== "analyzed")
        throw new Error("该录音尚未完成分析");
      notes = bundle.score_notes.map(noteClassOfBundle);
      sourceLabel = `来源（已分析乐曲）：${bundle.piece.title}`;
    } else {
      const cmp = await api(`/api/comparisons/${sourceId}`);
      if (cmp.stale)
        throw new Error("对比已过期，请先在对比页“显式刷新”再开处方");
      // 用基线快照谱音（含当前判定色）
      notes = cmp.matrix.cells.map(noteClassOfCell);
      const base = cmp.members.find((m) => m.is_baseline);
      sourceLabel = `来源（未过期对比，按基线《${base.title}》谱面）：${cmp.title}`;
    }
  } catch (err) {
    alert("无法载入来源：" + err.message);
    closeRxBuilder();
    return;
  }

  rxState.builder = {
    source_type: sourceType, source_id: sourceId,
    title: "", segments: [newSegment(notes)], notes, editing: 0,
  };
  $("#rx-title").value = "";
  $("#rx-builder-source").textContent = sourceLabel;
  renderBuilder();
}

async function pickSource(sourceType) {
  if (sourceType === "piece") {
    const pieces = (await api("/api/pieces"))
      .filter((p) => p.status === "analyzed");
    if (!pieces.length) { alert("还没有已完成分析的录音"); return null; }
    const title = prompt("输入要开处方的乐曲标题（精确匹配）：\n"
      + pieces.map((p) => p.title).join("\n"));
    const hit = pieces.find((p) => p.title === title);
    return hit ? hit.id : null;
  }
  const cmps = await api("/api/comparisons");
  if (!cmps.length) { alert("还没有多次演奏对比"); return null; }
  const title = prompt("输入未过期对比的标题（精确匹配）：\n"
    + cmps.map((c) => `${c.title}${c.stale ? "（过期）" : ""}`).join("\n"));
  const hit = cmps.find((c) => c.title === title && !c.stale);
  if (cmps.some((c) => c.title === title) && !hit) {
    alert("该对比已过期，请先“显式刷新”。");
    return null;
  }
  return hit ? hit.id : null;
}

function newSegment(notes) {
  const max = notes.length ? notes[notes.length - 1].index : 0;
  return {
    score_index_start: 0,
    score_index_end: Math.min(3, max),
    deviation_categories: ["wrong_pitch"],
    max_onset_error_s: RX_DEFAULT_MAX_ONSET,
    max_duration_ratio_dev: RX_DEFAULT_DUR_DEV,
    max_abnormal_notes: 0,
    forbid_pending: false,
  };
}

function noteClassOfBundle(n) {
  // 谱面构造器只需要坐标 + 下标；类别色由点击提交后的复测呈现，
  // 这里统一给中性色（bundle 不直接带逐音判定标签，避免与快照结构耦合）。
  return { index: n.index, measure: n.measure,
           beat_in_measure: n.beat_in_measure, occurrence: n.occurrence,
           midi: n.midi, cats: [] };
}

function noteClassOfCell(c) {
  const cats = [];
  const side = c.baseline;
  if (side.state === "abnormal") {
    if (side.status === "wrong") cats.push("wrong_pitch");
    if (side.status === "missing") cats.push("missing");
    (side.tags || []).forEach((t) => {
      if (t === "onset_offset" || t === "duration_deviation") cats.push(t);
    });
  }
  if (side.state === "pending_confirm") cats.push("pending");
  if (side.state === "excluded") cats.push("excluded");
  return { index: c.score_index, measure: c.measure,
           beat_in_measure: c.beat_in_measure,
           occurrence: c.occurrence || 0, midi: c.midi, cats };
}

function closeRxBuilder(keepDetail) {
  $("#rx-builder").classList.add("hidden");
  rxState.builder = null;
  if (!keepDetail) $("#rx-empty").classList.remove("hidden");
}

function renderBuilder() {
  const b = rxState.builder;
  if (!b) return;
  const editor = $("#rx-seg-editor");
  editor.innerHTML = "";
  b.segments.forEach((seg, i) => editor.appendChild(segCard(seg, i)));
  if (b.segments.length < 20) {
    editor.appendChild(htmlEl("button", {
      className: "rx-seg-add",
      onclick: () => {
        b.segments.push(newSegment(b.notes));
        b.editing = b.segments.length - 1;
        renderBuilder();
      },
    }, "＋ 增加一个连续谱段"));
  }
  renderNoteStrip();
}

function segCard(seg, i) {
  const card = document.createElement("div");
  card.className = "rx-seg-card";
  const head = document.createElement("div");
  head.className = "rx-seg-head";
  head.innerHTML = `谱段 ${i + 1}`
    + (i === rxState.builder.editing
      ? ' <span class="rx-sub">（正在用下方谱面条设置起/终点）</span>' : "");
  card.appendChild(head);

  const grid = document.createElement("div");
  grid.className = "rx-seg-grid";

  const startN = numberField("起点谱音#", seg.score_index_start, (v) => {
    seg.score_index_start = v; renderNoteStrip();
  });
  const endN = numberField("终点谱音#", seg.score_index_end, (v) => {
    seg.score_index_end = v; renderNoteStrip();
  });
  grid.appendChild(startN);
  grid.appendChild(endN);
  [startN, endN].forEach((wrap) => {
    wrap.querySelector("input").onfocus = () => { rxState.builder.editing = i; };
  });

  const onset = numberField("最大起音误差(s)", seg.max_onset_error_s,
    (v) => { seg.max_onset_error_s = v; }, 0.01, 0, 10);
  onset.querySelector("input").step = "0.01";
  grid.appendChild(onset);
  const dur = numberField("最大时值比例误差", seg.max_duration_ratio_dev,
    (v) => { seg.max_duration_ratio_dev = v; }, 0.01, 0, 1);
  dur.querySelector("input").step = "0.01";
  grid.appendChild(dur);
  grid.appendChild(numberField("最多异常音数", seg.max_abnormal_notes,
    (v) => { seg.max_abnormal_notes = v; }, 1, 0, 10000));

  // 类别
  const catBox = document.createElement("div");
  catBox.className = "rx-cats";
  for (const [key, label] of RX_CATEGORIES) {
    const lab = document.createElement("label");
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = seg.deviation_categories.includes(key);
    chk.onchange = () => {
      const set = new Set(seg.deviation_categories);
      chk.checked ? set.add(key) : set.delete(key);
      seg.deviation_categories = RX_CATEGORIES
        .map(([k]) => k).filter((k) => set.has(k));
    };
    lab.append(chk, document.createTextNode(label));
    catBox.appendChild(lab);
  }
  grid.appendChild(catBox);

  // 禁止待确认
  const forbidLab = document.createElement("label");
  forbidLab.style.gridColumn = "1/-1";
  const forbid = document.createElement("input");
  forbid.type = "checkbox";
  forbid.checked = seg.forbid_pending;
  forbid.onchange = () => { seg.forbid_pending = forbid.checked; };
  forbidLab.append(forbid, document.createTextNode(
    "禁止待确认（段内存在未处置歧义即判未通过；否则仅判不可判定）"));
  grid.appendChild(forbidLab);

  card.appendChild(grid);

  if (rxState.builder.segments.length > 1) {
    card.appendChild(htmlEl("button", {
      onclick: () => {
        rxState.builder.segments.splice(i, 1);
        rxState.builder.editing = 0;
        renderBuilder();
      },
    }, "删除此谱段"));
  }
  card.appendChild(htmlEl("button", {
    style: "margin-left:8px",
    onclick: () => { rxState.builder.editing = i; renderNoteStrip(); },
  }, "用谱面条设置起/终点"));
  return card;
}

function numberField(label, value, onChange, step = 1, min = 0, max) {
  const lab = document.createElement("label");
  lab.textContent = label + " ";
  const inp = document.createElement("input");
  inp.type = "number";
  inp.value = value;
  inp.step = String(step);
  inp.min = String(min);
  if (max != null) inp.max = String(max);
  inp.oninput = () => {
    const v = Number(inp.value);
    if (Number.isFinite(v)) onChange(v);
  };
  lab.appendChild(inp);
  return lab;
}

function renderNoteStrip() {
  const b = rxState.builder;
  const wrap = $("#rx-note-strip-wrap");
  const strip = $("#rx-note-strip");
  if (!b) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  strip.innerHTML = "";
  const editing = b.segments[b.editing] || b.segments[0];
  const inSeg = new Set();
  b.segments.forEach((s) => {
    for (let i = s.score_index_start; i <= s.score_index_end; i++)
      inSeg.add(i);
  });
  b.notes.forEach((n) => {
    const cell = document.createElement("div");
    cell.className = "rx-note-cell"
      + (n.cats.includes("wrong_pitch") ? " cat-wrong_pitch" : "")
      + (n.cats.includes("missing") ? " cat-missing" : "")
      + (n.cats.includes("onset_offset") ? " cat-onset_offset" : "")
      + (n.cats.includes("duration_deviation")
        ? " cat-duration_deviation" : "")
      + (n.cats.includes("pending") ? " cat-pending" : "")
      + (n.cats.includes("excluded") ? " cat-excluded" : "")
      + (inSeg.has(n.index) ? " inseg" : "")
      + (editing && n.index === editing.score_index_start
        ? " editing-start" : "")
      + (editing && n.index === editing.score_index_end
        ? " editing-end" : "");
    cell.innerHTML = `<b>#${n.index}</b>${midiName(n.midi)}`
      + (n.measure != null ? `<br>小${n.measure}` : "");
    cell.title = `#${n.index} 小节 ${n.measure ?? "?"}`
      + (n.occurrence ? `（第 ${n.occurrence + 1} 次）` : "")
      + ` 拍${n.beat_in_measure ?? "?"} — 点击：第 1 下设起点，第 2 下设终点`;
    cell.onclick = () => {
      const seg = b.segments[b.editing];
      if (seg._set !== "end") {
        seg.score_index_start = n.index; seg._set = "end";
      } else {
        seg.score_index_end = n.index; seg._set = "start";
      }
      if (seg.score_index_end < seg.score_index_start) {
        const t = seg.score_index_end;
        seg.score_index_end = seg.score_index_start;
        seg.score_index_start = t;
      }
      renderBuilder();
    };
    strip.appendChild(cell);
  });
}

async function submitRxCreate() {
  const b = rxState.builder;
  if (!b) return;
  const payload = {
    request_id: rid(),
    source_type: b.source_type,
    source_id: b.source_id,
    title: $("#rx-title").value || undefined,
    segments: b.segments.map((s) => ({
      score_index_start: s.score_index_start,
      score_index_end: s.score_index_end,
      deviation_categories: s.deviation_categories,
      max_onset_error_s: s.max_onset_error_s,
      max_duration_ratio_dev: s.max_duration_ratio_dev,
      max_abnormal_notes: s.max_abnormal_notes,
      forbid_pending: s.forbid_pending,
    })),
  };
  try {
    const created = await api("/api/prescriptions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await openRx(created.id);
  } catch (err) {
    alert("创建处方失败：" + err.message);
  }
}

/* 最佳片段合成轨 —— 原生 JS（与 app.js / compare.js 同页加载）
 *
 * 数据流：
 *   GET  /api/comparisons/{id}/synthesis          读取任务（无任务 -> null）
 *   POST /api/comparisons/{id}/synthesis          新建（先规划，对比过期会 409）
 *   PUT  /api/synthesis/{tid}/overrides           钉住 / 禁用（重新规划，失效旧 WAV）
 *   POST /api/synthesis/{tid}/rebuild             显式重建：固化 WAV + 清单
 *   GET  /api/synthesis/{tid}/wav?download=1      试听 / 下载
 *   GET  /api/synthesis/{tid}/manifest            确定性清单 JSON
 *   DELETE /api/synthesis/{tid}
 */
"use strict";

const synthState = {
  task: null,
  busy: false,
};

const BLOCKED_REASON_LABEL = {
  no_eligible_source: "无合格来源",
  all_disabled: "所有来源均已禁用",
  pinned_unavailable: "钉住的来源在该谱位不合格",
  pinned_but_disabled: "钉住的来源已被禁用",
  pinned_unknown_piece: "钉住的录音不在对比中",
};
const CAND_REASON_LABEL = {
  ok: "合格",
  pending_unresolved: "待确认未处置",
  low_confidence: "置信度不足",
  excluded: "用户已排除",
  no_aligned_onset: "无已对齐起音",
  "not_confirmed:delete": "漏音",
  "not_confirmed:insert": "多音",
  no_snapshot_note: "快照缺该谱音",
};

// ------------------------------------------------------------ 由 compare.js 调用

async function synthOnComparisonOpened() {
  $("#synth-panel").classList.remove("hidden");
  try {
    synthState.task = await api(
      `/api/comparisons/${cmpState.data.id}/synthesis`);
  } catch (err) {
    synthState.task = null;
  }
  renderSynth();
}

function synthOnComparisonClosed() {
  synthState.task = null;
  const a = $("#synth-audio");
  if (a) { a.pause(); a.removeAttribute("src"); a.load(); }
}

// ------------------------------------------------------------ 操作

async function synthAction(promiseFactory, okMsg) {
  if (synthState.busy) return;
  synthState.busy = true;
  try {
    synthState.task = await promiseFactory();
    renderSynth();
    if (okMsg) console.log(okMsg);
  } catch (err) {
    await synthHandleError(err);
  } finally {
    synthState.busy = false;
  }
}

async function synthHandleError(err) {
  // api() 已把响应体的 detail 字段放进 err.message（可能是 JSON 文本）
  let detail = err.message;
  let blocked = null;
  try {
    const body = JSON.parse(detail);
    if (body && typeof body === "object") {
      detail = body.detail || detail;
      blocked = body.blocked || null;
    }
  } catch { /* detail 就是普通字符串 */ }
  if (blocked) detail += `（${blocked.length} 个谱位无合格来源，见红色清单）`;
  alert(detail);
  // 过期 / 阻塞后刷新面板以同步最新规划
  if (synthState.task) {
    try {
      synthState.task = await api(
        `/api/synthesis/${synthState.task.id}`);
      renderSynth();
    } catch { /* ignore */ }
  }
}

async function createSynthTask() {
  return synthAction(() => api(
    `/api/comparisons/${cmpState.data.id}/synthesis`,
    { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}) }), "created");
}

async function rebuildSynth() {
  const t = synthState.task;
  return synthAction(() => api(`/api/synthesis/${t.id}/rebuild`,
                               { method: "POST" }), "rebuilt");
}

async function deleteSynthTask() {
  const t = synthState.task;
  if (!confirm("删除合成轨任务（含已固化 WAV）？")) return;
  await api(`/api/synthesis/${t.id}`, { method: "DELETE" })
    .catch(handleErr);
  synthState.task = null;
  renderSynth();
}

async function putOverrides(next) {
  const t = synthState.task;
  return synthAction(() => api(`/api/synthesis/${t.id}/overrides`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(next),
  }));
}

function currentOverrides() {
  const o = (synthState.task && synthState.task.overrides) || {};
  return { pins: { ...(o.pins || {}) }, disabled: [...(o.disabled || [])] };
}

function togglePin(scoreIndex, pieceId) {
  const ov = currentOverrides();
  const key = String(scoreIndex);
  if (ov.pins[key] === pieceId) delete ov.pins[key];
  else ov.pins[key] = pieceId;
  putOverrides(ov);
}

function toggleDisable(pieceId) {
  const ov = currentOverrides();
  const k = ov.disabled.indexOf(pieceId);
  if (k >= 0) ov.disabled.splice(k, 1);
  else ov.disabled.push(pieceId);
  putOverrides(ov);
}

// ------------------------------------------------------------ 渲染

function renderSynth() {
  const panel = $("#synth-panel");
  if (!cmpState.data) { panel.classList.add("hidden"); return; }

  const t = synthState.task;
  renderSynthActions(t);
  renderSynthStatus(t);

  const blockedBox = $("#synth-blocked");
  if (!t) {
    $("#synth-cost").innerHTML = "";
    $("#synth-weights").innerHTML = "";
    $("#synth-segments").innerHTML = "";
    $("#synth-cuts").innerHTML = "";
    $("#synth-table").innerHTML = "";
    blockedBox.classList.add("hidden");
    $("#synth-player").classList.add("hidden");
    $("#synth-stale-banner").classList.add("hidden");
    return;
  }

  const plan = t.plan;
  renderStaleBanner(t);
  renderCost(t, plan);
  renderWeights(t);
  renderBlocked(t, plan);
  renderSegments(plan);
  renderCuts(plan);
  renderTable(t, plan);
  renderPlayer(t);
}

function renderSynthActions(t) {
  const box = $("#synth-actions");
  box.innerHTML = "";
  if (!t) {
    box.appendChild(htmlEl("button", {
      onclick: createSynthTask,
    }, "＋ 创建最佳片段合成轨（按当前只读快照规划）"));
    return;
  }
  const planBlocked = t.plan_status === "blocked"
    || (t.plan.blocked && t.plan.blocked.length > 0);
  const cannotRebuild = planBlocked || t.stale;
  const rebuildBtn = htmlEl("button", {
    onclick: rebuildSynth,
    disabled: cannotRebuild,
    title: planBlocked ? "存在无合格来源的谱位，解除前无法重建（旧固化 WAV 仍保留）"
      : (t.stale ? "来源已变化：请先在对比页“显式刷新”，再显式重建"
        : (t.rebuild_required
          ? "按当前钉住/禁用显式重建，原子替换旧 WAV + 清单"
          : "显式重建并固化 WAV + 确定性清单")),
  }, planBlocked ? "⛔ 有谱位无合格来源"
    : (t.stale ? "⏳ 已过期，请先刷新对比" : "⚙ 显式重建并导出 WAV"));
  box.appendChild(rebuildBtn);
  box.appendChild(htmlEl("button", { onclick: deleteSynthTask },
                         "删除合成轨"));
  const ov = t.overrides || {};
  const nPin = Object.keys(ov.pins || {}).length;
  const nDis = (ov.disabled || []).length;
  if (nPin || nDis) {
    box.appendChild(htmlEl("button", {
      onclick: () => putOverrides({ pins: {}, disabled: [] }),
    }, `清除全部钉住/禁用（${nPin} 钉 · ${nDis} 禁）`));
  }
}

function renderSynthStatus(t) {
  const elx = $("#synth-status");
  if (!t) {
    elx.textContent = "从当前对比的只读快照（须未过期）规划一条可试听、"
      + "可下载的合成 WAV。";
    return;
  }
  const label = { draft: "已规划·未固化", built: "已固化" }[t.status]
    || t.status;
  const planLabel = t.plan_status === "blocked"
    ? '<span class="synth-pill blocked">规划阻塞</span>' : "";
  const rebuildTag = t.rebuild_required
    ? '<span class="synth-pill draft">待重建</span>' : "";
  const preservedTag = t.build_preserved
    ? '<span class="synth-pill built">旧 WAV 已保留</span>' : "";
  elx.innerHTML =
    `<span class="synth-pill ${t.status}">${label}</span>`
    + planLabel + rebuildTag + preservedTag
    + ` 创建 ${t.created_at || ""}`
    + (t.built_at ? ` · 固化于 ${t.built_at}` : "")
    + (t.wav_sha256
      ? ` · WAV sha256 <code>${t.wav_sha256.slice(0, 12)}…</code>` : "");
}

function renderStaleBanner(t) {
  const b = $("#synth-stale-banner");
  if (!t.stale) { b.classList.add("hidden"); return; }
  const names = t.stale_members.map((s) => {
    const m = cmpState.data.members.find((x) => x.piece_id === s.piece_id);
    return (m ? m.title : s.piece_id)
      + "（" + s.reasons.map(staleReasonLabel).join("、") + "）";
  }).join("；");
  b.textContent = "⚠ 来源已变化，合成轨过期：" + names
    + "。已固化 WAV 保持不变；请先在对比页“显式刷新”，再点“显式重建”。";
  b.classList.remove("hidden");
}

function renderCost(t, plan) {
  const c = plan.cost;
  $("#synth-cost").innerHTML =
    `<span>总代价 <b>${c.total}</b></span>`
    + `<span>异常谱音 ×${c.anomaly_count} → ${c.terms.anomaly}</span>`
    + `<span>切换 ×${c.switch_count} → ${c.terms.switch}</span>`
    + `<span>响度差 Σ${c.loudness_delta_db_sum} dB → ${c.terms.loudness}</span>`
    + `<span>速度差 Σ${c.tempo_ratio_delta_sum} → ${c.terms.tempo}</span>`;
}

function renderWeights(t) {
  const w = t.params.weights;
  $("#synth-weights").innerHTML =
    "公开权重："
    + `异常 <code>${w.anomaly}</code>`
    + ` · 切换 <code>${w.switch}</code>`
    + ` · 响度/dB <code>${w.loudness_per_db}</code>`
    + ` · 速度比差 <code>${w.tempo_ratio}</code>`
    + ` ；同分排序：${t.params.tie_break}`
    + ` ；交叉淡化 ${t.params.crossfade_s}s · 置信阈值 ≥ ${
      t.params.min_confidence}`;
}

function renderBlocked(t, plan) {
  const box = $("#synth-blocked");
  if (!plan.blocked.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.innerHTML = "<b>阻止导出：以下谱位无合格来源（已逐位给出原因）</b>"
    + (t.build_preserved
      ? "<div class=\"synth-note\">当前规划已阻塞，但<b>上次固化的 WAV 与清单"
        + "仍然保留</b>，可继续试听 / 下载；解除钉住/禁用后需点"
        + "“显式重建”才会原子替换。</div>"
      : "<div class=\"synth-note\">解除阻塞后点“显式重建”即可导出。</div>")
    + "<ul>" + plan.blocked.map((b) => {
      const reason = BLOCKED_REASON_LABEL[b.reason] || b.reason;
      const pinTxt = b.pinned_piece
        ? `（钉住：${pieceTitle(b.pinned_piece)}）` : "";
      return `<li>谱音 #${b.score_index} · 小节 ${b.measure ?? "?"}`
        + ` 拍 ${b.beat_in_measure ?? "?"} ${midiName(b.midi)}`
        + ` — <b>${reason}</b>${pinTxt}</li>`;
    }).join("") + "</ul>";
}

function pieceTitle(pid) {
  const m = cmpState.data.members.find((x) => x.piece_id === pid);
  return m ? m.title : pid;
}

function renderSegments(plan) {
  const colors = ["var(--correct)", "var(--onset)", "var(--extra)",
                  "var(--duration)", "var(--accent)"];
  const ordColor = new Map(plan.pieces.map((p, i) =>
    [p.piece_id, colors[i % colors.length]]));
  const box = $("#synth-segments");
  box.innerHTML = "";
  plan.segments.forEach((seg, i) => {
    const d = document.createElement("span");
    d.className = "synth-seg";
    d.style.borderColor = ordColor.get(seg.piece_id);
    d.innerHTML =
      (i > 0 ? '<span class="cut-mark">✂</span> ' : "")
      + `<b>${escapeHtml(seg.piece_title)}</b>`
      + ` #${seg.score_index_start}–${seg.score_index_end}`
      + `（源 ${seg.source_time_start_s}–${seg.source_time_end_s}s`
      + ` → 输出 ${seg.output_time_start_s}–${seg.output_time_end_s}s）`;
    box.appendChild(d);
  });
}

function renderCuts(plan) {
  const box = $("#synth-cuts");
  if (!plan.cuts.length) { box.innerHTML = "无切点（单一来源）。"; return; }
  box.innerHTML = "切点（全部落在已对齐起音边界，短交叉淡化）："
    + "<table><thead><tr><th>谱位</th><th>出</th><th>入</th>"
    + "<th>输出时刻</th><th>响度差(dB)</th><th>速度比(左→右)</th>"
    + "<th>切点代价</th></tr></thead><tbody>"
    + plan.cuts.map((c) =>
      `<tr><td>#${c.score_index}（小节 ${c.measure ?? "?"}）</td>`
      + `<td>${escapeHtml(pieceTitle(c.out_piece))}</td>`
      + `<td>${escapeHtml(pieceTitle(c.in_piece))}</td>`
      + `<td>${c.output_time_s}s</td>`
      + `<td>${c.loudness_delta_db}</td>`
      + `<td>${c.tempo_ratio_left} → ${c.tempo_ratio_right}`
      + `（Δ${c.tempo_ratio_delta}）</td>`
      + `<td>${c.cost}</td></tr>`).join("")
    + "</tbody></table>";
}

function renderTable(t, plan) {
  const table = $("#synth-table");
  table.innerHTML = "";
  const pieces = plan.pieces;
  const pins = t.overrides.pins || {};
  const disabled = new Set(t.overrides.disabled || []);
  const selMap = new Map(
    plan.selection.filter(Boolean).map((s) => [s.score_index, s.piece_id]));
  const blockedSet = new Set(plan.blocked.map((b) => b.score_index));
  const filter = cmpState.filterMeasure;

  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  hr.appendChild(th("谱位 / 谱音"));
  for (const p of pieces) {
    const thEl = th("", "src-head");
    const dis = disabled.has(p.piece_id);
    thEl.innerHTML = `<abbr title="点击表头禁用/启用该录音">`
      + `${p.is_baseline ? "★ " : ""}${escapeHtml(p.title)}`
      + (dis ? "（已禁用）" : "") + `</abbr>`;
    thEl.style.cursor = "pointer";
    thEl.onclick = () => toggleDisable(p.piece_id);
    hr.appendChild(thEl);
  }
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const pos of plan.positions) {
    if (filter && `${pos.measure ?? ""}:${pos.occurrence || 0}` !== filter)
      continue;
    const tr = document.createElement("tr");
    if (blockedSet.has(pos.score_index)) tr.className = "blocked-row";
    const td = document.createElement("td");
    td.innerHTML =
      `<span class="m-num">#${pos.score_index} 小节 ${pos.measure ?? "?"}`
      + (pos.occurrence ? `'${pos.occurrence + 1}` : "")
      + ` 拍${pos.beat_in_measure ?? "?"}</span>`
      + ` <span class="midi-num">${midiName(pos.midi)}</span>`;
    tr.appendChild(td);
    for (const p of pieces) {
      const c = pos.candidates[p.piece_id];
      const isSel = selMap.get(pos.score_index) === p.piece_id;
      const isPin = String(pins[pos.score_index]) === p.piece_id;
      const isDis = disabled.has(p.piece_id);
      const cell = document.createElement("td");
      cell.className = "synth-cell"
        + (isSel ? " sel" : "")
        + (c.abnormal ? " abnormal" : "")
        + (!c.eligible ? " ineligible" : "")
        + (isPin ? " pin" : "")
        + (isDis ? " dis" : "");
      let txt = "";
      if (c.eligible) txt = c.abnormal ? abnormalCellText(c) : "✓";
      else txt = "·";
      cell.textContent = txt + (isPin ?" 📌" : "");
      const reason = c.eligible
        ? `${c.abnormal ? "异常但可用：" : "合格 · "}置信度 ${c.confidence}`
          + ` · 起音 ${c.onset_s}s · 响度 ${c.loudness_db}dB`
          + (isSel ? " · 当前选用" : "")
        : CAND_REASON_LABEL[c.reason] || c.reason;
      cell.title = `${p.title}：${reason}`
        + (isDis ? "（该录音已被禁用）" : "")
        + (c.abnormal ? `\n标签：${c.tags.join("/") || c.status}` : "");
      if (c.eligible && !isDis)
        cell.onclick = () => togglePin(pos.score_index, p.piece_id);
      tr.appendChild(cell);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
}

function abnormalCellText(c) {
  const parts = [];
  if (c.status === "wrong") parts.push("错");
  if (c.status === "missing") parts.push("漏");
  for (const tag of c.tags || []) {
    if (ABNORMAL_TAG_LABEL[tag]) parts.push(ABNORMAL_TAG_LABEL[tag]);
  }
  return parts.join("/") || "异";
}

function th(text, cls) {
  const t = document.createElement("th");
  if (cls) t.className = cls;
  t.textContent = text;
  return t;
}

function renderPlayer(t) {
  const box = $("#synth-player");
  if (t.status !== "built" || !t.wav_url) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const note = $("#synth-player-note");
  if (t.plan_status === "blocked") {
    note.textContent = "以下为上次固化的 WAV：当前规划被阻塞，重建返回 422；"
      + "旧文件、清单与下载能力均未被清空或删除。";
    note.classList.remove("hidden");
  } else if (t.rebuild_required) {
    note.textContent = "钉住/禁用已变更：以下仍是旧固化 WAV，"
      + "点“显式重建”后原子替换。";
    note.classList.remove("hidden");
  } else {
    note.textContent = "";
    note.classList.add("hidden");
  }
  const audio = $("#synth-audio");
  // 用固化时间作 cache-buster：显式重建后 URL 变化，避免拿到旧 WAV
  const url = `${t.wav_url}?v=${encodeURIComponent(t.built_at || "")}`;
  if (audio.dataset.url !== url) {
    audio.dataset.url = url;
    audio.src = url;
  }
  $("#synth-download").href =
    `${t.wav_url}?download=1&v=${encodeURIComponent(t.built_at || "")}`;
  $("#synth-manifest").href = `/api/synthesis/${t.id}/manifest`;
}

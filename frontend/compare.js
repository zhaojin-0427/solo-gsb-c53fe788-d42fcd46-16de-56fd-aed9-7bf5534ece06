/* 多次演奏对比 —— 原生 JS 模块（与 app.js 同页加载，无构建步骤）
 *
 * 数据流：
 *   GET  /api/comparisons            列表（带 stale 标记）
 *   POST /api/comparisons            创建（指纹不同会被后端 409 拒绝）
 *   GET  /api/comparisons/{id}       只读快照 + 版本矩阵 + 区段/回退
 *   POST /api/comparisons/{id}/refresh  用户显式刷新（不静默改写）
 *   DELETE ...
 */
"use strict";

const VERDICT_LABEL = {
  resolved: "已解决", new: "新出现", persistent: "持续",
  incomparable: "不可比",
};
const SIDE_STATE_LABEL = {
  normal: "正常", abnormal: "异常", pending_confirm: "待确认",
  excluded: "已排除", low_confidence: "低置信",
};
const ABNORMAL_TAG_LABEL = {
  wrong_pitch: "错音", onset_offset: "起音偏移",
  duration_deviation: "时值偏差",
  wrong: "错音", missing: "漏音",
};

const cmpState = {
  view: "review",            // review | compare
  data: null,                // 当前对比完整数据
  pieces: [],                // 可选录音列表（同 analyzed）
  selected: new Set(),       // builder 勾选
  baseline: null,            // builder 基线
  filterMeasure: "",         // 小节筛选（"measure:occurrence"）
  audios: new Map(),         // piece_id -> Audio
  playing: false,
  raf: null,
};

// ---------------------------------------------------------------- 视图切换

$("#tab-review").addEventListener("click", () => switchView("review"));
$("#tab-compare").addEventListener("click", () => switchView("compare"));
$("#tab-rx").addEventListener("click", () => switchView("rx"));
$("#tab-practice").addEventListener("click", () => switchView("practice"));
$("#tab-revision").addEventListener("click", () => switchView("revision"));

async function switchView(name) {
  cmpState.view = name;
  $("#tab-review").classList.toggle("active", name === "review");
  $("#tab-compare").classList.toggle("active", name === "compare");
  $("#tab-rx").classList.toggle("active", name === "rx");
  $("#tab-practice").classList.toggle("active", name === "practice");
  $("#tab-revision").classList.toggle("active", name === "revision");
  $("#review").classList.toggle("hidden", name !== "review"
    || state.pieceId == null);
  $("#anchors-panel").classList.toggle("hidden",
    name !== "review" || state.pieceId == null);
  $("#pending-panel").classList.toggle("hidden",
    name !== "review" || state.pieceId == null);
  $("#upload-section").classList.toggle("hidden", name !== "review");
  $("#list-section").classList.toggle("hidden", name !== "review");
  $("#compare-side-panel").classList.toggle("hidden", name !== "compare");
  $("#compare-view").classList.toggle("hidden", name !== "compare");
  $("#rx-side-panel").classList.toggle("hidden", name !== "rx");
  $("#rx-view").classList.toggle("hidden", name !== "rx");
  $("#practice-side-panel").classList.toggle("hidden", name !== "practice");
  $("#practice-view").classList.toggle("hidden", name !== "practice");
  $("#revision-side-panel").classList.toggle("hidden", name !== "revision");
  $("#revision-view").classList.toggle("hidden", name !== "revision");
  if (name === "compare") {
    await Promise.all([loadComparisonList(), loadPiecesForBuilder()]);
    if (!cmpState.data) $("#compare-empty").classList.remove("hidden");
  } else {
    pauseComparePlayback();
  }
  if (name === "rx" && window.rxOnViewEnter) await rxOnViewEnter();
  if (name === "practice" && window.practiceOnViewEnter)
    await practiceOnViewEnter();
  if (name === "revision" && window.revisionOnViewEnter)
    await revisionOnViewEnter();
}

// ---------------------------------------------------------------- 对比列表

async function loadComparisonList() {
  const list = await api("/api/comparisons");
  const ul = $("#comparison-list");
  ul.innerHTML = "";
  if (!list.length) {
    ul.appendChild(htmlEl("li", { className: "muted-li" }, "（暂无对比）"));
    return;
  }
  for (const c of list) {
    const li = htmlEl("li", {
      className: cmpState.data && c.id === cmpState.data.id ? "active" : "",
      onclick: () => openComparison(c.id),
    });
    const box = htmlEl("div", { className: "cmp-name-box" });
    box.appendChild(htmlEl("span", { className: "cmp-name" }, c.title));
    box.appendChild(htmlEl("span", { className: "cmp-sub" },
      `${c.member_count} 次录音`));
    li.appendChild(box);
    if (c.stale) {
      const b = htmlEl("span", {
        className: "stale-dot", title: "来源锚点或对齐版本已变化，"
          + "结论已过期，需显式刷新",
      }, "过期");
      li.appendChild(b);
    }
    ul.appendChild(li);
  }
}

async function loadPiecesForBuilder() {
  cmpState.pieces = await api("/api/pieces");
}

// ---------------------------------------------------------------- 新建

$("#btn-new-compare").addEventListener("click", openBuilder);
$("#btn-create-cancel").addEventListener("click", () => {
  $("#compare-builder").classList.add("hidden");
  if (cmpState.data) $("#compare-detail").classList.remove("hidden");
  else $("#compare-empty").classList.remove("hidden");
});
$("#btn-create-confirm").addEventListener("click", submitCreate);

async function openBuilder() {
  await loadPiecesForBuilder();
  cmpState.selected = new Set();
  cmpState.baseline = null;
  $("#compare-empty").classList.add("hidden");
  $("#compare-detail").classList.add("hidden");
  $("#compare-builder").classList.remove("hidden");
  renderBuilder();
}

function renderBuilder() {
  const ul = $("#builder-list");
  ul.innerHTML = "";
  const ready = cmpState.pieces.filter((p) => p.status === "analyzed");
  if (!ready.length) {
    ul.appendChild(htmlEl("li", {}, "（还没有已完成分析的录音，请先上传）"));
    return;
  }
  for (const p of ready) {
    const li = htmlEl("li");
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = cmpState.selected.has(p.id);
    chk.onchange = () => {
      if (chk.checked) {
        if (cmpState.selected.size >= 6) {
          chk.checked = false;
          alert("一次对比最多选择 6 次录音");
          return;
        }
        cmpState.selected.add(p.id);
      } else {
        cmpState.selected.delete(p.id);
        if (cmpState.baseline === p.id) cmpState.baseline = null;
      }
      renderBuilder();
    };
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "baseline-choice";
    radio.checked = cmpState.baseline === p.id;
    radio.disabled = !cmpState.selected.has(p.id);
    radio.title = "指定为基线";
    radio.onchange = () => { cmpState.baseline = p.id; };
    li.append(chk, radio,
      htmlEl("span", { className: "builder-title-text" }, p.title),
      htmlEl("span", { className: "cmp-sub" }, p.id));
    ul.appendChild(li);
  }
  const n = cmpState.selected.size;
  $("#builder-hint").textContent =
    `已选 ${n} 次（需 2–6 次同曲录音）；单选圆点指定基线。`
    + (n >= 2 && !cmpState.baseline ? "　请指定基线。" : "");
}

async function submitCreate() {
  const ids = [...cmpState.selected];
  if (ids.length < 2 || ids.length > 6) {
    alert("请选择 2–6 次录音");
    return;
  }
  if (!cmpState.baseline) {
    alert("请指定一次录音作为基线");
    return;
  }
  try {
    const created = await api("/api/comparisons", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        piece_ids: ids, baseline_piece: cmpState.baseline,
      }),
    });
    $("#compare-builder").classList.add("hidden");
    await openComparison(created.id);
  } catch (err) {
    alert("无法创建对比：" + err.message);
  }
}

// ---------------------------------------------------------------- 读取/刷新/删除

async function openComparison(id) {
  try {
    cmpState.data = await api(`/api/comparisons/${id}`);
  } catch (err) {
    alert("加载对比失败：" + err.message);
    return;
  }
  cmpState.filterMeasure = "";
  $("#compare-empty").classList.add("hidden");
  $("#compare-builder").classList.add("hidden");
  $("#compare-detail").classList.remove("hidden");
  prepareAudios();
  renderComparison();
  loadComparisonList();
  if (window.synthOnComparisonOpened) synthOnComparisonOpened();
}

$("#btn-compare-refresh").addEventListener("click", async () => {
  if (!cmpState.data) return;
  if (!confirm("显式刷新将以各录音当前已确认的对齐版本重新固化快照，"
      + "此前的对比结论会被替换。继续？")) return;
  try {
    cmpState.data = await api(
      `/api/comparisons/${cmpState.data.id}/refresh`,
      { method: "POST" });
    prepareAudios();
    renderComparison();
    loadComparisonList();
    if (window.synthOnComparisonOpened) await synthOnComparisonOpened();
  } catch (err) {
    alert("刷新失败：" + err.message);
  }
});

$("#btn-compare-delete").addEventListener("click", async () => {
  if (!cmpState.data) return;
  if (!confirm("删除该对比？只读快照与矩阵将一并移除（不影响各次复盘）。"))
    return;
  await api(`/api/comparisons/${cmpState.data.id}`,
            { method: "DELETE" }).catch(handleErr);
  cmpState.data = null;
  pauseComparePlayback();
  $("#compare-detail").classList.add("hidden");
  $("#synth-panel").classList.add("hidden");
  if (window.synthOnComparisonClosed) synthOnComparisonClosed();
  $("#compare-empty").classList.remove("hidden");
  loadComparisonList();
});

// ---------------------------------------------------------------- 渲染

function memberMap() {
  return new Map(cmpState.data.members.map((m) => [m.piece_id, m]));
}

function prepareAudios() {
  // 释放上一组
  for (const a of cmpState.audios.values()) a.pause();
  cmpState.audios.clear();
  for (const m of cmpState.data.members) {
    if (!m.audio_url) continue;
    const a = new Audio(m.audio_url);
    a.preload = "none";
    cmpState.audios.set(m.piece_id, a);
  }
}

function renderComparison() {
  const d = cmpState.data;
  $("#compare-title").textContent = d.title;
  $("#compare-meta").textContent =
    `创建 ${d.created_at} · 最近固化 ${d.refreshed_at} · `
    + `不可比置信阈值 < ${d.thresholds.min_confidence}`;

  const banner = $("#compare-stale-banner");
  if (d.stale) {
    const names = d.stale_members.map((s) => {
      const m = d.members.find((x) => x.piece_id === s.piece_id);
      return (m ? m.title : s.piece_id)
        + "（" + s.reasons.map(staleReasonLabel).join("、") + "）";
    }).join("；");
    banner.textContent = "⚠ 结论已过期：" + names
      + "。来源变化不会自动改写结论，请确认后显式刷新。";
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }

  renderMembers();
  renderMeasureFilter();
  renderMatrix();
  renderSegments();
}

function staleReasonLabel(r) {
  return {
    alignment_version_changed: "对齐版本已变化（锚点/待确认被修改）",
    audio_changed: "录音文件已变化",
    score_changed: "谱面文件已变化",
    alignment_missing: "对齐结果缺失",
  }[r] || r;
}

function renderMembers() {
  const box = $("#compare-members");
  box.innerHTML = "";
  const d = cmpState.data;
  for (const m of d.members) {
    const card = document.createElement("div");
    card.className = "member-card"
      + (m.is_baseline ? " baseline" : "")
      + (m.stale ? " stale" : "");
    const head = document.createElement("div");
    head.className = "member-head";
    head.innerHTML =
      `<b>${escapeHtml(m.title)}</b>`
      + (m.is_baseline ? ' <span class="base-tag">基线 · v'
          + m.frozen_version + "</span>"
        : ` <span class="ver-tag">快照 v${m.frozen_version}</span>`)
      + (m.stale ? ' <span class="stale-tag">过期</span>' : "");
    card.appendChild(head);

    if (!m.is_baseline && m.stats) {
      const s = m.stats;
      const pills = document.createElement("div");
      pills.className = "member-pills";
      pills.innerHTML =
        pill("v-resolved", `已解决 ${s.resolved}`)
        + pill("v-new", `新出现 ${s.new}`)
        + pill("v-persistent", `持续 ${s.persistent}`)
        + pill("v-incomparable", `不可比 ${s.incomparable}`)
        + pill("v-normal", `均正常 ${s.normal}`)
        + (s.pending_count
          ? pill("v-incomparable", `待确认观测 ${s.pending_count}`) : "")
        + (s.new_extra_count
          ? pill("v-new", `新多音 ${s.new_extra_count}`) : "");
      card.appendChild(pills);

      const regs = m.regressions.new_notes;
      const extraRegs = m.regressions.new_extras;
      if (regs.length || extraRegs.length) {
        const lines = regs.map((r) =>
          `小节 ${coordText(r)} #${r.score_index}`
          + ` ${abnormalText(r.status, r.tags)}`);
        const extraLines = extraRegs.map((e) =>
          `约小节 ${nearMeasureText(e.near_score_index)} 多音`
          + (e.midi != null ? ` ${midiName(e.midi)}` : ""));
        const div = document.createElement("div");
        div.className = "reg-box";
        div.innerHTML = "<b>相对基线的回退：</b><br>"
          + [...lines, ...extraLines]
            .map((t) => `• ${escapeHtml(t)}`).join("<br>");
        card.appendChild(div);
      }
    }
    box.appendChild(card);
  }
}

function pill(cls, text) {
  return `<span class="pill ${cls}">${text}</span>`;
}

function coordText(r) {
  return `${r.measure ?? "?"}${r.occurrence ? `'${r.occurrence + 1}` : ""}`
    + ` 拍${r.beat_in_measure ?? "?"}`;
}

function nearMeasureText(scoreIndex) {
  const cell = cmpState.data.matrix.cells
    .find((c) => c.score_index === scoreIndex);
  return cell ? `${cell.measure ?? "?"}` : "?";
}

function abnormalText(status, tags) {
  const parts = [];
  if (status === "wrong") parts.push("错音");
  if (status === "missing") parts.push("漏音");
  for (const t of tags || []) {
    if (ABNORMAL_TAG_LABEL[t] && !parts.includes(ABNORMAL_TAG_LABEL[t]))
      parts.push(ABNORMAL_TAG_LABEL[t]);
  }
  return parts.join("/") || status;
}

// ---------------------------------------------------------------- 小节筛选

function measureKey(cell) {
  return `${cell.measure ?? ""}:${cell.occurrence || 0}`;
}

function renderMeasureFilter() {
  const sel = $("#measure-select");
  const keys = new Map();
  for (const c of cmpState.data.matrix.cells) {
    const k = measureKey(c);
    if (!keys.has(k))
      keys.set(k, `小节 ${c.measure ?? "?"}`
        + (c.occurrence ? `（第 ${c.occurrence + 1} 次演奏）` : ""));
  }
  const cur = cmpState.filterMeasure;
  sel.innerHTML = '<option value="">全部小节</option>'
    + [...keys.entries()].map(([k, label]) =>
      `<option value="${k}"${k === cur ? " selected" : ""}>`
      + `${escapeHtml(label)}</option>`).join("");
  sel.onchange = () => {
    cmpState.filterMeasure = sel.value;
    renderMatrix();
    renderSegments();
    if (window.synthState && synthState.task) renderSynth();
  };
}

// ---------------------------------------------------------------- 矩阵

function renderMatrix() {
  const d = cmpState.data;
  const table = $("#matrix-table");
  table.innerHTML = "";
  const targetOrder = d.matrix.target_order;

  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  hr.appendChild(th("原谱坐标 / 谱音"));
  hr.appendChild(th("基线", "base-head"));
  for (const pid of targetOrder) {
    const m = d.members.find((x) => x.piece_id === pid);
    const thEl = document.createElement("th");
    thEl.textContent = m.title
      + (m.stale ? " ⚠过期" : "");
    if (m.stale) thEl.classList.add("stale");
    hr.appendChild(thEl);
  }
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  const cells = d.matrix.cells.filter((c) =>
    !cmpState.filterMeasure
    || measureKey(c) === cmpState.filterMeasure);
  for (const c of cells) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    tdName.className = "coord-cell";
    tdName.innerHTML =
      `<span class="m-num">小节 ${c.measure ?? "?"}</span>`
      + (c.occurrence ? `<span class="occ-tag">'${c.occurrence + 1}</span>`
        : "")
      + `<span class="beat-num">拍 ${c.beat_in_measure ?? "?"}</span>`
      + `<span class="midi-num">${midiName(c.midi)}</span>`;
    tr.appendChild(tdName);
    tr.appendChild(sideCell(c.baseline, d.baseline_piece, c, true));
    for (const pid of targetOrder) {
      const t = c.targets[pid];
      tr.appendChild(targetCell(t, pid, c));
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
}

function th(text, cls) {
  const t = document.createElement("th");
  if (cls) t.className = cls;
  t.textContent = text;
  return t;
}

function sideCell(side, pid, cell, isBaseline) {
  const td = document.createElement("td");
  td.className = `side-cell state-${side.state}`;
  td.innerHTML = sideInner(side);
  td.onclick = () => locateAll(cell);
  td.title = `点击同步定位（${SIDE_STATE_LABEL[side.state] || side.state}）`;
  return td;
}

function targetCell(t, pid, cell) {
  const td = document.createElement("td");
  td.className = `side-cell verdict-${t.verdict} state-${t.state}`;
  td.innerHTML =
    `<div class="verdict-tag">${VERDICT_LABEL[t.verdict]}</div>`
    + sideInner(t);
  td.onclick = () => locateAll(cell);
  td.title = `${VERDICT_LABEL[t.verdict]} · 点击同步定位`;
  return td;
}

function sideInner(side) {
  const tags = (side.tags || []).map((t) =>
    `<span class="mini-tag tag-${t}">`
    + `${ABNORMAL_TAG_LABEL[t] || t}</span>`).join("");
  const stateLabel = SIDE_STATE_LABEL[side.state] || side.state;
  const conf = side.confidence != null
    ? `<span class="conf">c=${side.confidence}</span>` : "";
  const offset = side.onset_offset_s != null && Math.abs(
    Number(side.onset_offset_s)) >= 0.001
    ? `<span class="conf">Δ${side.onset_offset_s}s</span>` : "";
  return `<div class="state-line"><span class="state-dot state-badge-${
      side.state}"></span>${stateLabel} ${conf} ${offset}</div>`
    + (tags ? `<div class="tag-line">${tags}</div>` : "");
}

// ---------------------------------------------------------------- 区段

function renderSegments() {
  const box = $("#compare-segments");
  box.innerHTML = "";
  const d = cmpState.data;
  const filter = cmpState.filterMeasure;
  let any = false;
  for (const m of d.members) {
    if (m.is_baseline || !m.problem_segments) continue;
    const segs = m.problem_segments.filter((sg) => {
      if (!filter) return true;
      const [mNum, occ] = filter.split(":");
      return String(sg.measure_start ?? "") === mNum
        && String(sg.occurrence || 0) === occ;
    });
    if (!segs.length) continue;
    any = true;
    const wrap = document.createElement("div");
    wrap.className = "seg-wrap";
    const title = document.createElement("div");
    title.className = "seg-title";
    title.innerHTML = `<b>${escapeHtml(m.title)}</b> 仍异常问题区段（相对基线）`;
    wrap.appendChild(title);
    const ul = document.createElement("ul");
    for (const sg of segs) {
      const li = document.createElement("li");
      const verd = sg.verdicts.map((v) =>
        `<span class="seg-v v-${v}">${VERDICT_LABEL[v] || v}</span>`)
        .join(" ");
      li.innerHTML =
        `小节 ${sg.measure_start ?? "?"}`
        + (sg.measure_end && sg.measure_end !== sg.measure_start
          ? `–${sg.measure_end}` : "")
        + (sg.occurrence ? `（第 ${sg.occurrence + 1} 次）` : "")
        + ` · 谱音 #${sg.score_index_start}`
        + (sg.score_index_end !== sg.score_index_start
          ? `–#${sg.score_index_end}` : "")
        + ` · ${sg.note_count} 音 &nbsp; ${verd}`;
      li.style.cursor = "pointer";
      li.onclick = () => {
        const cell = d.matrix.cells.find(
          (c) => c.score_index === sg.score_index_start);
        if (cell) locateAll(cell);
      };
      ul.appendChild(li);
    }
    wrap.appendChild(ul);
    box.appendChild(wrap);
  }
  if (!any)
    box.innerHTML = '<div class="transport-hint" style="margin-top:8px">'
      + "当前筛选范围内没有仍异常（新出现/持续）的问题区段。</div>";
}

// ---------------------------------------------------------------- 同步定位 / 播放

function locateAll(cell) {
  // 用每次录音自己的对齐快照把谱音映射到该录音内时间：
  // 已配对 -> 实际起音；未配对（漏音等）-> 期望起音预测。
  const d = cmpState.data;
  const parts = [];
  for (const m of d.members) {
    const a = cmpState.audios.get(m.piece_id);
    if (!a) continue;
    const snapSide = m.is_baseline
      ? cell.baseline
      : cell.targets[m.piece_id];
    let t = snapSide.onset_s != null ? Number(snapSide.onset_s) : null;
    if (t == null && snapSide.predicted_onset_s != null)
      t = Number(snapSide.predicted_onset_s);
    if (t == null) continue;
    try { a.currentTime = Math.max(0, t); } catch { /* metadata 未就绪 */ }
    parts.push(`${m.title}@${t.toFixed(2)}s`);
  }
  $("#compare-time").textContent =
    `已同步定位：谱音 #${cell.score_index}（小节 ${cell.measure ?? "?"}）`
    + ` — ${parts.join("；")}`;
}

$("#btn-compare-play").addEventListener("click", () => {
  if (cmpState.playing) {
    pauseComparePlayback();
  } else {
    for (const a of cmpState.audios.values()) a.play().catch(() => {});
    cmpState.playing = true;
    $("#btn-compare-play").textContent = "⏸ 全部暂停";
    const loop = () => {
      if (!cmpState.playing) return;
      cmpState.raf = requestAnimationFrame(loop);
    };
    cmpState.raf = requestAnimationFrame(loop);
  }
});

function pauseComparePlayback() {
  if (!cmpState || !cmpState.audios) return;
  for (const a of cmpState.audios.values()) a.pause();
  cmpState.playing = false;
  const btn = $("#btn-compare-play");
  if (btn) btn.textContent = "▶ 同时播放";
  if (cmpState.raf) cancelAnimationFrame(cmpState.raf);
}

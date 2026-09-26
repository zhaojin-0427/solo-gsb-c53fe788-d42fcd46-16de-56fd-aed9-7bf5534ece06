/* 演奏录音与乐谱偏差复盘台 —— 原生 JS 前端 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const SVGNS = "http://www.w3.org/2000/svg";

// 布局常量
const PAD_L = 90, PAD_R = 30, ROW_H = 46, LANE_GAP = 14;
const LANES = [
  { key: "score", label: "乐谱（展开）", y: 0 },
  { key: "deviation", label: "偏差轨道", y: 1 },
  { key: "obs", label: "录音起音", y: 2 },
];
const SVG_H = 70 + LANES.length * ROW_H + (LANES.length - 1) * LANE_GAP;
const PX_PER_SEC_DEFAULT = 90;

const STATUS_COLOR = {
  correct: "var(--correct)", wrong: "var(--wrong)",
  missing: "var(--missing)", extra: "var(--extra)",
  onset_offset: "var(--onset)", duration: "var(--duration)",
  pending: "var(--pending)", excluded: "var(--excluded)",
};
const STATE_COLOR = { stable: "var(--correct)", poly: "var(--pending)",
                      silent: "var(--excluded)" };
const STATUS_LABEL = {
  correct: "正确", wrong: "错音", missing: "漏音", extra: "多音",
  onset_offset: "起音偏移", duration: "时值偏差",
  pending: "待确认", excluded: "已排除",
};

const state = {
  bundle: null,
  pieceId: null,
  selectedScore: null,
  selectedObs: null,
  pxPerSec: PX_PER_SEC_DEFAULT,
  audio: new Audio(),
  raf: null,
};

// ---------------------------------------------------------------- 工具

function api(path, opts) {
  return fetch(path, opts).then(async (r) => {
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail
                               || `HTTP ${r.status}`);
    return r.json();
  });
}
function el(tag, attrs = {}, text) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text != null) n.textContent = text;
  return n;
}
function htmlEl(tag, attrs = {}, text) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else n[k] = v;
  }
  if (text != null) n.textContent = text;
  return n;
}
function t2x(t) { return PAD_L + t * state.pxPerSec; }
function x2t(x) { return (x - PAD_L) / state.pxPerSec; }
function laneY(key) {
  const lane = LANES.find((l) => l.key === key);
  return 56 + lane.y * (ROW_H + LANE_GAP);
}
function midiName(midi) {
  if (midi == null) return "?";
  const names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A",
                 "A#", "B"];
  const m = Math.round(midi);
  return `${names[m % 12]}${Math.floor(m / 12) - 1}`;
}

// ---------------------------------------------------------------- 上传

$("#upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = $("#upload-status");
  status.className = "status-msg";
  status.textContent = "上传中…";
  const fd = new FormData();
  fd.append("title", $("#title").value || "未命名");
  fd.append("musicxml", $("#musicxml").files[0]);
  fd.append("audio", $("#audio").files[0]);
  try {
    const created = await api("/api/pieces", { method: "POST", body: fd });
    status.textContent = "音频分析进行中…";
    pollJob(created.id);
  } catch (err) {
    status.className = "status-msg error";
    status.textContent = "失败：" + err.message;
  }
});

async function pollJob(id) {
  const status = $("#upload-status");
  const j = await api(`/api/jobs/${id}`);
  if (j.status === "running") {
    status.textContent = "正在提取基频与起音…";
    setTimeout(() => pollJob(id), 1500);
  } else if (j.status === "failed") {
    status.className = "status-msg error";
    status.textContent = "分析失败：" + j.error;
    loadPieceList();
  } else {
    status.className = "status-msg ok";
    status.textContent = "分析完成 ✓";
    await loadPieceList();
    openPiece(id);
  }
}

// ---------------------------------------------------------------- 列表

async function loadPieceList() {
  const pieces = await api("/api/pieces");
  const ul = $("#piece-list");
  ul.innerHTML = "";
  if (!pieces.length) {
    ul.appendChild(htmlEl("li", {}, "（暂无）"));
    return;
  }
  for (const p of pieces) {
    const li = htmlEl("li", {
      className: p.id === state.pieceId ? "active" : "",
      onclick: () => openPiece(p.id),
    });
    const name = htmlEl("span", {}, p.title);
    const badge = htmlEl("span", {
      className: `badge ${p.status}`,
      textContent: p.status === "analyzed" ? "已分析"
        : p.status === "failed" ? "失败" : "分析中",
    });
    li.append(name, badge);
    ul.appendChild(li);
  }
}

// ---------------------------------------------------------------- 复盘

async function openPiece(id) {
  state.pieceId = id;
  state.selectedScore = state.selectedObs = null;
  await refreshBundle();
}

async function refreshBundle() {
  const b = await api(`/api/pieces/${state.pieceId}/bundle`);
  state.bundle = b;
  // 仅在“单次复盘”视图下展开右侧复盘区；对比视图下数据在后台静默更新
  if (cmpState.view === "review") {
    $("#review").classList.remove("hidden");
    $("#anchors-panel").classList.remove("hidden");
    $("#pending-panel").classList.remove("hidden");
  }
  // 音频源（带版本参数防止缓存旧文件场景）
  if (b.piece.audio_url && state.audio.dataset.url !== b.piece.audio_url) {
    state.audio.src = b.piece.audio_url;
    state.audio.dataset.url = b.piece.audio_url;
  }
  renderAll();
  loadPieceList();
}

function renderAll() {
  renderHead();
  renderWarnings();
  renderTracks();
  renderAnchors();
  renderPending();
  updateSelectionBar();
}

function entryByScore() {
  const m = new Map();
  for (const e of state.bundle.alignment.entries)
    if (e.score_index != null) m.set(e.score_index, e);
  return m;
}
function entryByObs() {
  const m = new Map();
  for (const e of state.bundle.alignment.entries)
    if (e.obs_index != null) m.set(e.obs_index, e);
  return m;
}

function renderHead() {
  const b = state.bundle;
  $("#piece-title").textContent = b.piece.title;
  const counts = {};
  for (const e of b.alignment.entries) {
    counts[e.status] = (counts[e.status] || 0) + 1;
    for (const t of e.tags || [])
      if (t === "onset_offset" || t === "duration_deviation")
        counts["tag:" + t] = (counts["tag:" + t] || 0) + 1;
  }
  const pills = [
    ["correct", "正确", counts.correct || 0],
    ["wrong", "错音", counts.wrong || 0],
    ["missing", "漏音", counts.missing || 0],
    ["extra", "多音", counts.extra || 0],
    ["onset", "起音偏移", counts["tag:onset_offset"] || 0],
    ["duration", "时值偏差", counts["tag:duration_deviation"] || 0],
    ["pending", "待确认", counts.pending || 0],
  ];
  $("#stats").innerHTML = pills
    .map(([cls, label, n]) =>
      `<span class="pill ${cls}">${label} <b>${n}</b></span>`).join("");
  const c = b.alignment.cache;
  $("#cache-info").textContent = c
    ? `对齐 v${b.alignment.version} · 增量片段 命中 ${c.hits} / 重算 ${c.misses}`
    : `对齐 v${b.alignment.version}`;
}

function renderWarnings() {
  const ws = state.bundle.piece.warnings || [];
  $("#warnings").innerHTML = ws.length
    ? "⚠ " + ws.map((w) => escapeHtml(w)).join("<br>⚠ ") : "";
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ---------------------------------------------------------------- 轨道

function timeDomain() {
  const b = state.bundle;
  return b.piece.duration_s ||
    Math.max(...b.obs_notes.map((o) => o.onset + o.duration), 1);
}

function renderTracks() {
  const svg = $("#tracks");
  svg.innerHTML = "";
  const dur = timeDomain();
  // 自动缩放：超长录音压缩到约 6000px 以内
  state.pxPerSec = Math.max(18, Math.min(PX_PER_SEC_DEFAULT, 5800 / dur));
  const width = t2x(dur) + PAD_R;
  svg.setAttribute("width", width);
  svg.setAttribute("height", SVG_H);
  svg.onclick = onSvgClick;

  // 泳道背景与标签
  for (const lane of LANES) {
    const y = laneY(lane.key);
    svg.appendChild(el("rect", {
      x: PAD_L - 10, y: y - 6, width: width - PAD_L + 10 - PAD_R,
      height: ROW_H + 12, rx: 6, fill: "rgba(255,255,255,0.025)",
    }));
    svg.appendChild(el("text", {
      x: 8, y: y + ROW_H / 2 + 4, class: "lane-label",
    }, lane.label));
  }

  drawGrid(svg, dur);
  drawWaveform(svg);
  drawConnectors(svg);
  drawScoreBlocks(svg);
  drawObsBlocks(svg);
  drawDeviation(svg);
  drawPlayhead(svg);
}

function drawGrid(svg, dur) {
  const step = dur > 60 ? 5 : dur > 20 ? 2 : 1;
  for (let t = 0; t <= dur + 1e-6; t += step) {
    const tt = Math.min(t, dur);
    const x = t2x(tt);
    svg.appendChild(el("line", {
      x1: x, x2: x, y1: laneY("score") - 6,
      y2: laneY("obs") + ROW_H + 6, class: "grid-line",
      opacity: 0.5,
    }));
    svg.appendChild(el("text", {
      x: x + 2, y: 46, class: "lane-label",
    }, `${Math.round(tt)}s`));
  }
}

function drawWaveform(svg) {
  const peaks = state.bundle.piece.peaks || [];
  if (!peaks.length) return;
  const yTop = laneY("obs");
  const cy = yTop + ROW_H / 2;
  const dur = timeDomain();
  const w = t2x(dur) - PAD_L;
  const n = peaks.length;
  // 上下对称的包络：上沿 + 反向回连
  let up = "", down = "";
  for (let i = 0; i < n; i++) {
    const x = PAD_L + (i / (n - 1)) * w;
    const h = peaks[i] * (ROW_H / 2 - 2);
    up += `${i ? "L" : "M"}${x.toFixed(1)},${(cy - h).toFixed(1)}`;
    down = `L${x.toFixed(1)},${(cy + h).toFixed(1)}` + down;
  }
  svg.appendChild(el("path", {
    d: up + down + "Z", fill: "#5a6473", opacity: 0.22,
  }));
}

function drawScoreBlocks(svg) {
  const b = state.bundle;
  const eByS = entryByScore();
  const alpha = b.alignment.alpha || 0.5;
  const y = laneY("score");
  for (const n of b.score_notes) {
    const e = eByS.get(n.index);
    const status = e ? e.status : "correct";
    let x = e && e.predicted_onset != null
      ? t2x(e.predicted_onset)
      : t2x(n.beat * alpha + (b.alignment.beta || 0));
    const w = Math.max(6, n.dur_q * alpha * state.pxPerSec - 2);
    const fill = status === "correct"
      ? "var(--correct)"
      : (STATUS_COLOR[status] || "var(--correct)");
    const g = el("g", { class: "score-block-g" });
    const rect = el("rect", {
      x, y, width: w, height: ROW_H, rx: 3,
      fill, class: "score-block",
      "data-score": n.index,
    });
    if (state.selectedScore === n.index) rect.classList.add("selected");
    g.appendChild(rect);
    g.appendChild(el("text", {
      x: x + 3, y: y + 14,
    }, midiName(n.midi)));
    if (n.measure != null)
      g.appendChild(el("text", {
        x: x + 3, y: y + ROW_H - 5, opacity: 0.75,
      }, `${n.measure}${n.occurrence ? `'${n.occurrence + 1}` : ""}`));
    // 排除/漏音叠加斜纹
    if (e && (e.op === "excluded_score"))
      g.appendChild(el("line", {
        x1: x, y1: y, x2: x + w, y2: y + ROW_H,
        stroke: "#101318", "stroke-width": 3,
      }));
    g.style.cursor = "pointer";
    svg.appendChild(g);
  }
}

function drawObsBlocks(svg) {
  const b = state.bundle;
  const eByO = entryByObs();
  const y = laneY("obs");
  for (const o of b.obs_notes) {
    const e = eByO.get(o.index);
    const x = t2x(o.onset);
    const w = Math.max(5, o.duration * state.pxPerSec - 2);
    let fill;
    if (e && e.op === "excluded_obs") fill = "var(--excluded)";
    else if (o.state !== "stable")
      fill = o.state === "poly" ? "var(--pending)" : "var(--excluded)";
    else if (e && e.status === "wrong") fill = "var(--wrong)";
    else if (e && e.op === "insert") fill = "var(--extra)";
    else fill = "var(--correct)";
    const rect = el("rect", {
      x, y, width: w, height: ROW_H, rx: 3,
      fill, class: "obs-block", "data-obs": o.index,
    });
    if (state.selectedObs === o.index) rect.classList.add("selected");
    if (e && e.op === "excluded_obs") rect.setAttribute("opacity", 0.45);
    svg.appendChild(rect);
    if (o.state !== "stable") {
      svg.appendChild(el("text", {
        x: x + 3, y: y + 17, fill: "#fff", "font-size": 10,
      }, o.state === "poly" ? "多声?" : "无声?"));
    } else if (o.midi != null && w > 22) {
      svg.appendChild(el("text", { x: x + 3, y: y + 17 },
                         midiName(o.midi)));
    }
  }
}

function drawConnectors(svg) {
  const b = state.bundle;
  const eByS = entryByScore();
  const yS = laneY("score") + ROW_H;
  const yO = laneY("obs");
  for (const n of b.score_notes) {
    const e = eByS.get(n.index);
    if (!e || e.obs_index == null) continue;
    const obs = b.obs_notes.find((o) => o.index === e.obs_index);
    if (!obs) continue;
    const x1 = t2x(e.predicted_onset != null ? e.predicted_onset
      : n.beat * (b.alignment.alpha || 0.5));
    const x2 = t2x(obs.onset);
    const locked = (e.tags || []).includes("locked");
    const color = e.status === "wrong" ? "var(--wrong)"
      : (e.tags || []).includes("onset_offset") ? "var(--onset)"
      : locked ? "var(--locked)" : "var(--correct)";
    const mid = (yS + yO) / 2;
    svg.appendChild(el("path", {
      d: `M${x1},${yS} C${x1},${mid} ${x2},${mid} ${x2},${yO}`,
      stroke: color,
      class: "connector" + (locked ? " locked" : ""),
    }));
  }
}

function drawDeviation(svg) {
  // 偏差轨道：期望时间(竖线) 到 实际起音(菱形) 的水平差；
  // 音符框宽度表示实际时长 / 期望时长。
  const b = state.bundle;
  const eByS = entryByScore();
  const y = laneY("deviation");
  const cy = y + ROW_H / 2;
  for (const n of b.score_notes) {
    const e = eByS.get(n.index);
    if (!e || e.obs_index == null || e.predicted_onset == null) continue;
    const obs = b.obs_notes.find((o) => o.index === e.obs_index);
    if (!obs) continue;
    const xp = t2x(e.predicted_onset);
    const xa = t2x(obs.onset);
    const onsetTagged = (e.tags || []).includes("onset_offset");
    const durTagged = (e.tags || []).includes("duration_deviation");
    const col = onsetTagged ? "var(--onset)" : "var(--muted)";
    svg.appendChild(el("line", {
      x1: xp, y1: cy, x2: xa, y2: cy,
      stroke: col, "stroke-width": onsetTagged ? 2.5 : 1,
      opacity: onsetTagged ? 1 : 0.5,
    }));
    svg.appendChild(el("circle", {
      cx: xp, cy, r: 3, fill: "none", stroke: "var(--muted)",
    }));
    // 实际时长框（相对期望位置），偏差超阈着色
    const dw = Math.max(4, obs.duration * state.pxPerSec);
    svg.appendChild(el("rect", {
      x: xa, y: cy + 7, width: dw, height: 9, rx: 2,
      fill: "none",
      stroke: durTagged ? "var(--duration)" : "var(--muted)",
      "stroke-width": durTagged ? 1.8 : 0.8,
      opacity: 0.9,
    }));
  }
}

function drawPlayhead(svg) {
  const line = el("line", {
    x1: PAD_L, x2: PAD_L,
    y1: laneY("score") - 6, y2: laneY("obs") + ROW_H + 8,
    class: "playhead",
  });
  line.id = "playhead-line";
  svg.appendChild(line);
}

function updatePlayhead() {
  const t = state.audio.currentTime || 0;
  const l = $("#playhead-line");
  if (l) {
    l.setAttribute("x1", t2x(t));
    l.setAttribute("x2", t2x(t));
  }
  $("#time-readout").textContent = `${t.toFixed(2)}s`;
}

// ---------------------------------------------------------------- 交互

function onSvgClick(ev) {
  const scoreRect = ev.target.closest ? ev.target.closest(".score-block")
    : null;
  const obsRect = ev.target.closest ? ev.target.closest(".obs-block") : null;
  if (scoreRect) {
    const i = Number(scoreRect.dataset.score);
    state.selectedScore = state.selectedScore === i ? null : i;
    state.selectedObs = null;
  } else if (obsRect) {
    const i = Number(obsRect.dataset.obs);
    state.selectedObs = state.selectedObs === i ? null : i;
  } else {
    // 空白处 -> 定位播放
    const rect = $("#tracks").getBoundingClientRect();
    const x = ev.clientX - rect.left;
    const t = x2t(x);
    if (t >= 0 && t <= timeDomain()) seek(t);
    return;
  }
  renderTracks();
  updateSelectionBar();
  showDetail();
}

function seek(t) {
  state.audio.currentTime = Math.max(0, t);
  updatePlayhead();
}

$("#btn-play").addEventListener("click", () => {
  const a = state.audio;
  if (a.paused) {
    a.play();
    $("#btn-play").textContent = "⏸ 暂停";
    const loop = () => {
      updatePlayhead();
      if (!a.paused) state.raf = requestAnimationFrame(loop);
    };
    state.raf = requestAnimationFrame(loop);
  } else {
    a.pause();
    $("#btn-play").textContent = "▶ 播放";
    cancelAnimationFrame(state.raf);
  }
});
state.audio.addEventListener("ended", () => {
  $("#btn-play").textContent = "▶ 播放";
});

function updateSelectionBar() {
  const bar = $("#selection-bar");
  const sSel = state.selectedScore;
  const oSel = state.selectedObs;
  if (sSel == null && oSel == null) {
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  const b = state.bundle;
  const sn = sSel != null ? b.score_notes[sSel] : null;
  const on = oSel != null ? b.obs_notes[oSel] : null;
  $("#selection-info").innerHTML =
    `乐谱：${sn ? `#${sn.index} ${midiName(sn.midi)} `
      + `（小节 ${sn.measure}${sn.occurrence ? " 第" + (sn.occurrence + 1) + "次" : ""}）` : "—"}`
    + ` &nbsp;|&nbsp; 观测：${on ? `#${on.index} ${on.midi != null
      ? midiName(on.midi) : "无基频"} @${on.onset.toFixed(2)}s `
      + `[${on.state}]` : "—"}`;
  $("#btn-lock").disabled = !(sSel != null && oSel != null
    && on && on.state === "stable");
}

$("#btn-clear-sel").addEventListener("click", () => {
  state.selectedScore = state.selectedObs = null;
  renderTracks();
  updateSelectionBar();
});

$("#btn-lock").addEventListener("click", async () => {
  await api(`/api/pieces/${state.pieceId}/anchors`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: "lock", score_index: state.selectedScore,
      obs_index: state.selectedObs, note: "manual_lock",
    }),
  }).catch(handleErr);
  state.selectedScore = state.selectedObs = null;
  await refreshBundle();
});

$("#btn-exclude-score").addEventListener("click", async () => {
  if (state.selectedScore == null) return;
  await api(`/api/pieces/${state.pieceId}/anchors`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: "exclude_score", score_index: state.selectedScore,
    }),
  }).catch(handleErr);
  state.selectedScore = null;
  await refreshBundle();
});

$("#btn-exclude-obs").addEventListener("click", async () => {
  if (state.selectedObs == null) return;
  await api(`/api/pieces/${state.pieceId}/anchors`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: "exclude_obs", obs_index: state.selectedObs,
    }),
  }).catch(handleErr);
  state.selectedObs = null;
  await refreshBundle();
});

function handleErr(err) { alert("操作失败：" + err.message); }

// ---------------------------------------------------------------- 详情

function showDetail() {
  const b = state.bundle;
  const eByS = entryByScore();
  const eByO = entryByObs();
  const parts = [];
  let found = false;

  if (state.selectedScore != null) {
    const n = b.score_notes[state.selectedScore];
    const e = eByS.get(n.index);
    found = true;
    parts.push(detailScore(n, e));
  }
  if (state.selectedObs != null) {
    const o = b.obs_notes[state.selectedObs];
    const e = eByO.get(o.index);
    found = true;
    parts.push(detailObs(o, e));
  }
  $("#detail").innerHTML = found
    ? parts.join("<hr style='border-color:var(--line)'>")
    : "<em>点击谱块或观测块查看判定依据（basis）。</em>";
}

function detailScore(n, e) {
  const tagHtml = (e?.tags || []).map((t) =>
    `<span class="tag ${t}">${({
      wrong_pitch: "错音", onset_offset: "起音偏移",
      duration_deviation: "时值偏差", locked: "已锁定",
    })[t] || t}</span>`).join("");
  return `
    <b>乐谱音 #${n.index} ${midiName(n.midi)}</b>
    （原谱坐标：小节 ${n.measure}，拍 ${n.beat_in_measure}
     ${n.occurrence ? `，第 ${n.occurrence + 1} 次演奏` : ""}；
     展开 beat=${n.beat.toFixed(2)}，时长 ${n.dur_q} 四分音符）<br>
    判定：<b style="color:${STATUS_COLOR[e?.status] || ""}">
      ${STATUS_LABEL[e?.status] || "—"}</b> ${tagHtml}
    置信度 <code>${e?.confidence ?? "—"}</code><br>
    基频偏差 <code>${e?.pitch_delta ?? "—"}</code> 半音 ·
    起音偏移 <code>${e?.onset_offset ?? "—"}</code>s ·
    期望起音 <code>${e?.predicted_onset ?? "—"}</code>s ·
    时值比 <code>${e?.duration_ratio ?? "—"}</code><br>
    判定依据：${(e?.basis || []).map((b) => `<code>${escapeHtml(b)}</code>`)
      .join(" ")}`;
}

function detailObs(o, e) {
  return `
    <b>观测 #${o.index}</b> @${o.onset.toFixed(3)}s，
    时长 ${o.duration.toFixed(3)}s，基频
    <code>${o.midi != null ? midiName(o.midi) + "(" + o.midi + ")"
      : "无"}</code>，
    状态 <b style="color:${STATE_COLOR[o.state]}">${
      { stable: "稳定", poly: "多声部歧义", silent: "无声歧义" }[o.state]}
    </b><br>
    有声帧比例 <code>${o.voiced_ratio}</code> ·
    双峰帧比例 <code>${o.poly_ratio}</code> ·
    起音强度 <code>${o.onset_strength}</code><br>
    对齐结果：<code>${e ? STATUS_LABEL[e.status] + " (" + e.op + ")"
      : "未参与"}</code><br>
    判定依据：${(e?.basis || []).map((b) => `<code>${escapeHtml(b)}</code>`)
      .join(" ") || "<em>无</em>"}`;
}

// ---------------------------------------------------------------- 锚点列表

function renderAnchors() {
  const ul = $("#anchor-list");
  ul.innerHTML = "";
  const anchors = state.bundle.anchors.filter((a) =>
    a.kind === "lock" || a.kind === "force_lock"
    || a.kind.startsWith("exclude"));
  if (!anchors.length) {
    ul.appendChild(htmlEl("li", {}, "（尚无锚点，自动对齐结果可直接修正）"));
    return;
  }
  for (const a of anchors) {
    const li = htmlEl("li");
    const label = a.kind === "lock"
      ? `🔒 谱#${a.score_index} ↔ 录#${a.obs_index}`
      : a.kind === "force_lock"
        ? `✅ 确认配对 谱#${a.score_index} ↔ 录#${a.obs_index}（歧义已消解）`
        : a.kind === "exclude_score"
          ? `🚫 排除谱#${a.score_index}`
          : `🚫 排除录#${a.obs_index}`;
    li.appendChild(htmlEl("span", {}, label));
    const del = htmlEl("button", {}, "删除");
    del.onclick = async () => {
      await api(`/api/anchors/${a.id}?piece_id=${state.pieceId}`,
                { method: "DELETE" }).catch(handleErr);
      await refreshBundle();
    };
    li.appendChild(del);
    ul.appendChild(li);
  }
}

// ---------------------------------------------------------------- 待确认

function renderPending() {
  const ul = $("#pending-list");
  ul.innerHTML = "";
  const pending = state.bundle.alignment.pending || [];
  if (!pending.length) {
    ul.appendChild(htmlEl("li", {}, "（无）"));
    return;
  }
  const eByO = entryByObs();
  for (const p of pending) {
    const o = state.bundle.obs_notes[p.obs_index];
    const li = htmlEl("li");
    const left = htmlEl("div");
    left.appendChild(htmlEl("div", {
      className: "reason",
      textContent: `录#${o.index} @${o.onset.toFixed(2)}s — `
        + (p.reason === "polyphony_ambiguous" ? "多声部歧义"
          : p.reason === "silent_segment" ? "有声起音但无稳定基频"
          : p.reason),
    }));
    const acts = htmlEl("div", { className: "amb-actions" });

    const btnHear = htmlEl("button", {}, "▶ 试听");
    btnHear.onclick = () => {
      seek(Math.max(0, o.onset - 0.15));
      if (state.audio.paused) $("#btn-play").click();
      renderTracks();
    };
    const btnMatch = htmlEl("button", {}, "配对…");
    btnMatch.onclick = () => {
      const ans = prompt(
        `把录#${o.index} 锁定到哪个乐谱音（输入乐谱 # 下标）？\n`
        + `提示：可先在乐谱轨道点击音符查看 # 下标与原谱小节。`);
      if (ans == null || ans.trim() === "") return;
      resolvePending(p.obs_index, "match", Number(ans.trim()));
    };
    const btnExtra = htmlEl("button", {}, "确认多音");
    btnExtra.onclick = () => resolvePending(p.obs_index, "extra");
    const btnIgnore = htmlEl("button", {}, "忽略");
    btnIgnore.onclick = () => resolvePending(p.obs_index, "ignore");
    acts.append(btnHear, btnMatch, btnExtra, btnIgnore);
    li.append(left, acts);
    ul.appendChild(li);
  }
}

async function resolvePending(obsIndex, action, scoreIndex) {
  try {
    await api(`/api/pieces/${state.pieceId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        obs_index: obsIndex, action, score_index: scoreIndex,
      }),
    });
    await refreshBundle();
  } catch (err) { handleErr(err); }
}

// ---------------------------------------------------------------- 导出

$("#btn-export").addEventListener("click", () => {
  const a = document.createElement("a");
  a.href = `/api/pieces/${state.pieceId}/export`;
  a.download = "";
  a.click();
});

// ---------------------------------------------------------------- 启动

loadPieceList();
setInterval(updatePlayhead, 250);

/* 偏差片段练习谱 —— 原生 JS（与 app.js / compare.js / rx.js 同页）
 *
 * 数据流：
 *   GET  /api/pieces/{id}/practice/editor-data  展开小节 + 每谱位状态/阈值
 *   POST /api/pieces/{id}/practice-scores       创建（422 逐位阻止；成功=v1）
 *   GET  /api/practice-scores[?piece_id=...]    列表（当前版本/版本数/过期）
 *   GET  /api/practice-scores/{id}              详情（当前版本）
 *   GET  /api/practice-scores/{id}/versions     版本历史
 *   GET  /api/practice-scores/{id}/versions/{v} 单版本详情
 *   POST /api/practice-scores/{id}/rebuild      显式重建（成功追加新版本；
 *                                               422 不新增不改写，旧稿保留）
 *   GET  /api/practice-scores/{id}/versions/{v}/musicxml  任一版本下载
 *   GET  /api/practice-scores/{id}/versions/{v}/manifest  任一版本清单
 *   GET  /api/practice-scores/{id}/musicxml     当前版本下载
 *   GET  /api/practice-scores/{id}/manifest     当前版本清单
 *   DELETE /api/practice-scores/{id}            删除（清理全部版本）
 */
"use strict";

const practiceState = {
  list: [],
  current: null,          // 练习谱详情（当前版本）
  versions: null,         // 版本历史 {current_version, versions:[...]}
  editor: null,           // {piece_id, title, thresholds, measures, builder}
};

const PR_BLOCK_LABEL = {
  pending_confirmation: "待确认（未处置歧义）",
  excluded: "用户已排除",
  low_confidence: "置信度低于阈值",
  no_confirmed_deviation: "该片段没有已确认的偏差音",
};
const PR_TEMPO_DIR_LABEL = { up: "逐轮增速", down: "逐轮减速", same: "恒速" };

// 练习谱接口：422 逐位阻止时 detail 是普通字符串、真正的载荷在 JSON 主体，
// 通用 api() 只解析 detail，因此这里返回 {status, body} 由调用方分流。
async function practiceApi(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => null);
  return { status: r.status, body, ok: r.ok };
}

function blockedText(body) {
  if (!body || body.reason !== "blocked_positions") return null;
  return (body.blockers || []).map((b) =>
    b.score_index == null
      ? `片段 ${b.segment}：${PR_BLOCK_LABEL[b.reason] || b.reason}`
      : `片段 ${b.segment} · 谱位 #${b.score_index}（小节 ${b.measure}`
        + (b.occurrence ? ` 第 ${b.occurrence + 1} 次` : "")
        + `）：${PR_BLOCK_LABEL[b.reason] || b.reason}`).join("\n");
}

async function practiceOnViewEnter() {
  await loadPracticeList();
  if (practiceState.current) {
    await openPractice(practiceState.current.id);
  } else {
    showPracticeEmpty();
  }
}

function showPracticeEmpty() {
  $("#practice-detail").classList.add("hidden");
  $("#practice-builder").classList.add("hidden");
  $("#practice-empty").classList.remove("hidden");
}

// ---------------------------------------------------------------- 入口

$("#btn-review-practice").addEventListener("click", async () => {
  if (!state.pieceId) { alert("请先打开一支已分析的录音"); return; }
  await switchView("practice");
  openPracticeBuilder(state.pieceId);
});
$("#btn-new-practice").addEventListener("click", async () => {
  if (!state.pieceId) {
    alert("请先在“单次复盘”页打开一支已分析的录音。");
    await switchView("review");
    return;
  }
  openPracticeBuilder(state.pieceId);
});
$("#btn-practice-cancel").addEventListener("click", () => closePracticeBuilder());
$("#btn-practice-create").addEventListener("click", submitPracticeCreate);
$("#btn-practice-rebuild").addEventListener("click", submitRebuild);
$("#btn-practice-delete").addEventListener("click", submitDelete);

// ---------------------------------------------------------------- 列表

async function loadPracticeList() {
  practiceState.list = await api("/api/practice-scores");
  const ul = $("#practice-list");
  ul.innerHTML = "";
  if (!practiceState.list.length) {
    ul.appendChild(htmlEl("li", { className: "muted-li" }, "（暂无练习谱）"));
    return;
  }
  for (const p of practiceState.list) {
    const li = htmlEl("li", {
      className: practiceState.current && p.id === practiceState.current.id
        ? "active" : "",
      onclick: () => openPractice(p.id),
    });
    const name = document.createElement("span");
    name.className = "rx-name";
    name.innerHTML = `${escapeHtml(p.title)}<span class="rx-sub">`
      + `${escapeHtml(p.piece_title || "")} · ${p.round_count} 轮`
      + (p.status === "built"
        ? ` · v${p.current_version}（共 ${p.version_count} 版）` : "")
      + "</span>";
    li.appendChild(name);
    if (p.stale) {
      li.appendChild(htmlEl("span", {
        className: "stale-dot",
        title: "当前版本的来源谱/对齐已变化，旧稿仍可下载；显式重建会追加新版本",
      }, "过期"));
    }
    ul.appendChild(li);
  }
}

// ---------------------------------------------------------------- 构造器

async function openPracticeBuilder(pieceId) {
  let data;
  try {
    data = await api(`/api/pieces/${pieceId}/practice/editor-data`);
  } catch (err) {
    alert("无法载入练习谱编辑器：" + err.message);
    showPracticeEmpty();
    return;
  }
  practiceState.editor = {
    ...data,
    builder: {
      segments: [newPracticeSegment(data.measures)],
      editing: 0,
    },
  };
  $("#practice-detail").classList.add("hidden");
  $("#practice-empty").classList.add("hidden");
  $("#practice-builder").classList.remove("hidden");
  $("#practice-title").value = "";
  $("#practice-builder-source").textContent =
    `来源（单次复盘）：${data.title} · 当前对齐 v${data.alignment_version}`
    + ` · 置信阈值 ≥ ${data.thresholds.min_confidence}`;
  renderPracticeBuilder();
}

function closePracticeBuilder() {
  $("#practice-builder").classList.add("hidden");
  practiceState.editor = null;
  if (practiceState.current) {
    $("#practice-detail").classList.remove("hidden");
  } else {
    $("#practice-empty").classList.remove("hidden");
  }
}

function newPracticeSegment(measures) {
  const firstOk = measures.find((m) => m.selectable && m.deviation_indexes.length)
    || measures[0];
  const m0 = firstOk ? firstOk.measure : 0;
  const o0 = firstOk ? firstOk.occurrence : 0;
  return {
    measure_start: m0, occurrence_start: o0,
    measure_end: m0, occurrence_end: o0,
    loops: 2, start_tempo_bpm: 60,
    tempo_increment_bpm: 6, tempo_direction: "up",
  };
}

function renderPracticeBuilder() {
  const ed = practiceState.editor;
  if (!ed) return;
  const box = $("#practice-seg-editor");
  box.innerHTML = "";
  ed.builder.segments.forEach((seg, i) =>
    box.appendChild(practiceSegCard(seg, i)));
  if (ed.builder.segments.length < ed.thresholds.max_segments) {
    box.appendChild(htmlEl("button", {
      className: "rx-seg-add",
      onclick: () => {
        ed.builder.segments.push(newPracticeSegment(ed.measures));
        ed.builder.editing = ed.builder.segments.length - 1;
        renderPracticeBuilder();
      },
    }, "＋ 增加一个连续片段（按此顺序练习）"));
  }
  renderMeasureStrip();
  renderPracticeBlockPreview();
}

function measureOptions(selectedMeasure, selectedOcc) {
  const ed = practiceState.editor;
  return ed.measures.map((m) => {
    const label = `小节 ${m.measure}`
      + (m.occurrence ? `（第 ${m.occurrence + 1} 次演奏）` : "");
    return { value: `${m.measure}:${m.occurrence}`, label, m };
  }).map((o) => {
    const sel = (o.m.measure === selectedMeasure
      && o.m.occurrence === selectedOcc) ? " selected" : "";
    return `<option value="${o.value}"${sel}>${escapeHtml(o.label)}</option>`;
  }).join("");
}

function practiceSegCard(seg, i) {
  const card = document.createElement("div");
  card.className = "rx-seg-card";
  const head = document.createElement("div");
  head.className = "rx-seg-head";
  head.innerHTML = `片段 ${i + 1}`
    + (i === practiceState.editor.builder.editing
      ? ' <span class="rx-sub">（正在用下方小节条设置起/止）</span>' : "")
    + ` <span class="rx-sub">练习顺序第 ${i + 1} 个</span>`;
  card.appendChild(head);

  const grid = document.createElement("div");
  grid.className = "rx-seg-grid";

  const mkSelect = (label, value, onchange) => {
    const wrap = document.createElement("label");
    wrap.textContent = label + " ";
    const sel = document.createElement("select");
    sel.innerHTML = measureOptions(...value.split(":").map(Number));
    sel.onchange = onchange;
    wrap.appendChild(sel);
    return wrap;
  };
  const startSel = mkSelect("起始小节",
    `${seg.measure_start}:${seg.occurrence_start}`, (e) => {
      const [m, o] = e.target.value.split(":").map(Number);
      seg.measure_start = m; seg.occurrence_start = o;
      renderPracticeBuilder();
    });
  const endSel = mkSelect("结束小节",
    `${seg.measure_end}:${seg.occurrence_end}`, (e) => {
      const [m, o] = e.target.value.split(":").map(Number);
      seg.measure_end = m; seg.occurrence_end = o;
      renderPracticeBuilder();
    });
  [startSel, endSel].forEach((w) => {
    w.querySelector("select").onfocus = () => {
      practiceState.editor.builder.editing = i;
    };
  });
  grid.appendChild(startSel);
  grid.appendChild(endSel);

  grid.appendChild(numberField("循环次数", seg.loops,
    (v) => { seg.loops = v; renderPracticeBlockPreview(); },
    1, 1, practiceState.editor.thresholds.max_loops));
  grid.appendChild(numberField("起始速度(BPM)", seg.start_tempo_bpm,
    (v) => { seg.start_tempo_bpm = v; renderPracticeBlockPreview(); },
    1, 20, 300));
  grid.appendChild(numberField("逐轮增速(BPM)", seg.tempo_increment_bpm,
    (v) => { seg.tempo_increment_bpm = v; renderPracticeBlockPreview(); },
    1, 0, 100));

  const dirLab = document.createElement("label");
  dirLab.textContent = "速度方向 ";
  const dir = document.createElement("select");
  dir.innerHTML = [["up", "逐轮增速"], ["same", "恒速"],
    ["down", "逐轮减速"]].map(([v, l]) =>
    `<option value="${v}"${seg.tempo_direction === v ? " selected" : ""}>${l}</option>`
  ).join("");
  dir.onchange = (e) => {
    seg.tempo_direction = e.target.value;
    renderPracticeBlockPreview();
  };
  dirLab.appendChild(dir);
  grid.appendChild(dirLab);

  // 末轮速度预览
  const preview = document.createElement("div");
  preview.className = "rx-sub";
  preview.style.gridColumn = "1/-1";
  preview.id = `tempo-preview-${i}`;
  grid.appendChild(preview);

  card.appendChild(grid);
  card.appendChild(htmlEl("button", {
    style: "margin-top:6px",
    onclick: () => { practiceState.editor.builder.editing = i; renderMeasureStrip(); },
  }, "用小节条设置起/止"));
  if (practiceState.editor.builder.segments.length > 1) {
    card.appendChild(htmlEl("button", {
      style: "margin-left:8px",
      onclick: () => {
        practiceState.editor.builder.segments.splice(i, 1);
        practiceState.editor.builder.editing = 0;
        renderPracticeBuilder();
      },
    }, "删除此片段"));
  }

  // 片段级逐位阻止预览
  const blk = document.createElement("div");
  blk.className = "pr-seg-blocks";
  blk.id = `pr-seg-blocks-${i}`;
  card.appendChild(blk);
  return card;
}

function tempoOf(seg, loop) {
  const d = loop - 1;
  if (seg.tempo_direction === "up")
    return seg.start_tempo_bpm + d * seg.tempo_increment_bpm;
  if (seg.tempo_direction === "down")
    return seg.start_tempo_bpm - d * seg.tempo_increment_bpm;
  return seg.start_tempo_bpm;
}

function lastTempo(seg) {
  return tempoOf(seg, Math.max(1, seg.loops || 1));
}

// 展开顺序定位
function measureOrderMap() {
  const m = new Map();
  practiceState.editor.measures.forEach((x, i) =>
    m.set(`${x.measure}:${x.occurrence}`, i));
  return m;
}

function segmentRangeIndexes(seg) {
  const map = measureOrderMap();
  const a = map.get(`${seg.measure_start}:${seg.occurrence_start}`);
  const b = map.get(`${seg.measure_end}:${seg.occurrence_end}`);
  if (a == null || b == null || a > b) return null;
  return [a, b];
}

function renderMeasureStrip() {
  const ed = practiceState.editor;
  const wrap = $("#practice-measure-strip-wrap");
  const strip = $("#practice-measure-strip");
  if (!ed) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  strip.innerHTML = "";
  const seg = ed.builder.segments[ed.builder.editing]
    || ed.builder.segments[0];
  const ranges = ed.builder.segments.map(segmentRangeIndexes);
  ed.measures.forEach((m, i) => {
    const cell = document.createElement("div");
    const inAny = ranges.some((r) => r && r[0] <= i && i <= r[1]);
    const cur = ranges[ed.builder.editing];
    cell.className = "rx-note-cell pr-measure-cell"
      + (m.selectable ? "" : " blocked")
      + (m.deviation_indexes.length ? " has-dev" : "")
      + (inAny ? " inseg" : "")
      + (cur && i === cur[0] ? " editing-start" : "")
      + (cur && i === cur[1] ? " editing-end" : "");
    const occ = m.occurrence ? `'${m.occurrence + 1}` : "";
    cell.innerHTML = `<b>小节 ${m.measure}${occ}</b>`
      + `<div class="pr-cell-flags">`
      + (m.deviation_indexes.length
        ? `<span class="pr-badge dev">偏差×${m.deviation_indexes.length}</span>`
        : `<span class="pr-badge nodev">无偏差</span>`)
      + (m.blockers.length
        ? `<span class="pr-badge blk">阻止×${m.blockers.length}</span>` : "")
      + `</div>`;
    cell.title = `小节 ${m.measure}`
      + (m.occurrence ? `（第 ${m.occurrence + 1} 次演奏）` : "")
      + (m.blockers.length
        ? "\n阻止谱位：" + m.blockers.map((b) =>
          `#${b.score_index} ${PR_BLOCK_LABEL[b.reason] || b.reason}`).join("；")
        : "")
      + (m.deviation_indexes.length
        ? `\n已确认偏差音：#${m.deviation_indexes.join("、#")}` : "");
    cell.onclick = () => {
      const s = ed.builder.segments[ed.builder.editing];
      if (s._set !== "end") {
        s.measure_start = m.measure; s.occurrence_start = m.occurrence;
        s._set = "end";
      } else {
        s.measure_end = m.measure; s.occurrence_end = m.occurrence;
        s._set = "start";
      }
      const r = segmentRangeIndexes(s);
      if (r && r[0] > r[1]) {
        [s.measure_start, s.measure_end] = [s.measure_end, s.measure_start];
        [s.occurrence_start, s.occurrence_end] =
          [s.occurrence_end, s.occurrence_start];
      }
      renderPracticeBuilder();
    };
    strip.appendChild(cell);
  });
}

function renderPracticeBlockPreview() {
  const ed = practiceState.editor;
  if (!ed) return;
  const banner = $("#practice-block-banner");
  const covered = new Set();
  const problems = [];
  ed.builder.segments.forEach((seg, i) => {
    const range = segmentRangeIndexes(seg);
    const box = $(`#pr-seg-blocks-${i}`);
    const pv = document.getElementById(`tempo-preview-${i}`);
    if (pv) {
      pv.textContent = `第 1 轮 ${seg.start_tempo_bpm} BPM → 第 ${seg.loops} 轮 `
        + `${lastTempo(seg)} BPM（${PR_TEMPO_DIR_LABEL[seg.tempo_direction]}）`;
    }
    if (!box) return;
    box.innerHTML = "";
    if (!range) {
      box.appendChild(htmlEl("div", { className: "pr-blk-line" },
        "⚠ 起始小节晚于结束小节"));
      problems.push(`片段 ${i + 1}：起止顺序错误`);
      return;
    }
    const measures = ed.measures.slice(range[0], range[1] + 1);
    // 重叠
    const overlap = measures.filter((m) =>
      covered.has(`${m.measure}:${m.occurrence}`));
    measures.forEach((m) => covered.add(`${m.measure}:${m.occurrence}`));
    const blockers = measures.flatMap((m) => m.blockers.map((b) => ({ ...b, m })));
    const hasDev = measures.some((m) => m.deviation_indexes.length);
    if (overlap.length) {
      const t = `与其他片段重叠：${overlap.map((m) =>
        `小节${m.measure}${m.occurrence ? `'${m.occurrence + 1}` : ""}`).join("、")}`;
      box.appendChild(htmlEl("div", { className: "pr-blk-line" }, "⚠ " + t));
      problems.push(`片段 ${i + 1}：${t}`);
    }
    for (const b of blockers) {
      box.appendChild(htmlEl("div", { className: "pr-blk-line" },
        `⚠ 谱位 #${b.score_index}（小节 ${b.m.measure}）：`
        + `${PR_BLOCK_LABEL[b.reason] || b.reason} —— 阻止生成`));
      problems.push(`片段 ${i + 1} #${b.score_index}：`
        + (PR_BLOCK_LABEL[b.reason] || b.reason));
    }
    if (!hasDev) {
      box.appendChild(htmlEl("div", { className: "pr-blk-line" },
        "⚠ 该片段不含任何已确认偏差音 —— 阻止生成"));
      problems.push(`片段 ${i + 1}：无已确认偏差音`);
    }
    const tLast = lastTempo(seg);
    if (tLast < ed.thresholds.tempo_bpm_min
      || tLast > ed.thresholds.tempo_bpm_max
      || seg.start_tempo_bpm < ed.thresholds.tempo_bpm_min
      || seg.start_tempo_bpm > ed.thresholds.tempo_bpm_max) {
      const t = `速度超出 ${ed.thresholds.tempo_bpm_min}–`
        + `${ed.thresholds.tempo_bpm_max} BPM`;
      box.appendChild(htmlEl("div", { className: "pr-blk-line" }, "⚠ " + t));
      problems.push(`片段 ${i + 1}：${t}`);
    }
  });
  if (problems.length) {
    banner.classList.remove("hidden");
    banner.innerHTML = "<b>当前无法生成（逐位原因）：</b><br>"
      + problems.map(escapeHtml).join("<br>");
  } else {
    banner.classList.add("hidden");
    banner.innerHTML = "";
  }
}

async function submitPracticeCreate() {
  const ed = practiceState.editor;
  if (!ed) return;
  const payload = {
    title: $("#practice-title").value || undefined,
    segments: ed.builder.segments.map((s) => ({
      measure_start: s.measure_start,
      occurrence_start: s.occurrence_start,
      measure_end: s.measure_end,
      occurrence_end: s.occurrence_end,
      loops: s.loops,
      start_tempo_bpm: s.start_tempo_bpm,
      tempo_increment_bpm: s.tempo_increment_bpm,
      tempo_direction: s.tempo_direction,
    })),
  };
  try {
    const res = await practiceApi(`/api/pieces/${ed.piece_id}/practice-scores`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    if (!res.ok) {
      await showPracticeBlockErrorByStatus(res.status, res.body);
      return;
    }
    practiceState.editor = null;
    await openPractice(res.body.id);
    loadPracticeList();
  } catch (err) {
    alert("生成失败：" + err.message);
  }
}

async function showPracticeBlockErrorByStatus(status, body) {
  if (status === 422) {
    const lines = blockedText(body);
    if (lines != null) {
      alert("已阻止生成（不产生任何版本），请逐位处理后再试：\n\n" + lines);
      if (practiceState.editor) renderPracticeBlockPreview();
      return;
    }
  }
  alert("生成失败：" + ((body && body.detail) || `HTTP ${status}`));
}

// ---------------------------------------------------------------- 详情

async function openPractice(id) {
  try {
    const [detail, versions] = await Promise.all([
      api(`/api/practice-scores/${id}`),
      api(`/api/practice-scores/${id}/versions`),
    ]);
    practiceState.current = detail;
    practiceState.versions = versions;
  } catch (err) {
    alert("加载练习谱失败：" + err.message);
    return;
  }
  $("#practice-builder").classList.add("hidden");
  practiceState.editor = null;
  $("#practice-empty").classList.add("hidden");
  $("#practice-detail").classList.remove("hidden");
  renderPracticeDetail();
  loadPracticeList();
}

function renderPracticeDetail() {
  const p = practiceState.current;
  const curV = p.current_version;
  $("#practice-detail-title").textContent = p.title;
  $("#practice-detail-meta").textContent =
    `来源乐曲：${p.piece_title || p.piece_id} · 创建 ${p.created_at}`
    + (p.built_at ? ` · 当前版本生成 ${p.built_at}` : "")
    + ` · 状态：${p.status === "built"
      ? `已固化（v${curV}，共 ${p.version_count} 版）` : p.status}`;

  const banner = $("#practice-stale-banner");
  if (!p.stale) {
    banner.classList.add("hidden");
  } else {
    banner.textContent = "⚠ 当前版本（v" + curV + "）已过期："
      + p.stale_reasons.map(staleReasonLabel).join("、")
      + "。当前版本与全部历史版本都可下载；点“显式重建”且校验通过后"
      + "会追加新版本并切换当前版本，失败不影响任何旧版本。";
    banner.classList.remove("hidden");
  }

  $("#practice-frozen").innerHTML =
    `当前版本 <b>v${p.frozen.version ?? "—"}</b> · 只读固化：来源谱 sha256 `
    + `<code>${(p.frozen.source_musicxml_sha256 || "").slice(0, 16)}…</code>`
    + ` · 对齐版本 v${p.frozen.alignment_version}`
    + `（当前 v${p.frozen.current_alignment_version ?? "—"}）`
    + ` · 练习谱 sha256 <code>${
      (p.frozen.generated_musicxml_sha256 || "").slice(0, 16)}…</code>`
    + ` · 算法 ${p.frozen.algorithm_version}`
    + ` · 置信阈值 ≥ ${p.params.min_confidence}`;

  renderPracticeVersions();
  renderPracticeOrder(p);
  renderPracticeSegments(p);

  $("#practice-download-xml").href = p.musicxml_url || "#";
  $("#practice-download-manifest").href = p.manifest_url || "#";
  $("#practice-download-xml").classList.toggle("dl-disabled",
    !p.musicxml_available);
  $("#practice-download-xml").textContent =
    `⬇ 下载当前版本（v${curV}）练习谱 MusicXML`;
  $("#practice-download-manifest").textContent =
    `⬇ 下载当前版本（v${curV}）确定性清单 JSON`;
}

function renderPracticeVersions() {
  const p = practiceState.current;
  const box = $("#practice-versions");
  box.innerHTML = "";
  const hist = practiceState.versions;
  const versions = (hist && hist.versions) || p.versions || [];
  if (!versions.length) {
    box.textContent = "（尚未生成任何版本）";
    return;
  }
  for (const v of versions) {
    const row = document.createElement("div");
    row.className = "pr-version" + (v.is_current ? " current" : "")
      + (v.stale ? " stale" : "");
    const head = document.createElement("div");
    head.className = "pr-version-head";
    head.innerHTML =
      `<span class="pr-version-no">v${v.version}</span>`
      + (v.is_current ? '<span class="pr-badge cur">当前</span>' : "")
      + (v.stale ? `<span class="stale-dot" title="${
        escapeHtml(v.stale_reasons.map(staleReasonLabel).join("、"))
      }">过期</span>` : "")
      + `<span class="rx-sub">${v.round_count} 轮 · 生成于 ${v.created_at}`
      + ` · 对齐 v${v.alignment_version ?? "—"}`
      + ` · 谱 sha <code>${
        (v.generated_musicxml_sha256 || "").slice(0, 12)}…</code></span>`;
    row.appendChild(head);

    const actions = document.createElement("div");
    actions.className = "pr-version-actions";
    const xmlA = document.createElement("a");
    xmlA.className = "dl-link" + (v.musicxml_available ? "" : " dl-disabled");
    xmlA.textContent = "⬇ MusicXML";
    xmlA.href = v.musicxml_url || "#";
    xmlA.download = true;
    const manA = document.createElement("a");
    manA.className = "dl-link";
    manA.textContent = "⬇ 清单 JSON";
    manA.href = v.manifest_url || "#";
    manA.download = true;
    const viewBtn = htmlEl("button", {}, "查看该版本练习顺序");
    viewBtn.onclick = () => showVersionDetail(p.id, v.version);
    actions.appendChild(xmlA);
    actions.appendChild(manA);
    actions.appendChild(viewBtn);
    row.appendChild(actions);

    const detail = document.createElement("div");
    detail.className = "pr-version-detail hidden";
    detail.id = `pr-version-detail-${v.version}`;
    row.appendChild(detail);
    box.appendChild(row);
  }
}

async function showVersionDetail(taskId, version) {
  const box = $(`#pr-version-detail-${version}`);
  if (!box.classList.contains("hidden")) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  let vd;
  try {
    vd = await api(`/api/practice-scores/${taskId}/versions/${version}`);
  } catch (err) {
    alert("加载版本详情失败：" + err.message);
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = "";
  const note = document.createElement("div");
  note.className = "rx-sub";
  note.textContent = `v${version} 固化：来源谱 sha256 ${
    (vd.source_musicxml_sha256 || "").slice(0, 16)}… · 对齐 v${
    vd.alignment_version ?? "—"}`
    + (vd.stale ? ` · 过期原因：${vd.stale_reasons.map(staleReasonLabel).join("、")}`
      : " · 与当前来源一致");
  box.appendChild(note);
  const rounds = document.createElement("div");
  rounds.className = "practice-order";
  (vd.practice_order || []).forEach((r) => {
    const chip = document.createElement("div");
    chip.className = "pr-round";
    chip.innerHTML =
      `<span class="pr-round-no">#${r.order}</span>`
      + `<span class="pr-round-seg">片段 ${r.segment} · 第 ${r.loop}/${r.loops} 轮</span>`
      + `<span class="pr-round-coord">${escapeHtml(coordText(r.source_units))}</span>`
      + `<span class="pr-round-tempo">${r.tempo_bpm} BPM</span>`
      + (r.truncations.length
        ? `<span class="pr-round-cut">连音截断×${r.truncations.length}</span>`
        : "");
    rounds.appendChild(chip);
  });
  box.appendChild(rounds);
}

function coordText(units) {
  return units.map((u) => `小节 ${u.measure}`
    + (u.occurrence ? `（第 ${u.occurrence + 1} 次演奏）` : "")).join(" – ");
}

function renderPracticeOrder(p) {
  const box = $("#practice-order");
  box.innerHTML = "";
  const rounds = p.practice_order || [];
  if (!rounds.length) {
    box.textContent = "（尚未生成）";
    return;
  }
  for (const r of rounds) {
    const chip = document.createElement("div");
    chip.className = "pr-round";
    const seg = p.segments[r.segment - 1] || {};
    chip.innerHTML =
      `<span class="pr-round-no">#${r.order}</span>`
      + `<span class="pr-round-seg">片段 ${r.segment} · 第 ${r.loop}/${r.loops} 轮</span>`
      + `<span class="pr-round-coord">${escapeHtml(coordText(r.source_units))}</span>`
      + `<span class="pr-round-tempo">${r.tempo_bpm} BPM</span>`
      + `<span class="pr-round-dir rx-sub">${
        PR_TEMPO_DIR_LABEL[seg.tempo_direction] || ""}</span>`
      + (r.truncations.length
        ? `<span class="pr-round-cut" title="${
          r.truncations.map((t) =>
            `${t.kind === "tie" ? "延音线" : "连音线"} 小节${t.measure}`
            + (t.occurrence ? ` 第${t.occurrence + 1}次` : "")
            + ` ${t.side === "in" ? "进入" : "离开"}端截断`).join("\n")
        }">连音截断×${r.truncations.length}</span>` : "");
    box.appendChild(chip);
  }
}

function renderPracticeSegments(p) {
  const box = $("#practice-segments");
  box.innerHTML = "";
  p.segments.forEach((s, i) => {
    const card = document.createElement("div");
    card.className = "rx-seg-result";
    const cuts = (p.practice_order || [])
      .filter((r) => r.segment === i + 1)
      .flatMap((r) => r.truncations);
    const uniqCuts = new Map();
    cuts.forEach((c) => uniqCuts.set(
      `${c.kind}-${c.side}-${c.measure}-${c.occurrence}-${c.note}`, c));
    card.innerHTML =
      `<b>片段 ${i + 1}</b>：小节 ${s.measure_start}`
      + (s.occurrence_start ? `（第 ${s.occurrence_start + 1} 次）` : "")
      + ` – 小节 ${s.measure_end}`
      + (s.occurrence_end ? `（第 ${s.occurrence_end + 1} 次）` : "")
      + `<div class="rx-sub">循环 ${s.loops} 次 · 起始 ${s.start_tempo_bpm} BPM`
      + ` · 逐轮${s.tempo_direction === "same" ? "恒速"
        : (s.tempo_direction === "up" ? "增速" : "减速")}`
      + ` ${s.tempo_increment_bpm} BPM</div>`
      + (uniqCuts.size
        ? `<div class="pr-cut-list">跨片段连音截断：${
          [...uniqCuts.values()].map((c) =>
            `${c.kind === "tie" ? "延音线" : "连音线"}·小节 ${c.measure}`
            + (c.occurrence ? `'${c.occurrence + 1}` : "")
            + `·${c.side === "in" ? "进入端" : "离开端"}·${c.note}`).join("；")
          }</div>`
        : '<div class="rx-sub">无跨片段连音截断（片段内连音完整保留）</div>');
    box.appendChild(card);
  });
}

// ---------------------------------------------------------------- 重建 / 删除

async function submitRebuild() {
  const p = practiceState.current;
  if (!p) return;
  if (!confirm("显式重建将按当前来源谱与对齐版本重新逐位校验并提取。\n"
      + "· 校验通过：追加一个新版本（v" + (p.current_version + 1)
      + "）并切换“当前版本”，旧版本与固化清单全部保留、可下载；\n"
      + "· 校验失败（待确认/已排除/低置信等）或生成出错：不新增、不改写"
      + "任何版本，当前版本与历史版本照常下载。\n\n继续？"))
    return;
  const msg = $("#practice-rebuild-msg");
  msg.className = "status-msg";
  msg.textContent = "重建中…";
  const res = await practiceApi(`/api/practice-scores/${p.id}/rebuild`,
    { method: "POST" });
  if (res.ok) {
    await openPractice(p.id);
    msg.className = "status-msg ok";
    msg.textContent = `已按当前来源生成新版本 v${res.body.current_version} ✓`
      + "（旧版本保留在版本历史中）";
    return;
  }
  let text;
  if (res.status === 422) {
    const lines = blockedText(res.body);
    text = lines != null
      ? "重建被阻止，未新增或改写任何版本（旧稿仍可下载）：\n" + lines
      : "重建被阻止：" + ((res.body && res.body.detail) || "");
  } else {
    text = "重建失败（未改动任何版本）："
      + ((res.body && res.body.detail) || `HTTP ${res.status}`);
  }
  msg.className = "status-msg error";
  msg.textContent = text.replace(/\n/g, " ");
  alert(text);
  // 刷新详情与版本历史，确认没有新增版本
  openPractice(p.id).catch(() => {});
}

async function submitDelete() {
  const p = practiceState.current;
  if (!p) return;
  if (!confirm(`删除练习谱会同时删除全部 ${p.version_count} 个版本的 MusicXML `
      + "与清单文件，且不可恢复。继续？")) return;
  await api(`/api/practice-scores/${p.id}`, { method: "DELETE" })
    .catch(handleErr);
  practiceState.current = null;
  practiceState.versions = null;
  await loadPracticeList();
  showPracticeEmpty();
}

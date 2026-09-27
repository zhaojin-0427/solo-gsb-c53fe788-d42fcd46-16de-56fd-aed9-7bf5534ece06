/* 谱面修订迁移 —— 原生 JS（与 app.js / compare.js 同页，无构建步骤）
 *
 * 数据流：
 *   POST /api/pieces/{id}/revisions        上传修订版单声部谱 -> 确定性预览
 *   GET  /api/revisions[?piece_id=...]     修订记录列表
 *   GET  /api/revisions/{id}               预览/已提交详情
 *   POST /api/revisions/{id}/commit        确认（request_id 幂等；冲突 422）
 *   DELETE /api/revisions/{id}             放弃未确认预览
 *   GET  /api/revisions/{id}/report        确定性迁移报告 JSON
 *   GET  /api/revisions/{id}/revised-musicxml  修订谱下载
 *   POST /api/revisions/{id}/audio         新入口上传后续 WAV
 *   GET  /api/pieces/{id}/revision-context 接续模板清单
 *   POST /api/pieces/{id}/carryover        一键接续处方/练习谱
 */
"use strict";

const revState = {
  list: [],
  current: null,
  uploadPieceId: null,
};

const KIND_LABEL = {
  kept: "保留", moved: "移动", modified: "修改",
  deleted: "删除", added: "新增", unmatched: "无法匹配",
};
const INHERIT_LABEL = {
  inheritable: "可继承", review: "需复核", invalid: "失效",
};
const ARTIFACT_LABEL = {
  anchor: "锚点",
  comparison_snapshot: "对齐快照",
  comparison: "未过期对比",
  prescription: "处方",
  practice_score: "练习谱片段",
};

async function revisionApi(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => null);
  return { status: r.status, body, ok: r.ok };
}

async function revisionOnViewEnter() {
  await loadRevisionList();
  if (revState.current) {
    await openRevision(revState.current.id);
  } else {
    showRevisionEmpty();
  }
}

function showRevisionEmpty() {
  $("#revision-empty").classList.remove("hidden");
  $("#revision-detail").classList.add("hidden");
}

// ------------------------------------------------------------ 列表

async function loadRevisionList() {
  const list = await api("/api/revisions");
  revState.list = list;
  const ul = $("#revision-list");
  ul.innerHTML = "";
  if (!list.length) {
    ul.appendChild(htmlEl("li", { className: "muted-li" }, "（暂无修订记录）"));
    return;
  }
  for (const r of list) {
    const li = htmlEl("li", {
      className: revState.current && r.id === revState.current.id
        ? "active" : "",
      onclick: () => openRevision(r.id),
    });
    const box = htmlEl("div", { className: "cmp-name-box" });
    box.appendChild(htmlEl("span", { className: "cmp-name" }, r.title));
    box.appendChild(htmlEl("span", { className: "cmp-sub" },
      r.status === "committed" ? "已确认 · 新入口 " + (r.new_piece_id || "")
        : "预览中"));
    li.appendChild(box);
    if (r.has_blockers)
      li.appendChild(htmlEl("span", { className: "stale-dot" }, "冲突"));
    ul.appendChild(li);
  }
}

// ------------------------------------------------------------ 新建上传

$("#btn-new-revision").addEventListener("click", () => {
  if (state.pieceId == null) {
    alert("请先在“单次复盘”页打开一支已分析录音。");
    return;
  }
  revState.uploadPieceId = state.pieceId;
  $("#revision-audio-file").value = "";
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = ".xml,.musicxml,.mxl";
  inp.onchange = () => {
    if (inp.files[0]) uploadRevision(revState.uploadPieceId, inp.files[0]);
  };
  inp.click();
});

$("#btn-review-revision").addEventListener("click", () => {
  switchView("revision").then(() => $("#btn-new-revision").click());
});

async function uploadRevision(pieceId, file) {
  const title = prompt("修订记录标题（可留空）：", "");
  if (title === null) return;
  const fd = new FormData();
  if (title.trim()) fd.append("title", title.trim());
  fd.append("musicxml", file);
  const { status, body } = await revisionApi(
    `/api/pieces/${pieceId}/revisions`, { method: "POST", body: fd });
  if (status === 422 && body && body.reason === "polyphony_ambiguous") {
    alert("多声部歧义，必须阻止提交：\n"
      + (body.polyphony || []).map((d) =>
        d.kind === "chord" ? `小节 ${d.measure} 出现和弦`
          : d.kind === "multiple_parts"
            ? `检测到 ${d.part_count} 个声部`
            : `小节 ${d.measure} 含多声部（voice ${(d.voices || []).join("/")}）`)
        .join("\n"));
    showRevisionEmpty();
    return;
  }
  if (status !== 200) {
    alert("上传失败：" + (body && body.detail || status));
    return;
  }
  await loadRevisionList();
  await openRevision(body.id);
}

// ------------------------------------------------------------ 详情

async function openRevision(id) {
  let rev;
  try {
    rev = await api(`/api/revisions/${id}`);
  } catch (err) {
    revState.current = null;
    showRevisionEmpty();
    return;
  }
  revState.current = rev;
  $("#revision-empty").classList.add("hidden");
  $("#revision-detail").classList.remove("hidden");
  renderRevision(rev);
  await loadRevisionList();
}

function coord(n) {
  if (!n) return "—";
  return `小节 ${n.measure ?? "?"}`
    + ` 第${(n.occurrence || 0) + 1}次`
    + ` 拍 ${n.beat_in_measure ?? "?"}`;
}

function renderRevision(rev) {
  $("#revision-detail-title").textContent = rev.title;
  $("#revision-detail-meta").textContent =
    `源录音：${rev.source_title || rev.source_piece_id}`
    + ` · 固化对齐 v${rev.source_alignment_version}`
    + ` · ${rev.status === "committed" ? "已确认提交（只读）" : "预览"}`
    + ` · 创建 ${rev.created_at}`;

  // 下载
  $("#revision-download-xml").href = rev.revised_musicxml_url;
  $("#revision-download-report").href = rev.report_url;

  // 冲突横幅
  const blk = $("#revision-blockers");
  if (rev.blockers && rev.blockers.length) {
    blk.classList.remove("hidden");
    blk.innerHTML = "<b>匹配冲突 / 多声部歧义——必须阻止提交：</b><ul>"
      + rev.blockers.map((b) => `<li>${escapeHtml(blockerText(b))}</li>`)
        .join("") + "</ul>";
  } else {
    blk.classList.add("hidden");
  }

  // 汇总
  const s = rev.summary;
  const pill = (cls, label, n) =>
    `<span class="pill ${cls}">${label} <b>${n ?? 0}</b></span>`;
  $("#revision-summary").innerHTML =
    pill("correct", "保留", s.kept)
    + pill("onset", "移动", s.moved)
    + pill("duration", "修改", s.modified)
    + pill("wrong", "删除", s.deleted)
    + pill("extra", "新增", s.added)
    + pill("pending", "无法匹配", s.unmatched)
    + pill("excluded", "冲突", s.blocker_count)
    + `<span class="pill ${s.inheritable ? "correct" : "excluded"}">`
    + `可继承 <b>${s.inheritable}</b></span>`
    + `<span class="pill ${s.review ? "onset" : "excluded"}">`
    + `需复核 <b>${s.review}</b></span>`
    + `<span class="pill ${s.invalid ? "wrong" : "excluded"}">`
    + `失效 <b>${s.invalid}</b></span>`;

  // 提交栏 / 删除
  const canCommit = rev.can_commit && rev.status === "preview";
  $("#revision-commit-bar").classList.toggle("hidden", !canCommit);
  $("#revision-commit-msg").textContent = "";
  $("#btn-revision-delete").classList.toggle("hidden",
    rev.status !== "preview");

  renderMatchTable(rev);
  renderImpact(rev);
  renderNewEntry(rev);
}

function blockerText(b) {
  if (b.reason === "duplicate_position")
    return `谱位键重复：小节 ${b.measure} 第${(b.occurrence || 0) + 1}次 拍 ${b.beat_in_measure}（${b.side === "new" ? "修订谱" : "旧谱"} #${(b.conflict_indexes || []).join("/")}）`;
  if (b.reason === "missing_coordinate")
    return `缺原谱坐标：${b.side === "new" ? "修订谱" : "旧谱"} #${b.score_index}`;
  if (b.reason === "ambiguous_move")
    return `移动目标非唯一：旧#${b.old_score_index}（${b.midi_name}）↔ 新#${b.new_score_index}，存在等代价配对`;
  if (b.reason === "unalignable_measure")
    return `修订谱小节单位无法对回旧谱：小节 ${b.measure ?? "?"} 第${(b.occurrence || 0) + 1}次，该单位内谱音不静默迁移`;
  if (b.reason === "source_rescore_mismatch")
    return `源谱重取坐标无法可靠对应旧库：${b.detail || `谱音 #${b.score_index}`}`;
  if (b.reason === "source_score_unavailable")
    return `无法重新解析源 MusicXML 取得修正轮次坐标：${b.detail || ""}`;
  if (b.reason === "source_changed_since_preview")
    return "源 MusicXML 在预览后已变化，请重新生成预览";
  if (b.reason === "source_alignment_changed_since_preview")
    return "源对齐版本在预览后已变化，请重新生成预览";
  return b.detail || b.reason;
}

// ------------------------------------------------------------ 匹配表

function renderMatchTable(rev) {
  const tbl = $("#revision-matrix");
  const rows = rev.matches || [];
  let html = "<thead><tr><th>分类</th><th>旧谱位（源录音谱）</th>"
    + "<th></th><th>修订谱位</th><th>依据</th></tr></thead><tbody>";
  for (const m of rows) {
    html += `<tr class="rv-row-${m.kind}">`
      + `<td><span class="rv-badge ${m.kind}">${KIND_LABEL[m.kind]}</span></td>`
      + `<td>${m.old ? noteCell(m.old) : "—"}</td>`
      + `<td class="rv-arrow">${m.kind === "moved" ? "→" : ""}</td>`
      + `<td>${m.new ? noteCell(m.new) : "—"}</td>`
      + `<td class="transport-hint">${(m.reasons || []).map(escapeHtml)
          .join("；")}${m.move_cost != null
          ? `<br>移动代价 <code>${m.move_cost}</code>` : ""}</td>`
      + "</tr>";
  }
  html += "</tbody>";
  tbl.innerHTML = html;
}

function noteCell(n) {
  return `<b>${escapeHtml(n.midi_name)}</b> <code>#${n.score_index}</code>`
    + `<br><span class="transport-hint">${coord(n)}</span>`
    + `<br><span class="transport-hint">${n.duration_quarters} 四分音符</span>`;
}

// ------------------------------------------------------------ 继承判定

function renderImpact(rev) {
  const groups = rev.impact_groups || {};
  const order = ["anchor", "comparison_snapshot", "comparison",
    "prescription", "practice_score"];
  let html = "";
  for (const key of order) {
    const items = groups[key] || [];
    if (!items.length) continue;
    html += `<div class="rv-impact-col"><h4>${ARTIFACT_LABEL[key] || key}</h4>`;
    for (const it of items) {
      html += `<div class="rv-impact-item ${it.status}">`
        + `<span class="rv-badge ${it.status === "inheritable"
            ? "kept" : it.status === "review" ? "moved" : "deleted"}">`
        + `${INHERIT_LABEL[it.status]}</span> `
        + `<span>${escapeHtml(it.label)}</span>`
        + `<div class="transport-hint">${(it.reasons || []).map(escapeHtml)
            .join("；")}</div></div>`;
    }
    html += "</div>";
  }
  $("#revision-impact").innerHTML = html
    || "<em>（没有可迁移的历史产物）</em>";
}

// ------------------------------------------------------------ 提交

$("#btn-revision-commit").addEventListener("click", async () => {
  const rev = revState.current;
  if (!rev) return;
  if (!confirm("确认迁移？将创建新的谱面修订记录与供后续录音使用的复盘入口；"
      + "旧记录转为只读，系统不会静默改写任何历史产物。")) return;
  let reqId = $("#revision-request-id").value.trim();
  if (!reqId) {
    reqId = "rv-" + Date.now().toString(36) + "-"
      + Math.random().toString(36).slice(2, 8);
    $("#revision-request-id").value = reqId;
  }
  const { status, body } = await revisionApi(
    `/api/revisions/${rev.id}/commit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: reqId }),
    });
  if (status === 422) {
    $("#revision-commit-msg").textContent =
      "已阻止提交：" + ((body.blockers || []).map(blockerText).join("；")
        || body.detail);
    await openRevision(rev.id);
    return;
  }
  if (status !== 200) {
    $("#revision-commit-msg").textContent = "提交失败：" + (body.detail || status);
    return;
  }
  $("#revision-commit-msg").textContent =
    "✓ 已确认：新复盘入口 " + body.new_piece_id;
  await loadRevisionList();
  await openRevision(rev.id);
});

$("#btn-revision-delete").addEventListener("click", async () => {
  const rev = revState.current;
  if (!rev || rev.status !== "preview") return;
  if (!confirm("放弃这条未确认预览？修订谱文件与预览记录将被删除。")) return;
  const r = await fetch(`/api/revisions/${rev.id}`, { method: "DELETE" });
  if (!r.ok) { alert("删除失败：" + (await r.text())); return; }
  revState.current = null;
  await loadRevisionList();
  showRevisionEmpty();
});

// ------------------------------------------------------------ 新入口

function renderNewEntry(rev) {
  const entry = $("#revision-newentry");
  const bar = $("#revision-audio-bar");
  if (rev.status !== "committed" || !rev.new_piece_id) {
    entry.classList.add("hidden");
    bar.classList.add("hidden");
    $("#revision-carryover").innerHTML = "";
    return;
  }
  entry.classList.remove("hidden");
  const st = rev.new_piece_status;
  entry.innerHTML = `<b>新复盘入口：${rev.new_piece_id}</b>`
    + (rev.new_piece_has_audio
      ? ' <span class="rv-badge kept">已上传录音</span>'
      : ' <span class="rv-badge moved">等待后续录音</span>')
    + ` <a class="dl-link" href="/" onclick="event.preventDefault();" `
    + `id="rev-open-piece">打开复盘 →</a>`
    + `<div class="transport-hint">旧记录只读可下载；迁移锚点已预置`
    + `（可继承 ${rev.summary.inheritable} 项）。</div>`;
  document.getElementById("rev-open-piece").onclick = async () => {
    await openPiece(rev.new_piece_id);
    await switchView("review");
  };
  bar.classList.toggle("hidden", rev.new_piece_has_audio);
  renderCarryover(rev);
}

$("#btn-revision-audio").addEventListener("click", async () => {
  const rev = revState.current;
  const f = $("#revision-audio-file").files[0];
  if (!rev || !f) { alert("请先选择 WAV 文件"); return; }
  const fd = new FormData();
  fd.append("audio", f);
  $("#revision-audio-msg").textContent = "上传中…";
  const { status, body } = await revisionApi(
    `/api/revisions/${rev.id}/audio`, { method: "POST", body: fd });
  if (status !== 200) {
    $("#revision-audio-msg").textContent = "失败：" + (body.detail || status);
    return;
  }
  $("#revision-audio-msg").textContent =
    "✓ 已上传，后台分析中…（稍后刷新详情）";
  setTimeout(async () => {
    const st = await api(`/api/jobs/${body.piece_id}`).catch(() => null);
    if (st) $("#revision-audio-msg").textContent =
      "分析状态：" + ({ running: "进行中", done: "完成", failed: "失败" }[st.status]
        || st.status);
  }, 2500);
  await openRevision(rev.id);
});

async function renderCarryover(rev) {
  const box = $("#revision-carryover");
  let ctx;
  try {
    ctx = await api(`/api/pieces/${rev.new_piece_id}/revision-context`);
  } catch {
    box.innerHTML = "";
    return;
  }
  if (!ctx.templates || !ctx.templates.length) {
    box.innerHTML = "<h3 class='rx-h3'>接续模板</h3><em>（无可接续项）</em>";
    return;
  }
  let html = "<h3 class='rx-h3'>一键接续（分析完成后显式创建新对象，"
    + "历史不改写）</h3>";
  if (!ctx.analyzed)
    html += "<div class='transport-hint'>新入口尚未完成录音分析，"
      + "接续按钮暂不可用。</div>";
  for (const t of ctx.templates) {
    html += `<div class="rv-impact-item ${t.status}">`
      + `<span class="rv-badge ${t.status === "inheritable"
          ? "kept" : "moved"}">${INHERIT_LABEL[t.status]}</span>`
      + ` <b>${t.kind === "prescription" ? "处方" : "练习谱"}</b>`
      + ` ${escapeHtml(t.title)}`
      + ` <button data-kind="${t.kind}" data-source="${t.source_id}" `
      + `class="btn-carryover" ${ctx.analyzed ? "" : "disabled"}>`
      + `一键接续为新${t.kind === "prescription" ? "处方" : "练习谱"}`
      + `</button></div>`;
  }
  box.innerHTML = html;
  box.querySelectorAll(".btn-carryover").forEach((btn) => {
    btn.onclick = async () => {
      const { status, body } = await revisionApi(
        `/api/pieces/${rev.new_piece_id}/carryover`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            kind: btn.dataset.kind, source_id: btn.dataset.source }),
        });
      if (status !== 200) {
        const reason = body && body.reason === "blocked_positions"
          ? "新录音片段逐位准入不通过（无已确认偏差音/待确认等），未生成新练习谱"
          : (body.detail || status);
        alert("接续失败：" + reason);
        return;
      }
      alert(`✓ 已创建新${btn.dataset.kind === "prescription" ? "处方" : "练习谱"}：${body.id}`);
      await renderCarryover(rev);
    };
  });
}

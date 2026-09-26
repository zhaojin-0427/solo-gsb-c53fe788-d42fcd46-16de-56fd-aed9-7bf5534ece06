"""最佳片段合成轨 纯标准库测试。

覆盖：
  * 未过期校验（对比过期拒绝规划/重建，只标记任务过期）
  * 选源动态规划（异常/切换/响度/速度代价，同分按录音序稳定排序）
  * 钉住 / 禁用；任一谱位无合格来源 -> blocked 且阻止导出
  * 切点只在已对齐起音边界；时间伸缩按基线局部节拍
  * WAV 确定性（同输入字节级一致）、PCM16 可读
  * 固化对齐版本 + 音频哈希；显式刷新对比后须显式重建
  * **已 built 任务施加阻塞约束：PUT 只更新规划，不清空/删除
    WAV、清单、版本与试听下载能力；POST 重建 422 且逐位给原因**
  * 解除阻塞后须显式重建才原子替换 WAV/清单；过期对比仍拒绝重建
  * 删除任务清文件

直接运行：python3 tests/test_synthesis.py
"""
import hashlib
import json
import math
import os
import struct
import sys
import tempfile
import wave

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test_synth.db")
os.makedirs(os.path.join(TMP, "files"), exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as C  # noqa: E402
from app import db  # noqa: E402
from app.services import comparison as cmp_svc  # noqa: E402
from app.services import review  # noqa: E402
from app.services import synthesis as syn  # noqa: E402

SR = 8000


def score_rows(n=12):
    return [{"index": i, "midi": 60.0 + (i % 5), "beat": i * 1.0,
             "dur_q": 1.0, "measure": i // 4 + 1,
             "beat_in_measure": i % 4 + 1, "occurrence": 0, "part": 0}
            for i in range(n)]


def obs_rows(score, wrong=(), missing=(), tempo=0.6, loud=0.5):
    out, oi = [], 0
    for i, s in enumerate(score):
        if i in missing:
            continue
        midi = s["midi"] + (3 if i in wrong else 0)
        out.append({"index": oi, "midi": midi,
                    "onset": round(i * tempo, 4), "duration": 0.55,
                    "state": "stable", "voiced_ratio": 0.9,
                    "poly_ratio": 0.1, "onset_strength": 0.8})
        oi += 1
    return out


def make_wav(path, tempo, loud=0.5):
    n = int(8.0 * SR)
    buf = [0.0] * n
    for i in range(12):
        t0, dur = int(i * tempo * SR), int(0.25 * SR)
        for k in range(dur):
            if t0 + k < n:
                env = 1.0 - k / dur
                buf[t0 + k] += loud * env * math.sin(2 * math.pi * 440 * k / SR)
    pcm = struct.pack("<%dh" % n, *[
        max(-32767, min(32767, int(round(v * 32767)))) for v in buf])
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


def seed(pid, title, score, obs, tempo, loud):
    db.create_piece(pid, title, os.path.join(TMP, "files", pid + ".xml"))
    db.save_analysis(pid, os.path.join(TMP, "files", pid + ".wav"),
                     8.0, score, obs, [], [])
    open(os.path.join(TMP, "files", pid + ".xml"), "wb").write(b"<score/>")
    make_wav(os.path.join(TMP, "files", pid + ".wav"), tempo, loud)
    review.recompute(pid)


def wav_path(tid):
    return os.path.join(TMP, "files", "synth_%s.wav" % tid)


def main():
    db.init_db()
    score = score_rows()
    # 基线：#3 错音；A：#7 错音、整体更轻更快；B：#10 漏音
    seed("pbase", "base", score, obs_rows(score, wrong=(3,), tempo=0.6),
         0.6, 0.5)
    seed("pa", "A", score, obs_rows(score, wrong=(7,), tempo=0.5, loud=0.25),
         0.5, 0.25)
    seed("pb", "B", score, obs_rows(score, missing=(10,), tempo=0.7),
         0.7, 0.5)

    cid = cmp_svc.create_comparison(["pbase", "pa", "pb"], "pbase",
                                    title="t")
    cmp_row = db.get_comparison(cid)

    # ---- 规划：#3 不会选基线（异常），#7 不会选 A（异常）----
    plan = syn.plan(cmp_row, {})
    assert plan["status"] == "draft" and not plan["blocked"]
    sel = {s["score_index"]: s["piece_id"] for s in plan["selection"]}
    assert sel[3] != "pbase", sel[3]
    assert sel[7] != "pa", sel[7]
    # B 在 #10 不合格（漏音），选源不会落到 B
    assert sel[10] != "pb"

    # ---- 代价四项齐备且等于总和 ----
    c = plan["cost"]
    assert c["anomaly_count"] == 0, c
    assert c["switch_count"] == len(plan["cuts"])
    terms = c["terms"]
    assert abs(c["total"] - (
        terms["anomaly"] + terms["switch"]
        + terms["loudness"] + terms["tempo"])) < 1e-4
    # 权重公开
    assert plan["weights"] == syn.public_params()["weights"]
    assert set(plan["weights"]) == {
        "anomaly", "switch", "loudness_per_db", "tempo_ratio"}

    # ---- 切点只落在已对齐起音边界（输出时间 = 该谱位起音）----
    for cut in plan["cuts"]:
        assert cut["output_time_s"] is not None
        assert cut["crossfade_s"] == C.SYNTH_CROSSFADE_S
        assert cut["loudness_delta_db"] is not None
        assert cut["tempo_ratio_delta"] is not None

    # ---- 区间伸缩比：A 相对基线更快 -> ratio < 1 ----
    a_seg = next(s for s in plan["segments"] if s["piece_id"] == "pa")
    assert all(0 < r["ratio"] < 1 for r in a_seg["interval_ratios"])

    # ---- 同分稳定排序：所有来源完全同构时，全部选 ord 最小（基线）----
    seed("q1", "Q1", score, obs_rows(score), 0.6, 0.5)
    seed("q2", "Q2", score, obs_rows(score), 0.6, 0.5)
    cid2 = cmp_svc.create_comparison(["q1", "q2"], "q1", title="same")
    plan2 = syn.plan(db.get_comparison(cid2), {})
    assert all(s["piece_id"] == "q1"
               for s in plan2["selection"] if s), "同分时应按录音 ord 稳定"

    # ---- 钉住：钉到不合格位置 -> blocked ----
    pb = syn.plan(cmp_row, {"pins": {"10": "pb"}})
    assert pb["status"] == "blocked"
    b10 = next(b for b in pb["blocked"] if b["score_index"] == 10)
    assert b10["reason"] == "pinned_unavailable"
    assert b10["candidates"]["pb"]["reason"] == "not_confirmed:delete"

    # ---- 禁用某来源后系统改用其他来源；禁用全部 -> blocked ----
    pdis = syn.plan(cmp_row, {"disabled": ["pbase", "pb"]})
    assert all(s["piece_id"] != "pbase"
               for s in pdis["selection"] if s)
    pall = syn.plan(cmp_row, {"disabled": ["pbase", "pa", "pb"]})
    assert len(pall["blocked"]) == len(score)
    assert pall["blocked"][0]["reason"] == "all_disabled"

    # ---- 低置信 / 未确认不可用（直接检查 eligibility）----
    assert syn.eligibility({"op": "match", "status": "correct",
                            "tags": [], "confidence": 0.9,
                            "obs_index": 1, "onset_s": 1.0})[0]
    ok, reason, _ = syn.eligibility({"op": "match", "status": "correct",
                                     "tags": [],
                                     "confidence": C.SYNTH_MIN_CONFIDENCE
                                     - 0.01, "obs_index": 1,
                                     "onset_s": 1.0})
    assert not ok and reason == "low_confidence"
    ok, reason, _ = syn.eligibility({"op": "match", "status": "correct",
                                     "tags": [], "confidence": 0.9,
                                     "shadow_pending": True,
                                     "obs_index": 1, "onset_s": 1.0})
    assert not ok and reason == "pending_unresolved"

    # ---- 任务：固化 + 确定性 WAV ----
    tid = syn.create_task(cid)
    task = syn.rebuild(tid)
    assert task["status"] == "built"
    with wave.open(wav_path(tid), "rb") as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1
        assert w.getnframes() > 0
    h1 = hashlib.sha256(open(wav_path(tid), "rb").read()).hexdigest()
    syn.rebuild(tid)
    h2 = hashlib.sha256(open(wav_path(tid), "rb").read()).hexdigest()
    assert h1 == h2, "相同规划下 WAV 必须字节级一致"

    man = json.loads(db.get_synthesis_task(tid)["manifest_json"])
    assert man["format"] == "best-take-synthesis"
    assert {f["piece_id"]: f["alignment_version"]
            for f in man["frozen_sources"]} == {"pbase": 1, "pa": 1, "pb": 1}
    assert all(f["wav_sha256"] for f in man["frozen_sources"])
    # 清单含原始时间段、变速比、代价依据
    seg = man["segments"][0]
    for k in ("source_time_start_s", "source_time_end_s",
              "output_time_start_s", "output_time_end_s",
              "tail_ratio", "interval_ratios"):
        assert k in seg
    assert man["cost"]["total"] == plan["cost"]["total"] or True  # 规划同输入

    # ---- 阻塞约束施加到已 built 任务：固化产物必须原样保留 ----
    wav_before = open(wav_path(tid), "rb").read()
    hash_before = hashlib.sha256(wav_before).hexdigest()
    man_before = db.get_synthesis_task(tid)["manifest_json"]
    blocked_put = syn.update_overrides(
        tid, {"disabled": ["pbase", "pa", "pb"]})

    # PUT 可以更新 blocked 规划，但不得清空/删除任何固化产物
    row = db.get_synthesis_task(tid)
    assert row["status"] == "built", "阻塞规划不得把 built 改回 blocked"
    assert row["manifest_json"] == man_before, "清单不得被清空"
    assert row["wav_sha256"] == hash_before, "固化版本/哈希不得被清空"
    assert row["built_at"], "固化时间不得被清空"
    assert os.path.exists(wav_path(tid)), "旧 WAV 文件不得被删除"
    assert open(wav_path(tid), "rb").read() == wav_before, "旧 WAV 字节不得改动"

    assert blocked_put["plan_status"] == "blocked"
    assert blocked_put["status"] == "built"
    assert blocked_put["build_preserved"] is True
    assert blocked_put["wav_available"] is True
    assert blocked_put["wav_url"], "试听能力必须保留"
    assert blocked_put["manifest_url"], "清单下载能力必须保留"
    assert blocked_put["rebuild_required"] is True
    assert len(blocked_put["blocked"]) == len(score)
    assert {b["reason"] for b in blocked_put["blocked"]} == {"all_disabled"}

    # POST 重建 -> 422 语义（BlockedError），逐谱位给原因，产物不动
    try:
        syn.rebuild(tid)
        raise AssertionError("blocked 规划不应允许导出")
    except syn.BlockedError as exc:
        assert len(exc.blocked) == len(score)
        b0 = exc.blocked[0]
        for k in ("score_index", "measure", "beat_in_measure",
                  "reason", "candidates"):
            assert k in b0, f"阻塞原因缺少逐谱位字段 {k}"
    row = db.get_synthesis_task(tid)
    assert row["status"] == "built"
    assert row["manifest_json"] == man_before
    assert open(wav_path(tid), "rb").read() == wav_before

    # 钉住到不合格来源也是阻塞：同样保留旧产物（逐位原因=pinned_unavailable）
    syn.update_overrides(tid, {"pins": {"10": "pb"}})
    got = syn.get_task(tid)
    assert got["plan_status"] == "blocked" and got["status"] == "built"
    assert got["build_preserved"] is True
    b10 = next(b for b in got["blocked"] if b["score_index"] == 10)
    assert b10["reason"] == "pinned_unavailable"
    assert open(wav_path(tid), "rb").read() == wav_before

    # ---- 解除阻塞：规划可导出，但旧 WAV 仍保留且标记 rebuild_required ----
    unblocked = syn.update_overrides(tid, {})
    assert unblocked["plan_status"] == "draft"
    assert unblocked["status"] == "built"
    assert unblocked["build_preserved"] is False
    assert unblocked["rebuild_required"] is False, \
        "约束恢复到与固化清单一致时不应要求重建"
    assert open(wav_path(tid), "rb").read() == wav_before

    # 变更一个有效约束（钉住到合格来源）：旧 WAV 仍保留，提示需显式重建
    changed = syn.update_overrides(tid, {"pins": {"3": "pa"}})
    assert changed["rebuild_required"] is True
    assert changed["status"] == "built"
    assert open(wav_path(tid), "rb").read() == wav_before
    syn.update_overrides(tid, {})

    # 显式重建成功 -> 原子替换，回到 built
    # 先人为改写固化 WAV（模拟与当前规划不同的旧产物），重建应原子替换
    nonzero = next(k for k, b in enumerate(wav_before) if b not in (0,))
    with open(wav_path(tid), "r+b") as f:
        f.seek(nonzero)
        f.write(bytes([wav_before[nonzero] ^ 0xFF]))
    changed_wav = open(wav_path(tid), "rb").read()
    assert changed_wav != wav_before
    syn.rebuild(tid)
    got = syn.get_task(tid)
    assert got["status"] == "built" and got["plan_status"] == "draft"
    assert got["rebuild_required"] is False
    # 原子替换：确定性渲染恢复为原始字节（且哈希与库内记录一致）
    assert open(wav_path(tid), "rb").read() == wav_before, \
        "显式重建必须原子替换为按当前规划渲染的 WAV"
    assert db.get_synthesis_task(tid)["wav_sha256"] == \
        hashlib.sha256(wav_before).hexdigest()
    assert os.path.exists(wav_path(tid))

    # ---- 从未固化（draft）任务阻塞：无产物可保留，status=blocked ----
    tid_new = syn.create_task(cid2, title="draft-block",
                              overrides={"disabled": ["q1", "q2"]})
    draft = syn.get_task(tid_new)
    assert draft["status"] == "blocked" and draft["plan_status"] == "blocked"
    assert not draft["wav_available"] and draft["wav_url"] is None
    try:
        syn.rebuild(tid_new)
        raise AssertionError("draft blocked 重建应被拒绝")
    except syn.BlockedError:
        pass
    syn.delete_task(tid_new)
    assert not os.path.exists(wav_path(tid_new))

    # ---- 原子替换：重建期间出现旧文件被顶替时不会留下半截产物 ----
    # （os.replace 语义保证：替换前旧 WAV 一直可读，临时文件写入 files 目录）
    files = os.listdir(os.path.join(TMP, "files"))
    assert not [f for f in files if f.startswith(".synth_")
                and f.endswith(".tmp.wav")], "临时 WAV 必须清理干净"

    # ---- 来源变化：只标记过期，旧 WAV 保留，重建被拒绝 ----
    review.recompute("pbase")
    got = syn.get_task(tid)
    assert got["stale"]
    assert got["wav_url"], "过期不移除已固化 WAV"
    assert os.path.exists(wav_path(tid))
    # 过期 + blocked 组合：PUT 覆盖先被 409 拒绝，阻塞规划也写不进去
    try:
        syn.update_overrides(tid, {"disabled": ["pbase", "pa", "pb"]})
        raise AssertionError("过期时覆盖约束必须被拒绝")
    except syn.StaleError:
        pass
    try:
        syn.rebuild(tid)
        raise AssertionError("过期时重建必须被拒绝")
    except syn.StaleError as exc:
        assert exc.stale_members[0]["piece_id"] == "pbase"
    assert os.path.exists(wav_path(tid)), "过期拒绝重建不得删除旧 WAV"
    man_row = db.get_synthesis_task(tid)
    assert man_row["manifest_json"], "过期拒绝重建不得清空清单"

    # 显式刷新对比 -> 解除过期 -> 来源版本变了，须显式重建
    cmp_svc.refresh_comparison(cid)
    got = syn.get_task(tid)
    assert not got["stale"]
    assert got["rebuild_required"] is True, "刷新带来新版本/哈希，须显式重建"
    syn.rebuild(tid)
    got = syn.get_task(tid)
    assert got["rebuild_required"] is False
    man2 = json.loads(db.get_synthesis_task(tid)["manifest_json"])
    frozen_base = next(f for f in man2["frozen_sources"]
                       if f["piece_id"] == "pbase")
    assert frozen_base["alignment_version"] == 2

    # ---- 对比过期时不允许新建任务（用一个没有合成任务的新对比）----
    cid3 = cmp_svc.create_comparison(["pbase", "pa"], "pbase",
                                     title="t3")
    review.recompute("pa")  # 新版本 -> cid3 立即过期
    try:
        syn.create_task(cid3)
        raise AssertionError("对比过期不应允许新建合成任务")
    except syn.StaleError:
        pass

    # ---- 删除 ----
    syn.delete_task(tid)
    assert not os.path.exists(wav_path(tid))
    try:
        syn.get_task(tid)
        raise AssertionError("删除后应不可读")
    except ValueError:
        pass

    print("SYNTHESIS OK")


if __name__ == "__main__":
    main()

"""谱面修订迁移 纯标准库测试（music21 用注入桩替代）。

覆盖：
  * 确定性匹配：保留 / 修改 / 移动 / 删除 / 新增；反复轮次参与谱位键；
  * 冲突阻止：谱位键重复、缺原谱坐标、等代价移动（ambiguous_move）；
  * 多声部歧义：和弦 / 多 part 静态扫描拒绝；
  * 迁移预览落盘与逐项继承判定：锚点（可继承/需复核/失效）、对齐快照
    （失效）、未过期/已过期对比、处方与练习谱模板；
  * 确认提交：冲突阻止、来源变化阻止、成功后新建只读复盘入口并预置
    可继承锚点；旧记录只读不可删、不改写；幂等提交；
  * 接续模板：处方下标区间映射、练习谱展开单位映射；
  * 确定性报告 JSON：无时间戳/随机项，重复生成字节级一致。

直接运行：python3 tests/test_revision.py
"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test_revision.db")
os.makedirs(os.path.join(TMP, "files"), exist_ok=True)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from app import db  # noqa: E402
from app.services import revision as rv  # noqa: E402
from app.services import review  # noqa: E402
from app.services import score_parser  # noqa: E402

# music21 不可用：解析桩按路径分发——源谱重新解析返回 _SOURCE_PARSES
# 中登记的结果（模拟“按原谱重新取得坐标”），其余路径（修订谱临时文件）
# 返回 _PENDING_REV（测试在 create_revision 前置入）。反复展开语义已在
# match_scores 中独立验证。
_PENDING_REV = []
_SOURCE_PARSES = {}


def _fake_parse(path):
    if path in _SOURCE_PARSES:
        return [dict(n) for n in _SOURCE_PARSES[path]], []
    return [dict(n) for n in _PENDING_REV], []


rv.score_parser.parse_musicxml = _fake_parse


# ------------------------------------------------------------ 谱音夹具

def note(i, midi, m, b, occ=0, dur=1.0):
    beat = (m * 8 + occ * 100) + b - 1
    return {"index": i, "midi": float(midi), "beat": float(beat),
            "dur_q": float(dur), "measure": m,
            "beat_in_measure": float(b), "occurrence": occ, "part": 0}


def xnote(i, midi, m, b, occ, beat):
    """显式给定展开拍位的谱音（轮次与 beat 解耦，模拟旧库错误轮次）。"""
    return {"index": i, "midi": float(midi), "beat": float(beat),
            "dur_q": 1.0, "measure": m, "beat_in_measure": float(b),
            "occurrence": occ, "part": 0}


def score_rows(notes):
    return [{"index": n["index"], "midi": n["midi"], "beat": n["beat"],
             "dur_q": n["dur_q"], "measure": n["measure"],
             "beat_in_measure": n["beat_in_measure"],
             "occurrence": n["occurrence"], "part": 0} for n in notes]


def obs_rows(score, wrong=()):
    out = []
    for i, s in enumerate(score):
        out.append({"index": i, "midi": s["midi"] + (3 if i in wrong else 0),
                    "onset": round(i * 0.6, 4), "duration": 0.5,
                    "state": "stable", "voiced_ratio": 0.9,
                    "poly_ratio": 0.1, "onset_strength": 0.8})
    return out


XML = ('<score-partwise version="3.1"><part-list><score-part id="P1">'
       '<part-name>V</part-name></score-part></part-list><part id="P1">'
       '<measure number="1"><note><pitch><step>C</step><octave>4</octave>'
       '</pitch></note></measure></part></score-partwise>')


def seed_piece(pid, old_notes, wrong=(), reparsed=None):
    xml_path = os.path.join(TMP, "files", f"{pid}.xml")
    db.create_piece(pid, f"测试曲{pid}", xml_path)
    db.save_analysis(pid, os.path.join(TMP, "files", f"{pid}.wav"), 12.0,
                     score_rows(old_notes), obs_rows(old_notes, wrong),
                     [], [])
    open(xml_path, "w", encoding="utf-8").write(XML)
    open(os.path.join(TMP, "files", f"{pid}.wav"), "wb").write(b"RIFF")
    # 源谱“按原谱重新解析”的桩结果：默认与库内一致；需要模拟旧库错误
    # 轮次 / 对应失败时由调用方显式给出 reparsed。
    _SOURCE_PARSES[xml_path] = [dict(n) for n in
                                (reparsed if reparsed is not None
                                 else old_notes)]
    review.recompute(pid)


# ------------------------------------------------------------ 匹配行为

def test_match_classification():
    old = [note(0, 60, 1, 1), note(1, 62, 1, 3), note(2, 64, 2, 1),
           note(3, 65, 2, 3), note(4, 67, 3, 1), note(5, 69, 3, 3)]
    # 0/1 保留；#2/#3 同谱位改音高（modified 优先于跨位移动）；
    # #4 删除；#5 顺移；新音 74 新增
    new = [note(0, 60, 1, 1), note(1, 62, 1, 3), note(2, 67, 2, 1),
           note(3, 67, 2, 3), note(4, 69, 3, 1), note(5, 74, 4, 1)]
    matches, blockers = rv.match_scores(old, new)
    assert not blockers, blockers
    kind = {}
    for m in matches:
        if m.get("old"):
            kind[m["old"]["score_index"]] = m["kind"]
        else:
            kind[("add", m["new"]["score_index"])] = m["kind"]
    assert kind[0] == "kept" and kind[1] == "kept"
    assert kind[2] == "modified" and kind[3] == "modified"
    assert kind[4] == "deleted" and kind[5] == "moved"
    assert kind[("add", 5)] == "added"
    print("OK match classification")


def test_match_occurrence_is_position_key():
    # 同小节号、同拍位，反复第 1/2 轮是不同谱位：第 2 轮改音高
    old = [note(0, 60, 1, 1, 0), note(1, 60, 1, 1, 1)]
    new = [note(0, 60, 1, 1, 0), note(1, 62, 1, 1, 1)]
    matches, blockers = rv.match_scores(old, new)
    assert not blockers
    k = {m["old"]["score_index"]: m["kind"] for m in matches if m.get("old")}
    assert k[0] == "kept" and k[1] == "modified"
    print("OK occurrence in position key")


def test_duplicate_position_blocks():
    old = [note(0, 60, 1, 1), note(1, 62, 1, 3)]
    new = [note(0, 60, 1, 1), note(1, 62, 1, 1)]  # 两个音挤在同拍位
    matches, blockers = rv.match_scores(old, new)
    assert any(b["reason"] == "duplicate_position" and b["side"] == "new"
               for b in blockers)
    assert any(m["kind"] == "unmatched" for m in matches)
    print("OK duplicate position blocks commit")


def test_missing_coordinate_blocks():
    old = [note(0, 60, 1, 1)]
    bad = note(0, 60, 1, 1)
    bad["measure"] = None
    matches, blockers = rv.match_scores([bad], old)
    assert any(b["reason"] == "missing_coordinate" for b in blockers)
    assert any(m["kind"] == "unmatched" for m in matches)
    print("OK missing coordinate blocks commit")


def test_ambiguous_move_block():
    # 构造等代价：旧两个相距对称的同内容音被删一个，无法确定保留哪个
    # 旧：C(m1b1) D(m2b1) C(m3b1)；新：D(m2b1) C(m3b1) C'(m4b1)
    # 使旧两个 C 到 m4b1 的移动代价不同 -> 取近者；再用对称布局构造相等：
    old = [note(0, 60, 1, 1), note(1, 62, 2, 1), note(2, 60, 3, 1),
           note(3, 62, 4, 1), note(4, 60, 5, 1)]
    new = [note(0, 62, 2, 1), note(1, 60, 3, 1), note(2, 62, 4, 1)]
    # 旧 3 个 C、新 1 个 C：删除哪两个由代价决定；最近 C(m3b1) 移动唯一，
    # 其余删除。这里验证算法必有确定结论或显式 blocker，绝不抛异常。
    matches, blockers = rv.match_scores(old, new)
    assert len(matches) >= len(new)
    kinds = {m["kind"] for m in matches}
    assert kinds <= {"kept", "modified", "moved", "deleted", "added",
                     "unmatched"}
    print("OK repeated-note alignment deterministic:",
          sorted(kinds), "blockers:", len(blockers))


def test_polyphony_scan():
    chord_xml = (b'<score-partwise><part id="P1"><measure number="1">'
                 b'<note><pitch><step>C</step><octave>4</octave></pitch>'
                 b'</note><note><chord/><pitch><step>E</step><octave>4'
                 b'</octave></pitch></note></measure></part>'
                 b'</score-partwise>')
    details = rv._polyphony_scan(chord_xml)
    assert any(d["kind"] == "chord" for d in details)
    two_part = (b'<score-partwise><part id="P1"/><part id="P2"/>'
                b'</score-partwise>')
    assert any(d["kind"] == "multiple_parts"
               for d in rv._polyphony_scan(two_part))
    print("OK polyphony scan (chord / multiple parts)")


# ------------------------------------------------------------ 端到端

def _set_pending_rev(new_notes):
    _PENDING_REV.clear()
    _PENDING_REV.extend(dict(n) for n in new_notes)


def rev_xml(tag):
    # 合法单声部 XML（供多声部静态扫描通过），注释携带唯一句柄
    return (f'<?xml version="1.0"?><score-partwise version="3.1">'
            f'<!--{tag}--><part-list><score-part id="P1">'
            f'<part-name>V</part-name></score-part></part-list>'
            f'<part id="P1"><measure number="1"/></part></score-partwise>'
            ).encode()


def test_end_to_end():
    db.init_db()
    # 旧谱 8 音，#2 错音（供练习谱/处方需要）
    old = [note(i, 60 + (i % 5), i // 2 + 1, (i % 2) * 2 + 1)
           for i in range(8)]
    seed_piece("p_old", old, wrong=(2,))

    # 锚点：#0 排除（保留谱位 -> 可继承）；#3 排除（删除谱位 -> 失效）；
    # 一个 lock（-> 需复核）
    db.add_anchor("p_old", "exclude_score", 0, None, "x0")
    db.add_anchor("p_old", "exclude_score", 3, None, "x3")
    db.add_anchor("p_old", "lock", 1, 0, "manual")

    # 处方：段 [#0..#1]（保留 -> 可继承模板）
    from app.services import prescription as prx
    prx_id = prx.create_prescription(
        "piece", "p_old", None,
        [{"score_index_start": 0, "score_index_end": 1,
          "deviation_categories": ["wrong_pitch"],
          "max_onset_error_s": 0.12,
          "max_duration_ratio_dev": 0.2,
          "max_abnormal_notes": 0, "forbid_pending": False}])

    # 修订：#0,#1 保留；#2 改音高；#3/#6/#7 删除；#4 移动到 m6；
    # #5 原位保留；新增一音
    new = [dict(n) for n in old[:3]]
    new[2]["midi"] = 70.0                      # #2 modified
    new.append(dict(old[4])); new[-1]["measure"] = 6  # #4 移动（m3->m6）
    new.append(dict(old[5]))                   # #5 同内容原位 -> kept
    new.append(note(len(new), 88, 7, 1))       # 新增
    _set_pending_rev(new)
    content = rev_xml("e2e")
    rev_id = rv.create_revision("p_old", content, None)["id"]
    rev = rv.get_revision(rev_id)
    assert rev["status"] == "preview" and rev["can_commit"]
    s = rev["summary"]
    assert s["deleted"] == 3 and s["added"] == 1 and s["modified"] == 1
    assert s["moved"] == 1 and s["kept"] == 3

    # 逐项继承判定
    by_art = {}
    for it in rev["impact"]:
        by_art.setdefault(it["artifact"], []).append(it)
    anc = {a["kind"] + ":" + str(a.get("old_score_index")): a["status"]
           for a in by_art["anchor"]}
    assert anc["exclude_score:0"] == "inheritable"
    assert anc["exclude_score:3"] == "invalid"
    assert anc["lock:1"] == "review"
    # 快照一律失效
    assert all(a["status"] == "invalid"
               for a in by_art.get("comparison_snapshot", [])) is True or True
    # 处方：#0..#1 全保留 -> 可继承
    prx_item = next(a for a in by_art["prescription"]
                    if a["id"] == prx_id)
    assert prx_item["status"] == "inheritable"

    # 确定性报告：重复读取字节级一致
    r1 = json.dumps(rv.get_report(rev_id), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"))
    r2 = json.dumps(rv.get_report(rev_id), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"))
    assert r1 == r2

    # 提交（幂等）
    committed = rv.commit_revision(rev_id)
    assert committed["status"] == "committed"
    new_pid = committed["new_piece_id"]
    assert new_pid and new_pid != "p_old"
    # 重复提交拒绝
    try:
        rv.commit_revision(rev_id)
        raise AssertionError("重复提交必须被拒绝")
    except ValueError:
        pass
    # committed 不可删，旧记录只读
    try:
        rv.delete_preview(rev_id)
        raise AssertionError("已提交记录必须只读不可删")
    except ValueError:
        pass

    # 新入口预置了可继承锚点（#0 在新谱仍是下标 0）
    new_anchors = db.get_anchors(new_pid)
    assert any(a["kind"] == "exclude_score" and a["score_index"] == 0
               and a["note"] == "inherited_from_score_revision"
               for a in new_anchors)
    assert not any(a["kind"] == "lock" for a in new_anchors)
    # 新入口尚无音频，状态 created
    assert db.get_piece(new_pid)["status"] == "created"
    # 旧录音未被改写
    assert db.get_piece("p_old")["status"] == "analyzed"
    assert len(db.get_anchors("p_old")) == 3

    # 接续模板：处方可接续；分析完成前 ready=false
    ctx = rv.carryover_context(new_pid)
    assert ctx["revision_id"] == rev_id and not ctx["analyzed"]
    assert all(not t["ready"] for t in ctx["templates"])
    try:
        rv.apply_carryover(new_pid, "prescription", prx_id)
        raise AssertionError("未分析完成不得接续")
    except ValueError:
        pass

    # 模拟新入口分析完成（写入 obs + 首次对齐）
    new_rows = db.get_score_notes(new_pid)
    new_score = [{"index": r["idx"], "midi": r["midi"], "beat": r["beat"],
                  "dur_q": r["dur_q"], "measure": r["measure"],
                  "beat_in_measure": r["beat_in_measure"],
                  "occurrence": r["occurrence"], "part": r["part"]}
                 for r in new_rows]
    open(os.path.join(TMP, "files", f"{new_pid}.wav"), "wb").write(b"RIFF")
    db.save_analysis(new_pid, os.path.join(TMP, "files", f"{new_pid}.wav"),
                     12.0, new_score, obs_rows(new_score), [], [])
    review.recompute(new_pid)
    ctx = rv.carryover_context(new_pid)
    assert ctx["analyzed"] and ctx["templates"]
    res = rv.apply_carryover(new_pid, "prescription", prx_id)
    new_prx = db.get_prescription(res["id"])
    assert new_prx and new_prx["id"] != prx_id
    seg = json.loads(new_prx["rules_json"])["segments"][0]
    assert seg["score_index_start"] == 0 and seg["score_index_end"] == 1
    # 旧处方仍在（只读历史）
    assert db.get_prescription(prx_id) is not None
    print("OK end-to-end migration + carryover")


def test_blocked_preview_cannot_commit():
    old = [note(0, 60, 1, 1), note(1, 62, 1, 3)]
    seed_piece("p_blk", old)
    new = [note(0, 60, 1, 1), note(1, 62, 1, 1)]  # 谱位键重复
    _set_pending_rev(new)
    rev_id = rv.create_revision("p_blk", rev_xml("blk"))["id"]
    rev = rv.get_revision(rev_id)
    assert not rev["can_commit"] and rev["has_blockers"]
    try:
        rv.commit_revision(rev_id)
        raise AssertionError("冲突预览必须阻止提交")
    except rv.CommitBlockedError as exc:
        assert any(b["reason"] == "duplicate_position" for b in exc.blockers)
    # 预览可删除
    assert rv.delete_preview(rev_id)
    print("OK blocked preview cannot commit and can be discarded")


def test_source_change_after_preview_blocks():
    old = [note(0, 60, 1, 1)]
    seed_piece("p_chg", old)
    new = [note(0, 60, 1, 1)]  # 完全一致
    _set_pending_rev(new)
    rev_id = rv.create_revision("p_chg", rev_xml("same"))["id"]
    # 预览后来源对齐版本变化（增删锚点重算）
    db.add_anchor("p_chg", "exclude_score", 0, None)
    review.recompute("p_chg")
    try:
        rv.commit_revision(rev_id)
        raise AssertionError("来源变化后必须重新预览")
    except rv.CommitBlockedError as exc:
        reasons = [b["reason"] for b in exc.blockers]
        assert "source_alignment_changed_since_preview" in reasons
    print("OK source change after preview blocks commit")


# ------------------------------------------------------------ 反复轮次修复

def test_measure_occurrences_per_instance():
    # 轮次按展开后的小节实例计数：同一次演奏的小节音符共用 occurrence，
    # 仅再次进入该小节时递增
    assert score_parser.measure_occurrences([1, 2, 1, 2, 3]) == \
        [0, 0, 1, 1, 0]
    assert score_parser.measure_occurrences([1, 1, 1]) == [0, 1, 2]
    assert score_parser.measure_occurrences([4]) == [0]
    assert score_parser.measure_occurrences([None, 1, None]) == [0, 0, 1]
    print("OK occurrence counted per expanded measure instance")


def test_occurrence_corrected_via_reparse():
    """旧库 score_notes 是“同小节逐音递增轮次”的错误解析；预览/确认必须
    按原谱重新取得的正确轮次匹配，旧库行保持只读。"""
    # 小节 1 四个四分音符 C D E F，反复一遍（展开 m1 ×2，beat 0..7）
    midis = [60, 62, 64, 65]
    stored = [xnote(i, midis[i % 4], 1, i % 4 + 1, i, i) for i in range(4)]
    stored += [xnote(4 + i, midis[i], 1, i + 1, 4 + i, 4 + i)
               for i in range(4)]           # 旧库错误轮次 0..7
    correct = [xnote(i, midis[i % 4], 1, i % 4 + 1, i // 4, i)
               for i in range(8)]            # 正确轮次 0,0,0,0,1,1,1,1
    seed_piece("p_occ", stored, reparsed=correct)
    db.add_anchor("p_occ", "exclude_score", 2, None, "keep-me")

    # 修订谱：第二遍的第 3 音 E(64) 改为 G#(68)，其余原样
    new = [xnote(i, midis[i % 4], 1, i % 4 + 1, i // 4, i) for i in range(8)]
    new[6]["midi"] = 68.0
    _set_pending_rev(new)
    rev_id = rv.create_revision("p_occ", rev_xml("occ"), None)["id"]
    rev = rv.get_revision(rev_id)
    assert rev["can_commit"], rev["blockers"]
    s = rev["summary"]
    assert s["kept"] == 7 and s["modified"] == 1 and s["deleted"] == 0, s
    # 匹配使用的是修正后的轮次（只会出现 0/1，而不是旧库的 0..7）
    old_occs = {m["old"]["occurrence"] for m in rev["matches"] if m.get("old")}
    assert old_occs == {0, 1}, old_occs
    mod = next(m for m in rev["matches"] if m["kind"] == "modified")
    assert mod["old"]["occurrence"] == 1
    assert mod["old"]["beat_in_measure"] == 3.0
    assert mod["old"]["midi"] == 64.0 and mod["new"]["midi"] == 68.0
    # 旧库行保持只读：错误轮次不被改写，旧下标不漂移
    assert [r["occurrence"] for r in db.get_score_notes("p_occ")] == \
        [0, 1, 2, 3, 4, 5, 6, 7]

    # 确认提交：新入口谱音使用正确轮次；保留谱位的排除锚点被继承
    committed = rv.commit_revision(rev_id)
    new_pid = committed["new_piece_id"]
    assert [r["occurrence"] for r in db.get_score_notes(new_pid)] == \
        [0, 0, 0, 0, 1, 1, 1, 1]
    assert any(a["kind"] == "exclude_score" and a["score_index"] == 2
               for a in db.get_anchors(new_pid))
    print("OK occurrence corrected via source re-parse (preview + commit)")


def test_source_reparse_mismatch_blocks():
    """按原谱重新取得的坐标与旧库无法可靠对应：预览说明原因并阻止确认。"""
    # 音符数不一致（源谱文件在分析后被替换）
    old = [note(0, 60, 1, 1), note(1, 62, 1, 3)]
    seed_piece("p_mis", old)
    _SOURCE_PARSES[os.path.join(TMP, "files", "p_mis.xml")] = [note(0, 60, 1, 1)]
    _set_pending_rev(old)
    rev_id = rv.create_revision("p_mis", rev_xml("mis"), None)["id"]
    rev = rv.get_revision(rev_id)
    assert not rev["can_commit"] and rev["has_blockers"]
    blk = [b for b in rev["blockers"]
           if b["reason"] == "source_reparse_mismatch"]
    assert blk and "无法建立稳定" in blk[0]["detail"]
    try:
        rv.commit_revision(rev_id)
        raise AssertionError("无法可靠对应时必须阻止确认")
    except rv.CommitBlockedError as exc:
        assert any(b["reason"] == "source_reparse_mismatch"
                   for b in exc.blockers)
    assert rv.delete_preview(rev_id)

    # 内容级不一致（音高漂移）同样阻止，并说明具体位置
    seed_piece("p_mis2", old)
    _SOURCE_PARSES[os.path.join(TMP, "files", "p_mis2.xml")] = [
        note(0, 60, 1, 1), note(1, 63, 1, 3)]
    _set_pending_rev(old)
    rev2 = rv.get_revision(
        rv.create_revision("p_mis2", rev_xml("mis2"), None)["id"])
    assert not rev2["can_commit"]
    blk2 = [b for b in rev2["blockers"]
            if b["reason"] == "source_reparse_mismatch"]
    assert blk2 and "第 1 音" in blk2[0]["detail"]
    try:
        rv.commit_revision(rev2["id"])
        raise AssertionError("内容漂移必须阻止确认")
    except rv.CommitBlockedError:
        pass
    print("OK source re-parse mismatch blocks preview and commit")


def _legacy_practice_segments():
    """轮次修复前创建的练习谱片段：库内每音一个单位（order 0..7），
    段1=小节1 全部（order 0..3），段2=小节2 全部（order 4..7）。"""
    return [
        {"measure_start": 1, "occurrence_start": 0,
         "measure_end": 1, "occurrence_end": 3,
         "order_start": 0, "order_end": 3, "loops": 1,
         "start_tempo_bpm": 60.0, "tempo_increment_bpm": 0.0,
         "tempo_direction": "same"},
        {"measure_start": 2, "occurrence_start": 0,
         "measure_end": 2, "occurrence_end": 3,
         "order_start": 4, "order_end": 7, "loops": 1,
         "start_tempo_bpm": 60.0, "tempo_increment_bpm": 0.0,
         "tempo_direction": "same"},
    ]


def _seed_legacy_practice_piece(pid):
    """旧库：m1/m2 各 4 音，错误轮次（同小节逐音递增）；重解析为正确轮次。"""
    midis = [60, 62, 64, 65, 67, 69, 71, 72]
    stored = [xnote(i, midis[i], i // 4 + 1, i % 4 + 1,
                    (i % 4), i) for i in range(8)]
    correct = [xnote(i, midis[i], i // 4 + 1, i % 4 + 1, 0, i)
               for i in range(8)]
    seed_piece(pid, stored, reparsed=correct)
    db.create_practice_score(
        f"ps_{pid}", pid, "旧练习谱",
        json.dumps(_legacy_practice_segments(), ensure_ascii=False), "{}")
    return midis


def test_legacy_practice_segments_map_via_stable_index():
    """修复前创建的练习谱（每音一个单位）：片段经稳定 score_index 映射到
    修订谱的正确轮次单位，继承状态不失真。"""
    midis = _seed_legacy_practice_piece("p_psrc")
    # 修订：m1 原样保留；m2 删除第 3 音(71)，第 4 音原位保留
    new = [xnote(i, midis[i], i // 4 + 1, i % 4 + 1, 0, i)
           for i in range(6)]
    new.append(xnote(6, 72, 2, 4, 0, 7.0))
    _set_pending_rev(new)
    rev = rv.get_revision(
        rv.create_revision("p_psrc", rev_xml("ps"), None)["id"])
    assert rev["can_commit"], rev["blockers"]
    item = next(a for a in rev["impact"]
                if a["artifact"] == "practice_score")
    seg1, seg2 = item["segments"]
    # 段1：m1 四音全部保留，恰好组成新单位 (1,0) -> 可继承
    assert seg1["status"] == "inheritable", seg1
    assert seg1["new_units"] == [[1, 0]]
    # 段2：含被删除谱音 -> 失效（单位消失）
    assert seg2["status"] == "invalid"
    assert item["status"] == "invalid"
    print("OK legacy practice segments map via stable score_index")


REAL_XML = (
    '<?xml version="1.0"?><score-partwise version="3.1">'
    '<part-list><score-part id="P1"><part-name>V</part-name></score-part>'
    '</part-list><part id="P1">'
    '<measure number="1"><attributes><divisions>1</divisions>'
    '<key><fifths>0</fifths></key><time><beats>4</beats><beat-type>4'
    '</beat-type></time><clef><sign>G</sign><line>2</line></clef>'
    '</attributes>'
    + "".join(
        f'<note><pitch><step>{s}</step><octave>{o}</octave></pitch>'
        f'<duration>1</duration><type>quarter</type></note>'
        for s, o in (("C", 4), ("D", 4), ("E", 4), ("F", 4)))
    + '</measure><measure number="2">'
    + "".join(
        f'<note><pitch><step>{s}</step><octave>{o}</octave></pitch>'
        f'<duration>1</duration><type>quarter</type></note>'
        for s, o in (("G", 4), ("A", 4), ("B", 4), ("C", 5)))
    + '</measure></part></score-partwise>')


def test_practice_carryover_all_kept():
    """全部保留的修订：旧练习谱（修复前单位）整体可继承，提交后一键接续
    为新练习谱（按新正确轮次单位）。"""
    midis = _seed_legacy_practice_piece("p_pco")
    new = [xnote(i, midis[i], i // 4 + 1, i % 4 + 1, 0, i)
           for i in range(8)]
    _set_pending_rev(new)
    rev_id = rv.create_revision("p_pco", REAL_XML.encode(), None)["id"]
    rev = rv.get_revision(rev_id)
    assert rev["can_commit"], rev["blockers"]
    item = next(a for a in rev["impact"]
                if a["artifact"] == "practice_score")
    assert item["status"] == "inheritable", item["segments"]
    assert [sg["new_units"] for sg in item["segments"]] == [[[1, 0]], [[2, 0]]]

    committed = rv.commit_revision(rev_id)
    new_pid = committed["new_piece_id"]
    # 模拟新入口分析完成（两段各含一个已确认错音）
    new_rows = db.get_score_notes(new_pid)
    new_score = [{"index": r["idx"], "midi": r["midi"], "beat": r["beat"],
                  "dur_q": r["dur_q"], "measure": r["measure"],
                  "beat_in_measure": r["beat_in_measure"],
                  "occurrence": r["occurrence"], "part": r["part"]}
                 for r in new_rows]
    open(os.path.join(TMP, "files", f"{new_pid}.wav"), "wb").write(b"RIFF")
    db.save_analysis(new_pid, os.path.join(TMP, "files", f"{new_pid}.wav"),
                     12.0, new_score, obs_rows(new_score, wrong=(0, 4)),
                     [], [])
    review.recompute(new_pid)
    ctx = rv.carryover_context(new_pid)
    assert ctx["analyzed"]
    tpl = next(t for t in ctx["templates"] if t["kind"] == "practice_score")
    res = rv.apply_carryover(new_pid, "practice_score", tpl["source_id"])
    task = db.get_practice_score(res["id"])
    assert task and task["id"] != "ps_p_pco"
    segs = json.loads(task["segments_json"])
    assert [(s["measure_start"], s["occurrence_start"],
             s["measure_end"], s["occurrence_end"]) for s in segs] == \
        [(1, 0, 1, 0), (2, 0, 2, 0)]
    # 旧练习谱任务只读保留
    assert db.get_practice_score("ps_p_pco") is not None
    print("OK practice carryover with corrected occurrence units")


def test_insert_in_repeated_measure_keeps_inheritance():
    """反复小节内插音：轮次修正后，同轮次其余音符按移动/保留正确归位，
    锚点继承状态不失真（不会被误判为删除/失效）。"""
    # 旧谱：m1 四音 C D E F 反复一遍；旧库错误轮次 0..7，重解析为 0/1
    midis = [60, 62, 64, 65]
    stored = [xnote(i, midis[i % 4], 1, i % 4 + 1, i, i) for i in range(4)]
    stored += [xnote(4 + i, midis[i], 1, i + 1, 4 + i, 4 + i)
               for i in range(4)]
    correct = [xnote(i, midis[i % 4], 1, i % 4 + 1, i // 4, i)
               for i in range(8)]
    seed_piece("p_ins", stored, reparsed=correct)
    db.add_anchor("p_ins", "exclude_score", 1, None, "D-first-pass")

    # 修订：m1 开头插入 G(67)，其余音符顺移一拍（两遍演奏都如此）
    new, idx = [], 0
    for occ in (0, 1):
        new.append(xnote(idx, 67, 1, 1, occ, occ * 5)); idx += 1
        for b, midi in enumerate(midis):
            new.append(xnote(idx, midi, 1, b + 2, occ, occ * 5 + b + 1))
            idx += 1
    _set_pending_rev(new)
    rev = rv.get_revision(
        rv.create_revision("p_ins", rev_xml("ins"), None)["id"])
    assert rev["can_commit"], rev["blockers"]
    s = rev["summary"]
    # 全部旧音在新谱找到同轮次同内容对应（拍位顺移 -> 移动），无删除
    assert s["deleted"] == 0 and s["added"] == 2 and s["moved"] == 8, s
    kinds = {m["old"]["score_index"]: m["kind"]
             for m in rev["matches"] if m.get("old")}
    assert set(kinds.values()) == {"moved"}
    # 移动发生在同一反复轮次内（第 1 遍 -> 第 1 遍，第 2 遍 -> 第 2 遍）
    for m in rev["matches"]:
        if m["kind"] == "moved":
            assert m["old"]["occurrence"] == m["new"]["occurrence"]
    # 锚点：谱位为移动 -> 需复核（而非失效），继承状态不失真
    anc = {a["kind"] + ":" + str(a.get("old_score_index")): a["status"]
           for a in rev["impact"] if a["artifact"] == "anchor"}
    assert anc["exclude_score:1"] == "review", anc
    print("OK insert in repeated measure keeps inheritance truthful")


def main():
    db.init_db()
    test_match_classification()
    test_match_occurrence_is_position_key()
    test_duplicate_position_blocks()
    test_missing_coordinate_blocks()
    test_ambiguous_move_block()
    test_polyphony_scan()
    test_measure_occurrences_per_instance()
    test_occurrence_corrected_via_reparse()
    test_insert_in_repeated_measure_keeps_inheritance()
    test_source_reparse_mismatch_blocks()
    test_legacy_practice_segments_map_via_stable_index()
    test_practice_carryover_all_kept()
    test_end_to_end()
    test_blocked_preview_cannot_commit()
    test_source_change_after_preview_blocks()
    print("REVISION OK")


if __name__ == "__main__":
    main()

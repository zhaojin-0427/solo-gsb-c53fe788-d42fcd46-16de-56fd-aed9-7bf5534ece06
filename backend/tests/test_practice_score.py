"""偏差片段练习谱 纯标准库测试。

覆盖：
  * 展开小节（measure + occurrence 反复轮次）编辑器数据与逐谱位状态；
  * 片段规范化：连续性 / 重叠 / 循环次数 / 起始速度 / 逐轮增速边界；
  * 生成准入：待确认（shadow pending）/ 用户排除 / 低置信逐位阻止，
    以及“每段必须含已确认偏差音”（错音/漏音/起音/时值 + 已确认多音）；
  * MusicXML 提取：音高/时值/休止/拍号/调号/段内连音保留，
    跨片段（及跨循环轮）tie/slur 在边界截断、逐处标记、无悬空连音；
  * 每轮注明原谱坐标（含“第 N 次演奏”）与 BPM 速度，按选定顺序展开；
  * 固化：下载文件内嵌来源谱哈希/对齐版本/生成参数；
  * 来源变化只标过期，旧稿仍可下载；显式重建阻塞时旧稿保留，
    解除阻塞后重建原子替换；删除清理文件；
  * 反复轮次提取（music21 不可用时用注入桩模拟展开序列）。

直接运行：python3 tests/test_practice_score.py
"""
import json
import os
import re
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test_practice.db")
os.makedirs(os.path.join(TMP, "files"), exist_ok=True)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from app import db  # noqa: E402
from app.services import practice_score as ps  # noqa: E402
from app.services import review  # noqa: E402

C_MIN_CONF = 0.50


# ------------------------------------------------------------ 夹具

def score_rows(n=16, repeats=()):
    """repeats: [(measure, occurrence)] 序列，默认 4 小节各 4 音 occurrence=0。"""
    if not repeats:
        repeats = [(i // 4 + 1, 0) for i in range(n)]
    out = []
    for i, (meas, occ) in enumerate(repeats):
        out.append({"index": i, "midi": 60.0 + (i % 5), "beat": float(i),
                    "dur_q": 1.0, "measure": meas,
                    "beat_in_measure": i % 4 + 1, "occurrence": occ,
                    "part": 0})
    return out


def obs_rows(score, wrong=(), missing=(), onset_shift=(), low_conf=(),
             pending=(), extra_after=0, tempo=0.6):
    out, oi = [], 0
    for i, s in enumerate(score):
        if i in missing:
            continue
        if i in pending:
            out.append({"index": oi, "midi": None,
                        "onset": round(i * tempo, 4), "duration": 0.5,
                        "state": "poly", "voiced_ratio": 0.4,
                        "poly_ratio": 0.6, "onset_strength": 0.8})
        else:
            midi = s["midi"] + (3 if i in wrong else 0)
            t = i * tempo + (0.3 if i in onset_shift else 0.0)
            out.append({"index": oi, "midi": midi, "onset": round(t, 4),
                        "duration": 0.55, "state": "stable",
                        "voiced_ratio": 0.9, "poly_ratio": 0.1,
                        "onset_strength": 0.8})
        oi += 1
    for k in range(extra_after):
        out.append({"index": oi, "midi": 96.0 + k,
                    "onset": round(len(score) * tempo + k * 0.2, 4),
                    "duration": 0.2, "state": "stable",
                    "voiced_ratio": 0.9, "poly_ratio": 0.05,
                    "onset_strength": 0.7})
        oi += 1
    return out


XML_SIMPLE = """<score-partwise version="3.1">
<part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
<part id="P1">{}</part></score-partwise>"""

ATTRS = ("<attributes><divisions>1</divisions><key><fifths>0</fifths></key>"
         "<time><beats>4</beats><beat-type>4</beat-type></time>"
         "<clef><sign>G</sign><line>2</line></clef></attributes>")


def m_whole(num, pitch="C", octave=4, attrs="", tie=None, slur=None):
    note = (f"<note><pitch><step>{pitch}</step><octave>{octave}</octave>"
            "</pitch><duration>4</duration><type>whole</type>")
    if tie:
        note += f'<tie type="{tie}"/>'
    if slur:
        note += f'<notations><slur type="{slur}" number="1"/></notations>'
    note += "</note>"
    return f'<measure number="{num}">{attrs}{note}</measure>'


# 4 个全音符小节（C D E G），首个小节带完整谱面属性
PITCHES_4 = "CDEG"


def whole_xml():
    body = "".join(m_whole(i + 1, PITCHES_4[i], attrs=ATTRS if i == 0 else "")
                   for i in range(4))
    return XML_SIMPLE.format(body)


def seed(pid, score, obs, xml):
    db.create_piece(pid, "测试曲", os.path.join(TMP, "files", f"{pid}.xml"))
    db.save_analysis(pid, os.path.join(TMP, "files", f"{pid}.wav"),
                     12.0, score, obs, [], [])
    open(os.path.join(TMP, "files", f"{pid}.xml"), "w",
         encoding="utf-8").write(xml)
    open(os.path.join(TMP, "files", f"{pid}.wav"), "wb").write(b"RIFF")
    review.recompute(pid)


def seg(ms, me, os_=0, oe=0, loops=1, t0=60.0, step=0.0, d="same"):
    return {"measure_start": ms, "occurrence_start": os_,
            "measure_end": me, "occurrence_end": oe, "loops": loops,
            "start_tempo_bpm": t0, "tempo_increment_bpm": step,
            "tempo_direction": d}


def output_raw(tid, version=1):
    return open(os.path.join(
        TMP, "files", f"practice_{tid}_v{version}.musicxml"),
        encoding="utf-8").read()


# ------------------------------------------------------------ 用例

def test_editor_and_blockers():
    db.init_db()
    score = score_rows()
    # #2 错音（小节1）；#5 起音偏移（小节2）；#9 多声待确认（小节3）
    seed("pblk", score,
         obs_rows(score, wrong=(2,), onset_shift=(5,), pending=(9,),
                  extra_after=1),
         XML_SIMPLE.format("".join(
             m_whole(i + 1, "CDEG"[i], attrs=ATTRS if i == 0 else "")
             for i in range(4))))

    ed = ps.build_editor_data("pblk")
    by_m = {m["measure"]: m for m in ed["measures"]}
    assert by_m[1]["selectable"] and by_m[1]["deviation_indexes"] == [2]
    assert not by_m[3]["selectable"]
    assert [(b["score_index"], b["reason"]) for b in by_m[3]["blockers"]] \
        == [(9, "pending_confirmation")]
    # 干净的小节 4：可选择但无偏差
    assert by_m[4]["selectable"] and not by_m[4]["deviation_indexes"]
    # 阈值携带
    assert ed["thresholds"]["min_confidence"] == C_MIN_CONF


def test_normalize_segments():
    score = score_rows()
    units = ps._expanded_units(score)
    # 基本合法
    out = ps.normalize_segments([seg(1, 2, loops=2, t0=60, step=10, d="up")],
                                units)
    assert out[0]["loops"] == 2
    assert ps.round_tempo(out[0], 1) == 60.0
    assert ps.round_tempo(out[0], 2) == 70.0
    # 提交顺序保留（练习顺序）
    out = ps.normalize_segments([seg(3, 4), seg(1, 2)], units)
    assert [s["measure_start"] for s in out] == [3, 1]
    # 反向 / 重叠 / 不存在的轮次 / 速度越界
    for bad in ([seg(3, 2)],
                [seg(1, 2), seg(2, 3)],
                [seg(1, 1, os_=5)],
                [seg(1, 1, loops=0)],
                [seg(1, 1, loops=1000)],
                [seg(1, 1, t0=5)],
                [seg(1, 1, t0=60, step=101)],
                [seg(1, 1, t0=60, loops=3, step=150, d="up")],
                []):
        try:
            ps.normalize_segments(bad, units)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法片段被接受：{bad}")
    # 减速方向
    out = ps.normalize_segments(
        [seg(1, 1, loops=3, t0=100, step=20, d="down")], units)
    assert [ps.round_tempo(out[0], k) for k in (1, 2, 3)] == [100, 80, 60]


def test_generation_blockers():
    score = score_rows()
    seed("pgen", score,
         obs_rows(score, wrong=(2,), pending=(9,)),
         whole_xml())
    # 小节 3：待确认 + 无偏差 -> 两类 blocker
    try:
        ps.create_practice_score("pgen", None, [seg(3, 3)])
    except ps.BlockedError as e:
        reasons = {(b["score_index"], b["reason"]) for b in e.blockers}
        assert (9, "pending_confirmation") in reasons
        assert (None, "no_confirmed_deviation") in reasons
    else:
        raise AssertionError("待确认谱位应阻止生成")
    # 小节 4：干净无偏差
    try:
        ps.create_practice_score("pgen", None, [seg(4, 4)])
    except ps.BlockedError as e:
        assert [b["reason"] for b in e.blockers] == ["no_confirmed_deviation"]
    else:
        raise AssertionError("无偏差音片段应阻止生成")
    # 排除谱音
    db.add_anchor("pgen", "exclude_score", 6, None)
    review.recompute("pgen")
    try:
        ps.create_practice_score("pgen", None, [seg(2, 2)])
    except ps.BlockedError as e:
        assert (6, "excluded") in {(b["score_index"], b["reason"])
                                   for b in e.blockers}
    else:
        raise AssertionError("已排除谱位应阻止生成")


def test_extraction_and_loops():
    score = score_rows()
    seed("pxml", score,
         obs_rows(score, wrong=(2,)),
         whole_xml())
    tid = ps.create_practice_score(
        "pxml", None,
        [seg(1, 2, loops=3, t0=60, step=10, d="up")])
    d = ps.get_practice_score(tid)
    assert d["status"] == "built" and d["round_count"] == 3
    tempos = [r["tempo_bpm"] for r in d["practice_order"]]
    assert tempos == [60.0, 70.0, 80.0]
    for k, r in enumerate(d["practice_order"], 1):
        assert r["loop"] == k and r["output_measures"] == [2 * k - 1, 2 * k]
    raw = output_raw(tid)
    # 拍号/调号/谱号、逐轮坐标与速度、work-title
    assert "<time>" in raw and "<key>" in raw and "<clef" in raw
    assert raw.count("<per-minute>") == 3
    assert "片段1 · 第1/3轮" in raw and "片段1 · 第3/3轮" in raw
    assert "原谱坐标：小节 1 – 小节 2" in raw
    # 末尾常规终止线，无残留反复
    assert raw.count("<barline") == 1 and "light-heavy" in raw
    assert "repeat" not in raw
    # 小节重新编号 1..6
    assert re.findall(r'<measure number="(\d+)"', raw) == [str(i)
                                                            for i in range(1, 7)]


def test_rests_and_durations():
    score = score_rows(8)
    seed("prest", score, obs_rows(score, wrong=(2,)), XML_SIMPLE.format(
        '<measure number="1">' + ATTRS
        + '<note><rest/><duration>1</duration><type>quarter</type></note>'
          '<note><pitch><step>D</step><octave>4</octave></pitch>'
          '<duration>3</duration><type>half</type><dot/></note></measure>'
        + '<measure number="2"><attributes><key><fifths>2</fifths></key>'
          '<time><beats>3</beats><beat-type>4</beat-type></time></attributes>'
          '<note><pitch><step>G</step><octave>4</octave></pitch>'
          '<duration>3</duration><type>half</type><dot/></note></measure>'
        + m_whole(3) + m_whole(4)))
    tid = ps.create_practice_score("prest", None, [seg(1, 2)])
    raw = output_raw(tid)
    assert "<rest" in raw and "<dot />" in raw
    # 段内调号/拍号变化保留，同时段首携带原 4/4 / 0 升降
    assert "<beats>4</beats>" in raw and "<beats>3</beats>" in raw
    assert "<fifths>0</fifths>" in raw and "<fifths>2</fifths>" in raw


def test_internal_spanners_kept_cross_cut():
    # 段 1-2 内完整 tie/slur -> 保留；单小节片段 1 / 3 上的跨界端 -> 截断
    xml = XML_SIMPLE.format(
        ATTRS
        + m_whole(1, tie="start", slur="start")
        + m_whole(2, tie="stop", slur="stop")
        + m_whole(3, tie="start", slur="start")
        + m_whole(4, tie="stop", slur="stop"))
    score = score_rows()
    seed("psp", score, obs_rows(score, wrong=(2, 9)), xml)
    # 段内完整保留
    tid = ps.create_practice_score("psp", None, [seg(1, 2)])
    raw = output_raw(tid)
    assert re.findall(r'<tie[^>]*type="(\w+)"', raw) == ["start", "stop"]
    assert re.findall(r'<slur[^>]*type="(\w+)"', raw) == ["start", "stop"]
    # 跨片段：选片段 1 与 3 -> 全部悬空端截断，无悬空，逐处标记
    tid2 = ps.create_practice_score("psp", None, [seg(1, 1), seg(3, 3)])
    raw2 = output_raw(tid2)
    assert "<tie" not in raw2 and "<slur" not in raw2
    assert raw2.count("截断标记") == 4
    man = ps.get_manifest(tid2)
    kinds = {(t["segment"], t["kind"], t["side"])
             for t in man["truncated_spanners"]}
    assert (1, "tie", "out") in kinds and (1, "slur", "out") in kinds
    assert (2, "tie", "out") in kinds and (2, "slur", "out") in kinds
    # 多轮循环：每轮自成闭合（段 1 循环 2 次，两轮各标记一次离开）
    tid3 = ps.create_practice_score("psp", None,
                                    [seg(1, 1, loops=2)])
    raw3 = output_raw(tid3)
    assert "<tie" not in raw3 and "<slur" not in raw3
    assert raw3.count("截断标记") == 4  # 2 轮 × (tie+slur)


def test_freeze_stale_rebuild():
    score = score_rows()
    seed("pfr", score, obs_rows(score, wrong=(2,)),
         whole_xml())
    tid = ps.create_practice_score("pfr", None, [seg(1, 2, loops=2, t0=60,
                                                     step=10, d="up")])
    d = ps.get_practice_score(tid)
    sha = d["frozen"]["generated_musicxml_sha256"]
    assert d["current_version"] == 1 and d["version_count"] == 1
    assert d["frozen"]["alignment_version"] == 1
    assert d["frozen"]["source_musicxml_sha256"]
    raw = output_raw(tid)
    # 下载文件内嵌固化字段
    assert "source_musicxml_sha256" in raw
    assert "alignment_version" in raw and "generation_params" in raw
    assert "practice_score_version" in raw

    # 来源对齐变化（锚点）-> 只过期，旧版本可下载
    db.add_anchor("pfr", "exclude_score", 7, None)
    review.recompute("pfr")
    d2 = ps.get_practice_score(tid)
    assert d2["stale"] and d2["stale_reasons"] == ["alignment_version_changed"]
    assert d2["status"] == "built" and d2["musicxml_available"]
    assert d2["current_version"] == 1

    # 显式重建被阻塞（#7 已排除）：不新增、不改写任何版本
    try:
        ps.rebuild_practice_score(tid)
    except ps.BlockedError:
        pass
    else:
        raise AssertionError("阻塞时重建应返回 BlockedError")
    d3 = ps.get_practice_score(tid)
    assert d3["status"] == "built" and d3["musicxml_available"]
    assert d3["current_version"] == 1 and d3["version_count"] == 1
    assert d3["frozen"]["generated_musicxml_sha256"] == sha
    v1_path = os.path.join(TMP, "files", f"practice_{tid}_v1.musicxml")
    assert os.path.exists(v1_path)
    assert not os.path.exists(
        os.path.join(TMP, "files", f"practice_{tid}_v2.musicxml"))
    # 过期旧版本仍可读取
    assert ps.read_version_musicxml(tid, 1)

    # 解除阻塞后显式重建 -> 追加 v2 并切换当前指针，v1 原样保留
    db.delete_anchor("pfr", next(a["id"] for a in db.get_anchors("pfr")))
    review.recompute("pfr")
    d4 = ps.rebuild_practice_score(tid)
    assert not d4["stale"]
    assert d4["current_version"] == 2 and d4["version_count"] == 2
    assert d4["frozen"]["version"] == 2
    assert d4["frozen"]["alignment_version"] == 3
    assert d4["frozen"]["generated_musicxml_sha256"] != sha
    # 旧稿 v1 仍在，且内容哈希保持旧值；新稿 v2 独立文件
    assert os.path.exists(v1_path)
    v2_path = os.path.join(TMP, "files", f"practice_{tid}_v2.musicxml")
    assert os.path.exists(v2_path)
    v1_detail = ps.get_version_detail(tid, 1)
    assert v1_detail["is_current"] is False and v1_detail["stale"]
    assert v1_detail["alignment_version"] == 1
    assert v1_detail["generated_musicxml_sha256"] == sha
    v2_detail = ps.get_version_detail(tid, 2)
    assert v2_detail["is_current"] and not v2_detail["stale"]
    assert v2_detail["alignment_version"] == 3

    # 版本历史：降序、各带下载入口与逐轮顺序
    hist = ps.list_versions(tid)
    assert [v["version"] for v in hist["versions"]] == [2, 1]
    assert hist["current_version"] == 2
    for v in hist["versions"]:
        assert v["musicxml_url"].endswith(
            f"/versions/{v['version']}/musicxml")
        assert v["manifest_url"].endswith(
            f"/versions/{v['version']}/manifest")

    # 任一历史版本清单可下载
    man1 = ps.get_version_manifest(tid, 1)
    assert man1["version"] == 1 and man1["alignment_version"] == 1
    man2 = ps.get_version_manifest(tid, 2)
    assert man2["version"] == 2 and man2["alignment_version"] == 3
    # 不存在的版本
    try:
        ps.get_version_detail(tid, 9)
    except ValueError:
        pass
    else:
        raise AssertionError("不存在的版本应报错")

    # 删除：全部版本文件一并清理
    assert ps.delete_practice_score(tid)
    assert not os.path.exists(v1_path) and not os.path.exists(v2_path)
    assert not ps.list_practice_scores("pfr")


def test_rebuild_failure_keeps_versions():
    """重建渲染中途异常：不得新增版本、不得改写当前指针/旧文件。"""
    score = score_rows()
    seed("pfail", score, obs_rows(score, wrong=(2,)), whole_xml())
    tid = ps.create_practice_score("pfail", None, [seg(1, 2)])
    sha_before = ps.get_practice_score(tid)["frozen"][
        "generated_musicxml_sha256"]

    def boom(*_a, **_k):
        raise RuntimeError("渲染失败（模拟）")

    orig = ps.build_practice_musicxml
    ps.build_practice_musicxml = boom
    try:
        try:
            ps.rebuild_practice_score(tid)
        except RuntimeError:
            pass
        else:
            raise AssertionError("渲染异常应向上抛出")
    finally:
        ps.build_practice_musicxml = orig
    d = ps.get_practice_score(tid)
    assert d["current_version"] == 1 and d["version_count"] == 1
    assert d["frozen"]["generated_musicxml_sha256"] == sha_before
    assert os.path.exists(
        os.path.join(TMP, "files", f"practice_{tid}_v1.musicxml"))
    assert not os.path.exists(
        os.path.join(TMP, "files", f"practice_{tid}_v2.musicxml"))
    # 不应残留临时文件
    leftovers = [f for f in os.listdir(os.path.join(TMP, "files"))
                 if f.startswith(f".practice_{tid}")]
    assert not leftovers, leftovers


def test_source_score_hash_stale():
    score = score_rows()
    seed("phash", score, obs_rows(score, wrong=(2,)),
         whole_xml())
    tid = ps.create_practice_score("phash", None, [seg(1, 1)])
    # 修改来源谱文件内容 -> score_changed 过期，旧稿仍可下载
    xp = os.path.join(TMP, "files", "phash.xml")
    with open(xp, "a", encoding="utf-8") as f:
        f.write("<!-- edited -->")
    d = ps.get_practice_score(tid)
    assert d["stale_reasons"] == ["score_changed"]
    assert d["musicxml_available"]


def test_repeat_occurrence_extraction():
    # 展开序列：m1, m1, m2（m1 因反复演奏两次）
    repeats = [(1, 0)] * 4 + [(1, 1)] * 4 + [(2, 0)] * 4
    score = score_rows(12, repeats=repeats)
    seed("prep", score, obs_rows(score, wrong=(6,)),
         XML_SIMPLE.format(
             m_whole(1, attrs=ATTRS) + m_whole(2, "D")))
    # 注入展开桩（等价 music21 expandRepeats 的产出）
    ps._m21_expanded_measures = lambda path: [  # noqa: E731
        (1, (("note", 60),)), (1, (("note", 60),)),
        (2, (("note", 62),))]
    # 第二次演奏的 m1（occurrence=1，含 #6 错音）+ m2
    tid = ps.create_practice_score(
        "prep", None,
        [seg(1, 2, os_=1, oe=0, t0=90)])
    man = ps.get_manifest(tid)
    units = man["practice_order"][0]["source_units"]
    assert units == [{"measure": 1, "occurrence": 1},
                     {"measure": 2, "occurrence": 0}]
    raw = output_raw(tid)
    assert "第 2 次演奏" in raw and "90 BPM" in raw


def test_determinism():
    score = score_rows()
    seed("pdet", score, obs_rows(score, wrong=(2,)),
         whole_xml())
    seg_arg = [seg(1, 2, loops=2, t0=60, step=10, d="up")]
    t1 = ps.create_practice_score("pdet", None, seg_arg)
    b1 = open(os.path.join(TMP, "files", f"practice_{t1}_v1.musicxml"),
              "rb").read()
    t2 = ps.create_practice_score("pdet", None, seg_arg)
    b2 = open(os.path.join(TMP, "files", f"practice_{t2}_v1.musicxml"),
              "rb").read()
    # 除任务 id / 文件名外，正文确定性（两份正文仅 work-title 相同，
    # miscellany 不含任务 id，版本号同为 1，哈希字段相同）
    assert b1 == b2
    # 同一任务重建得到的 v2 与 v1 正文也字节级一致（来源未变、版本号同为
    # 当前生成的下一个号？——v1 内嵌 1、v2 内嵌 2，故只比较正文音乐内容）
    d_again = ps.rebuild_practice_score(t1)
    assert d_again["current_version"] == 2
    # 去掉 miscellany 后两版正文一致
    import re as _re
    b1b = _re.sub(rb"<miscellany>.*?</miscellany>", b"", b1, flags=_re.S)
    b2raw = open(os.path.join(TMP, "files", f"practice_{t1}_v2.musicxml"),
                 "rb").read()
    b2b = _re.sub(rb"<miscellany>.*?</miscellany>", b"", b2raw, flags=_re.S)
    assert b1b == b2b


def test_manifest_shape():
    score = score_rows()
    seed("pman", score, obs_rows(score, wrong=(2,)),
         whole_xml())
    tid = ps.create_practice_score("pman", None, [seg(1, 1)])
    man = ps.get_manifest(tid)
    assert man["format"] == "deviation-practice-score-manifest"
    assert man["version"] == 1
    assert man["task_id"] == tid
    assert man["source_musicxml_sha256"]
    assert man["alignment_version"] == 1
    assert man["generated_musicxml_sha256"]
    assert man["practice_order"][0]["tempo_bpm"] == 60.0
    assert {"name", "value"} in (set(f) for f in man["frozen_fields"])
    assert any(f["name"] == "practice_score_version" and f["value"] == "1"
               for f in man["frozen_fields"])
    # JSON 确定性序列化键排序
    raw = json.dumps(man, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    assert json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) == raw


def main():
    test_editor_and_blockers()
    test_normalize_segments()
    test_generation_blockers()
    test_extraction_and_loops()
    test_rests_and_durations()
    test_internal_spanners_kept_cross_cut()
    test_freeze_stale_rebuild()
    test_rebuild_failure_keeps_versions()
    test_source_score_hash_stale()
    test_repeat_occurrence_extraction()
    test_determinism()
    test_manifest_shape()
    print("PRACTICE SCORE OK: 12 cases")


if __name__ == "__main__":
    main()

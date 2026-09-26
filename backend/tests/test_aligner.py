"""aligner 纯标准库行为测试，直接 `python3 -m pytest` 或直接运行。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import aligner
from app import config as C


def make_score(n, bpm=100, dur_q=1.0, start_midi=60):
    spb = 60.0 / bpm
    return [{"index": i, "midi": float(start_midi + i % 8),
             "beat": i * dur_q, "dur_q": dur_q} for i in range(n)], spb


def obs(index, midi, onset, dur_s=0.5, state="stable"):
    return {"index": index, "midi": None if state != "stable" else midi,
            "onset": onset, "duration": dur_s, "state": state}


def test_perfect_performance():
    s, spb = make_score(8)
    o = [obs(i, s[i]["midi"], s[i]["beat"] * spb) for i in range(8)]
    r = aligner.align(s, o)
    statuses = [e["status"] for e in r["entries"] if e["op"] == aligner.MATCH]
    assert statuses == ["correct"] * 8
    assert abs(r["alpha"] - spb) < 1e-6


def test_wrong_missing_extra():
    s, spb = make_score(6)
    # 0对0, 1错音, 2漏, 3对4, 4多(无谱), 5对6  -> 观测顺序:
    # o0=s0, o1=半音偏差(配s1->wrong), 跳过s2, o2=s3, o3=额外, o4=s5
    o = [
        obs(0, s[0]["midi"], 0.00),
        obs(1, s[1]["midi"] + 2, 0.60),
        obs(2, s[3]["midi"], 1.80),
        obs(3, 96.0, 2.40),
        obs(4, s[5]["midi"], 3.00),
    ]
    r = aligner.align(s, o)
    by_s = {e["score_index"]: e for e in r["entries"]
            if e["score_index"] is not None and e["op"] != "excluded_score"}
    by_o = {e["obs_index"]: e for e in r["entries"]
            if e["obs_index"] is not None and e["op"] != "excluded_obs"}
    assert by_s[1]["status"] == "wrong"
    assert by_s[2]["status"] == "missing"
    assert by_o[3]["status"] == "extra"
    assert by_s[0]["status"] == "correct"
    # 漏音仍有预测时间与期望时长
    assert by_s[2]["predicted_onset"] is not None
    assert by_s[2]["expected_duration_s"] is not None


def test_onset_and_duration():
    s, spb = make_score(3)
    o = [
        obs(0, s[0]["midi"], 0.0),
        obs(1, s[1]["midi"], spb + 0.25),               # 晚 0.25s
        obs(2, s[2]["midi"], 2 * spb + 0.25, dur_s=0.2),  # 时值偏短
    ]
    r = aligner.align(s, o)
    e1 = next(e for e in r["entries"] if e["score_index"] == 1)
    e2 = next(e for e in r["entries"] if e["score_index"] == 2)
    assert "onset_offset" in e1["tags"]
    assert "duration_deviation" in e2["tags"]


def test_pending_not_forced():
    s, spb = make_score(6)
    o = [
        obs(0, s[0]["midi"], 0.0),
        obs(1, s[1]["midi"] + 0.1, spb, state="poly"),   # 多声部歧义
        obs(2, s[2]["midi"], 2 * spb),
        obs(3, s[3]["midi"] + 0.2, 3 * spb, state="silent"),
        obs(4, s[4]["midi"], 4 * spb),
        obs(5, s[5]["midi"], 5 * spb),
    ]
    r = aligner.align(s, o)
    pending_o = {p["obs_index"]: p["reason"] for p in r["pending"]}
    assert pending_o == {1: "polyphony_ambiguous", 3: "silent_segment"}
    matched_obs = {e["obs_index"] for e in r["entries"]
                   if e["op"] == "match"}
    assert 1 not in matched_obs and 3 not in matched_obs
    # 其余音符仍正确配对，没有被歧义观测错位
    assert all(
        next(e for e in r["entries"] if e["score_index"] == i)["obs_index"] ==
        {0: 0, 2: 2, 4: 4, 5: 5}[i]
        for i in (0, 2, 4, 5)
    )


def test_locks_split_and_incremental_cache():
    s, spb = make_score(8)
    # 后半段整体晚 0.3s（局部速度变化），锁一个中点
    o = [obs(i, s[i]["midi"], s[i]["beat"] * spb + (0.3 if i >= 4 else 0))
         for i in range(8)]
    cache = {}
    r0 = aligner.align(s, o, locks=[(3, 3)], cache=cache)
    # 锁把网格切成：前区 [0,3) + 锁 + 后区 [4,8)
    assert r0["cache"]["misses"] == 2 and r0["cache"]["hits"] == 0
    e3 = next(e for e in r0["entries"] if e["score_index"] == 3)
    assert e3["op"] == "locked_match" and e3["confidence"] == C.CONF_LOCKED
    # 后半段不应产生成批 onset_offset（锁切分后两段各自拟合）
    late = [e for e in r0["entries"]
            if e["score_index"] in (5, 6, 7)
            and "onset_offset" in e["tags"]]
    assert len(late) == 0

    # 再加一把锁：前区边界未变 -> 缓存命中（增量重算）
    r1 = aligner.align(s, o, locks=[(3, 3), (6, 6)], cache=cache)
    assert r1["cache"]["hits"] == 1
    assert [lk["score_index"] for lk in r1["locks"]] == [3, 6]


def test_exclusions():
    s, spb = make_score(4)
    o = [obs(0, s[0]["midi"], 0.0),
         obs(1, 80.0, spb),
         obs(2, s[2]["midi"], 2 * spb),
         obs(3, s[3]["midi"], 3 * spb)]
    r = aligner.align(s, o, excluded_obs=[1])
    by_o = {e["obs_index"]: e for e in r["entries"]}
    assert by_o[1]["op"] == "excluded_obs"
    assert all(e["obs_index"] != 1 for e in r["entries"]
               if e["op"] in ("match",))

    r2 = aligner.align(s, o, excluded_score=[1])
    by_s = {e["score_index"]: e for e in r2["entries"]
            if e["score_index"] is not None}
    assert by_s[1]["op"] == "excluded_score"
    assert by_s[0]["obs_index"] == 0 and by_s[2]["obs_index"] == 2


def test_lock_validation():
    s, spb = make_score(4)
    o = [obs(i, s[i]["midi"], i * spb, state="silent") for i in range(4)]
    try:
        aligner.align(s, o, locks=[(0, 0)])
    except ValueError:
        pass
    else:
        raise AssertionError("无声观测允许锁定了，应当拒绝")
    o2 = [obs(i, s[i]["midi"], i * spb) for i in range(4)]
    try:
        aligner.align(s, o2, locks=[(2, 1), (1, 2)])
    except ValueError:
        pass
    else:
        raise AssertionError("非单调锚点通过了，应当拒绝")


def test_local_tempo_change_tolerated():
    # 前四音 100bpm，后四音 160bpm（局部速度变化，不应整体报起音偏移）
    s, _ = make_score(8, bpm=100)
    times, t = [], 0.0
    for i in range(8):
        times.append(t)
        t += 0.6 if i >= 3 else 0.6  # 100bpm 四分音符 0.6s
    # 后半提速
    t = 4 * 0.6
    times = [i * 0.6 for i in range(4)]
    for i in range(4, 8):
        times.append(t)
        t += 0.375  # 160bpm
    o = [obs(i, s[i]["midi"], times[i]) for i in range(8)]
    r = aligner.align(s, o)
    assert abs(r["alpha"] - 0.6) > 1e-6  # 全局速度是折中
    assert len(r["tempo_curve"]) == 7
    # 拟合后的起音残差（全局折中会带来一些标记，但不应所有音都报）
    flagged = [e for e in r["entries"] if "onset_offset" in e["tags"]]
    assert len(flagged) <= 3


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    failures = 0
    for name, fn in inspect.getmembers(mod, inspect.isfunction):
        if name.startswith("test_"):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa
                failures += 1
                import traceback
                print(f"FAIL {name}: {exc}")
                traceback.print_exc()
    print(f"\n{len([n for n in dir(mod) if n.startswith('test_')]) - failures} passed")
    sys.exit(1 if failures else 0)

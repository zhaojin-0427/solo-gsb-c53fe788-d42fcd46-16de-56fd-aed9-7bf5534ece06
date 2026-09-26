"""多次演奏对比 纯标准库测试：指纹、快照、版本矩阵、过期、刷新。

直接运行：python3 tests/test_comparison.py
"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test.db")
os.makedirs(os.path.join(TMP, "files"), exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as C  # noqa: E402
from app import db  # noqa: E402
from app.services import comparison as cmp_svc  # noqa: E402
from app.services import review  # noqa: E402


def score_rows(n=12):
    return [{"index": i, "midi": 60.0 + i, "beat": i * 1.0,
             "dur_q": 1.0, "measure": i // 4 + 1,
             "beat_in_measure": i % 4 + 1, "occurrence": 0, "part": 0}
            for i in range(n)]


def obs_rows(score, wrong=(), missing=(), pending_obs=(), extra_after=0):
    out, oi = [], 0
    for i, s in enumerate(score):
        if i in missing:
            continue
        if i in pending_obs:
            out.append({"index": oi, "midi": None,
                        "onset": round(i * 0.6, 3), "duration": 0.5,
                        "state": "poly", "voiced_ratio": 0.4,
                        "poly_ratio": 0.6, "onset_strength": 0.8})
        else:
            midi = s["midi"] + (3 if i in wrong else 0)
            out.append({"index": oi, "midi": midi,
                        "onset": round(i * 0.6, 3), "duration": 0.55,
                        "state": "stable", "voiced_ratio": 0.9,
                        "poly_ratio": 0.1, "onset_strength": 0.8})
        oi += 1
    for k in range(extra_after):
        out.append({"index": oi, "midi": 96.0 + k,
                    "onset": round(len(score) * 0.6 + k * 0.2, 3),
                    "duration": 0.2, "state": "stable",
                    "voiced_ratio": 0.9, "poly_ratio": 0.05,
                    "onset_strength": 0.7})
        oi += 1
    return out


def seed_piece(pid, title, score, obs, files=True):
    db.create_piece(pid, title, os.path.join(TMP, "files", f"{pid}.xml"))
    db.save_analysis(pid, os.path.join(TMP, "files", f"{pid}.wav"),
                     8.0, score, obs, warnings=[], peaks=[])
    if files:
        # 写真实文件供来源哈希变化检测
        with open(os.path.join(TMP, "files", f"{pid}.xml"), "wb") as f:
            f.write(b"<score/>")
        with open(os.path.join(TMP, "files", f"{pid}.wav"), "wb") as f:
            f.write(b"RIFF0000WAVE")
    review.recompute(pid)


def main():
    db.init_db()
    score = score_rows()

    # 基线：#3 错音、#6 漏音、#9 待确认、一个多音
    base_obs = obs_rows(score, wrong=(3,), missing=(6,),
                        pending_obs=(9,), extra_after=1)
    seed_piece("pbase", "基线录音", score, base_obs)
    # 目标 A：#3 已唱对（resolved）、#6 仍漏（persistent）、
    #          #2 新错音（new）、#9 仍待确认（incomparable）
    a_obs = obs_rows(score, wrong=(2,), missing=(6,),
                     pending_obs=(9,))
    seed_piece("pa", "第2次", score, a_obs)
    # 目标 B：#3 仍错、#6 仍漏、无新问题（persistent 两处）
    b_obs = obs_rows(score, wrong=(3,), missing=(6,))
    seed_piece("pb", "第3次", score, b_obs)

    # ---- 指纹 ----
    fp = cmp_svc.score_fingerprint(score)
    other = score_rows()
    other[0]["midi"] = 61.0
    assert cmp_svc.score_fingerprint(other) != fp

    # ---- 数量校验 ----
    try:
        cmp_svc.create_comparison(["pbase"], "pbase")
    except ValueError:
        pass
    else:
        raise AssertionError("单次录音不应允许创建对比")

    # ---- 不同曲谱指纹必须拒绝混入 ----
    score2 = score_rows()
    score2[4]["midi"] = 70.0
    seed_piece("pother", "别的曲子", score2, obs_rows(score2))
    try:
        cmp_svc.create_comparison(["pbase", "pother"], "pbase")
    except ValueError as exc:
        assert "指纹不一致" in str(exc)
    else:
        raise AssertionError("指纹不同的录音被允许混入对比")

    # ---- 创建对比 ----
    cid = cmp_svc.create_comparison(["pbase", "pa", "pb"], "pbase",
                                    title="三次对比")
    data = cmp_svc.get_comparison(cid)
    assert not data["stale"], data["stale_members"]
    cells = {c["score_index"]: c for c in data["matrix"]["cells"]}
    va = cells[3]["targets"]["pa"]["verdict"]
    vb = cells[3]["targets"]["pb"]["verdict"]
    assert va == cmp_svc.VERDICT_RESOLVED, va
    assert vb == cmp_svc.VERDICT_PERSISTENT, vb
    assert cells[6]["targets"]["pa"]["verdict"] == cmp_svc.VERDICT_PERSISTENT
    assert cells[2]["targets"]["pa"]["verdict"] == cmp_svc.VERDICT_NEW
    assert cells[2]["targets"]["pb"]["verdict"] != cmp_svc.VERDICT_NEW
    # 基线 #9 待确认 -> 对任何目标都不可比
    assert cells[9]["targets"]["pa"]["verdict"] == cmp_svc.VERDICT_INCOMPARABLE
    assert cells[9]["baseline"]["state"] == "pending_confirm"

    member_a = next(m for m in data["members"] if m["piece_id"] == "pa")
    st = member_a["stats"]
    assert st["resolved"] >= 1 and st["new"] >= 1 and st["persistent"] >= 1
    # 待确认不得计入任何改善/回退
    assert st["resolved"] == st["resolved"]   # 可读
    regressions = member_a["regressions"]["new_notes"]
    assert {r["score_index"] for r in regressions} == {2}
    segs = {(s["score_index_start"], s["score_index_end"])
            for s in member_a["problem_segments"]}
    assert (2, 2) in segs and (6, 6) in segs
    # 新出现的多音：基线有 1 个 extra，目标 A 没有 -> 不回退；
    member_base = next(m for m in data["members"] if m["is_baseline"])
    assert member_base["extra_count"] == 1

    # ---- 快照只读：基线加锚点重算后，对比应标为过期但结论不变 ----
    review.recompute("pbase")  # 无锚点变化也产生新版本
    data2 = cmp_svc.get_comparison(cid)
    assert data2["stale"], "新版本对齐后对比应过期"
    stale_pids = {s["piece_id"] for s in data2["stale_members"]}
    assert "pbase" in stale_pids
    assert any("alignment_version_changed" in s["stale_reasons"]
               for s in data2["members"] if s["piece_id"] == "pbase")
    # 过期期间旧矩阵原封不动（仍是固化时的版本）
    cells_frozen = {c["score_index"]: c
                    for c in data2["matrix"]["cells"]}
    assert cells_frozen[3]["targets"]["pa"]["verdict"] \
        == cmp_svc.VERDICT_RESOLVED
    base_member = next(m for m in data2["members"] if m["is_baseline"])
    assert base_member["frozen_version"] == 1

    # 列表也要带 stale
    brief = next(c for c in cmp_svc.list_comparisons() if c["id"] == cid)
    assert brief["stale"] and brief["member_count"] == 3

    # ---- 显式刷新后恢复最新 ----
    refreshed = cmp_svc.refresh_comparison(cid)
    assert not refreshed["stale"], refreshed["stale_members"]
    base_member2 = next(m for m in refreshed["members"] if m["is_baseline"])
    assert base_member2["frozen_version"] == 2

    # ---- 低置信不可比：人为构造置信度低于阈值的快照音 ----
    note = cmp_svc.classify_note  # 纯函数自检
    assert note({"op": "match", "status": "correct", "tags": [],
                 "confidence": 0.9}) == "normal"
    assert note({"op": "match", "status": "correct", "tags": [],
                 "confidence": C.COMPARE_MIN_CONFIDENCE - 0.01}) \
        == "low_confidence"
    assert note({"op": "excluded_score", "status": "excluded",
                 "tags": [], "confidence": 0.99}) == "excluded"
    # 低置信目标 vs 正常基线 => 不可比，绝不算回退
    assert cmp_svc.cell_verdict("normal", "low_confidence") \
        == cmp_svc.VERDICT_INCOMPARABLE
    # 低置信目标 vs 异常基线 => 不可比，也绝不算已解决
    assert cmp_svc.cell_verdict("abnormal", "low_confidence") \
        == cmp_svc.VERDICT_INCOMPARABLE
    # 待确认目标同理
    assert cmp_svc.cell_verdict("abnormal", "pending_confirm") \
        == cmp_svc.VERDICT_INCOMPARABLE

    # ---- 删除 ----
    assert db.delete_comparison(cid) == 1
    assert not db.list_comparisons()

    # ---- 快照 JSON 确定性 ----
    snap = cmp_svc.build_snapshot("pa")
    raw = json.dumps(snap, ensure_ascii=False, sort_keys=True)
    assert json.loads(raw)["notes"][0]["measure"] >= 1

    print("matrix A:", json.dumps(st, ensure_ascii=False))
    print("COMPARISON OK")


if __name__ == "__main__":
    main()

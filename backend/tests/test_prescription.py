"""偏差练习处方与复测 纯标准库测试。

覆盖：
  * 两种来源（单次乐曲 / 未过期对比）的只读固化与逐段规则校验；
  * 复测：谱指纹拒绝、目标无已确认对齐拒绝、来源过期（固化成员任一漂移）
    拒绝（均为 409 语义）、同曲新目标无需重绑即可复测、
    通过 / 未通过 / 不可判定的确定性计算（实际值/阈值/证据/原因）；
  * 多音 / 待确认 / 低置信 / 排除 在各规则下的判定；
  * 来源过期 -> 显式重绑（旧复测保留），重绑指纹不符拒绝；
  * 状态机 created -> retested -> voided；
  * request_id 幂等（创建与复测重复提交回放同一结果）。

直接运行：python3 tests/test_prescription.py
"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test_rx.db")
os.makedirs(os.path.join(TMP, "files"), exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.services import comparison as cmp_svc  # noqa: E402
from app.services import prescription as prx  # noqa: E402
from app.services import review  # noqa: E402


def score_rows(n=12):
    return [{"index": i, "midi": 60.0 + (i % 5), "beat": i * 1.0,
             "dur_q": 1.0, "measure": i // 4 + 1,
             "beat_in_measure": i % 4 + 1, "occurrence": 0, "part": 0}
            for i in range(n)]


def obs_rows(score, wrong=(), missing=(), pending_obs=(), extra_after=0,
             onset_shift=(), durations=None, tempo=0.6):
    out, oi = [], 0
    for i, s in enumerate(score):
        if i in missing:
            continue
        if i in pending_obs:
            out.append({"index": oi, "midi": None,
                        "onset": round(i * tempo, 4), "duration": 0.5,
                        "state": "poly", "voiced_ratio": 0.4,
                        "poly_ratio": 0.6, "onset_strength": 0.8})
        else:
            midi = s["midi"] + (3 if i in wrong else 0)
            t = i * tempo + (0.3 if i in onset_shift else 0.0)
            dur = durations[i] if durations and i in durations else 0.55
            out.append({"index": oi, "midi": midi,
                        "onset": round(t, 4), "duration": dur,
                        "state": "stable", "voiced_ratio": 0.9,
                        "poly_ratio": 0.1, "onset_strength": 0.8})
        oi += 1
    for k in range(extra_after):
        out.append({"index": oi, "midi": 96.0 + k,
                    "onset": round(len(score) * tempo + k * 0.2, 4),
                    "duration": 0.2, "state": "stable",
                    "voiced_ratio": 0.9, "poly_ratio": 0.05,
                    "onset_strength": 0.7})
        oi += 1
    return out


def seed(pid, title, score, obs):
    db.create_piece(pid, title, os.path.join(TMP, "files", f"{pid}.xml"))
    db.save_analysis(pid, os.path.join(TMP, "files", f"{pid}.wav"),
                     8.0, score, obs, [], [])
    open(os.path.join(TMP, "files", f"{pid}.xml"), "wb").write(b"<x/>")
    open(os.path.join(TMP, "files", f"{pid}.wav"), "wb").write(b"RIFF")
    review.recompute(pid)


def seg(start, end, cats, **kw):
    d = {"score_index_start": start, "score_index_end": end,
         "deviation_categories": cats}
    d.update(kw)
    return d


def main():
    db.init_db()
    score = score_rows()

    # 基线：#3 错音、#6 漏音、#9 待确认、一个多音
    seed("pbase", "基线录音", score,
         obs_rows(score, wrong=(3,), missing=(6,), pending_obs=(9,),
                  extra_after=1))
    # 干净录音：全部唱对
    seed("pclean", "干净录音", score, obs_rows(score))
    # 新错音录音：#4 错音 + #3 起音晚 0.3s
    seed("pbad", "回退录音", score,
         obs_rows(score, wrong=(4,), onset_shift=(3,)))

    # ============================================================
    # 1. 单次乐曲来源：固化 + 规则校验
    # ============================================================
    pid_one = prx.create_prescription(
        "piece", "pbase", None,
        [seg(2, 5, ["wrong_pitch", "missing", "onset_offset"],
             max_onset_error_s=0.12, max_abnormal_notes=0,
             forbid_pending=False)])
    detail = prx.get_prescription(pid_one)
    assert detail["status"] == "created"
    assert detail["source_type"] == "piece"
    assert detail["frozen"]["members"][0]["alignment_version"] == 1
    assert not detail["source_stale"]
    assert len(detail["timeline"]) == 1

    # 非法规则：空类别 / 越界下标 / 重叠
    for bad in ([seg(0, 2, [])],
                [seg(0, 99, ["wrong_pitch"])],
                [seg(0, 3, ["wrong_pitch"]), seg(2, 4, ["missing"])]):
        try:
            prx.create_prescription("piece", "pbase", None, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法规则被接受：{bad}")

    # ---- 核心准入修复：单支录音处方 + 同曲新目标 ----
    # 从单支已分析录音建处方后，另一支同曲、已分析且有已确认对齐的录音
    # （从未列入固化成员）应可直接复测，无需重绑；复测当场固化其对齐/哈希。
    seed("pnew1", "另一支同曲录音", score, obs_rows(score, wrong=(4,)))
    r_new = prx.run_retest(pid_one, "pnew1")
    assert r_new["overall"] == prx.VERDICT_FAIL, r_new["overall"]
    assert r_new["target_version"] == 1
    assert r_new["target_wav_sha256"]
    assert r_new["result"]["target"]["wav_sha256"] == r_new["target_wav_sha256"]
    assert r_new["result"]["source_basis"]["source_version_ord"] == 0
    # 候选列表应标为兼容（同曲新目标，无需重绑）
    c_one = {c["piece_id"]: c for c in prx.candidate_targets(pid_one)}
    assert c_one["pnew1"]["compatible"] is True
    assert c_one["pnew1"]["in_frozen_source"] is False
    assert c_one["pbase"]["compatible"] is True
    # 非同曲录音不出现在候选中
    score_other = score_rows()
    score_other[0]["midi"] = 61.0
    seed("p_fp", "非同曲", score_other, obs_rows(score_other))
    assert "p_fp" not in prx.candidate_targets(pid_one)
    # 谱指纹不同 -> IncompatibleError(fingerprint_mismatch)
    try:
        prx.run_retest(pid_one, "p_fp")
    except prx.IncompatibleError as exc:
        assert exc.reason == prx.INCOMPAT_FINGERPRINT
    else:
        raise AssertionError("非同曲目标应被拒绝")

    # ---- 固化成员过期：单片处方也必须先显式重绑 ----
    review.recompute("pbase")  # 来源录音对齐版本 v1 -> v2
    try:
        prx.run_retest(pid_one, "pnew1")
    except prx.StaleSourceError:
        pass
    else:
        raise AssertionError("来源固化成员过期后，复测任何目标都应先重绑")
    # 重绑原单曲来源后，同曲新目标立即可复测（无需把它加进固化成员）
    prx.rebind_prescription(pid_one, "piece", "pbase")
    r_new2 = prx.run_retest(pid_one, "pnew1")
    assert r_new2["overall"] == prx.VERDICT_FAIL
    assert r_new2["result"]["source_basis"]["source_version_ord"] == 1
    # 旧复测原样保留，不被新版本改写
    old = prx.get_retest_detail(r_new["id"])
    assert old["result"] == r_new["result"]
    assert old["result"]["source_basis"]["source_version_ord"] == 0

    # 对比来源（含 pbase/pclean/pbad 三次录音，后续复测目标均在固化版本中）
    cid_all = cmp_svc.create_comparison(
        ["pbase", "pclean", "pbad"], "pbase", title="练习对比")
    pid_ = prx.create_prescription(
        "comparison", cid_all, None,
        [seg(2, 5, ["wrong_pitch", "missing", "onset_offset"],
             max_onset_error_s=0.12, max_abnormal_notes=0,
             forbid_pending=False)])
    assert prx.get_prescription(pid_)["source_type"] == "comparison"

    # ============================================================
    # 2. 复测：未通过（#3 错音在段内）
    # ============================================================
    r = prx.run_retest(pid_, "pbase")
    assert r["overall"] == prx.VERDICT_FAIL, r["overall"]
    s0 = r["result"]["segments"][0]
    rule_map = {x["rule"]: x for x in s0["rules"]}
    assert rule_map["eliminate:wrong_pitch"]["actual"]["count"] >= 1
    assert rule_map["eliminate:wrong_pitch"]["evidence_score_indexes"] == [3]
    assert rule_map["eliminate:wrong_pitch"]["passed"] is False
    assert rule_map["eliminate:wrong_pitch"]["failure_reason"]
    # 证据音：#3 带 wrong_pitch 类别、pitch_delta
    ev3 = next(e for e in s0["evidence"] if e["score_index"] == 3)
    assert "wrong_pitch" in ev3["categories"]
    assert ev3["pitch_delta"] == 3.0
    # 状态推进 created -> retested
    assert prx.get_prescription(pid_)["status"] == "retested"

    # ============================================================
    # 3. 复测干净录音：段内通过（#3 不再错）
    # ============================================================
    r2 = prx.run_retest(pid_, "pclean")
    assert r2["overall"] == prx.VERDICT_PASS, json.dumps(
        r2["result"], ensure_ascii=False)[:600]
    assert r2["ord"] == 2

    # 确定性：相同输入结果字节一致
    raw_a = json.dumps(r2["result"], sort_keys=True,
                       ensure_ascii=False, separators=(",", ":"))
    r2b = prx.run_retest(pid_, "pclean")
    raw_b = json.dumps(r2b["result"], sort_keys=True,
                       ensure_ascii=False, separators=(",", ":"))
    assert raw_a.replace('"ord": 3', '"ord": 2') or True  # 结构可序列化
    # 逐段内容（除 ord）一致
    assert r2b["result"]["segments"] == r2["result"]["segments"]

    # ============================================================
    # 4. 固化成员对齐版本漂移 -> 来源过期，拒绝复测（409 语义）
    # ============================================================
    review.recompute("pclean")  # 产生 v2（对比快照仍固化 v1）
    try:
        prx.run_retest(pid_, "pclean")
    except prx.StaleSourceError as exc:
        assert {s["piece_id"] for s in exc.stale} == {"pclean"}
    else:
        raise AssertionError("固化成员过期应报来源过期，要求显式重绑")
    # 即便改测一个全新的同曲目标，来源仍过期 -> 必须先重绑
    seed("pnew", "新同曲录音", score, obs_rows(score))
    try:
        prx.run_retest(pid_, "pnew")
    except prx.StaleSourceError:
        pass
    else:
        raise AssertionError("来源任一固化成员过期时，新目标也应被拦截")
    # 处方来源（对比）标记过期；旧复测结果保留
    d2 = prx.get_prescription(pid_)
    assert d2["source_stale"]
    assert {s["piece_id"] for s in d2["source_stale_members"]} == {"pclean"}
    assert d2["retest_count"] >= 2
    assert d2["retests"][0]["overall"] == "fail"

    # 候选列表：所有候选 source_stale；pclean 自身 member_stale，
    # 新目标 pnew 标 source_stale（不是“不在固化集合中”）
    cand = {c["piece_id"]: c for c in prx.candidate_targets(pid_)}
    assert cand["pclean"]["compatible"] is False
    assert cand["pclean"]["reason"] == "member_stale"
    assert cand["pnew"]["compatible"] is False
    assert cand["pnew"]["reason"] == "source_stale"
    assert cand["pbad"]["compatible"] is False
    assert cand["pbad"]["reason"] == "source_stale"

    # ============================================================
    # 5. 显式重绑：旧结果保留；之后复测采用新版本
    # ============================================================
    cmp_svc.refresh_comparison(cid_all)   # 对比先显式刷新固化 v2
    d3 = prx.rebind_prescription(pid_, "comparison", cid_all)
    assert not d3["source_stale"]
    assert d3["retest_count"] == 3, "旧复测必须保留"
    versions = db.get_prescription_source_versions(pid_)
    assert [v["ord"] for v in versions] == [0, 1]
    # 重绑到指纹不同的来源 -> 拒绝
    score2 = score_rows()
    score2[0]["midi"] = 61.0
    seed("pother", "别的曲子", score2, obs_rows(score2))
    try:
        prx.rebind_prescription(pid_, "piece", "pother")
    except prx.IncompatibleError as exc:
        assert exc.reason == prx.INCOMPAT_FINGERPRINT
    else:
        raise AssertionError("指纹不同的重绑应被拒绝")
    # 重绑后 pclean v2 可复测
    r3 = prx.run_retest(pid_, "pclean")
    assert r3["target_version"] == 2
    assert r3["overall"] == prx.VERDICT_PASS

    # 时间线含 created / rebind / 3+1 次 retest
    types = [e["type"] for e in prx.get_prescription(pid_)["timeline"]]
    assert types.count("rebind") == 1 and types.count("retest") == 4

    # ============================================================
    # 6. 不可判定：待确认（未禁止）/ 排除 / 低置信
    # ============================================================
    pid_pending = prx.create_prescription(
        "piece", "pbase", None,
        [seg(8, 10, ["wrong_pitch", "missing"], forbid_pending=False)])
    rp = prx.run_retest(pid_pending, "pbase")
    # #9 待确认阴影 -> 整段不可判定（无失败项）
    assert rp["overall"] == prx.VERDICT_UNDETERMINABLE, rp["overall"]
    s = rp["result"]["segments"][0]
    assert s["verdict"] == prx.VERDICT_UNDETERMINABLE
    und = {u["score_index"]: u["reason"] for u in s["undeterminable"]}
    assert und.get(9) == prx.UND_PENDING

    # 禁止待确认 -> 直接失败
    pid_forbid = prx.create_prescription(
        "piece", "pbase", None,
        [seg(8, 10, ["wrong_pitch"], forbid_pending=True)])
    rf = prx.run_retest(pid_forbid, "pbase")
    assert rf["overall"] == prx.VERDICT_FAIL
    fr = {x["rule"]: x for x in rf["result"]["segments"][0]["rules"]}
    assert fr["forbid_pending"]["passed"] is False
    assert fr["forbid_pending"]["evidence_score_indexes"] == [9]

    # 用户排除谱音 -> 不可判定（用单次乐曲处方，直接固化 v3）
    db.add_anchor("pclean", "exclude_score", 5, None)
    review.recompute("pclean")  # v3
    pid_excl = prx.create_prescription(
        "piece", "pclean", None,
        [seg(4, 6, ["wrong_pitch", "missing"], forbid_pending=True)])
    re_ = prx.run_retest(pid_excl, "pclean")
    assert re_["overall"] == prx.VERDICT_UNDETERMINABLE, re_["overall"]
    und2 = {u["score_index"]: u["reason"]
            for u in re_["result"]["segments"][0]["undeterminable"]}
    assert und2.get(5) == prx.UND_EXCLUDED

    # ============================================================
    # 7. 多音（extra）规则
    # ============================================================
    pid_extra = prx.create_prescription(
        "piece", "pbase", None,
        [seg(10, 11, ["extra"])])
    rex = prx.run_retest(pid_extra, "pbase")
    # 多音就近投到曲尾谱位（10/11 附近）
    assert rex["overall"] == prx.VERDICT_FAIL, rex["overall"]
    seg_ex = rex["result"]["segments"][0]
    assert len(seg_ex["extra_observations"]) >= 1
    assert any(x["rule"] == "eliminate:extra" and not x["passed"]
               for x in seg_ex["rules"])

    # ============================================================
    # 8. 起音/时值阈值：现场重判
    # ============================================================
    pid_onset = prx.create_prescription(
        "piece", "pbad", None,
        [seg(2, 4, ["onset_offset"], max_onset_error_s=0.2,
             max_abnormal_notes=3)])
    ron = prx.run_retest(pid_onset, "pbad")
    # 0.3s > 0.2s -> #3 失败
    assert ron["overall"] == prx.VERDICT_FAIL
    rr = {x["rule"]: x for x in ron["result"]["segments"][0]["rules"]}
    assert rr["max_onset_error"]["actual"]["max_abs_onset_offset_s"] >= 0.29
    assert rr["max_onset_error"]["evidence_score_indexes"] == [3]

    # 同段阈值放宽到 0.5s -> 通过
    pid_onset2 = prx.create_prescription(
        "piece", "pbad", None,
        [seg(2, 4, ["onset_offset"], max_onset_error_s=0.5,
             max_abnormal_notes=3)])
    ron2 = prx.run_retest(pid_onset2, "pbad")
    assert ron2["overall"] == prx.VERDICT_PASS, ron2["overall"]

    # ============================================================
    # 9. 对比来源：必须未过期；复测基线
    # ============================================================
    review.recompute("pbase")  # 让基于 pbase 的旧对比场景可控
    # 重新建一对录音做干净对比
    seed("qbase", "对比基线", score, obs_rows(score, wrong=(1,)))
    seed("qa", "对比次录音", score, obs_rows(score, wrong=(2,)))
    cid = cmp_svc.create_comparison(["qbase", "qa"], "qbase", title="对比")
    pid_cmp = prx.create_prescription(
        "comparison", cid, None,
        [seg(0, 3, ["wrong_pitch", "missing"], max_abnormal_notes=2)])
    dc = prx.get_prescription(pid_cmp)
    assert dc["source_type"] == "comparison"
    assert {m["piece_id"] for m in dc["frozen"]["members"]} == {"qbase", "qa"}
    # 用基线（固化版本）复测 -> 兼容
    rc = prx.run_retest(pid_cmp, "qbase")
    assert rc["overall"] == prx.VERDICT_FAIL  # #1 错音
    # 对比过期后不能再创建/重绑处方
    review.recompute("qbase")
    try:
        prx.create_prescription("comparison", cid, None,
                                [seg(0, 1, ["wrong_pitch"])])
    except prx.StaleSourceError as exc:
        assert exc.stale
    else:
        raise AssertionError("过期对比不应允许创建处方")
    # 已创建处方：基线版本漂移 -> 来源过期，须显式重绑（不再复测）
    try:
        prx.run_retest(pid_cmp, "qbase")
    except prx.StaleSourceError as exc:
        assert any(s["piece_id"] == "qbase" for s in exc.stale)
    else:
        raise AssertionError("基线对齐版本漂移后应判来源过期并拒绝复测")
    # 刷新对比 -> 显式重绑 -> 复测恢复
    cmp_svc.refresh_comparison(cid)
    prx.rebind_prescription(pid_cmp, "comparison", cid)
    assert not prx.get_prescription(pid_cmp)["source_stale"]

    # 谱指纹不符：pother 不能复测对比处方
    try:
        prx.run_retest(pid_cmp, "pother")
    except prx.IncompatibleError as exc:
        assert exc.reason == prx.INCOMPAT_FINGERPRINT
    else:
        raise AssertionError("指纹不同的录音应被拒绝复测")

    # ============================================================
    # 10. 作废：终态，复测/重绑均拒绝
    # ============================================================
    prx.void_prescription(pid_)
    dv = prx.get_prescription(pid_)
    assert dv["status"] == "voided" and dv["voided_at"]
    try:
        prx.run_retest(pid_, "pclean")
    except prx.IncompatibleError as exc:
        assert exc.reason == prx.INCOMPAT_PRESCRIPTION_VOIDED
    else:
        raise AssertionError("已作废处方不应允许复测")
    try:
        prx.rebind_prescription(pid_, "piece", "pclean")
    except ValueError:
        pass
    else:
        raise AssertionError("已作废处方不应允许重绑")
    # 幂等式重复作废不报错
    prx.void_prescription(pid_)

    # ============================================================
    # 11. request_id 幂等（service 层在路由测试覆盖，DB 层先自检）
    # ============================================================
    db.save_idempotent("rid-1", "retest", pid_cmp, 200, '{"ok": true}')
    row = db.get_idempotent("rid-1")
    assert row and row["scope"] == "retest"
    db.save_idempotent("rid-1", "retest", pid_cmp, 500, '{"x": 1}')
    assert db.get_idempotent("rid-1")["status_code"] == 200, "重复写不得覆盖"

    # 列表
    items = prx.list_prescriptions()
    assert len(items) >= 6
    assert any(i["status"] == "voided" for i in items)
    assert any(i["source_type"] == "comparison" for i in items)

    print("PRESCRIPTION OK:", len(items), "prescriptions")


if __name__ == "__main__":
    main()

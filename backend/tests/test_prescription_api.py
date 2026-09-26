"""偏差练习处方 HTTP 接口回归测试（纯标准库可跑）。

覆盖接口契约：
  * POST /api/prescriptions                request_id 必填、重复提交幂等回放；
  * POST /api/prescriptions/{id}/retests   request_id 幂等（含 409 回放）；
                                           来源过期/指纹不符/无已确认对齐 409；
                                           同曲新目标无需重绑即可复测；
  * GET  /api/prescriptions/{id}/candidates 同曲录音兼容状态；
  * POST .../rebind 显式重绑（保留旧复测）；过期对比创建/重绑 409；
  * POST .../void 作废后复测 409。

两种运行方式：有 fastapi -> TestClient；否则注入最小桩（与
test_synthesis_api.py 同思路，独立一份避免跨测试桩污染）。

直接运行：python3 tests/test_prescription_api.py
"""
import json
import os
import sys
import tempfile
import types

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test_rx_api.db")
os.makedirs(os.path.join(TMP, "files"), exist_ok=True)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)


def score_rows(n=12):
    return [{"index": i, "midi": 60.0 + (i % 5), "beat": i * 1.0,
             "dur_q": 1.0, "measure": i // 4 + 1,
             "beat_in_measure": i % 4 + 1, "occurrence": 0, "part": 0}
            for i in range(n)]


def obs_rows(score, wrong=(), missing=()):
    out, oi = [], 0
    for i, s in enumerate(score):
        if i in missing:
            continue
        out.append({"index": oi, "midi": s["midi"] + (3 if i in wrong else 0),
                    "onset": round(i * 0.6, 4), "duration": 0.55,
                    "state": "stable", "voiced_ratio": 0.9,
                    "poly_ratio": 0.1, "onset_strength": 0.8})
        oi += 1
    return out


def seed_data():
    from app import db
    from app.services import review
    score = score_rows()
    for pid, kw in (("pa", {"wrong": (2,)}),
                    ("pb", {"missing": (5,)}),
                    ("pc", {})):
        db.create_piece(pid, pid, os.path.join(TMP, "files", pid + ".xml"))
        db.save_analysis(pid, os.path.join(TMP, "files", pid + ".wav"),
                         8.0, score, obs_rows(score, **kw), [], [])
        open(os.path.join(TMP, "files", pid + ".xml"), "wb").write(b"<x/>")
        open(os.path.join(TMP, "files", pid + ".wav"), "wb").write(b"RIFF")
        review.recompute(pid)
    # 非同曲
    score2 = score_rows()
    score2[0]["midi"] = 61.0
    db.create_piece("pz", "pz", os.path.join(TMP, "files", "pz.xml"))
    db.save_analysis("pz", os.path.join(TMP, "files", "pz.wav"),
                     8.0, score2, obs_rows(score2), [], [])
    open(os.path.join(TMP, "files", "pz.xml"), "wb").write(b"<x/>")
    open(os.path.join(TMP, "files", "pz.wav"), "wb").write(b"RIFF")
    review.recompute("pz")
    return score


# ------------------------------------------------------------ 传输层
class _StubHTTP(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def install_stub():
    fastapi = types.ModuleType("fastapi")

    class HTTPException(_StubHTTP):
        def __init__(self, status_code, detail):
            super().__init__(status_code,
                             {"detail": str(detail)}
                             if not isinstance(detail, dict) else detail)

    class APIRouter:
        def __init__(self, prefix=""):
            self.prefix = prefix

        def _add(self, method, path, fn):
            REGISTERED_ROUTES.append((method, self.prefix + path, fn))
            return fn

        def get(self, p):
            return lambda fn: self._add("GET", p, fn)

        def post(self, p):
            return lambda fn: self._add("POST", p, fn)

        def put(self, p):
            return lambda fn: self._add("PUT", p, fn)

        def delete(self, p):
            return lambda fn: self._add("DELETE", p, fn)

    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    responses = types.ModuleType("fastapi.responses")

    class JSONResponse:
        def __init__(self, status_code=200, content=None):
            LAST_RESPONSES.append(self)
            self.status_code = status_code
            self.body = json.dumps(content, ensure_ascii=False,
                                   default=str).encode("utf-8")

    responses.JSONResponse = JSONResponse
    fastapi.responses = responses
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses


class Client:
    def __init__(self):
        self._client = None
        self._routes = None
        try:
            from fastapi.testclient import TestClient
            from fastapi import FastAPI
            from app.routers import prescriptions as r
            from app import db
            app = FastAPI()
            app.include_router(r.router)

            @app.on_event("startup")
            def _init():
                db.init_db()
            self._client = TestClient(app)
            with self._client:
                pass
        except ImportError:
            install_stub()
            global REGISTERED_ROUTES, LAST_RESPONSES
            REGISTERED_ROUTES, LAST_RESPONSES = [], []
            from app.routers import prescriptions  # noqa: F401
            self._routes = REGISTERED_ROUTES

    def call(self, method, path, body=None):
        if self._client is not None:
            resp = self._client.request(
                method, path,
                content=json.dumps(body) if body is not None else None,
                headers={"Content-Type": "application/json"})
            return resp.status_code, (resp.json() if resp.content else None)
        return self._stub(method, path, body)

    def _stub(self, method, path, body):
        import inspect
        segs = [s for s in path.strip("/").split("/") if s]
        handler = None
        kwargs = {}
        for m, pattern, fn in self._routes:
            if m != method:
                continue
            psegs = [s for s in pattern.strip("/").split("/") if s]
            if len(psegs) != len(segs):
                continue
            ok, kw = True, {}
            for ps, sv in zip(psegs, segs):
                if ps.startswith("{") and ps.endswith("}"):
                    kw[ps[1:-1]] = sv
                elif ps != sv:
                    ok = False
                    break
            if ok:
                handler, kwargs = fn, kw
                break
        assert handler, f"桩路由未匹配 {method} {path}"
        LAST_RESPONSES.clear()
        params = inspect.signature(handler).parameters
        try:
            if "body" in params:
                result = handler(body=body or {}, **kwargs)
            else:
                result = handler(**kwargs)
        except _StubHTTP as exc:
            return exc.status_code, exc.detail
        if LAST_RESPONSES:
            resp = LAST_RESPONSES[-1]
            return resp.status_code, json.loads(resp.body)
        return 200, result


def main():
    from app import db
    from app.services import comparison as cmp_svc
    from app.services import review

    db.init_db()
    seed_data()
    client = Client()
    call = client.call

    cid = cmp_svc.create_comparison(["pa", "pb", "pc"], "pa", title="c")

    seg_body = [{"score_index_start": 0, "score_index_end": 6,
                 "deviation_categories": ["wrong_pitch", "missing",
                                          "onset_offset"],
                 "max_onset_error_s": 0.12, "max_abnormal_notes": 0,
                 "forbid_pending": True}]

    # ---- request_id 必填 ----
    st, err = call("POST", "/api/prescriptions",
                   {"source_type": "comparison", "source_id": cid,
                    "segments": seg_body})
    assert st == 400, (st, err)

    # ---- 创建成功 ----
    st, pr1 = call("POST", "/api/prescriptions",
                   {"request_id": "rid-create-1",
                    "source_type": "comparison", "source_id": cid,
                    "segments": seg_body})
    assert st == 200, (st, pr1)
    pid_ = pr1["id"]
    assert pr1["status"] == "created" and len(pr1["segments"]) == 1

    # ---- 重复 request_id 幂等：回放同一处方 ----
    st, pr1b = call("POST", "/api/prescriptions",
                    {"request_id": "rid-create-1",
                     "source_type": "piece", "source_id": "pa",
                     "segments": seg_body})
    assert st == 200 and pr1b["id"] == pid_

    # ---- 复测：pb（#5 漏音）-> 失败 ----
    st, rt = call("POST", f"/api/prescriptions/{pid_}/retests",
                  {"request_id": "rid-rt-1", "target_piece_id": "pb"})
    assert st == 200 and rt["overall"] == "fail"
    seg = rt["result"]["segments"][0]
    rm = {x["rule"]: x for x in seg["rules"]}
    assert rm["eliminate:missing"]["evidence_score_indexes"] == [5]
    assert rm["eliminate:missing"]["threshold"] == {"max_count": 0}
    assert rm["eliminate:missing"]["actual"]["count"] == 1
    assert rm["eliminate:missing"]["failure_reason"]

    # ---- 复测幂等：同 request_id 回放首次结果（同一 ord）----
    st, rtb = call("POST", f"/api/prescriptions/{pid_}/retests",
                   {"request_id": "rid-rt-1", "target_piece_id": "pc"})
    assert st == 200 and rtb["id"] == rt["id"]
    assert rtb["target_piece_id"] == "pb"  # 以首次为准

    # ---- 干净录音 pc -> 通过 ----
    st, rtp = call("POST", f"/api/prescriptions/{pid_}/retests",
                   {"request_id": "rid-rt-2", "target_piece_id": "pc"})
    assert st == 200 and rtp["overall"] == "pass"

    # ---- 指纹不符 -> 409 ----
    st, err = call("POST", f"/api/prescriptions/{pid_}/retests",
                   {"request_id": "rid-rt-3", "target_piece_id": "pz"})
    assert st == 409 and err["reason"] == "fingerprint_mismatch", (st, err)
    assert set(["expected", "actual"]).issubset(err)

    # ---- 固化成员对齐漂移 -> 来源过期 409（须显式重绑）----
    review.recompute("pc")
    st, err = call("POST", f"/api/prescriptions/{pid_}/retests",
                   {"request_id": "rid-rt-4", "target_piece_id": "pc"})
    assert st == 409 and err["reason"] == "source_stale", (st, err)
    assert any(s["piece_id"] == "pc"
               and "alignment_version_changed" in s["reasons"]
               for s in err["stale_members"])
    # 同 request_id 回放首次 409（即使换成别的目标）
    st, err2 = call("POST", f"/api/prescriptions/{pid_}/retests",
                    {"request_id": "rid-rt-4", "target_piece_id": "pa"})
    assert st == 409 and err2["reason"] == "source_stale"

    # ---- 候选列表：来源过期时同曲候选全部不可复测 ----
    st, cands = call("GET", f"/api/prescriptions/{pid_}/candidates")
    assert st == 200
    cm = {c["piece_id"]: c for c in cands}
    assert "pz" not in cm, "非同曲不应出现在候选中"
    assert cm["pc"]["compatible"] is False
    assert cm["pc"]["reason"] == "member_stale"
    assert cm["pa"]["compatible"] is False
    assert cm["pa"]["reason"] == "source_stale"

    # ---- 过期对比上重绑 -> 409（先刷新对比才能重绑）----
    st, err = call("POST", f"/api/prescriptions/{pid_}/rebind",
                   {"source_type": "comparison", "source_id": cid})
    assert st == 409 and err["reason"] == "source_stale"
    assert any(s["piece_id"] == "pc" for s in err["stale_members"])
    cmp_svc.refresh_comparison(cid)
    st, rb = call("POST", f"/api/prescriptions/{pid_}/rebind",
                  {"source_type": "comparison", "source_id": cid})
    assert st == 200 and rb["source_stale"] is False
    # 旧复测保留
    assert rb["retest_count"] == 2
    # 重绑后复测 v2 通过
    st, rt3 = call("POST", f"/api/prescriptions/{pid_}/retests",
                   {"request_id": "rid-rt-5", "target_piece_id": "pc"})
    assert st == 200 and rt3["target_version"] == 2

    # ---- 单次乐曲处方 + 作废 ----
    st, one = call("POST", "/api/prescriptions",
                   {"request_id": "rid-create-2",
                    "source_type": "piece", "source_id": "pa",
                    "segments": [{"score_index_start": 0,
                                  "score_index_end": 3,
                                  "deviation_categories": ["wrong_pitch"]}]})
    assert st == 200 and one["source_type"] == "piece"

    # ---- 单支录音处方：另一支同曲录音未列入固化成员也能直接复测 ----
    # （pb/pc 与 pa 同曲，均已分析且有已确认对齐；无需重绑）
    st, cand = call("GET", f"/api/prescriptions/{one['id']}/candidates")
    assert st == 200
    cm1 = {c["piece_id"]: c for c in cand}
    assert cm1["pb"]["compatible"] is True
    assert cm1["pb"]["in_frozen_source"] is False
    assert cm1["pb"]["reason"] is None
    # pb 段 0..3 无错音（#5 漏音在段外）-> 通过
    st, rtnew = call("POST", f"/api/prescriptions/{one['id']}/retests",
                     {"request_id": "rid-rt-new-1", "target_piece_id": "pb"})
    assert st == 200 and rtnew["overall"] == "pass", (st, rtnew)
    assert rtnew["target_version"] == 1 and rtnew["target_wav_sha256"]
    assert rtnew["result"]["source_basis"]["source_version_ord"] == 0

    # 来源录音 pa 重算（固化成员过期）：新目标复测也被 409 source_stale
    review.recompute("pa")
    st, err = call("POST", f"/api/prescriptions/{one['id']}/retests",
                   {"request_id": "rid-rt-new-2", "target_piece_id": "pb"})
    assert st == 409 and err["reason"] == "source_stale"
    # 重绑原单曲来源后，pb 仍无需加入固化成员即可复测
    st, rb1 = call("POST", f"/api/prescriptions/{one['id']}/rebind",
                   {"source_type": "piece", "source_id": "pa"})
    assert st == 200 and rb1["source_stale"] is False
    st, cand = call("GET", f"/api/prescriptions/{one['id']}/candidates")
    cm2 = {c["piece_id"]: c for c in cand}
    assert cm2["pb"]["compatible"] is True
    st, rtnew2 = call("POST", f"/api/prescriptions/{one['id']}/retests",
                      {"request_id": "rid-rt-new-3", "target_piece_id": "pb"})
    assert st == 200 and rtnew2["overall"] == "pass"
    assert rtnew2["result"]["source_basis"]["source_version_ord"] == 1
    # 旧复测不被重绑/重算改写
    st, hist1 = call("GET", f"/api/prescriptions/{one['id']}/retests")
    first = hist1[0]
    assert first["id"] == rtnew["id"]
    st, old = call("GET", f"/api/retests/{first['id']}")
    assert old["result"]["source_basis"]["source_version_ord"] == 0

    # 指纹不同的目标 -> 409 fingerprint_mismatch
    st, err = call("POST", f"/api/prescriptions/{one['id']}/retests",
                   {"request_id": "rid-rt-new-4", "target_piece_id": "pz"})
    assert st == 409 and err["reason"] == "fingerprint_mismatch"

    st, vd = call("POST", f"/api/prescriptions/{one['id']}/void", None)
    assert st == 200 and vd["status"] == "voided"
    st, err = call("POST", f"/api/prescriptions/{one['id']}/retests",
                   {"request_id": "rid-rt-x", "target_piece_id": "pa"})
    assert st == 409 and err["reason"] == "prescription_voided"

    # ---- 列表 / 详情 / 复测历史 ----
    st, items = call("GET", "/api/prescriptions")
    assert st == 200 and len(items) == 2
    st, hist = call("GET", f"/api/prescriptions/{pid_}/retests")
    assert st == 200 and len(hist) == 3 and hist[0]["ord"] == 1
    st, one_rt = call("GET", f"/api/retests/{hist[0]['id']}")
    assert st == 200 and one_rt["result"]["format"] == \
        "deviation-prescription-retest"

    # ---- 404 ----
    st, _ = call("POST", "/api/prescriptions/nope/retests",
                 {"request_id": "x", "target_piece_id": "pa"})
    assert st == 404

    print("PRESCRIPTION API OK")


if __name__ == "__main__":
    main()

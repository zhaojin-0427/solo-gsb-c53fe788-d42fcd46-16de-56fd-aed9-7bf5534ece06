"""偏差片段练习谱 HTTP 接口回归测试（纯标准库可跑）。

覆盖接口契约：
  * GET  /api/pieces/{id}/practice/editor-data 编辑器数据（逐位状态）；
  * POST /api/pieces/{id}/practice-scores      创建：422 逐位阻止（待确认/
                                               排除/无偏差音）；
  * 成功创建 -> 200 built v1，可下载 MusicXML（内嵌固化字段）与清单；
  * GET 列表/详情含当前版本、版本数、练习顺序、过期状态、下载入口；
  * GET 版本历史与单版本详情；任一历史版本 MusicXML / 清单可下载；
  * POST /rebuild 来源变化时阻塞 422：**不新增版本**，旧稿仍可下载；
    解除后显式重建 -> 追加新版本并切换当前指针，旧版本保留；
  * 来源谱哈希变化 -> 当前版本 stale 但下载 200；DELETE 清理全部版本。

两种运行方式：装了 fastapi 走真实 TestClient；否则注入最小桩后直接调用
路由函数（与 test_synthesis_api 相同思路）。

直接运行：python3 tests/test_practice_score_api.py
"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test_practice_api.db")
os.makedirs(os.path.join(TMP, "files"), exist_ok=True)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)


def score_rows(n=16):
    return [{"index": i, "midi": 60.0 + (i % 5), "beat": float(i),
             "dur_q": 1.0, "measure": i // 4 + 1,
             "beat_in_measure": i % 4 + 1, "occurrence": 0, "part": 0}
            for i in range(n)]


def obs_rows(score, wrong=(), pending=()):
    out = []
    for i, s in enumerate(score):
        if i in pending:
            out.append({"index": i, "midi": None,
                        "onset": round(i * 0.6, 4), "duration": 0.5,
                        "state": "poly", "voiced_ratio": 0.4,
                        "poly_ratio": 0.6, "onset_strength": 0.8})
        else:
            out.append({"index": i, "midi": s["midi"] + (3 if i in wrong else 0),
                        "onset": round(i * 0.6, 4), "duration": 0.55,
                        "state": "stable", "voiced_ratio": 0.9,
                        "poly_ratio": 0.1, "onset_strength": 0.8})
    return out


ATTRS = ("<attributes><divisions>1</divisions><key><fifths>0</fifths></key>"
         "<time><beats>4</beats><beat-type>4</beat-type></time>"
         "<clef><sign>G</sign><line>2</line></clef></attributes>")
XML = ("<score-partwise version=\"3.1\"><part-list><score-part id=\"P1\">"
       "<part-name>V</part-name></score-part></part-list><part id=\"P1\">"
       + "".join(f'<measure number="{i + 1}">{ATTRS if i == 0 else ""}'
                 '<note><pitch><step>C</step><octave>4</octave></pitch>'
                 '<duration>4</duration><type>whole</type></note></measure>'
                 for i in range(4)) + "</part></score-partwise>")


def seed_data():
    from app import db
    from app.services import review
    db.init_db()
    score = score_rows()
    db.create_piece("p1", "测试曲", os.path.join(TMP, "files", "p1.xml"))
    db.save_analysis("p1", os.path.join(TMP, "files", "p1.wav"), 12.0,
                     score, obs_rows(score, wrong=(2,), pending=(9,)),
                     [], [])
    open(os.path.join(TMP, "files", "p1.xml"), "w", encoding="utf-8").write(XML)
    open(os.path.join(TMP, "files", "p1.wav"), "wb").write(b"RIFF")
    review.recompute("p1")
    return score


# ------------------------------------------------------------ 传输层
class Client:
    def __init__(self):
        self._client = self._routes = None
        try:
            from fastapi.testclient import TestClient
            from fastapi import FastAPI
            from app.routers import practice_scores as r
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
            _install_stub()
            from app.routers import practice_scores as _r  # noqa: F401
            from fastapi_stub import REGISTERED_ROUTES
            self._routes = REGISTERED_ROUTES

    def call(self, method, path, body=None, raw=False):
        if self._client is not None:
            resp = self._client.request(
                method, path,
                content=json.dumps(body) if body is not None else None,
                headers={"Content-Type": "application/json"})
            if raw:
                return resp.status_code, resp.content
            if resp.content:
                return resp.status_code, resp.json()
            return resp.status_code, None
        return self._call_stub(method, path, body, raw)

    def _call_stub(self, method, path, body, raw):
        import inspect
        from fastapi_stub import LAST_RESPONSES
        path, _, query = path.partition("?")
        qp = {}
        if query:
            for kv in query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    qp[k] = int(v) if v.lstrip("-").isdigit() else v
        handler, kwargs = _match(self._routes, method, path)
        for name, p in inspect.signature(handler).parameters.items():
            if name == "body":
                continue
            if name not in kwargs:
                if name in qp:
                    kwargs[name] = qp[name]
                elif p.default is not inspect.Parameter.empty:
                    kwargs[name] = p.default
        LAST_RESPONSES.clear()
        try:
            if "body" in inspect.signature(handler).parameters:
                result = handler(body=body if body is not None else None,
                                 **kwargs)
            else:
                result = handler(**kwargs)
        except _StubHTTP as exc:
            return exc.status_code, (None if raw else exc.detail)
        if LAST_RESPONSES:
            r = LAST_RESPONSES[-1]
            return r.status_code, (r.body if raw else json.loads(r.body))
        return 200, (None if raw else result)


def _match(routes, method, path):
    segs = [s for s in path.strip("/").split("/") if s]
    for m, pattern, fn in routes:
        if m != method:
            continue
        psegs = [s for s in pattern.strip("/").split("/") if s]
        if len(psegs) != len(segs):
            continue
        kwargs, ok = {}, True
        for a, b in zip(psegs, segs):
            if a.startswith("{") and a.endswith("}"):
                kwargs[a[1:-1]] = b
            elif a != b:
                ok = False
                break
        if ok:
            return fn, kwargs
    raise AssertionError(f"桩路由未匹配 {method} {path}")


class _StubHTTP(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _install_stub():
    import types
    fastapi = types.ModuleType("fastapi")

    class HTTPException(_StubHTTP):
        def __init__(self, status_code, detail):
            super().__init__(status_code, {"detail": str(detail)})

    class APIRouter:
        def __init__(self, prefix=""):
            self.prefix = prefix

        def _add(self, method, path):
            def deco(fn):
                from fastapi_stub import REGISTERED_ROUTES
                REGISTERED_ROUTES.append((method, self.prefix + path, fn))
                return fn
            return deco

        def get(self, path):
            return self._add("GET", path)

        def post(self, path):
            return self._add("POST", path)

        def delete(self, path):
            return self._add("DELETE", path)

    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi

    responses = types.ModuleType("fastapi.responses")

    class JSONResponse:
        def __init__(self, status_code=200, content=None):
            from fastapi_stub import LAST_RESPONSES
            self.status_code = status_code
            self.body = json.dumps(content, ensure_ascii=False,
                                   default=str).encode("utf-8")
            LAST_RESPONSES.append(self)

    class Response:
        def __init__(self, content=b"", media_type=None, headers=None):
            from fastapi_stub import LAST_RESPONSES
            self.status_code = 200
            self.body = content if isinstance(content, bytes) \
                else str(content).encode("utf-8")
            self.headers = headers or {}
            LAST_RESPONSES.append(self)

    responses.JSONResponse = JSONResponse
    responses.Response = Response
    sys.modules["fastapi.responses"] = responses

    stub = types.ModuleType("fastapi_stub")
    stub.REGISTERED_ROUTES = []
    stub.LAST_RESPONSES = []
    sys.modules["fastapi_stub"] = stub


# ------------------------------------------------------------ 用例

SEG_OK = {"measure_start": 1, "measure_end": 2, "loops": 2,
          "start_tempo_bpm": 60, "tempo_increment_bpm": 10,
          "tempo_direction": "up"}


def main():
    seed_data()
    c = Client()

    # 编辑器数据
    st, ed = c.call("GET", "/api/pieces/p1/practice/editor-data")
    assert st == 200, ed
    by_m = {m["measure"]: m for m in ed["measures"]}
    assert by_m[1]["selectable"] and by_m[1]["deviation_indexes"] == [2]
    assert not by_m[3]["selectable"]
    assert [b["reason"] for b in by_m[3]["blockers"]] == ["pending_confirmation"]

    # 阻塞：小节 3 含待确认
    st, body = c.call("POST", "/api/pieces/p1/practice-scores",
                      {"segments": [{"measure_start": 3, "measure_end": 3,
                                     "loops": 1, "start_tempo_bpm": 60}]})
    assert st == 422, (st, body)
    assert body["reason"] == "blocked_positions"
    reasons = {(b["score_index"], b["reason"]) for b in body["blockers"]}
    assert (9, "pending_confirmation") in reasons
    # 每段报告
    assert body["segments"][0]["segment"] == 1
    assert not body["segments"][0]["can_generate"]

    # 阻塞：干净小节 4 无偏差音
    st, body = c.call("POST", "/api/pieces/p1/practice-scores",
                      {"segments": [{"measure_start": 4, "measure_end": 4,
                                     "loops": 1, "start_tempo_bpm": 60}]})
    assert st == 422
    assert any(b["reason"] == "no_confirmed_deviation" for b in body["blockers"])

    # 成功创建
    st, d = c.call("POST", "/api/pieces/p1/practice-scores",
                   {"title": "练习 A", "segments": [SEG_OK]})
    assert st == 200, (st, d)
    tid = d["id"]
    assert d["status"] == "built" and d["round_count"] == 2
    assert d["current_version"] == 1 and d["version_count"] == 1
    assert [r["tempo_bpm"] for r in d["practice_order"]] == [60.0, 70.0]
    assert d["musicxml_url"].endswith("/versions/1/musicxml")
    assert d["manifest_url"].endswith("/versions/1/manifest")
    assert d["frozen"]["source_musicxml_sha256"]
    assert d["frozen"]["version"] == 1
    assert d["frozen"]["alignment_version"] == 1

    # 列表
    st, lst = c.call("GET", "/api/practice-scores?piece_id=p1")
    assert st == 200 and len(lst) == 1 and lst[0]["id"] == tid
    assert lst[0]["version_count"] == 1 and lst[0]["current_version"] == 1

    # 下载当前版本 MusicXML：内嵌固化字段
    st, raw = c.call("GET", f"/api/practice-scores/{tid}/musicxml", raw=True)
    assert st == 200
    text = raw.decode("utf-8")
    assert "source_musicxml_sha256" in text
    assert "generation_params" in text and "片段1 · 第2/2轮" in text
    assert "practice_score_version" in text

    # 按版本路径下载 v1（与当前版本入口内容一致）
    st, raw1 = c.call(
        "GET", f"/api/practice-scores/{tid}/versions/1/musicxml", raw=True)
    assert st == 200 and raw1 == raw
    st, man = c.call(
        "GET", f"/api/practice-scores/{tid}/versions/1/manifest")
    assert st == 200 and man["version"] == 1

    # 当前版本清单（兼容入口）
    st, man = c.call("GET", f"/api/practice-scores/{tid}/manifest")
    assert st == 200 and man["format"] == "deviation-practice-score-manifest"
    assert man["version"] == 1
    assert man["truncated_spanners"] is not None

    # 版本历史（创建后只有 v1，且为当前）
    st, hist = c.call("GET", f"/api/practice-scores/{tid}/versions")
    assert st == 200 and hist["current_version"] == 1
    assert [v["version"] for v in hist["versions"]] == [1]
    assert hist["versions"][0]["is_current"]
    st, vd = c.call("GET", f"/api/practice-scores/{tid}/versions/1")
    assert st == 200 and vd["practice_order"] and vd["segments"]

    # 404
    st, _ = c.call("GET", "/api/practice-scores/nope")
    assert st == 404
    st, _ = c.call("GET", f"/api/practice-scores/{tid}/versions/99")
    assert st == 404

    # 来源对齐变化 -> stale，旧版本仍可下载
    from app import db
    from app.services import review
    db.add_anchor("p1", "exclude_score", 7, None)
    review.recompute("p1")
    st, d2 = c.call("GET", f"/api/practice-scores/{tid}")
    assert d2["stale"] and d2["musicxml_available"]
    assert d2["current_version"] == 1 and d2["version_count"] == 1
    st, _ = c.call("GET", f"/api/practice-scores/{tid}/musicxml", raw=True)
    assert st == 200, "过期旧稿必须仍可下载"
    # 版本历史中 v1 被标过期但下载入口可用
    st, hist = c.call("GET", f"/api/practice-scores/{tid}/versions")
    assert hist["versions"][0]["stale"]
    assert hist["versions"][0]["musicxml_available"]

    # 显式重建：#7 已排除 -> 422，不新增版本、旧稿保留
    st, body = c.call("POST", f"/api/practice-scores/{tid}/rebuild")
    assert st == 422
    assert any(b["score_index"] == 7 and b["reason"] == "excluded"
               for b in body["blockers"])
    st, d3 = c.call("GET", f"/api/practice-scores/{tid}")
    assert d3["status"] == "built" and d3["musicxml_available"]
    assert d3["current_version"] == 1 and d3["version_count"] == 1
    import os as _os
    files_dir = _os.path.join(TMP, "files")
    assert _os.path.exists(_os.path.join(files_dir,
                                         f"practice_{tid}_v1.musicxml"))
    assert not _os.path.exists(_os.path.join(
        files_dir, f"practice_{tid}_v2.musicxml"))
    # 阻塞失败后 v1 仍可按版本下载
    st, old_raw = c.call(
        "GET", f"/api/practice-scores/{tid}/versions/1/musicxml", raw=True)
    assert st == 200 and old_raw == raw

    # 解除阻塞后显式重建 -> 追加 v2 并切换当前版本，v1 保留可下载
    db.delete_anchor("p1", next(a["id"] for a in db.get_anchors("p1")))
    review.recompute("p1")
    st, d4 = c.call("POST", f"/api/practice-scores/{tid}/rebuild")
    assert st == 200 and not d4["stale"]
    assert d4["current_version"] == 2 and d4["version_count"] == 2
    assert d4["frozen"]["alignment_version"] == 3

    # 版本历史：v2 当前且未过期；v1 仍在、已过期、可下载
    st, hist = c.call("GET", f"/api/practice-scores/{tid}/versions")
    assert [v["version"] for v in hist["versions"]] == [2, 1]
    by_v = {v["version"]: v for v in hist["versions"]}
    assert by_v[2]["is_current"] and not by_v[2]["stale"]
    assert not by_v[1]["is_current"] and by_v[1]["stale"]
    assert by_v[1]["alignment_version"] == 1
    assert by_v[2]["alignment_version"] == 3
    st, old = c.call(
        "GET", f"/api/practice-scores/{tid}/versions/1/musicxml", raw=True)
    assert st == 200 and old == raw, "旧版本内容必须原样保留"
    st, new = c.call(
        "GET", f"/api/practice-scores/{tid}/versions/2/musicxml", raw=True)
    assert st == 200 and new != raw
    st, cur = c.call("GET", f"/api/practice-scores/{tid}/musicxml", raw=True)
    assert st == 200 and cur == new, "当前版本入口应指向 v2"
    st, m1 = c.call("GET", f"/api/practice-scores/{tid}/versions/1/manifest")
    assert m1["alignment_version"] == 1
    st, m2 = c.call("GET", f"/api/practice-scores/{tid}/versions/2/manifest")
    assert m2["alignment_version"] == 3

    # 来源谱文件变化 -> score_changed（当前 v2 过期，v1 仍可下载）
    xp = os.path.join(TMP, "files", "p1.xml")
    with open(xp, "a", encoding="utf-8") as f:
        f.write("<!-- v2 -->")
    st, d5 = c.call("GET", f"/api/practice-scores/{tid}")
    assert d5["stale_reasons"] == ["score_changed"]
    st, _ = c.call(
        "GET", f"/api/practice-scores/{tid}/versions/1/musicxml", raw=True)
    assert st == 200

    # 删除：全部版本文件清理
    p1 = os.path.join(TMP, "files", f"practice_{tid}_v1.musicxml")
    p2 = os.path.join(TMP, "files", f"practice_{tid}_v2.musicxml")
    assert os.path.exists(p1) and os.path.exists(p2)
    st, _ = c.call("DELETE", f"/api/practice-scores/{tid}")
    assert st == 200 and not os.path.exists(p1) and not os.path.exists(p2)
    st, _ = c.call("GET", f"/api/practice-scores/{tid}")
    assert st == 404
    st, _ = c.call("GET", f"/api/practice-scores/{tid}/versions")
    assert st == 404

    print("PRACTICE SCORE API OK")


if __name__ == "__main__":
    main()

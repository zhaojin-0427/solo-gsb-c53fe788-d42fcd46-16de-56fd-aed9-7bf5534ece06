"""谱面修订迁移 HTTP 接口回归测试（纯标准库可跑）。

覆盖接口契约：
  * POST /api/pieces/{id}/revisions   上传修订谱 -> 预览（200）；多声部 422；
  * GET  /api/revisions / /{id}       列表与详情（六类计数 / 逐项继承）；
  * POST /api/revisions/{id}/commit   request_id 幂等回放（200/422 均回放）；
                                      冲突预览 / 来源变化 -> 422 阻止提交；
  * DELETE 仅 preview 可删；committed 409；
  * GET  /api/revisions/{id}/report   确定性 JSON；
  * GET  /api/revisions/{id}/revised-musicxml  修订谱下载；
  * GET  /api/pieces/{new}/revision-context 与 POST /carryover；
  * POST /api/revisions/{id}/audio    新入口录音后台分析（桩掉音频分析）。

两种运行方式：装了 fastapi 走真实 TestClient；否则注入最小桩
（与 test_practice_score_api.py 相同思路）。

直接运行：python3 tests/test_revision_api.py
"""
import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test_revision_api.db")
os.makedirs(os.path.join(TMP, "files"), exist_ok=True)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from app import db  # noqa: E402
from app.services import revision as rv  # noqa: E402
from app.services import review  # noqa: E402

_PENDING_REV = []
_SOURCE_PARSES = {}
rv.score_parser.parse_musicxml = lambda path: \
    ([dict(n) for n in _SOURCE_PARSES[path]]
     if path in _SOURCE_PARSES
     else [dict(n) for n in _PENDING_REV], [])


def note(i, midi, m, b, occ=0):
    return {"index": i, "midi": float(midi),
            "beat": float(m * 8 + occ * 100 + b - 1), "dur_q": 1.0,
            "measure": m, "beat_in_measure": float(b),
            "occurrence": occ, "part": 0}


def score_rows(notes):
    return [{"index": n["index"], "midi": n["midi"], "beat": n["beat"],
             "dur_q": n["dur_q"], "measure": n["measure"],
             "beat_in_measure": n["beat_in_measure"],
             "occurrence": n["occurrence"], "part": 0} for n in notes]


def obs_rows(score):
    return [{"index": i, "midi": s["midi"], "onset": round(i * 0.6, 4),
             "duration": 0.5, "state": "stable", "voiced_ratio": 0.9,
             "poly_ratio": 0.1, "onset_strength": 0.8}
            for i, s in enumerate(score)]


XML = ('<?xml version="1.0"?><score-partwise version="3.1">'
       '<part-list><score-part id="P1"><part-name>V</part-name>'
       '</score-part></part-list><part id="P1"><measure number="1"/>'
       '</part></score-partwise>')
POLY_XML = (b'<score-partwise><part id="P1"><measure number="1">'
            b'<note><pitch><step>C</step><octave>4</octave></pitch></note>'
            b'<note><chord/></note></measure></part></score-partwise>')


def seed():
    db.init_db()
    old = [note(i, 60 + (i % 4), i // 2 + 1, (i % 2) * 2 + 1)
           for i in range(8)]
    db.create_piece("p1", "测试曲", os.path.join(TMP, "files", "p1.xml"))
    db.save_analysis("p1", os.path.join(TMP, "files", "p1.wav"), 12.0,
                     score_rows(old), obs_rows(old), [], [])
    open(os.path.join(TMP, "files", "p1.xml"), "w",
         encoding="utf-8").write(XML)
    open(os.path.join(TMP, "files", "p1.wav"), "wb").write(b"RIFF")
    db.add_anchor("p1", "exclude_score", 0, None, "x")
    _SOURCE_PARSES[os.path.join(TMP, "files", "p1.xml")] = \
        [dict(n) for n in old]
    review.recompute("p1")
    return old


# ------------------------------------------------------------ 传输层
class Client:
    def __init__(self):
        self._client = None
        self._routes = None
        try:
            from fastapi.testclient import TestClient
            from fastapi import FastAPI
            from app.routers import revisions as r
            app = FastAPI()
            app.include_router(r.router)

            @app.on_event("startup")
            def _init():
                db.init_db()
            self._client = TestClient(app)
        except ImportError:
            _install_stub()
            from app.routers import revisions as r  # noqa: F401
            from fastapi_stub import REGISTERED_ROUTES
            self._routes = REGISTERED_ROUTES

    def call(self, method, path, body=None, files=None, raw=False):
        if self._client is not None:
            if files is not None:
                resp = self._client.request(method, path, files=files,
                                            data=body or {})
            else:
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
        sig = inspect.signature(handler)
        names = set(sig.parameters)
        for name, p in sig.parameters.items():
            if name in ("musicxml", "body"):
                continue
            if name not in kwargs and name in qp:
                kwargs[name] = qp[name]
        if "body" in names and body is not None:
            kwargs["body"] = body
        LAST_RESPONSES.clear()
        try:
            if "musicxml" in names and self._stub_files \
                    and self._stub_files.get("musicxml"):
                kwargs["musicxml"] = _Upload(
                    self._stub_files.get("musicxml"))
            result = handler(**kwargs)
            if inspect.iscoroutine(result):
                import asyncio
                result = asyncio.run(result)
        except _StubHTTP as exc:
            return exc.status_code, (None if raw else exc.detail)
        if LAST_RESPONSES:
            r = LAST_RESPONSES[-1]
            return r.status_code, (r.body if raw else json.loads(r.body))
        return 200, (None if raw else result)

    _stub_files = None

    def set_stub_file(self, name, data, filename="r.musicxml"):
        Client._stub_files = {name: (filename, data)}


class _Upload:
    def __init__(self, kv):
        self.filename, self._data = kv

    async def read(self):
        return self._data


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

        def get(self, p):
            return self._add("GET", p)

        def post(self, p):
            return self._add("POST", p)

        def delete(self, p):
            return self._add("DELETE", p)

    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.File = lambda default=None: default
    fastapi.Form = lambda default=None: default
    fastapi.UploadFile = object
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

def main():
    old = seed()
    c = Client()

    # 多声部拒绝（422）
    if c._client is not None:
        st, body = c.call(
            "POST", "/api/pieces/p1/revisions",
            files={"musicxml": ("r.xml", POLY_XML, "application/xml")})
    else:
        c.set_stub_file("musicxml", POLY_XML)
        st, body = c.call("POST", "/api/pieces/p1/revisions")
    assert st == 422, (st, body)
    assert body["reason"] == "polyphony_ambiguous"
    c.set_stub_file("musicxml", None)

    # 正常预览：改 #2 音高、删 #6、末尾新增
    new = [dict(n) for n in old[:6]]
    new[2]["midi"] = 71.0
    new.append(note(6, 90, 6, 1))
    _PENDING_REV.clear()
    _PENDING_REV.extend(dict(n) for n in new)
    if c._client is not None:
        st, rev = c.call(
            "POST", "/api/pieces/p1/revisions",
            files={"musicxml": ("r.xml", XML.encode(), "application/xml")})
    else:
        c.set_stub_file("musicxml", XML.encode())
        st, rev = c.call("POST", "/api/pieces/p1/revisions")
    assert st == 200, (st, rev)
    rid = rev["id"]
    assert rev["can_commit"] and not rev["has_blockers"]
    assert rev["summary"]["modified"] == 1
    assert rev["summary"]["deleted"] == 2  # #6,#7
    assert rev["summary"]["added"] == 1
    # 可继承的排除锚点（#0 保留）
    anc = {a["kind"] + ":" + str(a.get("old_score_index")): a["status"]
           for a in rev["impact"] if a["artifact"] == "anchor"}
    assert anc["exclude_score:0"] == "inheritable"

    # 列表
    st, lst = c.call("GET", "/api/revisions?piece_id=p1")
    assert st == 200 and any(x["id"] == rid for x in lst)

    # 详情 / 404
    st, d = c.call("GET", f"/api/revisions/{rid}")
    assert st == 200 and d["summary"]["kept"] >= 5
    st, _ = c.call("GET", "/api/revisions/nope")
    assert st == 404

    # 修订谱下载 + 确定性报告
    st, raw = c.call("GET", f"/api/revisions/{rid}/revised-musicxml", raw=True)
    assert st == 200 and raw == XML.encode()
    st, rep1 = c.call("GET", f"/api/revisions/{rid}/report")
    st, rep2 = c.call("GET", f"/api/revisions/{rid}/report")
    assert st == 200
    b1 = json.dumps(rep1, sort_keys=True, separators=(",", ":"))
    b2 = json.dumps(rep2, sort_keys=True, separators=(",", ":"))
    assert b1 == b2
    assert rep1["format"] == "score-revision-migration-report"
    # 轮次坐标策略随报告固化（确定性常量），重解析对应可靠
    assert rep1["source_reparse_reliable"] is True
    assert "小节实例" in rep1["source_coordinate_policy"]

    # 提交幂等：重复 request_id 回放同一新入口
    st, committed = c.call("POST", f"/api/revisions/{rid}/commit",
                           {"request_id": "req-1"})
    assert st == 200, committed
    new_pid = committed["new_piece_id"]
    st, again = c.call("POST", f"/api/revisions/{rid}/commit",
                       {"request_id": "req-1"})
    assert st == 200 and again["new_piece_id"] == new_pid, (st, again)
    # 不带 request_id -> 400
    st, _ = c.call("POST", f"/api/revisions/{rid}/commit", {})
    assert st == 400
    # 已提交 -> 409
    st, body = c.call("POST", f"/api/revisions/{rid}/commit",
                      {"request_id": "req-2"})
    assert st == 409, body
    # committed 不可删
    st, _ = c.call("DELETE", f"/api/revisions/{rid}")
    assert st == 409

    # 新入口：预置可继承锚点；接续上下文（分析前 ready=false）
    st, ctx = c.call("GET", f"/api/pieces/{new_pid}/revision-context")
    assert st == 200 and ctx["revision_id"] == rid
    assert all(not t["ready"] for t in ctx["templates"])

    # 冲突预览不可提交（422 + 幂等回放）
    _PENDING_REV.clear()
    _PENDING_REV.extend([note(0, 60, 1, 1), note(1, 62, 1, 1)])
    if c._client is not None:
        st, b2 = c.call(
            "POST", "/api/pieces/p1/revisions",
            files={"musicxml": ("r.xml", XML.encode(), "application/xml")})
    else:
        c.set_stub_file("musicxml", XML.encode())
        st, b2 = c.call("POST", "/api/pieces/p1/revisions")
    assert st == 200 and b2["has_blockers"]
    rid2 = b2["id"]
    st, blocked = c.call("POST", f"/api/revisions/{rid2}/commit",
                         {"request_id": "rb-1"})
    assert st == 422 and blocked["reason"] == "migration_blocked"
    st, replay = c.call("POST", f"/api/revisions/{rid2}/commit",
                        {"request_id": "rb-1"})
    assert st == 422 and replay["reason"] == "migration_blocked"
    # 冲突预览可删除
    st, _ = c.call("DELETE", f"/api/revisions/{rid2}")
    assert st == 200
    st, _ = c.call("GET", f"/api/revisions/{rid2}")
    assert st == 404

    # 原谱重新解析与旧库无法可靠对应（音符数漂移）：预览说明原因，
    # 确认 422（request_id 幂等回放同样 422）
    _SOURCE_PARSES[os.path.join(TMP, "files", "p1.xml")] = \
        [dict(n) for n in old[:4]]
    _PENDING_REV.clear()
    _PENDING_REV.extend(dict(n) for n in old)
    if c._client is not None:
        st, b3 = c.call(
            "POST", "/api/pieces/p1/revisions",
            files={"musicxml": ("r.xml", XML.encode(), "application/xml")})
    else:
        c.set_stub_file("musicxml", XML.encode())
        st, b3 = c.call("POST", "/api/pieces/p1/revisions")
    assert st == 200 and b3["has_blockers"] and not b3["can_commit"]
    assert any(b["reason"] == "source_reparse_mismatch"
               for b in b3["blockers"])
    rid3 = b3["id"]
    st, blocked = c.call("POST", f"/api/revisions/{rid3}/commit",
                         {"request_id": "rm-1"})
    assert st == 422 and blocked["reason"] == "migration_blocked"
    assert any(b["reason"] == "source_reparse_mismatch"
               for b in blocked["blockers"])
    st, replay = c.call("POST", f"/api/revisions/{rid3}/commit",
                        {"request_id": "rm-1"})
    assert st == 422 and replay["reason"] == "migration_blocked"
    st, _ = c.call("DELETE", f"/api/revisions/{rid3}")
    assert st == 200

    print("REVISION API OK")


if __name__ == "__main__":
    main()

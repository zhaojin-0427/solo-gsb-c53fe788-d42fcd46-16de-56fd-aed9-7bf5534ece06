"""最佳片段合成轨 HTTP 接口回归测试（纯标准库可跑）。

覆盖本次修复的接口契约：
  * PUT  /api/synthesis/{id}/overrides 在已 built 任务上施加阻塞约束后
    返回 200，status 仍为 built，plan_status=blocked，wav/manifest 可访问；
  * POST /api/synthesis/{id}/rebuild 阻塞时返回 **422** 与逐谱位原因，
    且旧 WAV / 清单 / 固化版本仍可下载（试听能力不丢）；
  * 解除阻塞后须再次 POST rebuild 才替换 WAV（built_at / 哈希更新）；
  * 对比过期时 PUT / POST rebuild 返回 409，旧产物原样保留；
  * DELETE 任务后 WAV 文件一并清除。

两种运行方式，自动选择：
  1. 安装了 fastapi（含 starlette TestClient）-> 走真实 ASGI 栈；
  2. 纯标准库环境 -> 注入最小 fastapi 桩后直接调用路由函数
     （仅用于 CI 无依赖环境，桩只实现路由模块实际用到的接口）。

直接运行：python3 tests/test_synthesis_api.py
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
os.environ["DB_PATH"] = os.path.join(TMP, "test_synth_api.db")
os.makedirs(os.path.join(TMP, "files"), exist_ok=True)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

SR = 8000


# ------------------------------------------------------------ 测试数据
def score_rows(n=12):
    return [{"index": i, "midi": 60.0 + (i % 5), "beat": i * 1.0,
             "dur_q": 1.0, "measure": i // 4 + 1,
             "beat_in_measure": i % 4 + 1, "occurrence": 0, "part": 0}
            for i in range(n)]


def obs_rows(score, wrong=(), missing=(), tempo=0.6):
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
                buf[t0 + k] += loud * env * math.sin(
                    2 * math.pi * 440 * k / SR)
    pcm = struct.pack("<%dh" % n, *[
        max(-32767, min(32767, int(round(v * 32767)))) for v in buf])
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


def seed_data():
    from app import db
    from app.services import review
    score = score_rows()
    for pid, tempo, loud, kwargs in (
            ("pbase", 0.6, 0.5, {"wrong": (3,)}),
            ("pa", 0.5, 0.25, {"wrong": (7,)}),
            ("pb", 0.7, 0.5, {"missing": (10,)})):
        db.create_piece(pid, pid, os.path.join(TMP, "files", pid + ".xml"))
        db.save_analysis(
            pid, os.path.join(TMP, "files", pid + ".wav"), 8.0,
            score, obs_rows(score, tempo=tempo, **kwargs), [], [])
        open(os.path.join(TMP, "files", pid + ".xml"), "wb").write(b"<x/>")
        make_wav(os.path.join(TMP, "files", pid + ".wav"), tempo, loud)
        review.recompute(pid)
    return score


# ------------------------------------------------------------ 传输层适配
class Client:
    """统一 (status, json/bytes) 返回；TestClient 或 fastapi 桩二选一。"""

    def __init__(self):
        self._client = None
        self._routes = None
        try:
            from fastapi.testclient import TestClient  # 真实 fastapi
            from fastapi import FastAPI
            from app.routers import synthesis as r
            from app import db
            app = FastAPI()
            app.include_router(r.router)

            @app.on_event("startup")
            def _init():
                db.init_db()

            self._client = TestClient(app)
            with self._client:  # 触发 startup
                pass
        except ImportError:
            _install_stub_router()
            from app.routers import synthesis as _r  # noqa: F401
            from app.routers import comparisons as _c  # noqa: F401
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

    # ------- 纯标准库桩路径 -------
    def _call_stub(self, method, path, body, raw):
        from fastapi_stub import LAST_RESPONSES  # type: ignore
        # 解析 /path?query
        path, _, query = path.partition("?")
        query_params = {}
        if query:
            for kv in query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    query_params[k] = int(v) if v.lstrip("-").isdigit() else v
        handler, path_kwargs = _match_route(method, path)
        # 路由签名中的非路径参数用查询参数填充（如 download）
        import inspect
        for name, p in inspect.signature(handler).parameters.items():
            if name not in path_kwargs and name in query_params:
                path_kwargs[name] = query_params[name]
        LAST_RESPONSES.clear()
        try:
            if "body" in inspect.signature(handler).parameters:
                result = handler(body=body or {}, **path_kwargs)
            else:
                result = handler(**path_kwargs)
        except _StubHTTP as exc:
            return exc.status_code, (exc.detail if not raw else None)
        if LAST_RESPONSES:
            resp = LAST_RESPONSES[-1]
            content = resp.body
            if raw:
                return resp.status_code, content
            return resp.status_code, json.loads(content)
        if raw:
            return 200, None
        return 200, result


def _match_route(method, path):
    from fastapi_stub import REGISTERED_ROUTES  # type: ignore
    segs = [s for s in path.strip("/").split("/") if s]
    for m, pattern, fn in REGISTERED_ROUTES:
        if m != method:
            continue
        psegs = [s for s in pattern.strip("/").split("/") if s]
        if len(psegs) != len(segs):
            continue
        kwargs, ok = {}, True
        for ps, sv in zip(psegs, segs):
            if ps.startswith("{") and ps.endswith("}"):
                kwargs[ps[1:-1]] = sv
            elif ps != sv:
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


def _install_stub_router():
    """注入最小 fastapi / fastapi.responses 桩，再导入 synthesis 路由。"""
    import types

    fastapi = types.ModuleType("fastapi")

    class HTTPException(_StubHTTP):
        def __init__(self, status_code, detail):
            super().__init__(status_code,
                             {"detail": str(detail)} if not isinstance(
                                 detail, dict) else detail)

    class APIRouter:
        def __init__(self, prefix=""):
            self.prefix = prefix

        def _add(self, method, path, fn):
            from fastapi_stub import REGISTERED_ROUTES
            REGISTERED_ROUTES.append((method, self.prefix + path, fn))
            return fn

        def get(self, path):
            return lambda fn: self._add("GET", path, fn)

        def post(self, path):
            return lambda fn: self._add("POST", path, fn)

        def put(self, path):
            return lambda fn: self._add("PUT", path, fn)

        def delete(self, path):
            return lambda fn: self._add("DELETE", path, fn)

    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException

    responses = types.ModuleType("fastapi.responses")

    class JSONResponse:
        media_type = "application/json"

        def __init__(self, status_code=200, content=None):
            import json as _json
            from fastapi_stub import LAST_RESPONSES
            self.status_code = status_code
            self.body = _json.dumps(content, ensure_ascii=False,
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
    fastapi.responses = responses

    stub_pkg = types.ModuleType("fastapi_stub")
    stub_pkg.REGISTERED_ROUTES = []
    stub_pkg.LAST_RESPONSES = []

    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    sys.modules["fastapi_stub"] = stub_pkg
    return stub_pkg.REGISTERED_ROUTES


# ------------------------------------------------------------ 用例
def wav_path(tid):
    return os.path.join(TMP, "files", f"synth_{tid}.wav")


def main():
    from app import db
    from app.services import comparison as cmp_svc
    from app.services import review
    from app.services import synthesis as syn

    db.init_db()
    score = seed_data()
    client = Client()

    def call(method, path, body=None, raw=False):
        return client.call(method, path, body=body, raw=raw)

    cid = cmp_svc.create_comparison(["pbase", "pa", "pb"], "pbase",
                                    title="t")

    # ---- 创建（POST 挂在对比下）----
    st, data = call("POST", f"/api/comparisons/{cid}/synthesis", {})
    assert st == 200, (st, data)
    tid = data["id"]
    assert data["status"] == "draft" and data["plan_status"] == "draft"

    # 重复创建 -> 409
    st, data2 = call("POST", f"/api/comparisons/{cid}/synthesis", {})
    assert st == 409, (st, data2)

    # GET 按对比读取
    st, got = call("GET", f"/api/comparisons/{cid}/synthesis")
    assert st == 200 and got["id"] == tid

    # 不存在任务的读取 / 404
    st, _ = call("GET", "/api/synthesis/nope123456")
    assert st == 404

    # ---- 显式重建：200 + WAV/清单可下载 ----
    st, built = call("POST", f"/api/synthesis/{tid}/rebuild")
    assert st == 200 and built["status"] == "built"
    assert built["wav_available"] and built["wav_url"]
    st, raw = call("GET", f"/api/synthesis/{tid}/wav", raw=True)
    assert st == 200 and raw[:4] == b"RIFF", "WAV 试听可用"
    st, raw_dl = call("GET", f"/api/synthesis/{tid}/wav?download=1", raw=True)
    assert st == 200 and raw_dl == raw
    st, man = call("GET", f"/api/synthesis/{tid}/manifest")
    assert st == 200 and man["format"] == "best-take-synthesis"
    wav_bytes_before = open(wav_path(tid), "rb").read()
    hash_before = hashlib.sha256(wav_bytes_before).hexdigest()
    built_at = db.get_synthesis_task(tid)["built_at"]
    assert built_at

    # ---- PUT 施加阻塞约束（禁用全部）：200 且固化产物原样保留 ----
    st, blocked = call("PUT", f"/api/synthesis/{tid}/overrides",
                       {"disabled": ["pbase", "pa", "pb"]})
    assert st == 200, (st, blocked)
    assert blocked["status"] == "built"
    assert blocked["plan_status"] == "blocked"
    assert blocked["build_preserved"] is True
    assert blocked["wav_available"] is True
    assert len(blocked["blocked"]) == len(score)
    assert all(b["reason"] == "all_disabled" for b in blocked["blocked"])
    row = db.get_synthesis_task(tid)
    assert row["manifest_json"], "清单不得清空"
    assert row["wav_sha256"] == hash_before, "固化哈希不得清空"
    assert row["built_at"] == built_at, "固化时间不得清空"
    assert open(wav_path(tid), "rb").read() == wav_bytes_before, \
        "WAV 文件不得删除或改动"

    # 阻塞期间 WAV / 清单仍可试听下载
    st, raw = call("GET", f"/api/synthesis/{tid}/wav", raw=True)
    assert st == 200 and raw == wav_bytes_before
    st, man2 = call("GET", f"/api/synthesis/{tid}/manifest")
    assert st == 200 and man2["frozen_sources"] == man["frozen_sources"]

    # ---- POST rebuild：422 + 逐谱位原因；产物仍在 ----
    st, err = call("POST", f"/api/synthesis/{tid}/rebuild")
    assert st == 422, (st, err)
    assert "blocked" in err and len(err["blocked"]) == len(score)
    b0 = err["blocked"][0]
    for k in ("score_index", "measure", "beat_in_measure", "reason",
              "candidates"):
        assert k in b0
    assert open(wav_path(tid), "rb").read() == wav_bytes_before

    # ---- 解除阻塞（PUT）：规划恢复 draft；此时还没重建，旧 WAV 仍在 ----
    st, unblocked = call("PUT", f"/api/synthesis/{tid}/overrides", {})
    assert st == 200
    assert unblocked["plan_status"] == "draft"
    assert unblocked["status"] == "built"
    assert open(wav_path(tid), "rb").read() == wav_bytes_before

    # 钉住到一个合格来源 -> rebuild_required=True，旧 WAV 保留
    st, changed = call("PUT", f"/api/synthesis/{tid}/overrides",
                       {"pins": {"5": "pa"}})
    assert st == 200 and changed["rebuild_required"] is True
    assert open(wav_path(tid), "rb").read() == wav_bytes_before
    call("PUT", f"/api/synthesis/{tid}/overrides", {})

    # ---- 显式重建成功：原子替换（built_at / 哈希按确定性内容更新）----
    st, rebuilt = call("POST", f"/api/synthesis/{tid}/rebuild")
    assert st == 200 and rebuilt["status"] == "built"
    assert rebuilt["rebuild_required"] is False

    # ---- 来源变化 -> 对比过期：PUT/POST 一律 409，旧产物保留 ----
    review.recompute("pbase")
    st, got = call("GET", f"/api/synthesis/{tid}")
    assert st == 200 and got["stale"] is True and got["wav_url"]
    st, err = call("PUT", f"/api/synthesis/{tid}/overrides",
                   {"disabled": ["pbase", "pa", "pb"]})
    assert st == 409, (st, err)
    assert err["stale_members"][0]["piece_id"] == "pbase"
    st, err = call("POST", f"/api/synthesis/{tid}/rebuild")
    assert st == 409 and err["stale_members"][0]["piece_id"] == "pbase"
    assert open(wav_path(tid), "rb").read(), "过期拒绝不得删除 WAV"
    assert db.get_synthesis_task(tid)["manifest_json"]

    # 刷新对比解除过期；新版本须显式重建
    st, _ = call("POST", f"/api/comparisons/{cid}/refresh")
    assert st == 200
    st, got = call("GET", f"/api/synthesis/{tid}")
    assert st == 200 and got["stale"] is False
    assert got["rebuild_required"] is True
    st, rebuilt = call("POST", f"/api/synthesis/{tid}/rebuild")
    assert st == 200
    man3 = json.loads(db.get_synthesis_task(tid)["manifest_json"])
    assert next(f for f in man3["frozen_sources"]
                if f["piece_id"] == "pbase")["alignment_version"] == 2

    # ---- DELETE：任务与 WAV 一并清除，再读 404 ----
    st, _ = call("DELETE", f"/api/synthesis/{tid}")
    assert st == 200
    assert not os.path.exists(wav_path(tid))
    st, _ = call("GET", f"/api/synthesis/{tid}")
    assert st == 404

    print("SYNTHESIS API OK")


if __name__ == "__main__":
    main()

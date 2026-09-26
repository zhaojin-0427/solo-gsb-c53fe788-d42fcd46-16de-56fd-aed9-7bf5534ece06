"""端到端冒烟测试：DB + review 编排 + 确定性导出（不依赖 librosa/music21）。

直接运行：python3 tests/test_review_flow.py
"""
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.services import review  # noqa: E402


def make_data():
    spb = 0.6
    score = [{"index": i, "midi": 60.0 + i, "beat": i * 1.0,
              "dur_q": 1.0, "measure": i // 4 + 1,
              "beat_in_measure": i % 4 + 1, "occurrence": 0, "part": 0}
             for i in range(12)]
    obs = []
    oi = 0
    for i in range(12):
        if i == 5:
            continue                       # 漏音
        midi = score[i]["midi"]
        if i == 7:
            midi += 3                      # 错音
        if i == 9:
            obs.append({"index": oi, "midi": None,
                        "onset": round(i * spb, 3), "duration": 0.55,
                        "state": "poly", "voiced_ratio": 0.4,
                        "poly_ratio": 0.6, "onset_strength": 0.8})
        else:
            obs.append({"index": oi, "midi": midi,
                        "onset": round(i * spb, 3), "duration": 0.55,
                        "state": "stable", "voiced_ratio": 0.9,
                        "poly_ratio": 0.1, "onset_strength": 0.8})
        oi += 1
    # 额外音（谱上不存在的高音）
    obs.append({"index": oi, "midi": 95.0,
                "onset": round(11 * spb - 0.2, 3), "duration": 0.2,
                "state": "stable", "voiced_ratio": 0.9,
                "poly_ratio": 0.05, "onset_strength": 0.7})
    return score, obs


def main():
    db.init_db()
    pid = "testpiece01"
    db.create_piece(pid, "冒烟测试", "/tmp/x.xml")
    score, obs = make_data()
    # save_analysis 需要内部精简结构
    db.save_analysis(pid, "/tmp/x.wav", 7.5, score, obs,
                     warnings=["测试警告"], peaks=[0.1, 0.9])

    # 首次自动对齐
    v1 = review.recompute(pid)
    assert v1 == 1

    b = review.get_bundle(pid)
    assert b["piece"]["status"] == "analyzed"
    by_s = {e["score_index"]: e for e in b["alignment"]["entries"]
            if e["score_index"] is not None}
    by_o = {e["obs_index"]: e for e in b["alignment"]["entries"]
            if e["obs_index"] is not None}
    assert by_s[5]["status"] == "missing", by_s[5]
    assert by_s[7]["status"] == "wrong", by_s[7]
    assert by_o[8]["status"] == "pending", by_o[8]
    assert any(e["status"] == "extra"
               for e in b["alignment"]["entries"]), "应有额外音"
    pending_idx = [p["obs_index"] for p in b["alignment"]["pending"]]
    assert 8 in pending_idx

    # 歧义音不允许锁定（重算会拒绝；随后清理该非法锚点）
    anchors_before = {a["id"] for a in db.get_anchors(pid)}
    db.add_anchor(pid, "lock", 8, 8)
    try:
        review.recompute(pid)
    except ValueError:
        pass
    else:
        raise AssertionError("歧义观测被允许锁定")
    for a in db.get_anchors(pid):
        if a["id"] not in anchors_before:
            db.delete_anchor(pid, a["id"])

    # 增量：加排除观测锚点后重算（不触碰其他片段 -> 有缓存命中）
    db.add_anchor(pid, "exclude_obs", None, 11, "ignore extra")
    v2 = review.recompute(pid)
    assert v2 == 2
    b2 = review.get_bundle(pid)

    # 待确认处置：确认多音
    review.resolve_pending(pid, 8, "extra")
    v3 = review.recompute(pid)
    assert v3 == 3

    # 导出确定性：两次导出字节一致
    p1 = review.build_export(pid)
    raw1 = json.dumps(p1, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    p2 = review.build_export(pid)
    raw2 = json.dumps(p2, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    assert raw1 == raw2, "导出不确定"

    data = json.loads(raw1)
    assert data["format"] == "score-audio-review"
    assert data["piece"]["id"] == pid
    assert data["note_results"][0]["score"]["measure"] >= 1
    # 原谱坐标 + 音频时间 + basis 三要素
    for nr in data["note_results"]:
        assert "basis" in nr and isinstance(nr["basis"], list)
        if nr["score"] is not None:
            assert "measure" in nr["score"]
            assert "beat_expanded" in nr["score"]
        if nr["audio"] is not None:
            assert "onset_s" in nr["audio"]
    assert data["summary"]["missing"] >= 1
    assert data["summary"]["wrong_pitch"] >= 1
    assert data["params"]["pitch_wrong_semitones"] == 1.0

    print("export bytes:", len(raw1))
    print("summary:", json.dumps(data["summary"], ensure_ascii=False))
    print("anchors:", json.dumps(data["anchors"], ensure_ascii=False))
    print("REVIEW FLOW OK")


if __name__ == "__main__":
    main()

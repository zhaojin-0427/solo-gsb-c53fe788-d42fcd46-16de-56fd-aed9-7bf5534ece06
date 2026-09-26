"""单调对齐核心：两遍动态规划（仅依赖标准库）。

数据
----
score_notes: [{"index": 全局下标, "midi": 浮点 MIDI, "beat": 展开后四分音符
               起点, "dur_q": 四分音符时长, ...原谱坐标由上层附加}]
obs_notes:   [{"index": 全局下标, "midi": 浮点/None, "onset": 秒,
               "duration": 秒, "state": stable|poly|silent, ...}]

两遍 DP
-------
Pass A（粗配）：对角代价仅音高 + 低权重起音残差，收集 match 点后用
               Theil–Sen 稳健拟合全局线性速度 y = alpha*x + beta。
Pass B（细配）：对角代价 = 音高 + 全权重起音残差；三种转移
               match / delete（漏音）/ insert（多音）。
               state != stable 的观测不允许自动匹配，只能以低代价暂挂，
               进入"待确认"，绝不强配。

锁定锚点强制成对并把网格切成互不影响的片段；排除项不进入网格。
Pass B 的每个片段以 (边界, 排除集合, alpha, beta) 为键写入 cache，
锚点增删后只有受影响片段需要重算 —— 增量重算。

每个 match 记录"对角方案相对最优非对角方案"的代价间隔 margin，
映射为置信度。
"""

from __future__ import annotations

from typing import Optional

from .. import config as C

MATCH = "match"
DELETE = "delete"
INSERT = "insert"
LOCKED_MATCH = "locked_match"
EXCLUDED_SCORE = "excluded_score"
EXCLUDED_OBS = "excluded_obs"


# ------------------------------------------------------------ 统计小工具

def median(values):
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def _theil_sen(points):
    """对 (x, y) 点集做 Theil–Sen 稳健线性拟合，返回 (slope, intercept)。"""
    slopes = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            dx = points[j][0] - points[i][0]
            if dx > 1e-9:
                slopes.append((points[j][1] - points[i][1]) / dx)
    if not slopes:
        return None
    alpha = median(slopes)
    beta = median([y - alpha * x for x, y in points])
    return alpha, beta


def _pitch_cost(delta: float) -> float:
    return min(C.COST_PITCH_MAX, C.COST_PITCH_PER_SEMI * abs(delta))


def _onset_cost(obs_t: float, beat: float, alpha: float, beta: float):
    resid = obs_t - (alpha * beat + beta)
    return min(C.COST_ONSET_MAX, C.COST_ONSET_PER_SEC * abs(resid)), resid


def _insert_cost(state: str) -> float:
    return C.COST_INSERT if state == "stable" else C.COST_INSERT_PENDING


# ------------------------------------------------------------ 单片段 DP

def _dp_region(s_sub, o_sub, alpha, beta, onset_weight: float):
    """对一个片段跑单调 DTW，返回有序 op 列表。

    op: (MATCH, s_index, o_index, margin)
        (DELETE, s_index, None, 0)
        (INSERT, None, o_index, 0)
    """
    n, m = len(s_sub), len(o_sub)
    cost = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[-1] * (m + 1) for _ in range(n + 1)]   # 0 对角 / 1 上 / 2 左
    margin = [[0.0] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        cost[i][0] = cost[i - 1][0] + C.COST_DELETE
        back[i][0] = 1
    for j in range(1, m + 1):
        cost[0][j] = cost[0][j - 1] + _insert_cost(o_sub[j - 1]["state"])
        back[0][j] = 2

    for i in range(1, n + 1):
        sn = s_sub[i - 1]
        for j in range(1, m + 1):
            on = o_sub[j - 1]
            up = cost[i - 1][j] + C.COST_DELETE
            left = cost[i][j - 1] + _insert_cost(on["state"])
            best, ptr = up, 1
            if left < best:
                best, ptr = left, 2

            if on["state"] == "stable" and on.get("midi") is not None:
                delta = on["midi"] - sn["midi"]
                # 音高差距过远：不允许自动配对（锁定锚点不走此路径），
                # 宁可判"漏音 + 额外音"，也不能把不相关的音强配成错音。
                if abs(delta) <= C.PITCH_MATCH_MAX_SEMI:
                    oc, _ = _onset_cost(on["onset"], sn["beat"], alpha, beta)
                    diag = (cost[i - 1][j - 1]
                            + _pitch_cost(delta)
                            + onset_weight * oc)
                    alt = min(up, left)
                    if diag <= best:
                        best, ptr = diag, 0
                        margin[i][j] = alt - diag
                    else:
                        margin[i][j] = diag - alt
            cost[i][j] = best
            back[i][j] = ptr

    ops = []
    i, j = n, m
    while i > 0 or j > 0:
        ptr = back[i][j]
        if ptr == 0:
            ops.append((MATCH, s_sub[i - 1]["index"],
                        o_sub[j - 1]["index"], margin[i][j]))
            i -= 1
            j -= 1
        elif ptr == 1:
            ops.append((DELETE, s_sub[i - 1]["index"], None, 0.0))
            i -= 1
        else:
            ops.append((INSERT, None, o_sub[j - 1]["index"], 0.0))
            j -= 1
    ops.reverse()
    return ops


# ------------------------------------------------------------ 片段切分

def _make_regions(locks, n, m, excluded_score, excluded_obs,
                  score_notes, obs_notes):
    """按锁定锚点切网格。每个 region = 两锁之间（或两端）的开区间。"""
    regions = []
    prev_s, prev_o = 0, 0
    for s, o in locks:
        regions.append(_build_region(prev_s, s, prev_o, o,
                                     excluded_score, excluded_obs,
                                     score_notes, obs_notes))
        regions.append({"lock": (s, o)})
        prev_s, prev_o = s + 1, o + 1
    regions.append(_build_region(prev_s, n, prev_o, m,
                                 excluded_score, excluded_obs,
                                 score_notes, obs_notes))
    return regions


def _build_region(s_lo, s_hi, o_lo, o_hi, excluded_score, excluded_obs,
                  score_notes, obs_notes):
    s_sub = [score_notes[i] for i in range(s_lo, s_hi)
             if i not in excluded_score]
    o_sub = [obs_notes[j] for j in range(o_lo, o_hi)
             if j not in excluded_obs]
    return {"s_lo": s_lo, "s_hi": s_hi, "o_lo": o_lo, "o_hi": o_hi,
            "s_sub": s_sub, "o_sub": o_sub, "lock": None}


# ------------------------------------------------------------ 主入口

def align(score_notes, obs_notes, locks=(), excluded_score=(),
          excluded_obs=(), cache: Optional[dict] = None,
          forced_locks=()):
    """对齐入口，返回结构化结果。cache 非空时启用片段级增量复用。

    forced_locks：用户在"待确认"中显式确认的配对，即使观测为歧义/无声
    也强制成对（人工已消解歧义）；普通 locks 仍拒绝歧义观测。
    """
    n, m = len(score_notes), len(obs_notes)
    locks = sorted((int(a), int(b)) for a, b in locks)
    forced_locks = sorted((int(a), int(b)) for a, b in forced_locks)
    all_locks = sorted(locks + forced_locks)
    excluded_score = set(int(i) for i in excluded_score)
    excluded_obs = set(int(i) for i in excluded_obs)
    _validate_locks(locks, n, m, obs_notes)
    # 强制锁只校验越界与单调性（不校验稳定性）
    _validate_locks(forced_locks, n, m, obs_notes, check_state=False)

    regions = _make_regions(all_locks, n, m, excluded_score, excluded_obs,
                            score_notes, obs_notes)

    # 全局速度初值（仅作为片段拟合失败时的回退）
    alpha0, beta0 = _initial_tempo(score_notes, obs_notes,
                                   excluded_score, excluded_obs)

    # 逐片段：Pass A 粗配拟合"片段局部速度" -> Pass B 细配。
    # 锁之间本就独立，局部速度按片段拟合更准确；缓存键含局部参数，
    # 因此锚点增删后只有边界/排除集合真正变化的片段才会重算。
    ops = []
    cache_hits, cache_misses = [], []
    for r in regions:
        if r["lock"] is not None:
            ops.append((LOCKED_MATCH, r["lock"][0], r["lock"][1], None))
            continue
        alpha, beta = _fit_region_tempo(r, alpha0, beta0)
        # 配对点不足时，用片段内首个稳定观测锚定全局速度的截距
        if len(r["s_sub"]) < 2 or len(r["o_sub"]) < 2:
            alpha, beta = _region_fallback(r, alpha0)
        key = _cache_key(r, excluded_score, excluded_obs, alpha, beta)
        if cache is not None and key in cache:
            region_ops = cache[key]
            cache_hits.append(key)
        else:
            region_ops = _dp_region(r["s_sub"], r["o_sub"],
                                    alpha, beta, 1.0)
            cache_misses.append(key)
            if cache is not None:
                cache[key] = region_ops
        ops.extend(region_ops)

    result = _postprocess(ops, score_notes, obs_notes, alpha0, beta0,
                          excluded_score, excluded_obs,
                          {s: (o, s in dict(forced_locks))
                           for s, o in all_locks})
    result["cache"] = {"hits": len(cache_hits), "misses": len(cache_misses)}
    return result


def _region_fallback(r, alpha):
    """无足够配对点时的局部回退：全局速度 + 片段内首个稳定观测定截距。"""
    first_o = next((o for o in r["o_sub"] if o["state"] == "stable"),
                   r["o_sub"][0] if r["o_sub"] else None)
    first_s = r["s_sub"][0] if r["s_sub"] else None
    if first_o is None or first_s is None:
        return alpha, 0.0
    return alpha, first_o["onset"] - alpha * first_s["beat"]


def _fit_region_tempo(r, fallback_alpha, fallback_beta):
    """片段内 Pass A 粗配 + Theil–Sen 拟合局部速度。"""
    s_sub, o_sub = r["s_sub"], r["o_sub"]
    if len(s_sub) < 2 or len(o_sub) < 2:
        return fallback_alpha, fallback_beta
    pairs = []
    s_beat = {sn["index"]: sn["beat"] for sn in s_sub}
    o_time = {on["index"]: on["onset"] for on in o_sub}
    for op in _dp_region(s_sub, o_sub, fallback_alpha, fallback_beta, 0.25):
        if op[0] == MATCH:
            pairs.append((s_beat[op[1]], o_time[op[2]]))
    if len(pairs) < 2:
        return _region_fallback(r, fallback_alpha)
    fitted = _theil_sen(pairs)
    if fitted and fitted[0] > 0:
        return fitted
    return _region_fallback(r, fallback_alpha)


def _cache_key(r, excluded_score, excluded_obs, alpha, beta):
    es = tuple(sorted(i for i in range(r["s_lo"], r["s_hi"])
                      if i in excluded_score))
    eo = tuple(sorted(j for j in range(r["o_lo"], r["o_hi"])
                      if j in excluded_obs))
    return (r["s_lo"], r["s_hi"], r["o_lo"], r["o_hi"], es, eo,
            round(alpha, 8), round(beta, 8))


def _validate_locks(locks, n, m, obs_notes, check_state=True):
    last_s, last_o = -1, -1
    for s, o in locks:
        if not (0 <= s < n and 0 <= o < m):
            raise ValueError(f"锚点越界: score_index={s} obs_index={o}")
        if s <= last_s or o <= last_o:
            raise ValueError("锚点在两个序列上都必须严格单调")
        if check_state and obs_notes[o].get("state") != "stable":
            raise ValueError(f"观测 {o} 处于歧义/无声状态，不能锁定，"
                             "请先在待确认中处理")
        last_s, last_o = s, o


def _initial_tempo(score_notes, obs_notes, excluded_score, excluded_obs):
    valid_s = [s for s in score_notes if s["index"] not in excluded_score]
    stable = [o for o in obs_notes
              if o["state"] == "stable" and o["index"] not in excluded_obs]
    if len(valid_s) < 2 or len(stable) < 2:
        return 0.5, 0.0
    dx = valid_s[-1]["beat"] - valid_s[0]["beat"]
    if dx <= 0:
        return 0.5, 0.0
    alpha = (stable[-1]["onset"] - stable[0]["onset"]) / dx
    return max(alpha, 1e-3), stable[0]["onset"] - alpha * valid_s[0]["beat"]


# ------------------------------------------------------------ 后处理判定

def _local_slope_at(beat, chain, score_notes, smooth_slopes, default):
    """取 beat 所在音符的局部斜率（秒/四分音符）。

    区间 (chain[k] -> chain[k+1]) 的斜率对应"起点音符"的时长；
    横跨速度切换的单个区间会是异常值，用相邻区间斜率做 3 点中值稳健化。
    """
    if not chain:
        return default
    if beat <= score_notes[chain[0][0]]["beat"]:
        return smooth_slopes[0] if smooth_slopes else default
    for k in range(len(chain) - 1):
        b0 = score_notes[chain[k][0]]["beat"]
        b1 = score_notes[chain[k + 1][0]]["beat"]
        if b0 <= beat < b1:
            window = smooth_slopes[max(0, k - 1): k + 2]
            return median(window) if window else default
    return smooth_slopes[-1] if smooth_slopes else default


def _postprocess(ops, score_notes, obs_notes, alpha, beta,
                 excluded_score, excluded_obs, lock_map):
    matched_s = {}      # score_index -> obs_index
    matched_o = {}      # obs_index -> score_index
    margins = {}
    for op in ops:
        kind, si, oj, mg = op
        if kind in (MATCH, LOCKED_MATCH):
            matched_s[si] = oj
            matched_o[oj] = si
            margins[(si, oj)] = mg

    chain = sorted(matched_s.items())   # [(si, oj), ...]

    # ---- 期望起音基线 ----
    # 1) 段内逐段斜率（实际局部速度）-> 3 点中值平滑，用于 tempo_curve
    #    展示与漏音预测；
    # 2) 对所有匹配点做 Theil–Sen 稳健斜率（单点唱早/晚不影响斜率），
    #    作为判定起音偏移的"稳健速度基线"；锚点之间各自独立。
    seg_slopes = []
    for k in range(len(chain) - 1):
        s0, o0 = chain[k]
        s1, o1 = chain[k + 1]
        dbv = score_notes[s1]["beat"] - score_notes[s0]["beat"]
        if dbv > 0:
            seg_slopes.append(
                (obs_notes[o1]["onset"] - obs_notes[o0]["onset"]) / dbv)
    smooth_slopes = [median(seg_slopes[max(0, k - 1): k + 2])
                     for k in range(len(seg_slopes))]
    tempo_curve = [{
        "score_index_a": chain[k][0],
        "score_index_b": chain[k + 1][0],
        "tempo_spb": round(smooth_slopes[k], C.ROUND_NDIGITS),
        "tempo_bpm": round(60.0 / smooth_slopes[k], 3)
        if smooth_slopes[k] > 0 else None,
    } for k in range(len(smooth_slopes))]

    points = [(score_notes[si]["beat"], obs_notes[oj]["onset"])
              for si, oj in chain]
    ts = _theil_sen(points) if len(points) >= 2 else None
    # 以首个匹配（或锁定锚点）为时间零点：单点延迟不改变起点位置，
    # 它在残差序列中就是孤立脉冲（前后音符残差都为 0）。
    anchor_beat = score_notes[chain[0][0]]["beat"] if chain else 0.0
    anchor_t = obs_notes[chain[0][1]]["onset"] if chain else 0.0
    robust_slope = ts[0] if ts and ts[0] > 0 else alpha

    def predict_time(beat):
        """期望起音：锚点起点 + 稳健速度；无匹配退化为全局模型。"""
        if not chain:
            return alpha * beat + beta
        return anchor_t + (beat - anchor_beat) * robust_slope

    def predict_missing_time(beat):
        """漏音预测时间：用平滑后的实际局部速度，贴近演奏时间轴。"""
        if not chain:
            return alpha * beat + beta
        first_s, first_o = chain[0]
        if beat <= score_notes[first_s]["beat"]:
            slope = smooth_slopes[0] if smooth_slopes else robust_slope
            return (obs_notes[first_o]["onset"]
                    - (score_notes[first_s]["beat"] - beat) * slope)
        for k in range(len(chain) - 1):
            s0, o0 = chain[k]
            b0 = score_notes[s0]["beat"]
            b1 = score_notes[chain[k + 1][0]]["beat"]
            if b0 <= beat <= b1:
                return obs_notes[o0]["onset"] \
                    + (beat - b0) * (smooth_slopes[k]
                                     if k < len(smooth_slopes)
                                     else robust_slope)
        last_s, last_o = chain[-1]
        return (obs_notes[last_o]["onset"]
                + (beat - score_notes[last_s]["beat"])
                * (smooth_slopes[-1] if smooth_slopes else robust_slope))

    # 起音残差序列（实际 - 稳健速度预测）。判定"净起音偏移"时区分两种信号：
    #   * 渐快/渐慢：残差沿序列持续单向变化 -> 由局部漂移基线（5 点中值
    #     斜率的局部截距修正）吸收；
    #   * 单点唱早/晚：残差在孤立位置突跳，前一个音和后一个音都正常。
    raw_resid = []
    for si, oj in chain:
        raw_resid.append(
            (si, obs_notes[oj]["onset"]
             - predict_time(score_notes[si]["beat"])))
    # 局部漂移基线 = 残差的 5 点中值（孤立脉冲被剔除，连片趋势保留）
    resid_vals = [v for _, v in raw_resid]
    smooth_by_si = {}
    for k, (si, _v) in enumerate(raw_resid):
        smooth_by_si[si] = median(resid_vals[max(0, k - 2): k + 3])

    entries = []

    # 每个乐谱音一条
    for sn in score_notes:
        si = sn["index"]
        pred = predict_time(sn["beat"])
        # 期望时值必须用该音符位置的局部速度，不能用全局 α，
        # 否则局部提速/放慢段会被成片误报时值偏差。
        expected_dur = max(sn["dur_q"] * _local_slope_at(
            sn["beat"], chain, score_notes, smooth_slopes, robust_slope),
            1e-6)
        if si in excluded_score:
            entries.append(_entry(si, None, EXCLUDED_SCORE, "excluded",
                                  pred, expected_dur,
                                  ["user_excluded_score_note"],
                                  C.CONF_LOCKED))
            continue
        if si not in matched_s:
            entries.append(_entry(si, None, DELETE, "missing",
                                  predict_missing_time(sn["beat"]),
                                  expected_dur,
                                  ["no_observation_on_dp_path",
                                   "predicted_onset=local_tempo_smoothed"],
                                  C.CONF_DELETE_BASE))
            continue
        oj = matched_s[si]
        on = obs_notes[oj]
        _oi_lock, forced = lock_map.get(si, (None, False))
        entries.append(_judge_match(
            sn, on, pred, expected_dur, si in lock_map,
            margins.get((si, oj)),
            smooth_resid=smooth_by_si.get(si, 0.0),
            forced=forced))

    # 未匹配观测：多音 / 待确认
    for on in obs_notes:
        oj = on["index"]
        if oj in matched_o or oj in excluded_obs:
            continue
        if on["state"] == "stable":
            entries.append(_entry(None, oj, INSERT, "extra", None, None,
                                  ["no_score_note_on_dp_path",
                                   f"state={on['state']}"],
                                  C.CONF_INSERT_STABLE_BASE))
        else:
            reason = {"poly": "polyphony_ambiguous",
                      "silent": "silent_segment"}.get(on["state"],
                                                      on["state"])
            entries.append(_entry(None, oj, INSERT, "pending", None, None,
                                  [f"state={on['state']}",
                                   "no_forced_match", reason],
                                  C.CONF_PENDING))

    # 排除的观测也显式给出
    for oj in sorted(excluded_obs):
        entries.append(_entry(None, oj, EXCLUDED_OBS, "excluded", None, None,
                              ["user_excluded_observation"],
                              C.CONF_LOCKED))

    entries.sort(key=lambda e: (e["score_index"] is None,
                                e["score_index"] if e["score_index"] is not None
                                else -1,
                                e["obs_index"] if e["obs_index"] is not None
                                else -1))

    pending = [
        {"obs_index": e["obs_index"],
         "reason": next((b for b in e["basis"]
                         if b in ("polyphony_ambiguous", "silent_segment")),
                        "ambiguous")}
        for e in entries if e["status"] == "pending"
    ]

    return {
        "alpha": alpha, "beta": beta,
        "tempo_curve": tempo_curve,
        "entries": entries,
        "pending": pending,
        "locks": [{"score_index": s, "obs_index": v[0],
                   "forced_ambiguous": v[1]}
                  for s, v in sorted(lock_map.items())],
    }


def _judge_match(sn, on, pred, expected_dur, locked, margin,
                 smooth_resid=0.0, forced=False):
    tags, basis = [], []
    pitch_delta = on["midi"] - sn["midi"] if on.get("midi") is not None else None
    raw_offset = on["onset"] - pred
    # 去除连片速度漂移后的"净起音偏移"，用于判定；原始残差同时保留
    onset_offset = raw_offset - (smooth_resid or 0.0)
    dur_ratio = on["duration"] / expected_dur
    dur_dev = on["duration"] - expected_dur

    if margin is not None:
        basis.append(f"dp_margin={round(margin or 0.0, 3)}")
    else:
        basis.append("anchor_locked"
                     if not forced else "user_resolved_ambiguous_forced")
    if pitch_delta is not None:
        basis.append(f"pitch_delta={round(pitch_delta, 2)}semitones")
    else:
        basis.append(f"obs_state={on['state']}")
    basis.append(f"onset_offset={round(onset_offset, 3)}s")
    basis.append(f"raw_onset_residual={round(raw_offset, 3)}s")
    basis.append("local_tempo_baseline=median5")
    basis.append(f"duration_ratio={round(dur_ratio, 3)}")

    status = "correct"
    if pitch_delta is not None and abs(pitch_delta) >= C.PITCH_WRONG_SEMITONES:
        status = "wrong"
        tags.append("wrong_pitch")
    if abs(onset_offset) >= C.ONSET_OFFSET_S:
        tags.append("onset_offset")
        if status == "correct":
            status = "onset_offset"
    if (dur_ratio < C.DURATION_RATIO_LOW or dur_ratio > C.DURATION_RATIO_HIGH) \
            and abs(dur_dev) >= C.DURATION_MIN_DEV_S:
        tags.append("duration_deviation")
        if status == "correct":
            status = "duration"
    if on["state"] != "stable":
        tags.append(f"obs_{on['state']}")
    if locked:
        tags.append("locked")

    if locked:
        confidence = C.CONF_LOCKED
    else:
        m = margin if margin is not None else 0.0
        confidence = C.CONF_MATCH_BASE + max(0.0, m) / C.CONF_MARGIN_SCALE
        confidence = min(0.98, max(0.25, confidence))

    return {
        "score_index": sn["index"], "obs_index": on["index"],
        "op": LOCKED_MATCH if locked else MATCH,
        "status": status, "tags": tags,
        "confidence": round(confidence, 3),
        "pitch_delta": None if pitch_delta is None else round(pitch_delta, 3),
        "onset_offset": round(onset_offset, C.ROUND_NDIGITS),
        "duration_ratio": round(dur_ratio, 3),
        "duration_deviation_s": round(dur_dev, C.ROUND_NDIGITS),
        "predicted_onset": round(pred, C.ROUND_NDIGITS),
        "expected_duration_s": round(expected_dur, C.ROUND_NDIGITS),
        "basis": basis,
    }


def _entry(si, oj, op, status, pred, expected_dur, basis, confidence):
    return {
        "score_index": si, "obs_index": oj, "op": op,
        "status": status, "tags": [],
        "confidence": round(confidence, 3),
        "pitch_delta": None, "onset_offset": None,
        "duration_ratio": None, "duration_deviation_s": None,
        "predicted_onset": (round(pred, C.ROUND_NDIGITS)
                            if pred is not None else None),
        "expected_duration_s": (round(expected_dur, C.ROUND_NDIGITS)
                                if expected_dur is not None else None),
        "basis": basis,
    }

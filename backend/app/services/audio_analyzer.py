"""录音分析：起音检测 + 逐段基频提取与置信状态分类。

输出观测序列 obs_notes，每段带：
  onset / duration（秒）、midi（稳定时）、state：
    stable —— 段内多数有声帧支持同一主峰基频，可自动对齐；
    poly   —— 明显存在第二峰（多声部歧义），不允许强配，进待确认；
    silent —— 起音能量存在但无稳定基频（气息/噪音/漏唱），进待确认。
另返回波形峰（降采样振幅包络）供前端绘制，避免导出整段音频数据。
"""

from __future__ import annotations

import numpy as np

from .. import config as C

F0_MIN_MIDI = 36.0   # C2
F0_MAX_MIDI = 96.0   # C7


def analyze_audio(path: str):
    """返回 (obs_notes, duration, peaks, warnings)。"""
    import librosa

    warnings = []
    y, sr = librosa.load(path, sr=C.SAMPLE_RATE, mono=True)
    duration = float(len(y) / sr)
    if duration < 0.2:
        raise ValueError("音频过短（<0.2s），无法分析")

    # ---- 起音 ----
    hop = 512
    onset_env = librosa.onset.onset_strength(
        y=y, sr=sr, hop_length=hop, aggregate=np.median)
    onset_frames = librosa.util.peak_pick(
        onset_env, pre_max=3, post_max=6, pre_avg=3, post_avg=5,
        delta=0.07 * (onset_env.max() if onset_env.size else 1.0),
        wait=4)
    onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop)

    # 合并过近的起音
    onset_times = _merge_close(onset_times, C.ONSET_MIN_GAP_S, onset_env,
                               onset_frames)
    if len(onset_times) == 0:
        warnings.append("未检测到任何起音，全部乐谱音将判为漏音")

    # 段边界
    bounds = list(onset_times) + [duration]

    # ---- 整曲基频（pyin） ----
    f0, vflag, vprob = librosa.pyin(
        y, fmin=float(librosa.midi_to_hz(F0_MIN_MIDI)),
        fmax=float(librosa.midi_to_hz(F0_MAX_MIDI)),
        sr=sr, hop_length=hop, fill_na=np.nan)
    frame_times = librosa.frames_to_time(np.arange(len(f0)), sr=sr,
                                         hop_length=hop)

    # 频谱帧用于多声部判定（每帧主频峰占比）
    S = np.abs(librosa.stft(y, hop_length=hop))
    poly_frame = _polyphony_flags(S)

    obs_notes = []
    for i, t0 in enumerate(onset_times):
        t1 = bounds[i + 1]
        seg = _describe_segment(i, t0, t1, frame_times, f0, vprob,
                                poly_frame, onset_env, onset_frames,
                                sr, hop)
        obs_notes.append(seg)

    peaks = _waveform_peaks(y, sr, bins=1000)
    return obs_notes, duration, peaks, warnings


def _merge_close(times, min_gap, onset_env, onset_frames):
    """贪心合并：相邻起音过近时保留能量更强的一个。"""
    if len(times) < 2:
        return list(times)
    strengths = onset_env[onset_frames] if len(onset_frames) else None
    kept = []
    for k, t in enumerate(times):
        if kept and t - kept[-1][0] < min_gap:
            prev_t, prev_k = kept[-1]
            if strengths is not None and strengths[k] > strengths[prev_k]:
                kept[-1] = (t, k)
        else:
            kept.append((t, k))
    return np.array([t for t, _ in kept])


def _polyphony_flags(S):
    """每帧估计"第二频谱峰能量占比"，超过阈值即视为多声帧。"""
    if S.shape[1] == 0:
        return np.zeros(0)
    # 限制在基频可能区域的谐波范围（~60Hz-4kHz）
    freqs = librosa.fft_frequencies(sr=C.SAMPLE_RATE, n_fft=2048)
    band = (freqs >= 60) & (freqs <= 4000)
    sub = S[band].copy()
    peak1 = np.max(sub, axis=0)
    # 第二峰：向量化掩蔽每帧主峰 ±2 个 bin 后再取最大
    idx = np.argmax(sub, axis=0)
    cols = np.arange(sub.shape[1])
    for dk in (-2, -1, 0, 1, 2):
        rows = np.clip(idx + dk, 0, sub.shape[0] - 1)
        sub[rows, cols] = 0.0
    peak2 = np.max(sub, axis=0)
    ratio = np.divide(peak2, peak1, out=np.zeros_like(peak1),
                      where=peak1 > 1e-9)
    return ratio


def _describe_segment(i, t0, t1, frame_times, f0, vprob, poly_frame,
                      onset_env, onset_frames, sr, hop):
    mask = (frame_times >= t0) & (frame_times < t1)
    seg_f0 = f0[mask]
    seg_vp = vprob[mask]
    seg_poly = poly_frame[mask] if poly_frame.size else np.array([])

    voiced = ~np.isnan(seg_f0)
    voiced_ratio = float(np.mean(voiced)) if len(voiced) else 0.0
    poly_ratio = float(np.mean(seg_poly > 0.35)) if seg_poly.size else 0.0

    state = "stable"
    if voiced_ratio < C.VOICED_RATIO_SILENT:
        state = "silent"
    elif poly_ratio > C.POLY_RATIO_AMBIG:
        state = "poly"

    midi = None
    if state == "stable" and voiced.any():
        mvals = librosa.hz_to_midi(seg_f0[voiced])
        # 主峰：取出现最集中的 MIDI（整音聚类）
        rounded = np.round(mvals).astype(int)
        vals, counts = np.unique(rounded, return_counts=True)
        top = int(vals[np.argmax(counts)])
        support = counts.max() / counts.sum()
        if support < 0.5:
            state = "poly"
        else:
            midi = float(np.median(mvals[np.abs(mvals - top) <= 0.5]))
            if support < 0.7:
                # 能配上但基频不稳：仍可对齐，置信度交由上层压低
                pass

    # 起音强度（归一化 0~1）
    f0_frame = int(round(t0 * sr / hop))
    strength = float(onset_env[min(f0_frame, len(onset_env) - 1)])
    if onset_env.size and onset_env.max() > 0:
        strength /= float(onset_env.max())

    return {
        "index": i,
        "midi": round(midi, 3) if midi is not None else None,
        "onset": round(float(t0), C.ROUND_NDIGITS),
        "duration": round(float(max(t1 - t0, C.SEG_MIN_DURATION_S)),
                          C.ROUND_NDIGITS),
        "state": state,
        "voiced_ratio": round(voiced_ratio, 3),
        "poly_ratio": round(poly_ratio, 3),
        "onset_strength": round(strength, 3),
    }


def _waveform_peaks(y, sr, bins=1000):
    """把波形降采样为 bins 个正/负振幅峰，供前端轻量绘制。"""
    if len(y) == 0:
        return [0.0] * bins
    step = max(1, len(y) // bins)
    trimmed = y[: step * bins]
    if trimmed.size == 0:
        return [0.0]
    reshaped = trimmed.reshape(-1, step)
    peaks = np.max(np.abs(reshaped), axis=1)
    m = float(np.max(peaks)) or 1.0
    return [round(float(p / m), 4) for p in peaks]

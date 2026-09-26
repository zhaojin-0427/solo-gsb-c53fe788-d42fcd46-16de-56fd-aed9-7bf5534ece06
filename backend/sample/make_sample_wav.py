#!/usr/bin/env python3
"""生成示例 WAV（仅用标准库）。

对应 sample_score.musicxml 展开后的 24 个四分音符（4+4 小节反复一次，
100BPM，每音 0.6s）。刻意制造多种偏差，方便复盘台演示：

  #5  错音（谱 A4 弹 F#4，差 3 半音）
  #8  漏音（谱 C5 第二遍，直接不弹）
  #13 起音晚 0.25s
  #14 时值只弹 0.25s（时值偏短）
  #17 额外多音（在谱音前插入一个谱上没有的高音）
  #19 多声部歧义片段（两音叠加），不会被强配
  #22 有声起音但无稳定基频（噪声段），进入无声歧义

用法：
    python3 make_sample_wav.py [-o sample_performance.wav]
"""

import argparse
import math
import struct
import wave

SR = 22050
BEAT = 0.6  # 100 BPM

# 展开后的序列：C4 D4 E4 F4 G4 A4 B4 C5  x2，D5 E5 F5 G5，A5 G5 E5 C5
EXPANDED = (
    [60, 62, 64, 65, 67, 69, 71, 72] * 2
    + [74, 76, 77, 79]
    + [81, 79, 76, 72]
)


def midi_to_freq(m):
    return 440.0 * 2 ** ((m - 69) / 12.0)


def tone(freq, dur, amp=0.35, harmonics=True):
    """带起音瞬态与指数衰减的单音，便于 onset 检测。"""
    n = int(SR * dur)
    out = []
    for i in range(n):
        t = i / SR
        env = math.exp(-3.0 * t / max(dur, 1e-6))
        v = math.sin(2 * math.pi * freq * t)
        if harmonics:
            v += 0.35 * math.sin(2 * math.pi * 2 * freq * t)
            v += 0.15 * math.sin(2 * math.pi * 3 * freq * t)
        # 起音处加一个短脉冲（click）增强起音特征
        click = 0.6 * math.exp(-t * 200.0) * math.sin(
            2 * math.pi * 1800 * t) if t < 0.02 else 0.0
        out.append(amp * env * v + amp * click)
    return out


def noise_burst(dur, amp=0.25):
    """粉噪-ish 无声基频片段（有起音能量但无稳定 f0）。"""
    n = int(SR * dur)
    out, last = [], 0.0
    for i in range(n):
        t = i / SR
        x = math.sin(i * 12.9898) * 43758.5453
        x = x - math.floor(x)
        last = 0.7 * last + 0.3 * (x * 2 - 1)  # 低通滤波
        env = math.exp(-6 * t / max(dur, 1e-6))
        click = 0.5 * math.exp(-t * 150.0) if t < 0.03 else 0.0
        out.append(amp * env * last + amp * click)
    return out


def place(buf, start, samples):
    end = start + len(samples)
    if len(buf) < end:
        buf.extend([0.0] * (end - len(buf)))
    for i, v in enumerate(samples):
        buf[start + i] += v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="sample_performance.wav")
    args = ap.parse_args()

    buf = []
    events = []  # (开始秒, MIDI 或 None=噪声, 时长)
    for i, m in enumerate(EXPANDED):
        start = i * BEAT
        dur = BEAT * 0.95

        if i == 8:
            # 漏音：完全不发声
            continue
        if i == 22:
            # 有声起音但无稳定基频：用噪声替代谱音（无声歧义）
            events.append((start, None, BEAT * 0.8))
            continue

        if i == 5:
            m = 66                      # 错音：A4(69) -> F#4(66)
        if i == 13:
            start += 0.25               # 起音晚 0.25s
        if i == 14:
            dur = 0.25                  # 时值偏短

        events.append((start, m, dur))
        if i == 17:
            events.append((17 * BEAT - 0.22, 88, 0.2))    # 额外多音
        if i == 19:
            events.append((19 * BEAT, EXPANDED[19] + 5,
                           BEAT * 0.9))                  # 多声部叠加

    for start, m, dur in events:
        samples = noise_burst(dur) if m is None \
            else tone(midi_to_freq(m), dur)
        place(buf, int(start * SR), samples)

    # 尾部留白
    place(buf, int(24 * BEAT * SR) + SR // 4, [0.0])

    # 归一化并写 16bit PCM
    peak = max(abs(v) for v in buf) or 1.0
    frames = b"".join(struct.pack("<h", int(max(-1, min(1, v / peak))
                                        * 32767)) for v in buf)
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(frames)
    print(f"已写出 {args.out}：{len(buf)/SR:.2f}s，{len(events)} 个声音事件 "
          f"（谱面应为 24 音，含 1 漏音 + 1 额外音 + 2 歧义片段）")


if __name__ == "__main__":
    main()

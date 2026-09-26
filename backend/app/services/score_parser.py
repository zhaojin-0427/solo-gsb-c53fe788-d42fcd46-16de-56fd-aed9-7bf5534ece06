"""MusicXML 解析：展开反复记号，输出期望音符序列。

每个期望音符同时带两套坐标：
* beat / dur_q：展开后的四分音符坐标（对齐与播放使用）；
* measure / beat_in_measure / occurrence：原谱坐标（报告与导出使用），
  occurrence 表示该小节因反复第几次演奏（0 基）。

约定：单声部输入。多声部（多 part 或单小节内和弦）只取最高音，
并把歧义写入 warnings，供前端提示"多声部歧义片段"。
"""

from __future__ import annotations

from .. import config as C  # noqa: F401


def parse_musicxml(path: str):
    """返回 (score_notes, warnings)。

    score_notes: [{index, midi, beat, dur_q, measure, beat_in_measure,
                   occurrence, part}]
    """
    from music21 import converter, note as m21_note, chord as m21_chord
    from music21 import repeat, stream as m21_stream  # noqa: F401

    score = converter.parse(path)
    warnings = []

    parts = list(score.parts) if hasattr(score, "parts") else [score]
    if len(parts) > 1:
        warnings.append(f"检测到 {len(parts)} 个声部，仅取最高声部，"
                        "多声部片段需人工确认")

    part = parts[0]
    try:
        expanded = part.expandRepeats()
        expansion_ok = True
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"music21 反复展开失败({exc})，使用未展开谱面")
        expanded = part
        expansion_ok = False

    notes_out = []
    occurrence_counter = {}
    visited_measures = set()
    idx = 0

    for el in expanded.recurse():
        # 单声部：处理 Note；Chord 取最高音但保留原元素的上下文坐标
        if isinstance(el, m21_note.Note):
            midi = float(el.pitch.midi)
        elif isinstance(el, m21_chord.Chord):
            top = max(el.pitches, key=lambda p: p.midi)
            warnings.append(
                f"小节 {el.measureNumber} 出现和弦，"
                f"只取最高音 {top.nameWithOctave}，请人工确认")
            midi = float(top.midi)
        else:
            continue

        try:
            measure_obj = el.measureNumber
        except Exception:  # noqa: BLE001
            measure_obj = None
        measure = measure_obj if isinstance(measure_obj, int) else None

        # 展开后顺序遍历时，第一次回到已出现过的小节号，即反复的又一次演奏
        if measure is not None:
            if measure in visited_measures:
                occurrence_counter[measure] = occurrence_counter.get(
                    measure, 0) + 1
            visited_measures.add(measure)
        occ = occurrence_counter.get(measure, 0)

        try:
            beat_in_measure = float(el.beat)
        except Exception:  # noqa: BLE001
            beat_in_measure = None
        expanded_beat = float(el.getOffsetInHierarchy(expanded))

        notes_out.append({
            "index": idx,
            "midi": midi,
            "beat": expanded_beat,
            "dur_q": float(el.quarterLength),
            "measure": measure,
            "beat_in_measure": round(beat_in_measure, 4)
            if beat_in_measure is not None else None,
            "occurrence": occ,
            "part": 0,
        })
        idx += 1

    # 去重保护：展开后 recurse 在个别 music21 版本会重复产出元素，
    # 以展开坐标 + MIDI 去重，再重排 index。
    deduped, seen = [], set()
    for n in notes_out:
        key = (n["measure"], n["beat_in_measure"], n["occurrence"],
               n["midi"], round(n["beat"], 5))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)
    for i, n in enumerate(deduped):
        n["index"] = i

    if not expansion_ok:
        pass
    if not deduped:
        raise ValueError("谱面中未解析出任何音符，请确认是单声部 MusicXML")
    return deduped, warnings

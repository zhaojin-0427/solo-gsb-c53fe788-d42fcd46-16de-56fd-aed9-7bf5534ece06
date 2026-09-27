"""反复轮次计数回归测试（music21 用注入桩替代，纯标准库可跑）。

验证 score_parser.parse_musicxml 的轮次语义：
  * 轮次按**展开后的小节实例**计数：同一次演奏（展开序列中的同一个
    小节实例）内的所有音符共用同一 occurrence，仅再次进入同一原谱
    小节号时递增——旧版对小节内逐音递增，本测试锁定修正后的行为；
  * 无反复时全部轮次 0；无小节结构的扁平流退化为轮次 0；
  * 和弦只取最高音并写入 warnings。

直接运行：python3 tests/test_score_parser.py
"""
import os
import sys
import types

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)


# ------------------------------------------------------------ 最小 music21 桩
class _Pitch:
    def __init__(self, midi):
        self.midi = midi
        self.nameWithOctave = f"N{midi}"


class _El:
    def __init__(self, midi, beat, ql, expanded_beat):
        self.pitch = _Pitch(midi)
        self.pitches = [self.pitch]
        self.beat = beat
        self.quarterLength = ql
        self._expanded_beat = expanded_beat

    def getOffsetInHierarchy(self, _root):
        return self._expanded_beat


class _Note(_El):
    pass


class _Chord(_El):
    def __init__(self, midis, beat, ql, expanded_beat):
        super().__init__(max(midis), beat, ql, expanded_beat)
        self.pitches = [_Pitch(m) for m in midis]


class _Measure:
    def __init__(self, number, elements):
        self.number = number
        self._elements = elements

    def recurse(self):
        return iter(list(self._elements))


class _Part:
    def __init__(self, measures=None, flat=None):
        self._measures = measures or []
        self._flat = flat or []

    def expandRepeats(self):
        return self

    def getElementsByClass(self, cls):
        return [m for m in self._measures if isinstance(m, cls)]

    def recurse(self):
        return iter(list(self._flat))


class _Score:
    def __init__(self, part):
        self.parts = [part]


_PART = {"part": None}


def _install_music21_stub():
    m21 = types.ModuleType("music21")
    m21.converter = types.SimpleNamespace(
        parse=lambda path: _Score(_PART["part"]))
    m21.note = types.SimpleNamespace(Note=_Note)
    m21.chord = types.SimpleNamespace(Chord=_Chord)
    m21.stream = types.SimpleNamespace(Measure=_Measure)
    sys.modules["music21"] = m21


_install_music21_stub()

from app.services import score_parser  # noqa: E402


def _meas(number, midis, base_beat):
    """一小节：midis 依次落在拍 1..n，展开拍位从 base_beat 起。"""
    return _Measure(number, [
        _Note(m, i + 1, 1.0, base_beat + i) for i, m in enumerate(midis)])


# ------------------------------------------------------------ 用例

def test_occurrence_per_measure_instance():
    # 展开序列：m1 m2 m1 m2 m3（m1/m2 反复一遍），每小节 4 音
    _PART["part"] = _Part(measures=[
        _meas(1, [60, 62, 64, 65], 0.0),
        _meas(2, [67, 69, 71, 72], 4.0),
        _meas(1, [60, 62, 64, 65], 8.0),
        _meas(2, [67, 69, 71, 72], 12.0),
        _meas(3, [74, 76, 77, 79], 16.0),
    ])
    notes, warnings = score_parser.parse_musicxml("dummy.musicxml")
    assert not warnings
    occ = [n["occurrence"] for n in notes]
    # 同一次演奏的小节音符共用轮次；再次进入该小节才递增
    assert occ == [0] * 8 + [1] * 8 + [0] * 4, occ
    assert [n["measure"] for n in notes] == \
        [1] * 4 + [2] * 4 + [1] * 4 + [2] * 4 + [3] * 4
    # 旧版错误：同一小节第 2 个音起轮次逐音递增。锁定：小节内轮次唯一
    for k in range(0, 20, 4):
        assert len(set(occ[k:k + 4])) == 1
    # 展开拍位与小节内拍位保持解析结果
    assert notes[0]["beat"] == 0.0 and notes[0]["beat_in_measure"] == 1.0
    assert notes[8]["beat"] == 8.0 and notes[8]["occurrence"] == 1
    # 下标连续稳定
    assert [n["index"] for n in notes] == list(range(20))
    print("OK occurrence counted per expanded measure instance")


def test_no_repeat_all_zero():
    _PART["part"] = _Part(measures=[
        _meas(1, [60, 62], 0.0), _meas(2, [64, 65], 2.0)])
    notes, _ = score_parser.parse_musicxml("dummy.musicxml")
    assert [n["occurrence"] for n in notes] == [0, 0, 0, 0]
    print("OK no-repeat score keeps occurrence 0")


def test_flat_stream_fallback():
    # 无小节结构的扁平流：全部轮次 0、measure=None
    _PART["part"] = _Part(measures=[], flat=[
        _Note(60, 1, 1.0, 0.0), _Note(62, 2, 1.0, 1.0)])
    notes, _ = score_parser.parse_musicxml("dummy.musicxml")
    assert [n["occurrence"] for n in notes] == [0, 0]
    assert all(n["measure"] is None for n in notes)
    print("OK flat stream falls back to occurrence 0")


def test_chord_takes_top_and_shares_occurrence():
    chord = _Chord([60, 64, 67], 1, 1.0, 0.0)
    _PART["part"] = _Part(measures=[
        _Measure(1, [chord, _Note(65, 2, 1.0, 1.0)]),
        _Measure(1, [_Note(60, 1, 1.0, 2.0)]),  # 再次进入小节 1
    ])
    notes, warnings = score_parser.parse_musicxml("dummy.musicxml")
    assert notes[0]["midi"] == 67.0  # 和弦取最高音
    assert any("和弦" in w for w in warnings)
    # 第一遍两音（含和弦）共用轮次 0；第二遍轮次 1
    assert [n["occurrence"] for n in notes] == [0, 0, 1]
    print("OK chord top note + shared occurrence per instance")


def main():
    test_occurrence_per_measure_instance()
    test_no_repeat_all_zero()
    test_flat_stream_fallback()
    test_chord_takes_top_and_shares_occurrence()
    print("SCORE PARSER OK")


if __name__ == "__main__":
    main()

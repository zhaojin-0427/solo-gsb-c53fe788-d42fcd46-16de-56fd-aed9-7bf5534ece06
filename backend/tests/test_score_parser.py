"""score_parser 纯标准库测试（music21 用最小桩替代）。

覆盖反复轮次（occurrence）语义：轮次按**展开后的小节实例**计数，同一次
演奏的小节内全部音符共用同一 occurrence，仅再次进入同号小节（反复 /
房子）时递增；同一小节内有多个音符时不得从第二个音符起逐音递增。

直接运行：python3 tests/test_score_parser.py
"""
import os
import sys
import types
import tempfile

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["DB_PATH"] = os.path.join(TMP, "test_parser.db")

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)


# ------------------------------------------------------------ music21 桩

class Measure:
    def __init__(self, number):
        self.number = number


class Pitch:
    def __init__(self, midi):
        self.midi = midi


class Note:
    def __init__(self, midi, measure, beat, offset, ql=1.0):
        self.pitch = Pitch(midi)
        self._measure = measure
        self.beat = beat
        self._offset = offset
        self.quarterLength = ql
        self.measureNumber = measure.number

    def getContextByClass(self, cls):
        return self._measure if cls is Measure else None

    def getOffsetInHierarchy(self, _stream):
        return self._offset


class Chord(Exception):
    """桩 Chord 基类（与 Note 不相交，测试不产出和弦实例）。"""


class Part:
    def __init__(self, elements):
        self._elements = elements

    def recurse(self):
        return iter(self._elements)

    def expandRepeats(self):
        return self


class Score:
    def __init__(self, part):
        self.parts = [part]


def install_stub(elements):
    score = Score(Part(elements))
    music21 = types.ModuleType("music21")
    music21.converter = types.SimpleNamespace(parse=lambda path: score)
    music21.note = types.SimpleNamespace(Note=Note)
    music21.chord = types.SimpleNamespace(Chord=Chord)
    music21.repeat = types.SimpleNamespace()
    music21.stream = types.SimpleNamespace(Measure=Measure)
    sys.modules["music21"] = music21


# ------------------------------------------------------------ 用例

def _expanded_sequence():
    """m1(4音) m2(4音) 反复一遍 -> 展开 m1 m2 m1' m2'。"""
    elements, expected = [], []
    offset = 0.0
    for occ in (0, 1):
        for mnum in (1, 2):
            m = Measure(mnum)
            for b in range(4):
                elements.append(Note(60 + b, m, float(b + 1), offset))
                expected.append((mnum, occ))
                offset += 1.0
    return elements, expected


def test_repeated_measure_occurrence():
    elements, expected = _expanded_sequence()
    install_stub(elements)
    from app.services import score_parser
    notes, warnings = score_parser.parse_musicxml("dummy.xml")
    assert [(n["measure"], n["occurrence"]) for n in notes] == expected
    # 同一次演奏的小节音符共用 occurrence：每个小节实例 4 个音轮次相同
    assert [n["occurrence"] for n in notes] == \
        [0] * 8 + [1] * 8
    assert [n["index"] for n in notes] == list(range(16))
    assert [n["beat"] for n in notes] == [float(i) for i in range(16)]
    print("OK repeated measure: occurrence shared per measure instance")


def test_multi_note_measure_without_repeat():
    # 修复前：同小节第 2 个音符起逐音递增（0,1,2,3）；修复后共用 0
    m1 = Measure(1)
    elements = [Note(60 + b, m1, float(b + 1), float(b)) for b in range(4)]
    install_stub(elements)
    from app.services import score_parser
    notes, _ = score_parser.parse_musicxml("dummy2.xml")
    assert [n["occurrence"] for n in notes] == [0, 0, 0, 0]
    assert [n["measure"] for n in notes] == [1, 1, 1, 1]
    print("OK multi-note measure without repeat shares occurrence 0")


def test_measure_occurrences_helper():
    from app.services import score_parser
    assert score_parser.measure_occurrences([1, 2, 1, 2, 3]) == \
        [0, 0, 1, 1, 0]
    assert score_parser.measure_occurrences([1, 1, 1]) == [0, 1, 2]
    assert score_parser.measure_occurrences([4]) == [0]
    assert score_parser.measure_occurrences([]) == []
    assert score_parser.measure_occurrences([None, 1, None]) == [0, 0, 1]
    print("OK measure_occurrences helper")


def main():
    test_repeated_measure_occurrence()
    test_multi_note_measure_without_repeat()
    test_measure_occurrences_helper()
    print("SCORE PARSER OK")


if __name__ == "__main__":
    main()

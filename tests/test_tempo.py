import mido
import numpy as np
import pytest

from onbgm.arrange import arrange
from onbgm.midi_io import write_midi
from onbgm.score import ScoreError, load
from onbgm.tempo import Segment, TempoMap, build, parse_tempo, solve_base

SCORE = """
    meta: {{bpm: {bpm}}}
    sections:
{sections}
    harmony: {{a: "C", b: "G", c: "C"}}
    tracks:
      keys: {{sound: piano, texture: comp, rhythm: "x--- x--- x--- x---"}}
"""


def _score(write_score, sections, bpm=120):
    return load(write_score(SCORE.format(bpm=bpm, sections=sections)))


def test_ramp_time_is_the_exact_integral():
    seg = Segment(0, 8, 120, 60)  # 8 拍里从 120 线性降到 60
    k = -60 / 8
    assert seg.seconds_into(8) == pytest.approx(60 / k * np.log(60 / 120))
    numeric = sum(60 / (120 + k * (b + 0.0005)) * 0.001 for b in np.arange(0, 8, 0.001))
    assert seg.seconds_into(8) == pytest.approx(numeric, rel=1e-4)


def test_beat_and_seconds_round_trip():
    tmap = build([(0, 8), (8, 16), (16, 24)], [None, (110.0, 110.0), (None, 70.0)], 120)
    for beat in [0, 3.3, 8, 11.7, 16, 19.9, 24, 26]:
        assert tmap.beat_at(tmap.seconds(beat)) == pytest.approx(beat, abs=1e-9)


def test_sections_can_have_their_own_tempo_and_ritardando(write_score):
    score = _score(write_score, """
      - {name: a, bars: 2}
      - {name: b, bars: 2, tempo: 100}
      - {name: c, bars: 2, tempo: "->60"}""")
    assert score.section("b").start == 8 and score.seconds(8) == pytest.approx(4.0)          # 120 BPM 2 小节
    assert score.seconds(16) == pytest.approx(4.0 + 8 * 60 / 100)                          # 100 BPM 2 小节
    assert score.bpm_at(16) == pytest.approx(100) and score.bpm_at(23.999) == pytest.approx(60, abs=0.01)
    assert parse_tempo("->60") == (None, 60.0) and parse_tempo("96->72") == (96.0, 72.0)


def test_at_constraint_can_be_met_by_a_section_tempo(write_score):
    # 120 BPM 下第 3 段本应从 0:08 开始；把第 2 段设成 110 BPM，就推迟到 0:08.36
    score = _score(write_score, """
      - {name: a, bars: 2}
      - {name: b, bars: 2, tempo: 110}
      - {name: c, bars: 2, at: "0:08.36"}""")
    assert score.seconds(16) == pytest.approx(8.3636, abs=0.01)


def test_auto_base_tempo_is_solved_around_fixed_sections(write_score):
    score = load(write_score("""
        meta: {bpm: auto, length: "0:12"}
        sections:
          - {name: a, bars: 2}
          - {name: b, bars: 2, tempo: 60}
          - {name: c, bars: 2}
        harmony: {a: "C", b: "G", c: "C"}
    """))
    assert score.seconds(score.total_beats) == pytest.approx(12.0, abs=0.005)
    assert score.bpm == pytest.approx(240, rel=1e-3)  # b 段 60 BPM 占 8 秒，剩下 4 秒给 a、c 共 16 拍


def test_midi_tempo_events_reproduce_the_tempo_map(write_score, tmp_path):
    score = _score(write_score, """
      - {name: a, bars: 2}
      - {name: b, bars: 2, tempo: 100}
      - {name: c, bars: 2, tempo: "->60"}""")
    parts, _ = arrange(score)
    path = tmp_path / "t.mid"
    write_midi(path, score, parts)
    assert mido.MidiFile(path).length == pytest.approx(score.seconds(score.total_beats), abs=0.01)


def test_bad_tempo_is_reported(write_score):
    with pytest.raises(ScoreError) as err:
        _score(write_score, """
      - {name: a, bars: 2, tempo: "fast"}
      - {name: b, bars: 2}
      - {name: c, bars: 2}""")
    assert err.value.problems[0].path == "sections.0.tempo"


def test_auto_bpm_conflict_does_not_suggest_changing_bpm(write_score):
    """回归测试：bpm 已经是 auto，报错却同时建议"把 bpm 改为 X（或 auto）"。"""
    with pytest.raises(ScoreError) as err:
        load(write_score("""
            meta: {bpm: auto}
            sections:
              - {name: a, bars: 2}
              - {name: b, bars: 2, at: "0:04"}
              - {name: c, bars: 2, at: "0:09"}
            harmony: {a: "C", b: "G", c: "C"}
        """))
    p = err.value.problems[0]
    assert p.path == "sections.c.at" and "互相矛盾" in p.message and "把 bpm 改为" not in p.message


def test_length_shortfall_suggests_a_tail(write_score):
    with pytest.raises(ScoreError) as err:
        load(write_score("""
            meta: {bpm: 120, length: "0:13"}
            sections:
              - {name: a, bars: 2}
              - {name: b, bars: 4}
            harmony: {a: "C", b: "G"}
        """))
    p = err.value.problems[0]
    assert p.path == "meta.length" and p.fix["tail"] == 1.0

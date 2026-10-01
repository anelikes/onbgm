import mido
import pytest

from onbgm.arrange import arrange
from onbgm.midi_io import write_midi
from onbgm.player import player_data
from onbgm.score import ScoreError, load

WALTZ = """
    meta: {{bpm: 120, meter: "{meter}"{extra}}}
    sections:
      - {{name: a, bars: 2, energy: 0.6}}
      - {{name: b, bars: 2, energy: 0.9}}
    harmony: {{a: "C | G", b: "F | C"}}
    tracks:
      lead: {{sound: piano, texture: melody, in: [a], notes: {{a: "{melody}"}}}}
      bass: {{sound: pizzicato, texture: bass-pizz}}
      gtr: {{sound: guitar-steel, texture: strum, pattern: "{strum}"}}
"""


def _load(write_score, meter="3/4", melody="E5/4 D5/4 C5/4 | D5/2.", strum="D...D.D...D.", extra=""):
    return load(write_score(WALTZ.format(meter=meter, melody=melody, strum=strum, extra=extra)))


def test_three_four_bars_are_three_beats(write_score):
    score = _load(write_score)
    assert score.beats_per_bar == 3 and score.total_beats == 12
    assert [s.start for s in score.sections] == [0, 6]
    assert [(c.start, c.dur, c.chord.symbol) for c in score.chords[:2]] == [(0, 3, "C"), (3, 3, "G")]
    assert score.seconds(score.total_beats) == pytest.approx(6.0)  # 4 小节 × 3 拍 × 0.5 秒


def test_melody_bar_length_follows_the_meter(write_score):
    with pytest.raises(ScoreError) as err:
        _load(write_score, melody="E5/4 D5/4 C5/4 D5/4 | C5/2.")
    assert "应为 3 拍" in str(err.value)


def test_accents_follow_the_meter(write_score):
    waltz = _load(write_score)
    assert [waltz.accent(b) for b in (0, 1, 2, 0.5, 3)] == [8, 0, 0, -5, 8]
    jig = _load(write_score, meter="6/8", melody="E5/8 D5/8 C5/8 D5/4. | C5/2.")
    assert [jig.accent(b) for b in (0, 0.5, 1, 1.5, 2, 0.25)] == [8, -2, -2, 4, -2, -5]
    four = load(write_score("""
        meta: {bpm: 120}
        sections: [{name: a, bars: 1}]
        harmony: {a: "C"}
    """, "four.yaml"))
    assert [four.accent(b) for b in (0, 1, 2, 3, 0.5)] == [8, 0, 4, 0, -5]  # 4/4 和原来一样


def test_six_eight_patterns_and_bass_follow_the_pulse(write_score):
    score = _load(write_score, meter="6/8", melody="E5/8 D5/8 C5/8 D5/4. | C5/2.", strum="D.uD.u")
    parts = {p.name: p for p in arrange(score)[0]}
    strums = sorted({round(n.start * 2) / 2 for n in parts["gtr"].notes if n.start < 2.9})  # 下一小节第一拍可能被人性化提前一点
    assert strums == [0.0, 1.0, 1.5, 2.5]  # 每小节 6 步，每步一个八分音符
    bass = sorted(round(n.start, 1) for n in parts["bass"].notes if n.start < 3)
    assert bass == [0.0, 1.5]  # 每个附点四分大拍一下


def test_midi_time_signature_and_player_data(write_score, tmp_path):
    score = _load(write_score, meter="6/8", melody="E5/8 D5/8 C5/8 D5/4. | C5/2.")
    parts, _ = arrange(score)
    write_midi(tmp_path / "x.mid", score, parts)
    sig = next(m for t in mido.MidiFile(tmp_path / "x.mid").tracks for m in t if m.type == "time_signature")
    assert (sig.numerator, sig.denominator) == (6, 8)
    data = player_data(score, parts, tmp_path / "x.wav", 6.0)
    assert data["beats_per_bar"] == 3 and data["meter"] == "6/8" and len(data["beats"]) == 13


def test_bad_meter_and_compound_swing_are_reported(write_score):
    with pytest.raises(ScoreError) as err:
        _load(write_score, meter="5/4")
    assert "meta.meter" in str(err.value)
    with pytest.raises(ScoreError) as err:
        _load(write_score, meter="6/8", melody="E5/8 D5/8 C5/8 D5/4. | C5/2.", extra=", swing: 0.6")
    assert "复合拍子" in str(err.value)

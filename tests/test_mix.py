import numpy as np

from onbgm.arrange import arrange
from onbgm.render import SR, automix
from onbgm.score import load


def test_automix_ignores_note_jittered_into_previous_section(write_score):
    """回归测试：副歌第一个音被人性化提前了几毫秒，曾被当成前一段也有旋律，给出 +52 dB 的增益。"""
    score = load(write_score("""
        meta: {bpm: 120}
        sections: [{name: a, bars: 2}, {name: b, bars: 2}]
        harmony: {a: "C", b: "C"}
        tracks:
          keys: {sound: piano, texture: comp, rhythm: "x---------------"}
          lead: {sound: piano, notes: {b: "C5/1 | C5/1"}}
    """))
    parts, _ = arrange(score)
    lead = next(p for p in parts if p.name == "lead")
    lead.notes[0].start = score.section("b").start - 0.003
    n = round(score.seconds(score.total_beats) * SR)
    rng = np.random.default_rng(0)
    first = round(score.seconds(lead.notes[0].start) * SR)
    stems = {"keys": (rng.standard_normal((n, 2)) * 0.05).astype(np.float32),
             "lead": np.zeros((n, 2), np.float32)}
    stems["lead"][first:] = rng.standard_normal((n - first, 2)) * 0.05
    log, warnings = automix(score, parts, stems)
    assert [(x["track"], x["section"]) for x in log] == [("lead", "b")]
    assert warnings == []


def test_line_texture_is_leveled_as_accompaniment_not_automixed(write_score):
    """line（逐音写的伴奏线，如 walking bass）不应被自动混音当成主旋律推到前面。"""
    score = load(write_score("""
        meta: {bpm: 120}
        sections: [{name: a, bars: 1}]
        harmony: {a: "C"}
        tracks:
          walk: {sound: acoustic-bass, texture: line, notes: {a: "C2/4 D2/4 E2/4 G2/4"}}
          lead: {sound: piano, notes: {a: "C5/1"}}
    """))
    parts, _ = arrange(score)
    n = round(score.seconds(score.total_beats) * SR)
    rng = np.random.default_rng(1)
    stems = {p.name: (rng.standard_normal((n, 2)) * 0.05).astype(np.float32) for p in parts}
    log, _ = automix(score, parts, stems)
    assert any(x["track"] == "lead" for x in log)
    assert not any(x["track"] == "walk" for x in log)

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
    log = automix(score, parts, stems)
    assert [x for x in log if "@" in x] == [log[0]] and log[0].startswith("lead@b")
    assert not any(x.startswith("⚠") for x in log)

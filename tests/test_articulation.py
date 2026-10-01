import numpy as np
import pytest
import soundfile as sf

from onbgm.arrange import Part, Note, SOUNDS, arrange
from onbgm.library import SOUNDS_DIR, resolve
from onbgm.sampler import SR, Instrument, Zone, render_part
from onbgm.score import ScoreError, load

BASE = """
    meta: {{bpm: 120}}
    sections: [{{name: a, bars: 1}}]
    harmony: {{a: "C"}}
    tracks:
      lead: {{sound: strings, {extra} notes: {{a: "{notes}"}}}}
"""


def _lead(write_score, notes, extra=""):
    score = load(write_score(BASE.format(notes=notes, extra=extra)))
    return score, next(p for p in arrange(score)[0] if p.name == "lead")


def test_marks_parse_into_staccato_and_accent(write_score):
    score, lead = _lead(write_score, "C5/8' D5/8> E5/8.'> F5/16 G5/2")
    arts = [(n.art, round(n.dur, 3)) for n in sorted(lead.notes, key=lambda n: n.start)]
    assert arts[0] == ("staccato", 0.25)  # 断奏：写的时值的一半
    assert arts[1][0] == "" and arts[2][0] == "staccato"
    assert arts[3][0] == "" and arts[4][0] == ""


def test_accent_adds_velocity(write_score):
    _, plain = _lead(write_score, "C5/4 C5/4 C5/4 C5/4")
    _, accented = _lead(write_score, "C5/4 C5/4> C5/4 C5/4")
    v1 = [n.vel for n in sorted(plain.notes, key=lambda n: n.start)]
    v2 = [n.vel for n in sorted(accented.notes, key=lambda n: n.start)]
    assert v2[1] - v1[1] == 14 and v1[0] == v2[0]


def test_track_level_articulation_applies_to_every_note(write_score):
    _, lead = _lead(write_score, "C5/16 D5/16 E5/16 F5/16 G5/2.", extra="articulation: staccato,")
    assert all(n.art == "staccato" for n in lead.notes)


def test_articulation_is_rejected_for_textures_that_do_not_use_it(write_score):
    with pytest.raises(ScoreError) as err:
        load(write_score("""
            meta: {bpm: 120}
            sections: [{name: a, bars: 1}]
            harmony: {a: "C"}
            tracks:
              keys: {sound: piano, texture: comp, articulation: staccato, rhythm: "x--- ---- ---- ----"}
        """))
    assert "tracks.keys.articulation: comp 织体不使用这个字段" in str(err.value)


@pytest.fixture
def tone(tmp_path):
    """一个 3 秒长的 A4 正弦波采样，当作"长音采样"。"""
    t = np.arange(3 * SR) / SR
    path = tmp_path / "tone_A4_v1.wav"
    sf.write(path, np.stack([np.sin(2 * np.pi * 440 * t)] * 2, axis=1) * 0.3, SR)
    return path


def _render(write_score, inst, notes, texture="line"):
    score = load(write_score(BASE.format(notes="C5/1", extra="")))
    part = Part("x", "strings", SOUNDS["strings"], 0.0, notes, [], texture=texture)
    return score, render_part(score, part, lambda p: inst, 2 * SR)


def test_short_detached_note_gets_a_short_release(write_score, tone):
    inst = Instrument("tone", [Zone(tone, 69.0, 0, 69)], release=0.3)
    score, audio = _render(write_score, inst, [Note(0.0, 0.1, 69, 100)])  # 0.05 秒的短音，后面是空的
    tail = audio[round(0.2 * SR): round(0.3 * SR)]
    assert np.abs(tail).max() < 1e-3  # 0.3 秒的长释音会让这里还有声音


def test_legato_note_keeps_its_long_release(write_score, tone):
    inst = Instrument("tone", [Zone(tone, 69.0, 0, 69)], release=0.3)
    score, both = _render(write_score, inst, [Note(0.0, 0.5, 69, 100), Note(0.5, 0.5, 72, 100)])
    _, second = _render(write_score, inst, [Note(0.5, 0.5, 72, 100)])
    first = both - second  # 采样器是线性叠加的，相减就是第一个音自己的声音
    assert np.abs(first[round(0.35 * SR): round(0.4 * SR)]).max() > 0.01  # 接下一个音时仍在交叉淡化


def test_line_choke_cuts_the_previous_note_when_the_next_one_starts(write_score, tone):
    inst = Instrument("tone", [Zone(tone, 69.0, 0, 69)], sustain=False, line_choke=True)
    score, both = _render(write_score, inst, [Note(0.0, 0.25, 69, 100), Note(0.5, 0.25, 72, 100)])
    _, second = _render(write_score, inst, [Note(0.5, 0.25, 72, 100)])
    first = both - second
    assert np.abs(first[round(0.29 * SR):]).max() < 1e-3  # 0.25 秒处下一个音出来，30ms 内收掉


@pytest.mark.skipif(not (SOUNDS_DIR / "vsco2/Strings/Violin Section/Spic").exists(), reason="需要 VSCO 跳弓采样（onbgm fetch）")
def test_strings_staccato_uses_spiccato_samples():
    route = resolve("strings", "hq")
    assert route(72).articulations["staccato"].name == "violins-spic"
    pad = resolve("strings-slow", "hq")
    assert pad(50).articulations["staccato"].name == "celli-spic"

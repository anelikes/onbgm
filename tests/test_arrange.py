from pathlib import Path

from onbgm.arrange import arrange, swing_beat
from onbgm.midi_io import _expand
from onbgm.score import load
from onbgm.theory import GUITAR_STRINGS, guitar_voicing, parse_chord

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _notes(parts):
    return {p.name: [(round(n.start, 6), round(n.dur, 6), n.pitch, n.vel) for n in p.notes] for p in parts}


def test_arrangement_is_deterministic():
    score = EXAMPLES / "night_rain_v1.yaml"
    assert _notes(arrange(load(score))[0]) == _notes(arrange(load(score))[0])


def test_editing_one_part_leaves_others_untouched():
    """v2 只改了 build 段和贝斯：其余声部在没改的段落里必须逐音一致（包括人性化的力度和时间）。"""
    s1, s2 = load(EXAMPLES / "night_rain_v1.yaml"), load(EXAMPLES / "night_rain_v2.yaml")
    p1, p2 = {p.name: p for p in arrange(s1)[0]}, {p.name: p for p in arrange(s2)[0]}

    def by_section(score, part):
        out = {}
        for n in part.notes:
            sec = score.section_of(n.start)
            out.setdefault(sec.name, []).append((round(n.start - sec.start, 6), n.pitch, n.vel))
        return out

    for name in ["piano", "motif", "pad", "lead", "cymbal"]:
        a, b = by_section(s1, p1[name]), by_section(s2, p2[name])
        for sec in ["intro", "climax", "outro"]:
            assert a.get(sec) == b.get(sec), f"{name} 在 {sec} 段变了"


def test_guitar_voicings_match_what_guitarists_play():
    expected = {"G": "320003", "C": "x32010", "D": "xx0232", "Em": "022000", "Am": "x02210",
                "D/F#": "200232", "F": "133211", "Bm": None, "A7": "x02020", "Dm": "xx0231"}
    for symbol, shape in expected.items():
        voicing = guitar_voicing(parse_chord(symbol))
        tab = "".join("x" if p is None else str(p - o) for p, o in zip(voicing, GUITAR_STRINGS))
        if shape:
            assert tab == shape, f"{symbol}: {tab}"
        pitches = [p for p in voicing if p is not None]
        assert pitches[0] == min(pitches), f"{symbol}: 最低的弦必须是最低音"
        assert len(set(pitches)) == len(pitches), f"{symbol}: 不应有同度重复"


def test_swing_moves_only_offbeat_sixteenths(write_score):
    score = load(write_score("""
        meta: {bpm: 90, swing: 0.6}
        sections: [{name: a, bars: 1}]
        harmony: {a: "C"}
    """))
    assert [round(swing_beat(score, b), 3) for b in (0, 0.25, 0.5, 0.75, 1)] == [0, 0.3, 0.5, 0.8, 1]


def test_roll_expands_to_repeated_hits_for_midi():
    score = load(EXAMPLES / "night_rain_v1.yaml")
    timpani = next(p for p in arrange(score)[0] if p.name == "timpani")
    rolls = [n for n in timpani.notes if n.art == "roll"]
    assert len(rolls) == 1
    assert len(_expand(rolls)) == 32

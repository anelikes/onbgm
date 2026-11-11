import json
import subprocess
import sys

from onbgm import vocal as V
from onbgm.score import from_dict

BASE = {
    "meta": {"bpm": 100, "key": "F major"},
    "sections": [{"name": "intro", "bars": 1}, {"name": "verse", "bars": 2}, {"name": "chorus2", "bars": 1}],
    "harmony": {"intro": "F", "verse": "F C/E | Bb", "chorus2": "C7"},
    "tracks": {
        "lead": {"sound": "flute", "notes": {
            "intro": "A5/2 C6/2",
            "verse": "A4/4 B4/4 Bb4/2~ | Bb4/4 B4/4 B4/8 Db5/8 r/4",  # F 大调：还原 B、降 B 来回切，跨小节延音
            "chorus2": "F4/4. G4/8 A4/2",
        }},
    },
    "vocal": {"track": "lead", "style": "pop, female vocal", "lyrics": {"verse": "one two three\nfour five six",
                                                                         "chorus2": "la la la"}},
}


def plan_of(**changes):
    raw = json.loads(json.dumps(BASE))
    for k, v in changes.items():
        raw["vocal"][k] = v
    return V.plan(from_dict(raw))


def music(abc: str) -> list[str]:
    return [ln for ln in abc.splitlines() if ln[:2] not in ("X:", "T:", "M:", "L:", "Q:", "K:", "V:", "% ")]


def test_exports_native_two_voice_abc_with_chords_ties_and_accidentals():
    vp, problems = plan_of()
    assert not problems
    head = vp.abc.splitlines()[:8]
    assert head[2:5] == ["M:4/4", "L:1/16", "Q:1/4=100"] and head[-1] == "K:F"
    assert "% intro" in vp.abc and "% verse" in vp.abc and "% chorus" in vp.abc
    vocal_intro, ins_intro, vocal_verse, ins_verse, vocal_chorus, ins_chorus = music(vp.abc)
    assert vocal_intro == '"F"z16|'  # 没歌词：人声休止，和弦照写
    assert ins_intro == "a8c'8|"  # 旋律交给乐器
    # 临时记号整小节有效、下一小节重置；延音跨小节用连音线；调外音优先用本位，其次按调的方向用降号
    assert vocal_verse == '"F"A4=B4"C/E"_B8-|"Bb"B4=B4B2_d2z4|'
    assert ins_verse == "Z2|"
    assert vocal_chorus == '"C7"F6G2A8|'
    assert vp.lyrics == "[Intro]\n\n[Verse]\none two three\nfour five six\n\n[Chorus]\nla la la\n"
    assert vp.style == "pop, female vocal" and vp.seed == 831001
    verse = next(r for r in vp.sections if r["section"] == "verse")
    assert verse == {"section": "verse", "tag": "Verse", "bars": 2, "voice": "vocal", "notes": 6,
                          "lines": 2, "syllables": 6, "range": [69, 73]}


def test_dotted_sixteenths_switch_to_thirty_second_grid_and_transpose():
    raw = json.loads(json.dumps(BASE))
    raw["tracks"]["lead"]["notes"]["chorus2"] = "F4/16. G4/16. A4/16 C5/2."
    vp, _ = V.plan(from_dict(raw | {"vocal": raw["vocal"] | {"transpose": -12}}))
    assert "L:1/32" in vp.abc
    assert '"C7"F,3G,3A,2C24|' in vp.abc


def test_problems_point_at_the_vocal_block():
    _, p = plan_of(track="pad")
    assert p[0].code == "unknown_track" and p[0].path == "vocal.track"
    _, p = plan_of(lyrics={"bridge": "x"})
    assert p[0].code == "unknown_section"
    raw = json.loads(json.dumps(BASE))
    raw["sections"].append({"name": "drop", "bars": 1})
    raw["harmony"]["drop"] = "F"
    _, p = V.plan(from_dict(raw))
    assert [x.code for x in p] == ["unknown_tag"] and p[0].path == "vocal.tags.drop"
    raw["vocal"]["tags"] = {"drop": "Chorus"}
    raw["vocal"]["lyrics"]["drop"] = "no melody here"
    _, p = V.plan(from_dict(raw))
    assert [x.code for x in p] == ["no_melody_here"]
    raw["meta"]["key"] = "somewhere nice"
    _, p = V.plan(from_dict(raw))
    assert p[0].code == "bad_key"


def test_warns_about_range_and_syllables():
    vp, _ = plan_of(lyrics={"verse": "x", "chorus2": "this line has far too many syllables for three notes"},
                    transpose=24)
    codes = {w.code: w for w in vp.warnings}
    assert {"too_few_syllables", "too_many_syllables", "out_of_vocal_range"} <= set(codes)
    assert codes["out_of_vocal_range"].fix == {"transpose": 12}


def test_key_parsing_and_tags():
    assert V.parse_key("Ab major").abc == "Ab" and V.parse_key("F# minor").abc == "F#m"
    assert V.parse_key("D dorian").abc == "C" and V.parse_key("Am").sig == {}
    assert V.parse_key("H major") is None
    assert [V.guess_tag(x) for x in ("verse2", "pre-chorus", "break", "ending", "Chorus_3")] == \
        ["Verse", "Pre-Chorus", "Interlude", "Outro", "Chorus"]
    assert V.syllables("Roll the window down") == 5 and V.syllables("摇下车窗") == 4
    assert V.syllables("きょうはいい") == 5


def run(*args):
    proc = subprocess.run([sys.executable, "-m", "onbgm", *args, "--json"], capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


def test_cli_abc_writes_inputs_and_sing_reports_missing_engine(tmp_path):
    import yaml

    score = tmp_path / "s.yaml"
    score.write_text(yaml.safe_dump(BASE, allow_unicode=True), encoding="utf-8")
    code, data = run("abc", str(score), "-o", str(tmp_path / "v"), "--seed", "7")
    assert code == 0 and data["seed"] == 7
    assert (tmp_path / "v" / "song.abc").read_text() == data["abc"]
    assert (tmp_path / "v" / "lyrics.txt").exists() and (tmp_path / "v" / "style.txt").exists()
    code, data = run("sing", str(score), "--engine", str(tmp_path / "nowhere"))
    assert code == 2 and data["problems"][0]["code"] == "missing_dependency"
    code, data = run("check", str(score))
    assert code == 0
    bad = dict(BASE, vocal=dict(BASE["vocal"], track="nope"))
    score.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
    code, data = run("check", str(score))
    assert code == 1 and data["problems"][0]["path"] == "vocal.track"

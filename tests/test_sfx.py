import json
import subprocess
import sys

import numpy as np
import pytest

from onbgm.sampler import detect_pitch
from onbgm.score import ScoreError, load
from onbgm.sfx import render
from onbgm.synth import SR

SCORE = """
    meta: {{bpm: 120}}
    sections: [{{name: build, bars: 4}}, {{name: drop, bars: 4}}]
    harmony: {{build: "Am", drop: "F | G | C | Am"}}
    sfx:
{sfx}
"""


def _render(write_score, sfx_yaml):
    score = load(write_score(SCORE.format(sfx=sfx_yaml)))
    n = round((score.seconds(score.total_beats) + 4) * SR)
    audio, duck, placed = render(score, score.sfx, n)
    return score, audio, duck, placed


def _rms(x):
    return float(np.sqrt(np.mean(x ** 2)))


def at(seconds):
    return round(seconds * SR)


def test_riser_ends_exactly_on_the_drop(write_score):
    score, audio, _, placed = _render(write_score, "      - {type: riser, at: drop, length: 8}")
    hit = at(8.0)  # drop 在第 5 小节 = 8 秒
    assert placed[0]["start_s"] == 4.0 and placed[0]["end_s"] == 8.0
    assert _rms(audio[hit - at(0.1): hit]) > 30 * _rms(audio[hit + at(0.02): hit + at(0.2)])
    assert _rms(audio[hit - at(0.1): hit]) > 3 * _rms(audio[at(4.0): at(4.5)])  # 越来越响


def test_impact_starts_on_its_point(write_score):
    _, audio, _, _ = _render(write_score, "      - {type: impact, at: drop}")
    hit = at(8.0)
    assert np.abs(audio[: hit - at(0.005)]).max() < 1e-6
    assert _rms(audio[hit: hit + at(0.1)]) > 0.05


def test_whoosh_peaks_at_its_point(write_score):
    _, audio, _, _ = _render(write_score, '      - {type: whoosh, at: "0:06", length: 2}')
    win = at(0.05)
    env = [_rms(audio[i: i + win]) for i in range(at(5.0), at(7.0), win)]
    peak_s = 5.0 + np.argmax(env) * 0.05 + 0.025
    assert abs(peak_s - 6.0) < 0.15


def test_tuned_sfx_follow_the_current_chord(write_score):
    _, audio, _, _ = _render(write_score, '      - {type: ding, at: "0:10"}')  # 第 6 小节：G
    got = detect_pitch(audio[at(10.0): at(11.5)], hint=91, window=1.5)  # G6
    assert abs(got - 91) < 0.3
    _, audio, _, _ = _render(write_score, "      - {type: sub-drop, at: drop}")  # F 和弦，从 F2 往下滑
    mono = audio[at(8.0): at(8.1), 0]
    ups = np.where((mono[:-1] <= 0) & (mono[1:] > 0))[0]  # 上升过零点：第一个完整周期就是起始音高
    first_cycle_hz = SR / (ups[1] - ups[0])
    assert abs(69 + 12 * np.log2(first_cycle_hz / 440) - 41) < 0.8


def test_duck_dips_the_music_and_recovers(write_score):
    _, _, duck, _ = _render(write_score, "      - {type: impact, at: drop, duck: 6}")
    assert abs(20 * np.log10(duck[at(8.0)]) + 6) < 0.1
    assert duck[at(7.9)] == 1.0
    assert 20 * np.log10(duck[at(9.5)]) > -0.1


@pytest.mark.parametrize("sfx, code, path", [
    ("      - {type: riser, at: \"0:02\", length: 8}", "sfx_out_of_range", "sfx.0.at"),
    ("      - {type: impact, at: drop, length: 2}", "field_not_for_sfx", "sfx.0.length"),
    ("      - {type: impact, at: chorus}", "unknown_section", "sfx.0.at"),
    ("      - {type: rizer, at: drop}", "unknown_value", "sfx.0.type"),
])
def test_sfx_mistakes_are_caught(write_score, sfx, code, path):
    with pytest.raises(ScoreError) as err:
        load(write_score(SCORE.format(sfx=sfx)))
    p = err.value.problems[0]
    assert (p.code, p.path) == (code, path)
    if code == "sfx_out_of_range":
        assert p.fix == {"length": 4.0}  # 0:02 在 120 BPM 下是第 4 拍，最长只能 4 拍
    if code == "unknown_value":
        assert p.fix == {"replace_with": "riser"}


def test_timeline_and_render_report_sfx(write_score, tmp_path):
    path = write_score(SCORE.format(sfx="      - {type: riser, at: drop, length: 8}\n      - {type: impact, at: drop, duck: 4}")
                       + "    tracks:\n      pad: {sound: synth-pad, texture: pad}\n")
    out = subprocess.run([sys.executable, "-m", "onbgm", "timeline", str(path), "--json"], capture_output=True, text=True)
    rows = json.loads(out.stdout)["sfx"]
    assert [(r["type"], r["at_s"], r["anchor"]) for r in rows] == [("riser", 8.0, "end"), ("impact", 8.0, "start")]
    out = subprocess.run([sys.executable, "-m", "onbgm", "render", str(path), "-o", str(tmp_path / "o"), "--json"],
                         capture_output=True, text=True)
    data = json.loads(out.stdout)
    assert data["ok"] and len(data["sfx"]) == 2 and (tmp_path / "o" / "stems" / "sfx.wav").exists()
    assert all("level_db" in x and "vs_music_db" in x for x in data["sfx"])


@pytest.mark.parametrize("kind", ["riser", "reverse-cymbal", "downlifter", "impact", "sub-drop", "whoosh", "pop", "ding"])
def test_same_level_sounds_equally_loud(write_score, kind):
    """回归测试：同样 level 0.8，pop 曾比上升音响 8 dB（按 400ms 窗口定音量，短音效被窗口里的静音骗了）。"""
    from onbgm.sfx import TARGET_DB, peak_level
    _, audio, _, _ = _render(write_score, f"      - {{type: {kind}, at: drop, level: 0.8}}")
    nz = audio[np.abs(audio).max(axis=1) > 1e-6]
    assert abs(peak_level(nz) - (TARGET_DB + 20 * np.log10(0.8))) < 0.5


def test_removing_one_sfx_leaves_the_others_identical(write_score):
    """回归测试：随机种子曾按列表序号取，删掉前面一个音效，后面没动的上升音波形也会变。"""
    _, both, _, _ = _render(write_score, "      - {type: whoosh, at: \"0:02\"}\n      - {type: riser, at: drop, length: 8}")
    _, alone, _, _ = _render(write_score, "      - {type: riser, at: drop, length: 8}")
    riser = slice(at(4.0), at(8.0))  # whoosh 在 2.5 秒前就结束了，和上升音不重叠
    assert np.array_equal(both[riser], alone[riser])

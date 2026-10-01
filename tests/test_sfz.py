from collections import defaultdict

import numpy as np
import pytest
import soundfile as sf

from onbgm import sfz
from onbgm.arrange import Note, Part, SOUNDS
from onbgm.sampler import SR, detect_pitch, load_sfz, render_part
from onbgm.score import load


def _sine(path, midi, seconds=2.0, amp=0.3, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    sf.write(path, np.sin(2 * np.pi * 440 * 2 ** ((midi - 69) / 12) * t) * amp * np.exp(-t), sr)
    return path


@pytest.fixture
def lib(tmp_path):
    """一个小型 SFZ 库：两个音区 × 两个力度层，其中一层有两个轮替，另有一个松键声区域。"""
    (tmp_path / "samples dir").mkdir()
    for name, midi in [("c4 soft", 60), ("c4 loud", 60), ("c4 loud rr2", 60), ("g4", 67), ("release", 60)]:
        _sine(tmp_path / "samples dir" / f"{name}.wav", midi, sr=48000 if name == "g4" else SR)
    (tmp_path / "inst.sfz").write_text("""
        // 注释
        #define $DIR samples dir
        <control> default_path=$DIR/
        <global> ampeg_release=0.4 amp_veltrack=80
        <group> lokey=c4 hikey=f#4 pitch_keycenter=60
        <region> hivel=63 sample=c4 soft.wav volume=-6
        <region> lovel=64 seq_length=2 seq_position=1 sample=c4 loud.wav
        <region> lovel=64 seq_length=2 seq_position=2 sample=c4 loud rr2.wav
        <group> lokey=g4 hikey=127 pitch_keycenter=g4 tune=-20
        <region> sample=g4.wav
        <group> trigger=release
        <region> key=60 sample=release.wav
    """)
    return tmp_path / "inst.sfz"


def test_parse_inherits_group_settings_and_resolves_paths(lib):
    regions = sfz.parse(lib)
    assert len(regions) == 5
    assert regions[0]["ampeg_release"] == "0.4" and regions[0]["lokey"] == "c4"
    assert regions[0]["_sample"].endswith("samples dir/c4 soft.wav")  # 文件名带空格、default_path 前缀
    assert "lokey" not in regions[4]  # 新的 <group> 不继承上一个 group 的设定


def test_note_names():
    assert [sfz.note_number(x) for x in ["60", "c4", "C#4", "db4", "a-1", "g9"]] == [60, 60, 61, 61, 9, 127]


def test_playable_filters_release_triggers_keyswitches_and_pedal_layers():
    regions = [
        {"sample": "a"}, {"sample": "b", "trigger": "release"},
        {"sample": "c", "sw_last": "c1", "sw_default": "d1"}, {"sample": "d", "sw_last": "d1"},
        {"sample": "e", "locc64": "64"}, {"sample": "f", "hicc64": "63"},
    ]
    assert [r["sample"] for r in sfz.playable(regions)] == ["a", "d", "f"]


def test_voices_select_by_key_and_velocity_and_alternate_round_robins(lib):
    inst = load_sfz("t", lib)
    assert inst.sfz and inst.sustain and inst.release == 0.4 and len(inst.zones) == 4
    state = defaultdict(int)
    soft = inst.voices(62, 40, state)
    assert [z.path.name for z in soft] == ["c4 soft.wav"] and soft[0].gain_db == -6
    loud = [inst.voices(62, 100, state)[0].path.name for _ in range(3)]
    assert loud == ["c4 loud.wav", "c4 loud rr2.wav", "c4 loud.wav"]
    assert inst.voices(70, 100, state)[0].path.name == "g4.wav"
    assert inst.voices(30, 100, state)[0].keys == (60, 66)  # 音域外：用最近的音区移调


def test_rendered_pitch_follows_keycenter_transpose_and_tune(lib, write_score):
    inst = load_sfz("t", lib)
    score = load(write_score("""
        meta: {bpm: 120}
        sections: [{name: a, bars: 1}]
        harmony: {a: "C"}
    """))
    for pitch, expected in [(64, 64.0), (72, 72 - 0.2)]:  # 72 在 g4 音区，那个区域调低了 20 音分
        part = Part("x", "piano", SOUNDS["piano"], 0.0, [Note(0.0, 2.0, pitch, 100)], [], texture="line")
        audio = render_part(score, part, lambda p: inst, 2 * SR)
        assert detect_pitch(audio[: SR], hint=pitch, window=1) == pytest.approx(expected, abs=0.05)


def test_velocity_curve_follows_amp_veltrack(lib, write_score):
    inst = load_sfz("t", lib)
    score = load(write_score("""
        meta: {bpm: 120}
        sections: [{name: a, bars: 1}]
        harmony: {a: "C"}
    """))
    def peak(vel):
        part = Part("x", "piano", SOUNDS["piano"], 0.0, [Note(0.0, 1.0, 60, vel)], [], texture="line")
        return np.abs(render_part(score, part, lambda p: inst, SR)).max()
    # 同一层（hivel=63）里：力度 63 和 32 的音量比 = (1-0.8+0.8·(63/127)²) / (1-0.8+0.8·(32/127)²)
    expected = (0.2 + 0.8 * (63 / 127) ** 2) / (0.2 + 0.8 * (32 / 127) ** 2)
    assert peak(63) / peak(32) == pytest.approx(expected, rel=0.02)


def test_closed_hat_chokes_the_open_hat(tmp_path, write_score):
    rng = np.random.default_rng(0)
    for name in ["open", "closed"]:
        sf.write(tmp_path / f"{name}.wav", rng.standard_normal(2 * SR) * 0.1, SR)
    (tmp_path / "hats.sfz").write_text("""
        <group> loop_mode=one_shot pitch_keytrack=0
        <region> key=46 sample=open.wav group=1 off_by=2
        <region> key=42 sample=closed.wav group=2
    """)
    inst = load_sfz("hats", tmp_path / "hats.sfz")
    assert not inst.sustain
    score = load(write_score("""
        meta: {bpm: 120}
        sections: [{name: a, bars: 1}]
        harmony: {a: "C"}
    """))
    def render(notes):
        part = Part("x", "drums", SOUNDS["drums"], 0.0, notes, [], texture="groove")
        return render_part(score, part, lambda p: inst, 2 * SR)
    both = render([Note(0.0, 0.25, 46, 100), Note(1.0, 0.25, 42, 100)])
    closed = render([Note(1.0, 0.25, 42, 100)])
    open_hat = both - closed
    assert np.abs(open_hat[round(0.4 * SR): round(0.45 * SR)]).max() > 0.01  # 闭镲之前开镲一直在响
    assert np.abs(open_hat[round(0.53 * SR):]).max() < 1e-4  # 0.5 秒闭镲一出来，开镲 20ms 内收掉

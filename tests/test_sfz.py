from collections import defaultdict

import numpy as np
import pytest
import soundfile as sf

from onbgm import sfz
from onbgm.arrange import Note, Part, SOUNDS
from onbgm.library import SOUNDS_DIR
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
    assert [z.path.name for z in soft] == ["c4 soft.wav"]
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


def test_sample_preroll_is_compensated_so_the_attack_lands_on_the_beat(tmp_path, write_score):
    """回归测试：贝斯采样开头有约 25ms 的手指预备音，原样播放时整条贝斯拖拍。"""
    t = np.arange(SR) / SR
    pre = int(0.025 * SR)
    audio = np.concatenate([np.full(pre, 0.01), np.sin(2 * np.pi * 110 * t) * np.exp(-3 * t)])
    sf.write(tmp_path / "a2.wav", audio, SR)
    (tmp_path / "b.sfz").write_text("<region> key=45 sample=a2.wav")
    inst = load_sfz("b", tmp_path / "b.sfz")
    score = load(write_score("""
        meta: {bpm: 120}
        sections: [{name: a, bars: 1}]
        harmony: {a: "C"}
    """))
    part = Part("x", "piano", SOUNDS["piano"], 0.0, [Note(1.0, 1.0, 45, 127)], [], texture="line")
    out = np.abs(render_part(score, part, lambda p: inst, SR)).max(axis=1)
    attack = int(np.argmax(out > out.max() * 0.1))
    assert abs(attack - round(0.5 * SR)) < round(0.001 * SR)  # 第 2 拍 = 0.5 秒，误差 1ms 内


def test_velocity_layers_are_level_matched_at_their_boundary(tmp_path, write_score):
    """回归测试：贝斯库的 f 层没写力度曲线，力度从 95 到 96 音量反而掉了 4~6 dB。
    加载时校准各层增益，让相邻层在交界处音量连续，最响的一层保持原样。"""
    _sine(tmp_path / "soft.wav", 45, amp=0.3)
    _sine(tmp_path / "loud.wav", 45, amp=0.1)  # 响的那层采样反而录得轻
    (tmp_path / "b.sfz").write_text("""
        <region> key=45 hivel=63 sample=soft.wav
        <region> key=45 lovel=64 sample=loud.wav
    """)
    inst = load_sfz("b", tmp_path / "b.sfz")
    score = load(write_score("""
        meta: {bpm: 120}
        sections: [{name: a, bars: 1}]
        harmony: {a: "C"}
    """))
    def peak(vel):
        part = Part("x", "piano", SOUNDS["piano"], 0.0, [Note(0.0, 1.0, 45, vel)], [], texture="line")
        return 20 * np.log10(np.abs(render_part(score, part, lambda p: inst, SR)).max())
    assert abs(peak(64) - peak(63)) < 0.5
    assert peak(127) == pytest.approx(20 * np.log10(0.1), abs=0.5)  # 最响的一层不动
    assert (tmp_path / ".onbgm-levels.json").exists()


@pytest.mark.skipif(not (SOUNDS_DIR / "libs/black-pearl").exists(), reason="需要 AVL 鼓组（onbgm fetch）")
def test_drum_kit_uses_samples_only_when_every_piece_is_in_the_kit():
    from onbgm.library import SF2, resolve
    kit = resolve("drums", "hq", {36, 38, 42, 50, 70})  # 高嗵鼓、沙锤在 AVL 里换了键位
    assert kit(36).name == "black-pearl"
    assert isinstance(resolve("drums", "hq", {36, 62}), SF2)  # conga 没有采样：整套退回 GM
    assert resolve("drums", "hq", {49})(49).name == "crash"  # 只有镲：用管弦乐的镲


def test_cc_defaults_resolve_pan_tune_and_amplitude():
    """回归测试：刷子鼓的库先把整套设成"全部最左、降一个八度"，再靠控制器默认值调回原位；不折算的话全在最左边、低八度。"""
    r = sfz.apply_cc({"pan": "-100", "pan_oncc71": "200", "set_cc71": "63.5", "tune": "-1200", "tune_oncc72": "2400",
                      "set_cc72": "63.5", "amplitude_oncc70": "100", "set_cc70": "127", "ampeg_releasecc83": "5",
                      "set_cc83": "127", "ampeg_release": "0.03", "volume_oncc24": "7"})
    assert float(r["pan"]) == pytest.approx(0) and float(r["tune"]) == pytest.approx(0)
    assert float(r["amplitude"]) == pytest.approx(100) and float(r["ampeg_release"]) == pytest.approx(5.03)
    assert float(r["volume"]) == 0  # 控制器没设默认值：按 0 算


def test_silent_regions_only_trigger_choke_groups(tmp_path, write_score):
    rng = np.random.default_rng(0)
    sf.write(tmp_path / "open.wav", rng.standard_normal(2 * SR) * 0.1, SR)
    sf.write(tmp_path / "closed.wav", rng.standard_normal(SR // 4) * 0.1, SR)
    (tmp_path / "hats.sfz").write_text("""
        <group> loop_mode=one_shot
        <region> key=46 sample=open.wav off_by=7
        <region> key=42 sample=closed.wav
        <group> group=7 sample=*silence
        <region> key=42
    """)
    inst = load_sfz("hats", tmp_path / "hats.sfz")
    state = defaultdict(int)
    assert [z.silent for z in inst.voices(42, 100, state)] == [False, True]
    score = load(write_score("""
        meta: {bpm: 120}
        sections: [{name: a, bars: 1}]
        harmony: {a: "C"}
    """))
    part = Part("x", "drums", SOUNDS["drums"], 0.0, [Note(0.0, 0.25, 46, 100), Note(1.0, 0.25, 42, 100)], [], texture="groove")
    closed = Part("x", "drums", SOUNDS["drums"], 0.0, [Note(1.0, 0.25, 42, 100)], [], texture="groove")
    open_hat = render_part(score, part, lambda p: inst, 2 * SR) - render_part(score, closed, lambda p: inst, 2 * SR)
    assert np.abs(open_hat[round(0.53 * SR):]).max() < 1e-4  # 闭镲的无声区域切断了开镲


def test_power_chords_voice_as_root_fifth_octave():
    from onbgm.theory import guitar_voicing, parse_chord, voice_lead

    c5 = parse_chord("C5")
    assert c5.intervals == (0, 7, 12) and c5.third == 7  # 没有三音：用五音代替，织体不会出错
    assert guitar_voicing(parse_chord("E5")) == (40, 47, 52, None, None, None)
    assert voice_lead(c5, None, 40, 64) == (48, 55, 60)
    assert voice_lead(parse_chord("D5"), (48, 55, 60), 40, 64)[0] == 50  # 根音就近


def test_ukulele_voicings_match_real_shapes():
    from onbgm.theory import parse_chord, ukulele_voicing

    shapes = {c: tuple(p - s for p, s in zip(ukulele_voicing(parse_chord(c)), (67, 60, 64, 69)))
              for c in ("C", "Am", "F", "G", "G7")}
    assert shapes == {"C": (0, 0, 0, 3), "Am": (2, 0, 0, 0), "F": (2, 0, 1, 0), "G": (0, 2, 3, 2), "G7": (0, 2, 1, 2)}


def test_vibrato_wobbles_pitch_after_the_delay():
    from onbgm.sampler import _vibrato

    t = np.arange(2 * SR) / SR
    tone = np.stack([np.sin(2 * np.pi * 440 * t)] * 2, axis=1).astype(np.float32)
    out = _vibrato(tone, 50, 5.0, 0.5)
    assert np.allclose(out[: SR // 4], tone[: SR // 4], atol=1e-4)  # 延迟之前不变
    late = out[int(1.2 * SR): int(1.8 * SR), 0]
    zero_cross = np.diff(np.signbit(late)).nonzero()[0]
    periods = np.diff(zero_cross[::2]) / SR
    assert periods.max() / periods.min() > 1.03  # 音高在起伏（50 音分约 ±3%）


def test_key_range_retune_and_post_effects(tmp_path, write_score):
    from pedalboard import Gain

    _sine(tmp_path / "a.wav", 69.3)  # 偏高 30 音分的采样
    _sine(tmp_path / "noise.wav", 90)
    (tmp_path / "x.sfz").write_text("<region> lokey=60 hikey=80 pitch_keycenter=69 sample=a.wav\n"
                                    "<region> key=96 sample=noise.wav")
    inst = load_sfz("x", tmp_path / "x.sfz", key_range=(0, 88), retune=True, post=[Gain(-6)])
    assert len(inst.zones) == 1 and inst.zones[0].shift == pytest.approx(-0.3, abs=0.05)
    score = load(write_score("""
        meta: {bpm: 120}
        sections: [{name: a, bars: 1}]
        harmony: {a: "C"}
    """))
    part = Part("x", "piano", SOUNDS["piano"], 0.0, [Note(0.0, 2.0, 69, 100)], [], texture="line")
    audio = render_part(score, part, lambda p: inst, SR)
    assert detect_pitch(audio, hint=69, window=1) == pytest.approx(69.0, abs=0.05)  # 校准后正好 A4
    plain = load_sfz("y", tmp_path / "x.sfz", key_range=(0, 88), retune=True)
    ratio = np.abs(audio).max() / np.abs(render_part(score, part, lambda p: plain, SR)).max()
    assert ratio == pytest.approx(10 ** (-6 / 20), rel=0.02)  # 效果链作用在整个声部上

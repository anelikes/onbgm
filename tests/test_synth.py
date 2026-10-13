from dataclasses import replace

import numpy as np
import pytest

from onbgm.sampler import detect_pitch
from onbgm.synth import DRUM_KINDS, KITS, PRESETS, SR, drum_hit, oscillator, render_note


def _alias_db(x, f0):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / SR)
    harm = np.zeros_like(f, bool)
    for k in range(1, int(20000 / f0) + 1):
        harm |= np.abs(f - k * f0) < 12
    band = (f > 20) & (f < 20000)
    return 10 * np.log10(spec[band & ~harm].sum() / spec[band & harm].sum())


@pytest.mark.parametrize("wave", ["saw", "square"])
def test_polyblep_cuts_aliasing(wave):
    f0 = 440 * 2 ** ((96 - 69) / 12)  # C7：高音最容易混叠
    t = np.arange(SR) / SR
    freq = np.full(SR, f0)
    phase = np.cumsum(freq / SR) % 1.0
    naive = 2 * phase - 1 if wave == "saw" else np.where(phase < 0.5, 1.0, -1.0)
    blep = oscillator(wave, freq, 0.0, t)
    assert _alias_db(blep, f0) < -28
    assert _alias_db(blep, f0) < _alias_db(naive, f0) - 15


@pytest.mark.parametrize("name", ["synth-pluck", "supersaw", "synth-lead", "synth-pad", "synth-bass", "sub-bass", "fm-epiano",
                                  "chip-lead", "chip-square", "chip-pulse", "chip-bass"])
def test_presets_are_in_tune(name):
    p = replace(PRESETS[name], unison=1, detune=0, vibrato=(0, 0, 0))
    for pitch in (57, 69):
        audio = render_note(p, pitch, 100, 1.5, ("tune", pitch))
        got = detect_pitch(audio, hint=pitch, window=1.5)
        assert abs(got - pitch) < 0.1, f"{name} {pitch}: {got:.2f}"


def test_808_slides_down_to_the_written_pitch():
    audio = render_note(PRESETS["bass-808"], 45, 110, 1.5, ("808",))
    settled = audio[int(0.4 * SR):]
    assert abs(detect_pitch(np.pad(settled, ((int(0.25 * SR), 0), (0, 0))), hint=45, window=1.5) - 45) < 0.15


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_notes_start_and_end_without_clicks(name):
    audio = render_note(PRESETS[name], 60, 110, 0.3, ("click", name))
    assert np.isfinite(audio).all()
    assert np.abs(audio[0]).max() < 1e-3 and np.abs(audio[-1]).max() < 1e-6
    # 咔哒声是音量从 0 突然跳起：看包络是否平滑升起（锯齿、方波本身的陡峭跳变是波形特征，不算咔哒）
    def rms(x):
        return float(np.sqrt(np.mean(x ** 2)))
    first, settled = rms(audio[: int(0.0003 * SR)]), rms(audio[int(0.005 * SR): int(0.01 * SR)])
    assert first < 0.25 * settled, f"起音太陡：开头 {first:.4f}，之后 {settled:.4f}"


def test_rendering_is_deterministic():
    a = render_note(PRESETS["supersaw"], 64, 100, 0.5, ("same",))
    b = render_note(PRESETS["supersaw"], 64, 100, 0.5, ("same",))
    assert np.array_equal(a, b)


def _harmonic_db(x, f0, k):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return 20 * np.log10(spec[np.abs(f - k * f0) < 3].max() / spec[np.abs(f - f0) < 3].max())


@pytest.mark.parametrize("duty", [0.25, 0.125])
def test_pulse_duty_shapes_the_spectrum(duty):
    """占空比 1/n 的脉冲波缺第 n、2n… 次谐波（这是红白机脉冲波细、亮的来源），而且没有直流分量。"""
    t = np.arange(SR) / SR
    x = oscillator("square", np.full(SR, 220.0), 0.0, t, duty=duty)
    gap = round(1 / duty)
    assert abs(x.mean()) < 1e-3
    assert _harmonic_db(x, 220.0, gap) < -60
    assert _harmonic_db(x, 220.0, gap - 1) > -25


def test_nes_triangle_has_its_stair_step_buzz():
    """红白机三角波是 32 步的阶梯：第 31、33 次谐波附近比平滑三角波响得多，听起来带一点颗粒感。"""
    t = np.arange(SR) / SR
    stepped = oscillator("nes-triangle", np.full(SR, 55.0), 0.0, t)
    smooth = oscillator("triangle", np.full(SR, 55.0), 0.0, t)
    assert _harmonic_db(stepped, 55.0, 31) > _harmonic_db(smooth, 55.0, 31) + 20
    f0 = 440 * 2 ** ((96 - 69) / 12)  # 高音时阶梯的谐波超过奈奎斯特频率，要去掉才不混叠
    assert _alias_db(oscillator("nes-triangle", np.full(SR, f0), 0.0, t), f0) < -60


def test_chip_volume_moves_in_frame_steps():
    """芯片音色的音量每帧（1/60 秒）变一次、只有 15 级：衰减听起来是一格一格的。"""
    p = replace(PRESETS["chip-square"], vibrato=(0, 0, 0))
    audio = render_note(p, 60, 127, 1.0, ("steps",))[:, 0]
    frame = SR // 60  # 方波平顶的高度就是当时的音量
    levels = np.array([np.abs(audio[i * frame + 20:(i + 1) * frame - 20]).max() for i in range(1, 50)]) * 15
    assert np.allclose(levels, np.round(levels)), levels
    assert len(set(levels)) > 3  # 确实在衰减


@pytest.mark.parametrize("kit", sorted(KITS))
@pytest.mark.parametrize("kind", sorted(set(DRUM_KINDS.values())))
def test_synth_drums_render_cleanly(kind, kit):
    hit = drum_hit(kind, 0, kit)
    assert np.isfinite(hit).all() and abs(np.abs(hit).max() - 0.8) < 1e-3
    assert abs(hit[0]) < 0.05 and hit[-1] == 0

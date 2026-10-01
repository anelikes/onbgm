"""合成器：用代码直接生成音色，不需要采样，安装包也不会变大。

- 振荡器：正弦、锯齿、方波、三角、FM。锯齿和方波用 PolyBLEP 抑制混叠（否则高音会有刺耳的金属杂音）。
- 多声部失谐（unison）：几个略微跑调的振荡器叠在一起，左右展开，得到宽厚的"超级锯齿"。
- 包络：音量 ADSR、滤波器包络、音高包络（808 的下滑）、颤音。
- 滤波：Moog 风格的四阶低通（pedalboard 的 LadderFilter），截止频率按 5.8ms 一块随包络变化。
- 合成鼓组：底鼓、军鼓、拍手、踩镲等都由正弦扫频和滤波噪声合成，模板缓存后复用。

所有随机量（初始相位、噪声）都由音符位置决定，同一份乐谱永远渲染出同样的结果。
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from pedalboard import HighpassFilter, LadderFilter, LowpassFilter, Pedalboard

from .arrange import Part
from .score import Score

SR = 44100
BLOCK = 256  # 滤波器包络的更新粒度（约 5.8ms）


def _rng(*key) -> np.random.Generator:
    return np.random.default_rng(zlib.crc32(repr(key).encode()))


# ---------- 振荡器 ----------

def _polyblep(t: np.ndarray, dt: np.ndarray) -> np.ndarray:
    """多项式带限阶跃：在波形跳变处补一个小修正，去掉大部分混叠。"""
    out = np.zeros_like(t)
    m = t < dt
    x = t[m] / dt[m]
    out[m] = x + x - x * x - 1.0
    m = t > 1.0 - dt
    x = (t[m] - 1.0) / dt[m]
    out[m] = x * x + x + x + 1.0
    return out


def oscillator(wave: str, freq: np.ndarray, phase0: float, t: np.ndarray, fm: tuple | None = None) -> np.ndarray:
    dt = freq / SR
    phase = (phase0 + np.cumsum(dt)) % 1.0
    if wave == "sine":
        return np.sin(2 * np.pi * phase)
    if wave == "saw":
        return 2 * phase - 1 - _polyblep(phase, dt)
    if wave == "square":
        return np.where(phase < 0.5, 1.0, -1.0) + _polyblep(phase, dt) - _polyblep((phase + 0.5) % 1.0, dt)
    if wave == "triangle":
        return 2 * np.abs(2 * phase - 1) - 1
    if wave == "fm":
        # fm = ((调制比, 起始指数, 末尾指数, 指数衰减秒数), ...)：调制器正弦，按指数包络调制载波相位
        mod = np.zeros_like(phase)
        for ratio, i0, i1, decay in fm:
            m_phase = (np.cumsum(dt * ratio)) % 1.0
            index = i1 + (i0 - i1) * np.exp(-t * 6.9 / decay)
            mod += index * np.sin(2 * np.pi * m_phase)
        return np.sin(2 * np.pi * phase + mod)
    raise ValueError(wave)


@dataclass(frozen=True)
class Osc:
    wave: str
    level: float = 1.0
    semitones: float = 0.0
    fm: tuple = ()


@dataclass(frozen=True)
class Preset:
    """一个合成器音色。时间参数都是秒；decay/release 是衰减到约 -60 dB 的时间。"""
    name: str
    oscs: tuple[Osc, ...]
    unison: int = 1
    detune: float = 0.0  # 多声部的总失谐（音分）
    spread: float = 0.0  # 多声部的左右展开 0~1
    attack: float = 0.003
    decay: float = 0.5
    sustain: float = 0.8
    release: float = 0.2
    cutoff: float | None = None  # 低通截止频率；None 为不滤波
    resonance: float = 0.1
    filter_env: float = 0.0  # 滤波器包络的峰值（Hz，加在 cutoff 上）
    filter_decay: float = 0.3
    vel_to_filter: float = 0.5  # 力度对滤波器包络的影响 0~1
    key_track: float = 0.0  # 截止频率跟随音高的程度 0~1
    pitch_env: float = 0.0  # 起音时高出的半音数，随后下滑（808）
    pitch_decay: float = 0.05
    vibrato: tuple[float, float, float] = (0.0, 0.0, 0.0)  # (频率 Hz, 深度音分, 延迟秒)
    drive: float = 0.0  # 饱和：0 为不失真
    gain: float = 1.0


def _envelope(n: int, hold: float, p: Preset) -> np.ndarray:
    t = np.arange(n) / SR
    a = max(p.attack, 0.001)
    sustained = p.sustain + (1 - p.sustain) * np.exp(-np.maximum(t - a, 0) * 6.9 / max(p.decay, 1e-3))
    env = np.where(t < a, t / a, sustained)
    at_release = float(np.interp(hold, t, env)) if hold < t[-1] else float(env[-1])
    release = at_release * np.exp(-np.maximum(t - hold, 0) * 6.9 / max(p.release, 1e-3))
    env = np.where(t < hold, env, release)
    tail = min(n, int(0.005 * SR))
    env[-tail:] *= np.linspace(1, 0, tail)  # 最后 5ms 归零，避免咔哒
    return env


def render_note(p: Preset, pitch: int, vel: int, hold: float, seed) -> np.ndarray:
    """渲染一个音，返回立体声。hold 是按住的秒数，之后是释音。"""
    n = int((hold + p.release) * SR) + 64
    t = np.arange(n) / SR
    rng = _rng(p.name, seed)
    base = 440.0 * 2 ** ((pitch - 69) / 12)
    bend = np.ones(n)
    if p.pitch_env:
        bend *= 2 ** (p.pitch_env * np.exp(-t * 6.9 / p.pitch_decay) / 12)
    rate, depth, delay = p.vibrato
    if depth:
        ramp = np.clip((t - delay) / 0.3, 0, 1)
        bend *= 2 ** (depth * ramp * np.sin(2 * np.pi * rate * t) / 1200)
    left, right = np.zeros(n), np.zeros(n)
    voices = max(1, p.unison)
    for v in range(voices):
        x = (v / (voices - 1) - 0.5) if voices > 1 else 0.0
        cents, pan = x * p.detune, x * 2 * p.spread
        sig = np.zeros(n)
        for o in p.oscs:
            freq = base * 2 ** ((o.semitones + cents / 100) / 12) * bend
            sig += o.level * oscillator(o.wave, freq, rng.random(), t, o.fm or None)
        theta = (pan + 1) * np.pi / 4
        left += sig * np.cos(theta)
        right += sig * np.sin(theta)
    audio = np.stack([left, right]) * (np.sqrt(2) / np.sqrt(voices))
    if p.cutoff is not None:
        audio = _filter(audio, p, pitch, vel, t)
    if p.drive:
        audio = np.tanh(audio * (1 + p.drive)) / np.tanh(1 + p.drive)
    amp = (vel / 127) ** 1.5 * p.gain
    return (audio * _envelope(n, hold, p) * amp).T.astype(np.float32)


def _filter(audio: np.ndarray, p: Preset, pitch: int, vel: int, t: np.ndarray) -> np.ndarray:
    base = p.cutoff * 2 ** (p.key_track * (pitch - 60) / 12)
    amount = p.filter_env * (1 - p.vel_to_filter + p.vel_to_filter * vel / 127)
    lp = LadderFilter(mode=LadderFilter.Mode.LPF24, cutoff_hz=float(base), resonance=p.resonance, drive=1.0)
    audio = audio.astype(np.float32)
    if not amount:
        return lp(audio, SR)
    out = np.empty_like(audio)
    for i in range(0, audio.shape[1], BLOCK):
        lp.cutoff_hz = float(min(base + amount * np.exp(-t[i] * 6.9 / p.filter_decay), 18000))
        out[:, i:i + BLOCK] = lp.process(audio[:, i:i + BLOCK], SR, reset=False)
    return out


# ---------- 音色 ----------

PRESETS: dict[str, Preset] = {p.name: p for p in [
    # 科技感琶音的"拨弦"：锯齿加方波，滤波器包络快速关闭，力度越大越亮
    Preset("synth-pluck", (Osc("saw"), Osc("square", 0.5)), unison=3, detune=12, spread=0.4,
           attack=0.002, decay=0.45, sustain=0.0, release=0.2,
           cutoff=500, resonance=0.25, filter_env=5500, filter_decay=0.3, key_track=0.4, drive=0.3),
    # 宽厚的铺底：7 声部失谐锯齿，慢起慢收
    Preset("synth-pad", (Osc("saw"), Osc("square", 0.3, -12)), unison=7, detune=24, spread=0.9,
           attack=0.4, decay=1.5, sustain=0.85, release=1.0, cutoff=2000, resonance=0.1, key_track=0.3, gain=0.8),
    # 电子乐的"超级锯齿"和弦
    Preset("supersaw", (Osc("saw"),), unison=7, detune=36, spread=1.0,
           attack=0.005, decay=0.6, sustain=0.7, release=0.35, cutoff=5000, resonance=0.1,
           filter_env=4000, filter_decay=0.5, key_track=0.2),
    # 主旋律：锯齿加方波，带延迟颤音
    Preset("synth-lead", (Osc("saw"), Osc("square", 0.4)), unison=2, detune=8, spread=0.2,
           attack=0.01, decay=0.4, sustain=0.8, release=0.2, cutoff=3200, resonance=0.15,
           filter_env=2500, filter_decay=0.35, key_track=0.5, vibrato=(5.5, 14, 0.25)),
    # 合成贝斯：方波加锯齿，再垫一个低八度正弦，滤波器包络让每个音有"嗒"的起音
    Preset("synth-bass", (Osc("square", 0.7), Osc("saw", 0.5), Osc("sine", 0.6, -12)),
           attack=0.003, decay=0.6, sustain=0.55, release=0.08, cutoff=280, resonance=0.3,
           filter_env=1800, filter_decay=0.2, drive=0.5),
    # 纯净的超低音
    Preset("sub-bass", (Osc("sine"), Osc("triangle", 0.15)), attack=0.006, decay=2.0, sustain=0.8, release=0.15,
           drive=0.2),
    # 808：正弦从高一个八度迅速下滑，长衰减，轻度失真
    Preset("bass-808", (Osc("sine"),), attack=0.002, decay=2.2, sustain=0.0, release=0.2,
           pitch_env=12, pitch_decay=0.08, drive=1.2),
    # FM 钟声
    Preset("synth-bell", (Osc("fm", fm=((3.5, 4.0, 0.3, 1.5),)),), attack=0.002, decay=3.0, sustain=0.0,
           release=0.8, gain=0.8),
    # FM 电钢琴（DX7 风格）：温暖的基音加起音瞬间的"叮"
    Preset("fm-epiano", (Osc("fm", fm=((1.0, 1.8, 0.4, 1.2), (14.0, 0.8, 0.0, 0.08))),), attack=0.002,
           decay=2.5, sustain=0.0, release=0.4, gain=0.9),
]}


# ---------- 合成鼓组 ----------

def _band(x: np.ndarray, lo: float | None = None, hi: float | None = None) -> np.ndarray:
    fx = ([HighpassFilter(lo)] if lo else []) + ([LowpassFilter(hi)] if hi else [])
    return Pedalboard(fx)(x.astype(np.float32)[None, :], SR)[0] if fx else x


def _sweep(f_end: float, f_start: float, tau: float, t: np.ndarray) -> np.ndarray:
    freq = f_end + (f_start - f_end) * np.exp(-t / tau)
    return np.sin(2 * np.pi * np.cumsum(freq) / SR)


def _metal(t: np.ndarray, rng) -> np.ndarray:
    """808 式金属声：6 个非整数倍频率的方波叠加。"""
    return sum(np.sign(np.sin(2 * np.pi * f * t + rng.random() * 6.28)) for f in (205.3, 304.4, 369.6, 522.7, 540.0, 800.0)) / 6


@lru_cache(maxsize=None)
def drum_hit(kind: str, variant: int) -> np.ndarray:
    """一下鼓的模板（单声道），按种类和轮替编号缓存。"""
    rng = _rng("drum", kind, variant)
    dur = {"kick": 0.6, "snare": 0.35, "clap": 0.45, "hat": 0.12, "pedal-hat": 0.1, "open-hat": 0.6, "rim": 0.08,
           "tom": 0.5, "crash": 2.5, "ride": 1.6, "shaker": 0.12, "tambourine": 0.25, "cowbell": 0.35, "conga": 0.4}[kind]
    t = np.arange(int(dur * SR)) / SR
    noise = rng.standard_normal(len(t))
    if kind == "kick":
        x = _sweep(48, 160, 0.03, t) * np.exp(-t / 0.22) + _band(noise, 2000) * np.exp(-t / 0.003) * 0.25
        x = np.tanh(1.6 * x)
    elif kind == "snare":
        tone = 0.5 * np.sin(2 * np.pi * 185 * t) * np.exp(-t / 0.05) + 0.3 * np.sin(2 * np.pi * 330 * t) * np.exp(-t / 0.035)
        x = tone + 0.8 * _band(noise, 1500, 9000) * np.exp(-t / 0.09)
    elif kind == "clap":
        env = sum(np.where(t >= d, np.exp(-(t - d) / 0.006), 0) for d in (0.0, 0.011, 0.022))
        env += 0.6 * np.where(t >= 0.03, np.exp(-(t - 0.03) / 0.12), 0)
        x = _band(noise, 900, 5000) * env
    elif kind in ("hat", "pedal-hat", "open-hat"):
        decay = {"hat": 0.03, "pedal-hat": 0.02, "open-hat": 0.22}[kind]
        x = _band(0.6 * _metal(t, rng) + 0.5 * noise, 7000) * np.exp(-t / decay)
    elif kind == "rim":
        x = np.sin(2 * np.pi * 1700 * t) * np.exp(-t / 0.008) + 0.4 * _band(noise, 3000) * np.exp(-t / 0.004)
    elif kind == "tom":
        x = _sweep(110, 170, 0.05, t) * np.exp(-t / 0.18)
    elif kind in ("crash", "ride"):
        decay = 0.9 if kind == "crash" else 0.6
        x = _band(0.5 * _metal(t, rng) + noise, 3000 if kind == "crash" else 5000) * np.exp(-t / decay)
        if kind == "ride":
            x += 0.3 * np.sin(2 * np.pi * 3200 * t) * np.exp(-t / 0.25)
    elif kind in ("shaker", "tambourine"):
        att = np.clip(t / 0.008, 0, 1)
        x = _band(noise, 6000 if kind == "shaker" else 4000) * att * np.exp(-t / (0.035 if kind == "shaker" else 0.08))
        if kind == "tambourine":
            x += 0.4 * _band(_metal(t, rng), 5000) * np.exp(-t / 0.1)
    elif kind == "cowbell":
        x = _band(np.sign(np.sin(2 * np.pi * 540 * t)) + np.sign(np.sin(2 * np.pi * 800 * t)), 400, 6000) * np.exp(-t / 0.1)
    else:  # conga
        x = _sweep(200, 260, 0.04, t) * np.exp(-t / 0.12)
    x = x * np.minimum(1, t / 0.0005)  # 0.5ms 起音，避免咔哒
    x[-min(len(x), 220):] *= np.linspace(1, 0, min(len(x), 220))
    return (x / (np.abs(x).max() + 1e-9) * 0.8).astype(np.float32)


# GM 鼓键位 → 合成鼓的种类
DRUM_KINDS = {36: "kick", 37: "rim", 38: "snare", 39: "clap", 42: "hat", 44: "pedal-hat", 46: "open-hat",
              45: "tom", 47: "tom", 50: "tom", 49: "crash", 57: "crash", 51: "ride", 53: "ride",
              54: "tambourine", 56: "cowbell", 62: "conga", 64: "conga", 69: "shaker", 70: "shaker"}
TOM_PITCH = {45: 0.75, 47: 1.0, 50: 1.3}  # 通鼓高低用重采样改变音高


@dataclass(frozen=True)
class Kit:
    name: str = "drums-synth"
    pans: dict = field(default_factory=lambda: {"hat": 0.25, "pedal-hat": 0.25, "open-hat": 0.25, "ride": -0.3,
                                                "crash": -0.2, "shaker": 0.35, "tambourine": -0.35, "conga": 0.3})


KIT = Kit()


# ---------- 渲染一个声部 ----------

def render_part(score: Score, part: Part, impl, n: int) -> np.ndarray:
    from .sampler import _expression_curve, _pan

    out = np.zeros((n, 2), np.float32)
    rr: dict[int, int] = {}
    for note in part.notes:
        start = round(score.seconds(note.start) * SR)
        if isinstance(impl, Kit):
            kind = DRUM_KINDS.get(note.pitch)
            if kind is None:
                continue
            rr[note.pitch] = rr.get(note.pitch, -1) + 1
            hit = drum_hit(kind, rr[note.pitch] % 4)
            if note.pitch in TOM_PITCH:
                idx = np.arange(0, len(hit) - 1, TOM_PITCH[note.pitch])
                hit = np.interp(idx, np.arange(len(hit)), hit).astype(np.float32)
            audio = _pan(np.stack([hit, hit], axis=1), impl.pans.get(kind, 0.0)) * (note.vel / 127) ** 1.6
        else:
            audio = render_note(impl, note.pitch, note.vel, score.duration(note.start, note.dur),
                                (part.name, round(note.start, 4), note.pitch))
        end = min(n, start + len(audio))
        if end > start:
            out[start:end] += audio[: end - start]
    curve = _expression_curve(score, part, n)
    if curve is not None:
        out *= curve[:, None]
    return out

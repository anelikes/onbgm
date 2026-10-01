"""乐器性音效：和音乐共用一个时钟的合成音效（上升音、冲击、下坠低音、whoosh 等）。

只做需要和音乐对齐的合成类音效，不做拟音和环境音。有音高的音效（下坠低音、提示音、上升音里的音调）
自动取当时和弦的根音，和音乐同调。

每种音效有一个"对准点"：上升音和反向镲在 at 那一刻结束，冲击和下坠从 at 开始，whoosh 的最响处落在 at 上。
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np
import pyloudnorm as pyln
from pedalboard import HighpassFilter, LadderFilter, LowpassFilter, Pedalboard

from .synth import PRESETS, SR, drum_hit, oscillator, render_note
from .theory import place


@dataclass(frozen=True)
class SfxType:
    anchor: str  # "end"：在 at 结束；"start"：从 at 开始；"center"：最响处在 at
    length: float | None  # 默认长度（拍）；None 表示长度由音效本身决定
    description: str
    natural: float = 0.0  # 长度由音效本身决定时，大约多少秒（用于时间轴预览）


SFX_TYPES = {
    "riser": SfxType("end", 8, "上升音：噪声和音调一起往上扫、越来越响，在 at 那一刻戛然而止（常用在高潮或 drop 之前）"),
    "reverse-cymbal": SfxType("end", 4, "反向镲：吊镲倒放，在 at 那一刻达到最响（段落转换前的经典过门）"),
    "downlifter": SfxType("start", 4, "下降音：从 at 开始往下扫、逐渐消失（重击之后、或段落回落时）"),
    "impact": SfxType("start", None, "冲击：低频轰鸣加瞬态重击，从 at 开始（画面重点、产品亮相、标题出现）", 3.0),
    "sub-drop": SfxType("start", 4, "下坠低音：从当前和弦根音往下滑两个八度（drop、重点出现时托底）"),
    "whoosh": SfxType("center", 2, "whoosh：滤波噪声快速掠过，最响处落在 at（转场、镜头切换）"),
    "pop": SfxType("start", None, "pop：短促的音调气泡声，和当前和弦同调（图标、文字出现）", 0.15),
    "ding": SfxType("start", None, "ding：FM 钟声提示音，弹当前和弦的根音（完成、logo、通知）", 0.9),
}
TARGET_LUFS = -15.0  # level = 1 时，音效最响的 400ms 的响度（和高潮段的音乐相当）


@dataclass
class SfxEvent:
    type: str
    at: float  # 对准点（拍）
    length: float  # 拍
    level: float
    duck: float  # 发生时把音乐压低多少 dB
    path: str  # 在乐谱里的位置，用于报错

    def span(self, natural: float = 0.0) -> tuple[float, float]:
        """在时间轴上占据的范围（拍）；natural 是长度由音效本身决定时的长度。"""
        anchor = SFX_TYPES[self.type].anchor
        length = self.length or natural
        if anchor == "end":
            return self.at - length, self.at
        if anchor == "center":
            return self.at - length / 2, self.at + length / 2
        return self.at, self.at + length


def _rng(*key) -> np.random.Generator:
    return np.random.default_rng(zlib.crc32(repr(key).encode()))


def _sweep_filter(noise: np.ndarray, mode, cutoffs: np.ndarray, resonance: float = 0.3) -> np.ndarray:
    """按 256 采样一块更新截止频率的滤波（噪声扫频）。"""
    f = LadderFilter(mode=mode, cutoff_hz=float(cutoffs[0]), resonance=resonance)
    out = np.empty_like(noise)
    for i in range(0, noise.shape[1], 256):
        f.cutoff_hz = float(np.clip(cutoffs[i], 20, 18000))
        out[:, i:i + 256] = f.process(noise[:, i:i + 256], SR, reset=False)
    return out


def _fade(x: np.ndarray, fade_in: float = 0.002, fade_out: float = 0.01) -> np.ndarray:
    a, b = min(len(x), int(fade_in * SR)), min(len(x), int(fade_out * SR))
    x[:a] *= np.linspace(0, 1, a)[:, None]
    x[len(x) - b:] *= np.linspace(1, 0, b)[:, None]
    return x


def synthesize(ev: SfxEvent, seconds: float, root_pc: int, seed) -> np.ndarray:
    """合成一个音效，返回立体声（未定电平）。seconds 是长度（秒），root_pc 是当时和弦的根音。"""
    rng = _rng(ev.type, seed)
    n = max(1, int(seconds * SR))
    t = np.arange(n) / SR
    x = t / max(seconds, 1e-6)  # 0 → 1 的进度
    noise = rng.standard_normal((2, n)).astype(np.float32)
    root2 = place(root_pc, 36)  # 低八度的根音（C2–B2）
    if ev.type == "riser":
        hp = _sweep_filter(noise, LadderFilter.Mode.HPF24, 150 * (8000 / 150) ** (x ** 1.5))
        freq = 440 * 2 ** ((root2 + 12 - 69) / 12) * 2 ** (2 * x ** 2)  # 两个八度往上滑
        tone = oscillator("saw", freq, 0.0, t)
        tone = Pedalboard([LowpassFilter(6000)])(np.stack([tone, tone]).astype(np.float32), SR)
        out = (0.7 * hp + 0.35 * tone) * (x ** 2)
        return _fade(out.T, 0.02, 0.008)
    if ev.type == "reverse-cymbal":
        crash = drum_hit("crash", 0)[::-1]
        crash = crash[-n:] if len(crash) >= n else np.pad(crash, (n - len(crash), 0))
        crash2 = drum_hit("crash", 1)[::-1]
        crash2 = crash2[-n:] if len(crash2) >= n else np.pad(crash2, (n - len(crash2), 0))
        return _fade(np.stack([crash, crash2], axis=1).astype(np.float32), 0.05, 0.004)
    if ev.type == "downlifter":
        lp = _sweep_filter(noise, LadderFilter.Mode.LPF24, 9000 * (200 / 9000) ** (x ** 0.6))
        freq = 440 * 2 ** ((root2 + 24 - 69) / 12) * 2 ** (-2 * x)
        tone = oscillator("saw", freq, 0.0, t)
        out = (0.7 * lp + 0.25 * np.stack([tone, tone])) * (1 - x) ** 1.5
        return _fade(out.T, 0.003, 0.05)
    if ev.type == "impact":
        n = int(3.0 * SR)
        t = np.arange(n) / SR
        boom = np.sin(2 * np.pi * np.cumsum(28 + 50 * np.exp(-t / 0.15)) / SR) * np.exp(-t / 0.9)
        thump = np.sin(2 * np.pi * np.cumsum(60 + 90 * np.exp(-t / 0.03)) / SR) * np.exp(-t / 0.18)
        burst = Pedalboard([HighpassFilter(800)])(rng.standard_normal((2, n)).astype(np.float32), SR) * np.exp(-t / 0.08)
        metal = np.stack([drum_hit("crash", 2), drum_hit("crash", 3)])
        metal = np.pad(metal, ((0, 0), (0, max(0, n - metal.shape[1]))))[:, :n]
        out = np.tanh(1.5 * (np.stack([boom, boom]) + 0.6 * np.stack([thump, thump]))) + 0.35 * burst + 0.25 * metal
        return _fade(out.T.astype(np.float32), 0.001, 0.2)
    if ev.type == "sub-drop":
        freq = 440 * 2 ** ((root2 - 69) / 12) * 2 ** (-2 * (1 - np.exp(-3 * x)) / (1 - np.exp(-3)))
        tone = np.tanh(1.8 * oscillator("sine", freq, 0.0, t)) * (1 - x) ** 0.7
        return _fade(np.stack([tone, tone], axis=1).astype(np.float32), 0.003, 0.05)
    if ev.type == "whoosh":
        center = 400 * (3200 / 400) ** np.sin(np.pi * x)  # 掠过时频率先升后降
        band = _sweep_filter(_sweep_filter(noise, LadderFilter.Mode.HPF12, center * 0.5, 0.1),
                             LadderFilter.Mode.LPF12, center * 2.5, 0.1)
        env = np.sin(np.pi * x) ** 2.5
        pan = np.stack([np.cos((0.25 + 0.5 * x) * np.pi / 2), np.sin((0.25 + 0.5 * x) * np.pi / 2)])  # 从左掠到右
        return _fade((band * env * pan * 1.4).T, 0.003, 0.01)
    if ev.type == "pop":
        n = int(0.15 * SR)
        t = np.arange(n) / SR
        f0 = 440 * 2 ** ((place(root_pc, 72) - 69) / 12)
        tone = np.sin(2 * np.pi * np.cumsum(f0 * (1 + 0.8 * np.exp(-t / 0.012))) / SR) * np.exp(-t / 0.04)
        return _fade(np.stack([tone, tone], axis=1).astype(np.float32), 0.0005, 0.01)
    if ev.type == "ding":
        return render_note(PRESETS["synth-bell"], place(root_pc, 84), 110, 0.1, seed)
    raise ValueError(ev.type)


def _momentary_peak(audio: np.ndarray) -> float:
    """最响的 400ms 窗口的响度（LUFS）。"""
    meter = pyln.Meter(SR, block_size=0.4)
    win, hop = int(0.4 * SR), int(0.1 * SR)
    if len(audio) < win:
        audio = np.pad(audio, ((0, win - len(audio)), (0, 0)))
    best = -np.inf
    for i in range(0, len(audio) - win + 1, hop):
        seg = audio[i:i + win]
        if np.abs(seg).max() > 1e-5:
            best = max(best, meter.integrated_loudness(seg))
    return best


def render(score, events: list[SfxEvent], n: int) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """渲染所有音效。返回 (音效轨, 给音乐用的增益包络, 每个音效实际的起止时间)。"""
    out = np.zeros((n, 2), np.float32)
    duck = np.zeros(n, np.float32)  # dB，≤ 0
    placed = []
    for i, ev in enumerate(events):
        root = score.chord_at(min(ev.at, score.total_beats - 1e-6)).chord.root
        length_s = score.seconds(ev.length) if ev.length else 0.0
        audio = synthesize(ev, length_s, root, (i, round(ev.at, 4), ev.type))
        natural = len(audio) / SR * score.bpm / 60
        start_beat, end_beat = ev.span(natural)
        loud = _momentary_peak(audio)
        if np.isfinite(loud):
            audio = audio * 10 ** ((TARGET_LUFS + 20 * np.log10(max(ev.level, 1e-3)) - loud) / 20)
        start = round(score.seconds(start_beat) * SR)
        a, b = max(0, start), min(n, start + len(audio))
        if b > a:
            out[a:b] += audio[a - start: b - start]
        if ev.duck:
            hit = round(score.seconds(ev.at) * SR)
            t = (np.arange(n) - hit) / SR
            shape = np.where(t < -0.03, 0.0, np.where(t < 0, (t + 0.03) / 0.03, np.exp(-np.maximum(t, 0) / 0.35)))
            duck = np.minimum(duck, -ev.duck * shape)
        placed.append({"type": ev.type, "at_s": round(score.seconds(ev.at), 3),
                       "start_s": round(score.seconds(start_beat), 3), "end_s": round(score.seconds(end_beat), 3),
                       "path": ev.path})
    return out, (10 ** (duck / 20)).astype(np.float32), placed

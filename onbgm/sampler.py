"""轻量采样器：直接读 WAV 采样，用 soxr 高质量重采样变调，不依赖 SFZ 播放器。

加载时会做三件让声音更干净的事：
- 裁掉采样开头的静音，保证音符准时；
- 把各音高、各力度层的采样响度对齐，避免"有的音突然很响"；
- 用频谱检测每个采样的真实音高，不完全相信文件名（不同音源的八度记法不一样）。
"""
from __future__ import annotations

import json
import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

from .arrange import Part
from .score import Score

SR = 44100
_NOTE_RE = re.compile(r"_([A-G]#?)(-?\d)(?:_[vV](\d+))?(?:_(?:rr|RR)?(\d+))?(?=[_.])")
_PC = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}


@dataclass
class Zone:
    path: Path
    key: float | None  # 采样实际音高（MIDI，可带小数）；None 表示不变调（镲等）
    layer: int  # 力度层，0 最轻
    group: int  # 同一个音的几个轮替采样属于同一组


@dataclass
class Instrument:
    name: str
    zones: list[Zone]
    sustain: bool = True  # 持续音按音符长度截断（可循环延长）；否则让采样自然衰减
    release: float = 0.35  # 松开后的淡出时间（秒）
    pan: float = 0.0  # -1 左 … 1 右
    align_peak: bool = False  # 让采样的最响点对齐音符起点（渐强镲、上扬音效）
    legato_skip: float = 0.07  # 连奏时跳过新音符起音的秒数，减少重复运弓感
    articulations: dict | None = None  # 演奏法 → 另一件乐器，如 {"roll": 定音鼓滚奏}

    def pick(self, pitch: int, vel: int, rr_state: dict) -> Zone:
        zones = self.zones
        if zones[0].key is not None:
            # 先选音高最近的一组（距离相同时优先从下方往上移调），再在这组已有的力度层里选
            best = min(zones, key=lambda z: abs(z.key - pitch) + (0.3 if z.key > pitch else 0)).group
            zones = [z for z in zones if z.group == best]
        layers = sorted({z.layer for z in zones})
        layer = layers[min(len(layers) - 1, (vel - 1) * len(layers) // 127)]
        cands = [z for z in zones if z.layer == layer]
        k = (layer, cands[0].group)
        rr_state[k] += 1
        return cands[rr_state[k] % len(cands)]


# ---------- 采样分析 ----------

def detect_pitch(audio: np.ndarray, hint: float | None = None, window: float = 7.0) -> float:
    """谐波乘积谱估计基频；给了 hint 就只在其附近 ±window 半音内找。"""
    mono = audio.mean(axis=1)
    onset = _onset(audio)
    seg = mono[onset + int(0.25 * SR): onset + int(1.75 * SR)]
    if len(seg) < SR // 4:
        seg = mono[onset:]
    seg = seg * np.hanning(len(seg))
    n = 1 << 19
    spec = np.abs(np.fft.rfft(seg, n))
    freqs = np.fft.rfftfreq(n, 1 / SR)
    hps = spec.copy()
    for h in (2, 3, 4):
        dec = spec[::h]
        hps[: len(dec)] *= dec
    lo, hi = np.searchsorted(freqs, [25, 2500])
    if hint is not None:
        f_hint = 440 * 2 ** ((hint - 69) / 12)
        lo, hi = np.searchsorted(freqs, [f_hint * 2 ** (-window / 12), f_hint * 2 ** (window / 12)])
    peak = lo + int(np.argmax(hps[lo:hi]))
    return 69 + 12 * np.log2(freqs[peak] / 440)


def detect_membrane_pitch(audio: np.ndarray) -> float:
    """定音鼓的泛音不是整数倍，而是约 1 : 1.5 : 2（膜振动模式）。
    取余音里的强峰，找最低的、在 1.5 倍处也有峰的那个，就是听到的音高。"""
    mono = audio.mean(axis=1)[int(0.3 * SR): int(2.3 * SR)]
    mono = mono * np.hanning(len(mono))
    spec = np.abs(np.fft.rfft(mono, 1 << 19))
    freqs = np.fft.rfftfreq(1 << 19, 1 / SR)
    lo, hi = np.searchsorted(freqs, [50, 800])
    s, fr = spec[lo:hi], freqs[lo:hi]
    peaks = [i for i in range(1, len(s) - 1) if s[i] > s[i - 1] and s[i] > s[i + 1] and s[i] > s.max() * 0.1]
    for i in sorted(peaks, key=lambda i: fr[i]):
        if any(abs(fr[j] / fr[i] - 1.5) < 0.06 for j in peaks):
            return 69 + 12 * np.log2(fr[i] / 440)
    return detect_pitch(audio)


def _onset(audio: np.ndarray) -> int:
    env = np.abs(audio).max(axis=1)
    idx = int(np.argmax(env > env.max() * 0.02))
    return max(0, idx - int(0.005 * SR))


@lru_cache(maxsize=None)
def _load(path: Path) -> tuple[np.ndarray, float]:
    """读采样：转成 44.1k 立体声、裁掉开头静音、响度归一。返回 (音频, 峰值位置秒)。"""
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    if sr != SR:
        audio = soxr.resample(audio, sr, SR)
    audio = audio[_onset(audio):]
    head = audio[: int(1.5 * SR)]
    rms = float(np.sqrt(np.mean(head ** 2))) or 1.0
    audio = audio * (0.1 / rms)
    return np.ascontiguousarray(audio), float(np.argmax(np.abs(audio).max(axis=1)) / SR)


def _extend(audio: np.ndarray, needed: int) -> np.ndarray:
    """持续音采样不够长时，用等功率交叉淡化循环中段来延长。"""
    if len(audio) >= needed:
        return audio[:needed]
    a, b = int(len(audio) * 0.35), len(audio) - int(0.6 * SR)
    xf = int(0.25 * SR)
    body = audio[a:b]
    t = np.linspace(0, np.pi / 2, xf)[:, None]
    fade_out, fade_in = np.cos(t), np.sin(t)
    out = audio[:b]
    while len(out) < needed:
        joint = out[-xf:] * fade_out + body[:xf] * fade_in
        out = np.concatenate([out[:-xf], joint, body[xf:]])
    return out[:needed]


# ---------- 乐器定义 ----------

def load_instrument(name: str, folder: Path, **kwargs) -> Instrument | None:
    """从文件夹按文件名建乐器。音高以频谱检测为准，结果缓存在 .pitch.json。"""
    named = {}
    for f in sorted(folder.glob("*.wav")):
        if m := _NOTE_RE.search(f.name):
            named[f] = (12 * (int(m.group(2)) + 1) + _PC[m.group(1)], int(m.group(3) or 1))
    if not named:
        return None
    cache_path = folder / ".pitch.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if not all(f.name in cache for f in named):
        # 不同音源的八度记法不同（C4 或 C3 = 中央 C）：先粗测，按多数投票定出偏移，再在附近精确定位
        offsets = [round((detect_pitch(_load(f)[0]) - n) / 12) * 12 for f, (n, _) in named.items()]
        offset = max(set(offsets), key=offsets.count)
        for f, (n, _) in named.items():
            # 八度已由投票确定，文件名的音名可信；这里只修正音分偏差
            cache[f.name] = detect_pitch(_load(f)[0], hint=n + offset, window=1.5)
        cache_path.write_text(json.dumps(cache, indent=1))
    layers = sorted({v for _, v in named.values()})
    zones = [Zone(f, cache[f.name], layers.index(v), n) for f, (n, v) in named.items()]
    return Instrument(name, zones, **kwargs)


def unpitched(name: str, files: list[Path], layer_of=lambda p: 0, **kwargs) -> Instrument | None:
    files = [f for f in files if f.exists()]
    if not files:
        return None
    zones = [Zone(f, None, layer_of(f), 0) for f in files]
    return Instrument(name, zones, sustain=False, **kwargs)


def pitched_hits(name: str, files: list[Path], layer_of, sustain: bool = False, **kwargs) -> Instrument | None:
    """文件名里没有音高的定音鼓：音高全靠检测，同一面鼓的采样归为一组。"""
    files = [f for f in files if f.exists()]
    if not files:
        return None
    cache_path = files[0].parent / ".pitch.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    zones = []
    drums: dict[int, list[Path]] = defaultdict(list)
    for f in files:
        drums[int(m.group(1)) if (m := re.search(r"(\d+)_", f.name)) else 0].append(f)
    for drum, fs in drums.items():
        # 同一面鼓音高不变：所有采样一起投票（越响的层余音越清楚，权重越大），排除个别误判
        for f in fs:
            if f.name not in cache:
                cache[f.name] = detect_membrane_pitch(_load(f)[0])
        votes: dict[int, float] = defaultdict(float)
        for f in fs:
            votes[round(cache[f.name])] += layer_of(f) + 1
        winner = max(votes, key=votes.get)
        key = float(np.mean([cache[f.name] for f in fs if round(cache[f.name]) == winner]))
        zones += [Zone(f, key, layer_of(f), drum) for f in fs]
    cache_path.write_text(json.dumps(cache, indent=1))
    return Instrument(name, zones, sustain=sustain, **kwargs)


# ---------- 渲染 ----------

def _pan(audio: np.ndarray, pan: float) -> np.ndarray:
    theta = (pan + 1) * np.pi / 4
    return audio * np.array([np.cos(theta), np.sin(theta)], dtype=np.float32) * np.sqrt(2)


def _expression_curve(score: Score, part: Part, n: int) -> np.ndarray | None:
    pts = sorted((round(score.seconds(b) * SR), v) for b, cc, v in part.ccs if cc == 11)
    if not pts:
        return None
    x, v = zip(*pts)
    curve = np.interp(np.arange(n), x, v).astype(np.float32)
    return (curve / 127) ** 2  # 与 GM 的 CC11 曲线一致：40·log10(v/127) dB


def render_part(score: Score, part: Part, route, n: int) -> np.ndarray:
    """route(pitch) -> Instrument，允许按音区把音符分给不同乐器（弦乐分部）。"""
    out = np.zeros((n, 2), np.float32)
    rr_state: dict = defaultdict(int)
    ends = sorted(round(score.seconds(x.start + x.dur), 3) for x in part.notes)
    # 非持续音（拨弦、鼓）：同音高再次发声时压住上一个的余音，避免能量无限堆积
    next_same: dict[int, float] = {}
    choke_at = {}
    for x in sorted(part.notes, key=lambda x: -x.start):
        choke_at[id(x)] = next_same.get(x.pitch)
        next_same[x.pitch] = score.seconds(x.start)
    for note in part.notes:
        inst: Instrument = route(note.pitch)
        if note.art and inst.articulations and note.art in inst.articulations:
            inst = inst.articulations[note.art]
        zone = inst.pick(note.pitch, note.vel, rr_state)
        data, peak_t = _load(zone.path)
        ratio = 2 ** ((note.pitch - zone.key) / 12) if zone.key is not None else 1.0
        t0 = score.seconds(note.start)
        skip = 0
        if inst.sustain:
            i = bisect_left(ends, round(t0, 3) - 0.03)
            if i < len(ends) and abs(ends[i] - t0) <= 0.03:  # 紧接着上一个音：当作连奏
                skip = int(inst.legato_skip * SR * ratio)
            hold = score.seconds(note.dur)
            length = int((hold + inst.release) * SR)
            src = _extend(data[skip:], int(length * ratio) + 64)
        else:
            src = data
            if choke_at[id(note)] is not None and not inst.align_peak:
                keep = int((choke_at[id(note)] - t0 + 0.12) * SR * ratio)
                if keep < len(src):
                    fade = min(keep, int(0.12 * SR * ratio))
                    src = src[:keep].copy()
                    src[keep - fade:] *= np.linspace(1, 0, fade)[:, None]
        y = soxr.resample(src, SR * ratio, SR, quality="HQ") if ratio != 1.0 else src.copy()
        if not inst.sustain:
            tail = min(len(y), int(0.03 * SR))  # 有的采样文件结尾没衰减干净，硬切会有咔哒
            y[-tail:] *= np.linspace(1, 0, tail, dtype=np.float32)[:, None]
        if inst.sustain:
            y = y[:length]
            env = np.ones(len(y), np.float32)
            rel_start = int(hold * SR)
            rel = np.cos(np.linspace(0, np.pi / 2, len(y) - rel_start)) ** 2 if len(y) > rel_start else []
            env[rel_start:] = rel
            if skip:
                fade = min(len(env), int(0.04 * SR))
                env[:fade] *= np.linspace(0, 1, fade)
            y = y * env[:, None]
        gain = 10 ** (0.6 * 40 * np.log10(note.vel / 127) / 20)
        start = round(t0 * SR) - (round(peak_t / ratio * SR) if inst.align_peak else 0)
        a, b = max(0, start), min(n, start + len(y))
        if b > a:
            out[a:b] += _pan(y[a - start: b - start], inst.pan) * gain
    curve = _expression_curve(score, part, n)
    if curve is not None:
        out *= curve[:, None]
    return out

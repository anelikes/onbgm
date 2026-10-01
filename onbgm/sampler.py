"""轻量采样器：直接读 WAV 采样，用 soxr 高质量重采样变调，不依赖外部播放器。

乐器有两种来源：
- 一个文件夹的采样，音高写在文件名里（VSCO）。加载时会裁掉开头静音、把各采样的响度对齐、
  用频谱检测真实音高（不同音源的八度记法不一样，不完全相信文件名）；
- SFZ 文件（大多数免费采样库的格式）。区域、力度层、轮替、音量、调音和互斥组都按文件里写的来，
  采样原样使用：作者已经把它们调好了。
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
    # 以下只有 SFZ 区域用
    keys: tuple[int, int] = (0, 127)  # 负责的音高范围
    vels: tuple[int, int] = (0, 127)  # 负责的力度范围
    seq: float = 1  # 轮替位置：同一位置的几个区域一起发声（叠层），不同位置轮流
    gain_db: float = 0.0
    offset: float = 0.0  # 从采样的第几秒开始播放
    keytrack: float = 1.0  # 0 表示不随音高变调（鼓）
    shift: float = 0.0  # 额外的音高偏移（半音）
    veltrack: float = 1.0  # 力度对音量的影响：1 表示 (力度/127)² 的标准曲线
    velcurve: list | None = None  # 自定义力度曲线 [(力度, 增益), ...]，取代标准曲线
    choke: str | None = None  # 互斥组：这个组发声时，切断 off_by 指向它的区域（闭镲切断开镲）
    off_by: str | None = None
    pan: float = 0.0  # 叠加在乐器声像上（鼓组里每件鼓的位置）
    end: float | None = None  # 采样只用到第几秒
    loop: tuple[float, float] | None = None  # 循环段（秒）：音符比采样长时重复这一段
    env: tuple[float, float, float, float] | None = None  # 音量包络 (起音, 保持, 衰减, 持续电平) 秒 / 0~1
    release: float | None = None  # 松开后的淡出时间，不设则用乐器的


@dataclass
class Instrument:
    name: str
    zones: list[Zone]
    sustain: bool = True  # 持续音按音符长度截断（可循环延长）；否则让采样自然衰减
    release: float = 0.35  # 松开后的淡出时间（秒）
    pan: float = 0.0  # -1 左 … 1 右
    align_peak: bool = False  # 让采样的最响点对齐音符起点（渐强镲、上扬音效）
    legato_skip: float = 0.07  # 连奏时跳过新音符起音的秒数，减少重复运弓感
    line_choke: bool = False  # 演奏单声部旋律线时，下一个音出来就收掉上一个（同一组乐手换弓）
    articulations: dict | None = None  # 演奏法 → 另一件乐器，如 {"roll": 定音鼓滚奏}
    sfz: bool = False  # 采样原样使用（不裁静音、不对齐响度），音量按区域的 volume 和力度曲线
    loop: bool = True  # 持续音比采样长时循环中段延长；自然衰减的乐器（电钢琴、吉他）不循环
    kit: bool = False  # 鼓组：每个音高是一件鼓，没有采样的音高不能移调代替

    def voices(self, pitch: int, vel: int, rr_state: dict) -> list[Zone]:
        """这个音要发声的区域。SFZ 乐器可能叠好几层（同一轮替位置的区域一起发声）。"""
        if not self.sfz:
            return [self.pick(pitch, vel, rr_state)]
        vel = max(1, min(127, vel))
        cands = [z for z in self.zones if z.keys[0] <= pitch <= z.keys[1] and z.vels[0] <= vel <= z.vels[1]]
        if not cands:  # 超出采样音域：用最近的区域移调
            near = min(self.zones, key=lambda z: (max(z.keys[0] - pitch, pitch - z.keys[1], 0),
                                                  max(z.vels[0] - vel, vel - z.vels[1], 0)))
            cands = [z for z in self.zones if z.keys == near.keys and z.vels == near.vels]
        slots = sorted({z.seq for z in cands})
        k = ("sfz", cands[0].keys, cands[0].vels)
        slot = slots[rr_state[k] % len(slots)]
        rr_state[k] += 1
        return [z for z in cands if z.seq == slot]

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


@lru_cache(maxsize=None)
def _load_raw(path: Path) -> tuple[np.ndarray, float]:
    """SFZ 的采样原样读入，只转成 44.1k 立体声：起点和音量关系由 SFZ 文件决定。"""
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    audio = audio[:, :2]
    if sr != SR:
        audio = soxr.resample(audio, sr, SR)
    audio = np.ascontiguousarray(audio)
    return audio, float(np.argmax(np.abs(audio).max(axis=1)) / SR)


@lru_cache(maxsize=None)
def _attack_time(path: Path, offset: float) -> float:
    """SFZ 采样从播放起点到真正起音（第一次达到峰值的 10%）的秒数，最多算 50ms。
    有的库在起音前留了手指、踏板之类的预备音（贝斯约 25ms），不补偿的话这件乐器会整体拖拍。"""
    audio = _load_raw(path)[0][round(offset * SR):]
    env = np.abs(audio[: SR]).max(axis=1)
    if not len(env) or env.max() <= 0:
        return 0.0
    return min(int(np.argmax(env > env.max() * 0.1)) / SR, 0.05)


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


def _zone_velocity_db(z: Zone, vel: int) -> float:
    if z.velcurve:
        xs, ys = zip(*z.velcurve)
        curve = float(np.interp(vel, xs, ys))
    else:
        curve = (vel / 127) ** 2
    return 20 * np.log10(max(1 - z.veltrack + z.veltrack * curve, 1e-6))


def _sample_level(path: Path, offset: float) -> float:
    """采样起音后 0.4 秒的响度（近似 K 计权的均方根，dB）。"""
    from pedalboard import HighpassFilter, HighShelfFilter, Pedalboard

    audio = _load_raw(path)[0][round((offset + _attack_time(path, offset)) * SR):][: int(0.4 * SR)]
    if not len(audio):
        return -120.0
    k = Pedalboard([HighpassFilter(60), HighShelfFilter(1500, 4.0)])(np.ascontiguousarray(audio.T), SR)
    return float(10 * np.log10(np.mean(k ** 2) + 1e-12))


def _smooth_layers(zones: list[Zone], cache_path: Path) -> None:
    """让相邻力度层在交界处音量连续：以最响的一层为准，逐层往下调整增益。
    采样库的力度层是分别录的，交界处常常跳 5~10 dB（多一点力度，音量却突然变大或变小）。
    校准后整体力度曲线接近标准曲线，音色仍随力度换层。每个采样的响度只测一次，存在 cache_path。"""
    groups: dict[tuple, list[Zone]] = defaultdict(list)
    for z in zones:
        groups[z.keys].append(z)
    if all(len({z.vels for z in g}) < 2 for g in groups.values()):
        return
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    dirty = False
    for group in groups.values():
        layers = sorted({z.vels for z in group})
        if len(layers) < 2:
            continue
        level = []  # 每层在自身增益下的响度（同一轮替位置的几个区域叠加，再对轮替取平均）
        for vels in layers:
            slots: dict[float, float] = defaultdict(float)
            for z in (z for z in group if z.vels == vels):
                key = f"{z.path.name}@{z.offset:.4f}"
                if key not in cache:
                    cache[key] = _sample_level(z.path, z.offset)
                    dirty = True
                slots[z.seq] += 10 ** ((cache[key] + z.gain_db) / 10)
            level.append(float(np.mean([10 * np.log10(p) for p in slots.values()])))
        rep = {vels: next(z for z in group if z.vels == vels) for vels in layers}
        correction = [0.0] * len(layers)
        for i in range(len(layers) - 2, -1, -1):  # 从最响的一层往下
            lo_layer, hi_layer = layers[i], layers[i + 1]
            top = level[i] + _zone_velocity_db(rep[lo_layer], lo_layer[1])
            bottom = level[i + 1] + _zone_velocity_db(rep[hi_layer], max(hi_layer[0], 1)) + correction[i + 1]
            correction[i] = bottom - top
        for vels, c in zip(layers, correction):
            for z in group:
                if z.vels == vels:
                    z.gain_db += c
    if dirty:
        cache_path.write_text(json.dumps(cache, indent=0))


def load_sfz(name: str, path: Path, key_offset: int = 0, remap: dict | None = None, **kwargs) -> Instrument | None:
    """从 SFZ 文件建乐器。
    key_offset：把整个键位映射平移几个半音（有的贝斯库按记谱音高映射，比实际发声高一个八度）；
    remap：鼓组键位表 {我们的音高: 库里的键位 或 (键位, 升降半音)}，只有表里的鼓可用；
    其余 kwargs 覆盖从文件推断出的设置（sustain、release、pan 等）。"""
    from . import sfz

    if not path.exists():
        return None
    zones, releases, one_shot = [], [], []
    for r in sfz.playable(sfz.parse(path)):
        sample = Path(r["_sample"])
        if not sample.exists():
            continue
        key = r.get("key")
        lo = sfz.note_number(r.get("lokey", key if key is not None else 0)) + key_offset
        hi = sfz.note_number(r.get("hikey", key if key is not None else 127)) + key_offset
        center = sfz.note_number(r.get("pitch_keycenter", key if key is not None else 60)) + key_offset
        gain = float(r.get("volume", 0)) + (20 * np.log10(max(float(r["amplitude"]), 1e-3) / 100)
                                            if "amplitude" in r else 0.0)
        sr = sf.info(sample).samplerate
        mode = r.get("loop_mode")
        loop = None
        if mode not in ("no_loop", "one_shot"):
            points = ((int(r["loop_start"]), int(r["loop_end"])) if "loop_start" in r and "loop_end" in r
                      else sfz.wav_loop(sample))
            if points:  # 没写 loop_mode 但采样里有循环点时，SFZ 默认循环播放
                loop = (points[0] / sr, (points[1] + 1) / sr)
        env = None
        if any(k in r for k in ("ampeg_attack", "ampeg_hold", "ampeg_decay", "ampeg_sustain")):
            env = (float(r.get("ampeg_attack", 0)), float(r.get("ampeg_hold", 0)),
                   float(r.get("ampeg_decay", 0)), float(r.get("ampeg_sustain", 100)) / 100)
        zones.append(Zone(
            sample, float(center), 0, center,
            keys=(lo, hi), vels=(int(r.get("lovel", 0)), int(r.get("hivel", 127))),
            seq=float(r["lorand"]) if "lorand" in r else float(r.get("seq_position", 1)),
            gain_db=gain, offset=int(r.get("offset", 0)) / sr,
            keytrack=float(r.get("pitch_keytrack", 100)) / 100,
            shift=float(r.get("transpose", 0)) + float(r.get("tune", 0)) / 100,
            veltrack=float(r.get("amp_veltrack", 100)) / 100, velcurve=sfz.velcurve(r),
            choke=r.get("group"), off_by=r.get("off_by"), pan=float(r.get("pan", 0)) / 100,
            end=(int(r["end"]) + 1) / sr if "end" in r else None, loop=loop, env=env,
            release=float(r["ampeg_release"]) if "ampeg_release" in r else None,
        ))
        releases.append(float(r.get("ampeg_release", 0.3)))
        one_shot.append(mode == "one_shot")
    if not zones:
        return None
    if remap:
        from dataclasses import replace
        moved = []
        for ours, target in remap.items():
            lib_key, semis = target if isinstance(target, tuple) else (target, 0)
            moved += [replace(z, keys=(ours, ours), shift=z.shift + semis, keytrack=0.0)
                      for z in zones if z.keys == (lib_key, lib_key)]
        zones = moved
    _smooth_layers(zones, path.parent / ".onbgm-levels.json")
    settings = {"sustain": not all(one_shot), "release": max(0.05, float(np.median(releases))),
                "legato_skip": 0.0, "loop": False, **kwargs}
    return Instrument(name, zones, sfz=True, **settings)


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
    starts = sorted(round(score.seconds(x.start), 3) for x in part.notes)
    # 非持续音（拨弦、鼓）：同音高再次发声时压住上一个的余音，避免能量无限堆积；
    # 单声部旋律线上的跳弓之类（line_choke），任何下一个音都会收掉上一个
    line = part.texture in ("melody", "line", "bassline")
    next_same: dict[int, float] = {}
    choke_at, next_any = {}, {}
    upcoming = None
    for x in sorted(part.notes, key=lambda x: -x.start):
        choke_at[id(x)] = next_same.get(x.pitch)
        next_any[id(x)] = upcoming
        next_same[x.pitch] = score.seconds(x.start)
        upcoming = score.seconds(x.start)
    picked = []
    for note in part.notes:
        inst: Instrument = route(note.pitch)
        if note.art and inst.articulations and note.art in inst.articulations:
            inst = inst.articulations[note.art]
        picked.append((note, inst, inst.voices(note.pitch, note.vel, rr_state)))
    # SFZ 互斥组：某个组发声时，切断 off_by 指向它的、还在响的区域（闭镲切断开镲）
    group_starts: dict[str, list[float]] = defaultdict(list)
    for note, _, zones in picked:
        for z in zones:
            if z.choke is not None:
                group_starts[z.choke].append(score.seconds(note.start))
    for times in group_starts.values():
        times.sort()
    for note, inst, zones in picked:
        for zone in zones:
            _voice(out, score, note, inst, zone, ends, starts, choke_at, next_any, group_starts, line, n)
    curve = _expression_curve(score, part, n)
    if curve is not None:
        out *= curve[:, None]
    return out


def _voice(out, score, note, inst: Instrument, zone: Zone, ends, starts, choke_at, next_any, group_starts, line, n):
    """把一个区域的声音叠加到 out 上。"""
    loop = None
    if inst.sfz:
        data, peak_t = _load_raw(zone.path)
        if zone.end:
            data = data[:round(zone.end * SR)]
        if zone.loop:
            loop = (round((zone.loop[0] - zone.offset) * SR), round((zone.loop[1] - zone.offset) * SR))
        if zone.offset:
            data = data[round(zone.offset * SR):]
    else:
        data, peak_t = _load(zone.path)
    ratio = 2 ** (((note.pitch - zone.key) * zone.keytrack + zone.shift) / 12) if zone.key is not None else 1.0
    t0 = score.seconds(note.start)
    skip = 0
    if inst.sustain:
        i = bisect_left(ends, round(t0, 3) - 0.03)
        if i < len(ends) and abs(ends[i] - t0) <= 0.03:  # 紧接着上一个音：当作连奏
            skip = int(inst.legato_skip * SR * ratio)
        hold = score.duration(note.start, note.dur)
        release = zone.release if zone.release is not None else inst.release
        end = round(t0 + hold, 3)
        j = bisect_left(starts, end - 0.03)
        if not (j < len(starts) and abs(starts[j] - end) <= 0.03):
            # 后面有空隙的短音（断奏）：释音也要短，否则会拖住后面的音；连奏的音保留长释音做平滑衔接
            release = min(release, max(0.08, 0.5 * hold))
        length = int((hold + release) * SR)
        needed = int(length * ratio) + 64
        if loop and 0 <= loop[0] < loop[1] <= len(data) and skip + needed > loop[1]:
            # 采样自带循环段：播到循环终点后一直重复这一段（循环点由采样作者对齐过，直接拼接）
            seg = data[loop[0]:loop[1]]
            reps = -(-(skip + needed - loop[1]) // len(seg))
            src = np.concatenate([data[:loop[1]]] + [seg] * reps)[skip:skip + needed]
        elif inst.loop:
            src = _extend(data[skip:], needed)
        else:
            src = data[skip:skip + needed]
            if len(src) < needed:
                src = np.pad(src, ((0, needed - len(src)), (0, 0)))
    else:
        src = data
        cut, fade_s = choke_at[id(note)], 0.12
        if zone.off_by is not None:
            times = group_starts.get(zone.off_by, [])
            i = bisect_left(times, t0 + 1e-6)
            if i < len(times) and (cut is None or times[i] < cut):
                cut, fade_s = times[i], 0.02
        if inst.line_choke:
            # 跳弓这类短音：最多响到谱面写的长度（至少 0.12 秒），旋律线上下一个音出来也立刻收掉
            limits = [t0 + max(score.duration(note.start, note.dur), 0.12)]
            if line and next_any[id(note)] is not None:
                limits.append(next_any[id(note)])
            if cut is None or min(limits) < cut:
                cut, fade_s = min(limits), 0.03
        if cut is not None and not inst.align_peak:
            keep = int((cut - t0 + fade_s) * SR * ratio)
            if keep < len(src):
                fade = min(keep, int(fade_s * SR * ratio))
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
    if inst.sfz:
        vel = max(1, min(127, note.vel))
        if zone.velcurve:
            xs, ys = zip(*zone.velcurve)
            curve = float(np.interp(vel, xs, ys))
        else:
            curve = (vel / 127) ** 2
        gain = (1 - zone.veltrack + zone.veltrack * curve) * 10 ** (zone.gain_db / 20)
        if zone.env:
            y = y * _amp_envelope(zone.env, len(y))[:, None]
    else:
        gain = 10 ** (0.6 * 40 * np.log10(note.vel / 127) / 20)
    start = round(t0 * SR) - (round(peak_t / ratio * SR) if inst.align_peak else 0)
    if inst.sfz:  # 提前开始播放，让真正的起音落在拍子上
        start -= round(_attack_time(zone.path, zone.offset) / ratio * SR)
    a, b = max(0, start), min(n, start + len(y))
    if b > a:
        out[a:b] += _pan(y[a - start: b - start], float(np.clip(inst.pan + zone.pan, -1, 1))) * gain


def _amp_envelope(env: tuple[float, float, float, float], n: int) -> np.ndarray:
    """SFZ 的 ampeg：线性起音，保持，然后按指数衰减到持续电平（衰减时间内降 60 dB）。"""
    attack, hold, decay, sustain = env
    t = np.arange(n, dtype=np.float32) / SR
    out = np.ones(n, np.float32)
    if attack > 0:
        out = np.minimum(out, t / attack)
    if decay > 0:
        after = np.maximum(t - attack - hold, 0)
        out *= sustain + (1 - sustain) * np.exp(-after * 6.9 / decay)
    elif sustain < 1:
        out *= np.where(t >= attack + hold, sustain, 1.0)
    return out.astype(np.float32)

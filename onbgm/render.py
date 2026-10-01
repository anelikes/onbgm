"""渲染：逐声部渲染分轨（SF2 用 FluidSynth，采样乐器用内置采样器），再混音、母带处理。"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from pedalboard import Compressor, Distortion, Gain, HighpassFilter, LowpassFilter, Pedalboard

from . import library, sampler
from .arrange import Part
from .midi_io import write_midi
from .score import Score

SR = 44100
TAIL_SEC = 4.0  # 渲染时在乐谱结束后多渲染的秒数（混响和释音）
# 固定的总增益：各声部已经按角色电平对齐，整体响度天然稳定在 -16 LUFS 左右。
# 不按每首的平均响度去归一化，这样改一处只影响那一处，不会牵动整首的音量。
MASTER_GAIN_DB = 1.0
PEAK_CEILING_DB = -1.0
LIMIT_WARN_DB = 6.0  # 限幅器需要压掉超过这么多时提醒：多半是某个声部太响或叠得太厚
# 每种伴奏角色在自己出现的段落里的目标响度（LUFS）。按实际渲染结果测量后对齐，
# 所以换音源、换织体密度（扫弦一下 6 根弦、鼓一下一下）都不会打乱平衡。数值从《夜雨》认可的混音反推。
ROLE_LUFS = {
    "arpeggio": -21.0, "pad": -21.0, "comp": -21.0, "strum": -19.0,
    "bass-sustain": -22.0, "bass-pizz": -21.0, "bassline": -20.0,
    "groove": -19.0, "timpani": -22.0, "crash": -29.0,
}


def _db(x: float) -> float:
    return 10 ** (x / 20)


def _fit(audio: np.ndarray, n: int) -> np.ndarray:
    return np.pad(audio, ((0, max(0, n - len(audio))), (0, 0)))[:n]


def _fluidsynth(score: Score, part: Part, sf2: Path, n: int) -> np.ndarray:
    with tempfile.TemporaryDirectory() as tmp:
        mid, wav = Path(tmp) / "part.mid", Path(tmp) / "part.wav"
        write_midi(mid, score, [part], _tail_sec(score) * score.bpm / 60)
        subprocess.run(
            ["fluidsynth", "-ni", "-q", "-R", "0", "-C", "0", "-g", "0.5", "-r", str(SR),
             "-O", "float", "-T", "wav", "-F", str(wav), str(sf2), str(mid)],
            check=True, capture_output=True,
        )
        audio, _ = sf.read(wav, dtype="float32", always_2d=True)
    return _fit(audio, n)


def _render_raw(score: Score, part: Part, impl, n: int) -> np.ndarray:
    if isinstance(impl, library.SF2):
        if impl.program is not None:
            part = replace(part, sound=replace(part.sound, program=impl.program))
        return _fluidsynth(score, part, library.SOUNDS_DIR / impl.file, n)
    return sampler.render_part(score, part, impl, n)


def _describe(impl) -> str:
    if isinstance(impl, library.SF2):
        return impl.file
    names = sorted({impl(p).name for p in range(21, 109)})
    return "vsco2: " + "/".join(names)


def render_stems(score: Score, parts: list[Part], soundset: str):
    if not shutil.which("fluidsynth"):
        raise RuntimeError("找不到 fluidsynth，请先安装：brew install fluid-synth")
    n = round((score.seconds(score.total_beats) + _tail_sec(score)) * SR)
    stems, sources = {}, {}
    for part in parts:
        impl = library.resolve(part.sound_name, soundset, {n.pitch for n in part.notes})
        stems[part.name] = _render_raw(score, part, impl, n)
        sources[part.name] = _describe(impl)
    return stems, sources


def _tail_sec(score: Score) -> float:
    return max(TAIL_SEC, score.tail)


def _sections_with_notes(score: Score, part: Part) -> set[str]:
    return {score.section_of(x.start).name for x in part.notes}


def _active(score: Score, part: Part, audio: np.ndarray) -> np.ndarray:
    """只取这个声部有音符的那些段落。"""
    names = _sections_with_notes(score, part)
    secs = [s for s in score.sections if s.name in names]
    return np.concatenate([audio[round(score.seconds(s.start) * SR): round(score.seconds(s.end) * SR)] for s in secs])


def level_parts(score: Score, parts: list[Part], stems: dict[str, np.ndarray]) -> None:
    """伴奏声部按角色定音量：整轨一个固定增益，段落之间的强弱变化原样保留。"""
    meter = pyln.Meter(SR)
    for p in parts:
        if p.texture in ROLE_LUFS and p.notes:
            lufs = meter.integrated_loudness(_active(score, p, stems[p.name]))
            if np.isfinite(lufs):
                stems[p.name] = stems[p.name] * _db(ROLE_LUFS[p.texture] - lufs)


def automix(score: Score, parts: list[Part], stems: dict[str, np.ndarray]) -> list[str]:
    """旋律声部按每段的伴奏自动定音量：至少比最响的伴奏高 3 dB，且不低于伴奏总和 1.5 dB。
    伴奏稀疏时靠前一点，满编制时融进去一点。乐谱里的 gain 仍会叠加在结果上。"""
    meter = pyln.Meter(SR)

    def lufs(x: np.ndarray) -> float:
        return meter.integrated_loudness(x) if np.abs(x).max() > 1e-5 else -np.inf

    log = []
    accomp = [p for p in parts if p.texture != "melody"]
    for mel in (p for p in parts if p.texture == "melody"):
        points = []  # (采样位置, 增益 dB)
        gains: dict[str, float] = {}
        names = _sections_with_notes(score, mel)
        for s in score.sections:
            if s.name not in names:
                continue
            a, b = round(score.seconds(s.start) * SR), round(score.seconds(s.end) * SR)
            levels = [lufs(stems[p.name][a:b] * _db(p.gain_db)) for p in accomp]
            levels = [x for x in levels if np.isfinite(x)]
            own = lufs(stems[mel.name][a:b])
            if not levels or not np.isfinite(own):
                continue
            total = 10 * np.log10(sum(10 ** (x / 10) for x in levels))
            g = max(max(levels) + 3, total - 1.5) - own
            points += [(a + round(0.15 * SR), g), (b - round(0.15 * SR), g)]
            gains[s.name] = g
            log.append(f"{mel.name}@{s.name} {g:+.1f} dB")
        if gains:
            mid = float(np.median(list(gains.values())))
            for s, g in gains.items():
                if abs(g - mid) > 18:
                    log.append(f"⚠ {mel.name} 在 {s} 段的自动增益和其他段相差 {g - mid:+.0f} dB，"
                               f"请检查这一段的旋律是否只有零星几个音，或写错了段落")
        if points:
            x, g = zip(*points)
            env = _db(np.interp(np.arange(len(stems[mel.name])), x, g)).astype(np.float32)
            stems[mel.name] = stems[mel.name] * env[:, None]
    return log


# ---------- 混音 ----------

@lru_cache(maxsize=None)
def hall_ir(rt60: float = 2.3, predelay: float = 0.022, seed: int = 11) -> np.ndarray:
    """合成音乐厅脉冲响应：左右声道独立的噪声，按频段设置衰减（高频衰减更快，声音更暖）。"""
    n = int(SR * rt60 * 1.3)
    rng = np.random.default_rng(seed)
    spec = np.fft.rfft(rng.standard_normal((n, 2)), axis=0)
    f = np.fft.rfftfreq(n, 1 / SR)
    t = np.arange(n) / SR
    ir = np.zeros((n, 2))
    for lo, hi, k in [(20, 250, 1.15), (250, 1000, 1.0), (1000, 4000, 0.8), (4000, 9000, 0.55), (9000, SR / 2 + 1, 0.35)]:
        band = np.fft.irfft(spec * ((f >= lo) & (f < hi))[:, None], n, axis=0)
        ir += band * np.exp(-6.91 * t / (rt60 * k))[:, None]
    ir *= np.minimum(1.0, t / 0.06)[:, None]  # 前 60ms 逐渐变密，像早期反射
    ir = np.concatenate([np.zeros((int(predelay * SR), 2)), ir])
    return (ir / np.sqrt(np.sum(ir ** 2) / 2)).astype(np.float32)


def _convolve(x: np.ndarray, ir: np.ndarray) -> np.ndarray:
    nfft = 1 << (len(x) + len(ir) - 2).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, nfft, axis=0) * np.fft.rfft(ir, nfft, axis=0), nfft, axis=0)
    return y[: len(x)].astype(np.float32)


def _place(audio: np.ndarray, m: library.Mix) -> np.ndarray:
    """EQ → 立体声宽度 → 声像。"""
    if m.eq:
        audio = Pedalboard(m.eq)(np.ascontiguousarray(audio.T), SR).T
    mid, side = (audio[:, 0] + audio[:, 1]) / 2, (audio[:, 0] - audio[:, 1]) / 2 * m.width
    audio = np.stack([mid + side, mid - side], axis=1)
    theta = (m.pan + 1) * np.pi / 4
    return (audio * np.array([np.cos(theta), np.sin(theta)]) * np.sqrt(2)).astype(np.float32)


def mix(stems: dict[str, np.ndarray], parts: list[Part]) -> np.ndarray:
    placed = {p.name: _place(stems[p.name], library.mix_for(p.sound_name)) * _db(p.gain_db) for p in parts}
    dry = sum(placed.values())
    send = sum(placed[p.name] * p.sound.reverb for p in parts)
    send = Pedalboard([HighpassFilter(200), LowpassFilter(9000)])(np.ascontiguousarray(send.T), SR).T
    return (dry + _convolve(send, hall_ir()) * 0.8).astype(np.float32)


def lofi(x: np.ndarray, amount: float, loop_len: int | None = None) -> np.ndarray:
    """lo-fi 质感：磁带抖动（轻微变调）、低通、轻微饱和、黑胶嘶声和噼啪声。
    抖动频率取成循环长度的整数分之一，循环接缝处不会跳；底噪只铺在循环段内，避免尾巴叠回开头时加倍。"""
    n = len(x)
    t = np.arange(n) / SR
    period = (loop_len or n) / SR

    def snap(hz: float) -> float:
        return max(1, round(hz * period)) / period

    depth = 0.0012 * amount  # 秒
    delay = depth * (1.2 + 0.7 * np.sin(2 * np.pi * snap(0.55) * t) + 0.3 * np.sin(2 * np.pi * snap(2.3) * t + 1.3))
    src = np.arange(n) - delay * SR
    y = np.stack([np.interp(src, np.arange(n), x[:, c]) for c in range(2)], axis=1).astype(np.float32)
    tone = Pedalboard([HighpassFilter(30 + 70 * amount), LowpassFilter(14000 - 8000 * amount),
                       Distortion(drive_db=4 * amount)])
    y = tone(np.ascontiguousarray(y.T), SR).T

    rng = np.random.default_rng(7)
    m = loop_len or n
    hiss = Pedalboard([HighpassFilter(1500), LowpassFilter(7000)])(
        np.ascontiguousarray(rng.standard_normal((2, m)).astype(np.float32)), SR).T
    crackle = np.zeros((m, 2), np.float32)
    k = int(m / SR * 6 * amount)  # 每秒约 6 下
    crackle[rng.integers(0, m, k)] = rng.standard_normal((k, 2)) * rng.uniform(0.2, 1, (k, 1))
    crackle = Pedalboard([HighpassFilter(800), LowpassFilter(9000)])(np.ascontiguousarray(crackle.T), SR).T
    ref = float(np.sqrt(np.mean(y ** 2)))
    noise = (hiss * (ref * _db(-38) / (np.sqrt(np.mean(hiss ** 2)) + 1e-12))
             + crackle * (ref * _db(-24) / (np.abs(crackle).max() + 1e-12)))
    y[:m] += noise * amount
    return y


# ---------- 母带 ----------

_BUS = [HighpassFilter(30), Compressor(threshold_db=-18, ratio=2.0, attack_ms=30, release_ms=250)]


def limit(x: np.ndarray, ceiling_db: float = PEAK_CEILING_DB, lookahead_ms: float = 3.0,
          release_ms: float = 80.0) -> np.ndarray:
    """透明的预读限幅器：提前几毫秒看到峰值、平滑压下，峰值以外的部分不动。"""
    blk = 32
    n = len(x)
    nb = -(-n // blk)
    peaks = np.pad(np.abs(x).max(axis=1), (0, nb * blk - n)).reshape(nb, blk).max(axis=1)
    need = np.minimum(1.0, _db(ceiling_db) / np.maximum(peaks, 1e-9))
    la = max(1, round(lookahead_ms / 1000 * SR / blk))
    need = np.lib.stride_tricks.sliding_window_view(np.pad(need, (la, la), constant_values=1.0), 2 * la + 1).min(axis=1)
    rel = 1 - np.exp(-blk / (release_ms / 1000 * SR))
    gain, cur = np.empty(nb), 1.0
    for i, g in enumerate(need):
        cur = min(g, cur + (1 - cur) * rel)
        gain[i] = cur
    gain = np.interp(np.arange(n), np.arange(nb) * blk + blk / 2, gain).astype(np.float32)
    return x * gain[:, None]


def _master(x: np.ndarray, notes: list[str] | None = None) -> np.ndarray:
    pre = Pedalboard(_BUS + [Gain(MASTER_GAIN_DB)])(np.ascontiguousarray(x.T), SR).T
    over = 20 * np.log10(max(float(np.abs(pre).max()), 1e-9)) - PEAK_CEILING_DB
    if notes is not None and over > LIMIT_WARN_DB:
        notes.append(f"⚠ 限幅器压掉了 {over:.1f} dB 峰值，可能有声部太响或叠得太厚")
    return np.clip(limit(pre), -1.0, 1.0)


@dataclass
class Rendered:
    main: np.ndarray  # 可循环时是精确到采样的循环段；否则是乐谱时长（加 meta.tail）
    stems: dict[str, np.ndarray]
    sources: dict[str, str]
    notes: list[str]  # 自动混音的结果和警告
    files: list[Path]


def render(score: Score, parts: list[Part], out: Path, soundset: str = "hq") -> Rendered:
    (out / "stems").mkdir(parents=True, exist_ok=True)
    stems, sources = render_stems(score, parts, soundset)
    level_parts(score, parts, stems)
    mix_log = automix(score, parts, stems)
    for name, audio in stems.items():
        sf.write(out / "stems" / f"{name}.wav", audio, SR, subtype="PCM_24")
    full = mix(stems, parts)
    length = round(score.seconds(score.total_beats) * SR)
    if (score.fx or {}).get("lofi"):
        full = lofi(full, score.fx["lofi"], length if score.loop else None)
    files = []
    if score.loop:
        # 把结尾溢出的混响尾巴叠回开头，循环播放时接缝处就是连续的
        body = full[:length].copy()
        body[: len(full) - length] += full[length:]
        preview_raw = np.concatenate([body, full])  # 播放两遍再自然结束，第一遍到第二遍就是循环接缝
        main = _master(np.concatenate([body, body]), mix_log)[length:]  # 预热一遍，让压缩器状态也首尾一致
        preview = _master(preview_raw)
        sf.write(out / "loop.wav", main, SR, subtype="PCM_24")
        files.append(out / "loop.wav")
    else:
        # 时长精确等于乐谱时长（加 meta.tail），结尾淡出避免硬切
        body = full[: length + round(score.tail * SR)].copy()
        fade = min(len(body), round(max(0.3, score.tail) * SR))
        body[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]
        main = preview = _master(body, mix_log)
        sf.write(out / "song.wav", main, SR, subtype="PCM_24")
        files.append(out / "song.wav")
    sf.write(out / "preview.wav", preview, SR, subtype="PCM_24")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(out / "preview.wav"),
                    "-c:a", "aac", "-b:a", "192k", str(out / "preview.m4a")], check=True)
    write_midi(out / "song.mid", score, parts)
    files += [out / "preview.m4a", out / "preview.wav", out / "song.mid", out / "stems"]
    return Rendered(main, stems, sources, mix_log, files)

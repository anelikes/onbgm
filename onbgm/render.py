"""渲染：逐声部渲染分轨（SF2 用 FluidSynth，采样乐器用内置采样器），再混音、母带处理。"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from pedalboard import Compressor, Delay, Distortion, Gain, HighpassFilter, LowpassFilter, Pedalboard
from pedalboard.io import AudioFile

from . import library, sampler, sfx, synth
from .arrange import Part
from .midi_io import write_midi
from .score import Problem, Score

SR = 44100
TAIL_SEC = 4.0  # 渲染时在乐谱结束后多渲染的秒数（混响和释音）
# 固定的总增益：各声部已经按角色电平对齐，整体响度天然稳定在 -16 LUFS 左右。
# 不按每首的平均响度去归一化，这样改一处只影响那一处，不会牵动整首的音量。
MASTER_GAIN_DB = 1.0
PEAK_CEILING_DB = -1.0
LIMIT_WARN_DB = 6.0  # 限幅器需要压掉超过这么多时提醒：多半是某个声部太响或叠得太厚
# 每种伴奏角色的目标响度（LUFS）：指这个声部的参考片段（4 小节固定和弦、能量 0.7，见 _reference_config）应有的响度。
# 按配置而不是按乐谱内容定音量，改某一段的内容不会让这个声部在其他段落变响或变轻。
# 数值的校准方法：在所有示例和测试乐谱里，取平均能量 0.5~0.85 的伴奏声部，让新方法给出的增益平均和旧方法
# （按声部实际出现的段落测量）一致，这样已经认可过的混音基本不变。
ROLE_LUFS = {
    "arpeggio": -21.8, "pad": -23.5, "comp": -20.3, "arp": -24.1, "strum": -18.8, "line": -24.1,
    "bass-sustain": -25.2, "bass-pizz": -21.5, "bassline": -18.2,
    "groove": -21.3, "timpani": -23.1, "crash": -29.6,
}


def _db(x: float) -> float:
    return 10 ** (x / 20)


def _fit(audio: np.ndarray, n: int) -> np.ndarray:
    return np.pad(audio, ((0, max(0, n - len(audio))), (0, 0)))[:n]


def _fluidsynth(score: Score, part: Part, sf2: Path, n: int) -> np.ndarray:
    if not shutil.which("fluidsynth"):
        raise RuntimeError("找不到 fluidsynth，请先安装：brew install fluid-synth（全部用合成器音色的乐谱不需要它）")
    with tempfile.TemporaryDirectory() as tmp:
        mid, wav = Path(tmp) / "part.mid", Path(tmp) / "part.wav"
        write_midi(mid, score, [part], _tail_sec(score) * score.bpm_at(score.total_beats) / 60)
        subprocess.run(
            ["fluidsynth", "-ni", "-q", "-R", "0", "-C", "0", "-g", "0.5", "-r", str(SR),
             "-O", "float", "-T", "wav", "-F", str(wav), str(sf2), str(mid)],
            check=True, capture_output=True,
        )
        audio, _ = sf.read(wav, dtype="float32", always_2d=True)
    return _fit(audio, n)


def _render_raw(score: Score, part: Part, impl, n: int) -> np.ndarray:
    if isinstance(impl, (synth.Preset, synth.Kit)):
        return synth.render_part(score, part, impl, n)
    if isinstance(impl, library.SF2):
        if impl.program is not None:
            part = replace(part, sound=replace(part.sound, program=impl.program))
        return _fluidsynth(score, part, library.SOUNDS_DIR / impl.file, n)
    return sampler.render_part(score, part, impl, n)


def _describe(impl) -> str:
    if isinstance(impl, (synth.Preset, synth.Kit)):
        return f"synth: {impl.name}"
    if isinstance(impl, library.SF2):
        return impl.file
    insts = list({id(i): i for i in (impl(p) for p in range(21, 109))}.values())
    names = "/".join(sorted(i.name for i in insts))
    return f"{insts[0].source}: {names}"


def render_stems(score: Score, parts: list[Part], soundset: str):
    n = round((score.seconds(score.total_beats) + _tail_sec(score)) * SR)
    stems, sources, impls = {}, {}, {}
    for part in parts:
        impl = library.resolve(part.sound_name, soundset, {n.pitch for n in part.notes})
        stems[part.name] = _render_raw(score, part, impl, n)
        sources[part.name] = _describe(impl)
        impls[part.name] = impl
    return stems, sources, impls


def _tail_sec(score: Score) -> float:
    return max(TAIL_SEC, score.tail)


def _sections_with_notes(score: Score, part: Part) -> set[str]:
    return {score.section_of(x.start).name for x in part.notes}


def _active(score: Score, part: Part, audio: np.ndarray) -> np.ndarray:
    """只取这个声部有音符的那些段落。"""
    names = _sections_with_notes(score, part)
    secs = [s for s in score.sections if s.name in names]
    return np.concatenate([audio[round(score.seconds(s.start) * SR): round(score.seconds(s.end) * SR)] for s in secs])


LEVEL_CACHE = Path(os.environ.get("ONBGM_CACHE", Path.home() / ".cache" / "onbgm")) / "levels.json"
LEVEL_VERSION = 2  # 参考片段或测量方式改变时加一，旧缓存自动作废
REF_HARMONY = "C | Am | F | G"
_level_memo: dict[str, float] = {}


# 参考片段里各织体用的标准节奏型：增益只取决于音色和织体，不取决于这首曲子写的节奏型，
# 所以节奏型写得稀就自然轻、写得密就自然响，改哪一段的节奏型都不会影响整轨音量。
REF_PATTERNS = {
    "groove": {"pattern": {"kick": "x... .... x... ....", "snare": ".... x... .... x...", "hat": "x.x. x.x. x.x. x.x."}},
    "bassline": {"rhythm": "1--- 5--- 1--- 5---"},
    "comp": {"rhythm": "x--- x--- x--- x---"},
    "arp": {"rhythm": "x.x. x.x. x.x. x.x."},
    "strum": {"pattern": "D... D.U. ..U. D.U."},
}


def _reference_config(score: Score, track) -> dict:
    """这个声部的参考片段：同样的音色、织体和音区设置，用这种织体的标准节奏型，放在 4 小节固定和弦、能量 0.7 上。
    不取乐谱里实际写的内容和节奏型，所以改任何一段都不会改变整轨的音量。"""
    from .arrange import SOUNDS
    from .theory import pitch_name

    keep = ("range", "low", "mode", "articulation", "roll", "spread", "gate")
    cfg = {"sound": track.sound, "texture": track.texture,
           **{k: v for k, v in track.options.items() if k in keep}, **REF_PATTERNS.get(track.texture, {})}
    if track.texture == "line":  # 逐音写的伴奏线：用这件乐器音域中间的标准琶音
        s = SOUNDS[track.sound]
        mid = min(max((s.lo + s.hi) // 2, 40), 76)
        bar = " ".join(f"{pitch_name(mid + k)}/4" for k in (0, 4, 7, 12))
        cfg["notes"] = {"ref": " | ".join([bar] * 4)}
    return {"title": "reference", "meta": {"bpm": round(score.bpm, 3), "swing": score.swing,
                                           "swing_unit": score.swing_unit, "seed": 0},
            "sections": [{"name": "ref", "bars": 4, "energy": 0.7}], "harmony": {"ref": REF_HARMONY},
            "tracks": {"ref": cfg}}


def _reference_level(score: Score, track, impl) -> float | None:
    """参考片段的响度（LUFS），按配置缓存：同样的声部配置只渲染一次。"""
    import hashlib
    import json

    from .arrange import arrange
    from .score import from_dict

    cfg = _reference_config(score, track)
    key = hashlib.sha1(json.dumps([LEVEL_VERSION, cfg, _describe(impl), getattr(impl, "file", None)],
                                  sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    if key in _level_memo:
        return _level_memo[key]
    disk = json.loads(LEVEL_CACHE.read_text()) if LEVEL_CACHE.exists() else {}
    if key not in disk:
        ref = from_dict(cfg, "reference")
        part = arrange(ref)[0][0]
        bar_end = round(ref.seconds(ref.total_beats) * SR)
        audio = _render_raw(ref, part, impl, bar_end + 2 * SR)[:bar_end]
        lufs = pyln.Meter(SR).integrated_loudness(audio)
        disk[key] = float(lufs) if np.isfinite(lufs) else None
        LEVEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        LEVEL_CACHE.write_text(json.dumps(disk))
    _level_memo[key] = disk[key]
    return disk[key]


def level_parts(score: Score, parts: list[Part], stems: dict[str, np.ndarray], impls: dict) -> None:
    """伴奏声部按角色定音量：整轨一个固定增益，由声部配置的参考片段决定，段落之间的强弱变化原样保留。"""
    tracks = {t.name: t for t in score.tracks}
    for p in parts:
        if p.texture in ROLE_LUFS and p.notes:
            lufs = _reference_level(score, tracks[p.name], impls[p.name])
            if lufs is not None:
                stems[p.name] = stems[p.name] * _db(ROLE_LUFS[p.texture] - lufs)


def automix(score: Score, parts: list[Part], stems: dict[str, np.ndarray]) -> tuple[list[dict], list[Problem]]:
    """旋律声部按每段的伴奏自动定音量：至少比最响的伴奏高 3 dB，且不低于伴奏总和 1.5 dB。
    伴奏稀疏时靠前一点，满编制时融进去一点。乐谱里的 gain 仍会叠加在结果上。
    返回 (每段的增益, 警告)。增益是相对原始渲染电平的，数值大不代表有问题。"""
    meter = pyln.Meter(SR)

    def lufs(x: np.ndarray) -> float:
        return meter.integrated_loudness(x) if np.abs(x).max() > 1e-5 else -np.inf

    log: list[dict] = []
    warnings: list[Problem] = []
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
            log.append({"track": mel.name, "section": s.name, "gain_db": round(float(g), 1)})
        if gains:
            mid = float(np.median(list(gains.values())))
            for s, g in gains.items():
                if abs(g - mid) > 18:
                    warnings.append(Problem("automix_outlier", f"tracks.{mel.name}.notes.{s}",
                                            f"这一段的自动增益和其他段相差 {g - mid:+.0f} dB，"
                                            f"请检查这一段的旋律是否只有零星几个音，或写错了段落"))
        if points:
            x, g = zip(*points)
            env = _db(np.interp(np.arange(len(stems[mel.name])), x, g)).astype(np.float32)
            stems[mel.name] = stems[mel.name] * env[:, None]
    return log, warnings


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


def _place(audio: np.ndarray, m: library.Mix, bpm: float) -> np.ndarray:
    """EQ → 延迟 → 立体声宽度 → 声像。"""
    fx = list(m.eq)
    if m.delay_beats:
        fx.append(Delay(delay_seconds=m.delay_beats * 60 / bpm, feedback=m.delay_feedback, mix=m.delay_mix))
    if fx:
        audio = Pedalboard(fx)(np.ascontiguousarray(audio.T), SR).T
    mid, side = (audio[:, 0] + audio[:, 1]) / 2, (audio[:, 0] - audio[:, 1]) / 2 * m.width
    audio = np.stack([mid + side, mid - side], axis=1)
    theta = (m.pan + 1) * np.pi / 4
    return (audio * np.array([np.cos(theta), np.sin(theta)]) * np.sqrt(2)).astype(np.float32)


def mix(stems: dict[str, np.ndarray], parts: list[Part], bpm: float) -> np.ndarray:
    placed = {p.name: _place(stems[p.name], library.mix_for(p.sound_name, p.texture), bpm) * _db(p.gain_db) for p in parts}
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


def _master(x: np.ndarray, warnings: list[Problem] | None = None) -> np.ndarray:
    pre = Pedalboard(_BUS + [Gain(MASTER_GAIN_DB)])(np.ascontiguousarray(x.T), SR).T
    over = 20 * np.log10(max(float(np.abs(pre).max()), 1e-9)) - PEAK_CEILING_DB
    if warnings is not None and over > LIMIT_WARN_DB:
        warnings.append(Problem("heavy_limiting", "", f"限幅器压掉了 {over:.1f} dB 峰值，可能有声部太响或叠得太厚"))
    return np.clip(limit(pre), -1.0, 1.0)


@dataclass
class Rendered:
    main: np.ndarray  # 可循环时是精确到采样的循环段；否则是乐谱时长（加 meta.tail）
    stems: dict[str, np.ndarray]
    sources: dict[str, str]
    mix: list[dict]  # 旋律在各段的自动增益
    warnings: list[Problem]
    sfx: list[dict]  # 每个音效实际的起止时间
    files: dict[str, Path]  # audio / preview / midi / stems


def render(score: Score, parts: list[Part], out: Path, soundset: str = "hq") -> Rendered:
    (out / "stems").mkdir(parents=True, exist_ok=True)
    stems, sources, impls = render_stems(score, parts, soundset)
    level_parts(score, parts, stems, impls)
    mix_log, warnings = automix(score, parts, stems)
    for name, audio in stems.items():
        sf.write(out / "stems" / f"{name}.wav", audio, SR, subtype="PCM_24")
    full = mix(stems, parts, score.bpm)
    length = round(score.seconds(score.total_beats) * SR)
    if (score.fx or {}).get("lofi"):
        full = lofi(full, score.fx["lofi"], length if score.loop else None)
    placed_sfx: list[dict] = []
    if score.sfx:
        sfx_audio, duck, placed_sfx = sfx.render(score, score.sfx, len(full))
        wet = Pedalboard([HighpassFilter(200), LowpassFilter(9000)])(np.ascontiguousarray(sfx_audio.T), SR).T
        music = full * duck[:, None]
        for row in placed_sfx:  # 音效比当时的音乐响多少（同样的 K 计权），用来核对"再轻一点"之类的修改
            a, b = round(row["start_s"] * SR), round(row["end_s"] * SR)
            if b - a < int(0.4 * SR):
                mid = (a + b) // 2
                a, b = max(0, mid - int(0.2 * SR)), mid + int(0.2 * SR)
            row["vs_music_db"] = round(row["level_db"] - sfx.average_level(music[a:b]), 1)
        full = music + sfx_audio + _convolve(wet, hall_ir()) * 0.25
        stems["sfx"] = sfx_audio
        sf.write(out / "stems" / "sfx.wav", sfx_audio, SR, subtype="PCM_24")
    files: dict[str, Path] = {}
    if score.loop:
        # 把结尾溢出的混响尾巴叠回开头，循环播放时接缝处就是连续的
        body = full[:length].copy()
        body[: len(full) - length] += full[length:]
        preview_raw = np.concatenate([body, full])  # 播放两遍再自然结束，第一遍到第二遍就是循环接缝
        main = _master(np.concatenate([body, body]), warnings)[length:]  # 预热一遍，让压缩器状态也首尾一致
        preview = _master(preview_raw)
        files["audio"] = out / "loop.wav"
    else:
        # 时长精确等于乐谱时长（加 meta.tail），结尾淡出避免硬切
        body = full[: length + round(score.tail * SR)].copy()
        fade = min(len(body), round(max(0.3, score.tail) * SR))
        body[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]
        main = preview = _master(body, warnings)
        files["audio"] = out / "song.wav"
    sf.write(files["audio"], main, SR, subtype="PCM_24")
    files["preview"] = out / "preview.mp3"
    with AudioFile(str(files["preview"]), "w", SR, 2, quality="V2") as f:
        f.write(np.ascontiguousarray(preview.T))
    files["midi"] = out / "song.mid"
    write_midi(files["midi"], score, parts)
    files["stems"] = out / "stems"
    return Rendered(main, stems, sources, mix_log, warnings, placed_sfx, files)

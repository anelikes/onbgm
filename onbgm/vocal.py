"""人声（可选）：把乐谱里一条旋律声部 + 和弦 + 歌词交给 YuE2，唱成一首带人声的完整歌曲。

onbgm render 不碰人声，照样是纯器乐。onbgm sing 走另一条路：
  乐谱的 vocal 段 → 导出 YuE2 原生的 ABC 谱（Vocal / Ins 两个单音声部 + 和弦）和分段歌词
  → 调用本机的 YuE2 引擎（外部模型，权重 CC BY-NC 4.0）→ 整首歌的音频。
YuE2 会照着谱上的和弦、小节和旋律唱，伴奏由它自己编；它不保证一个音一个音地唱准，
也不能只改一处：改了谱或歌词就整首重新生成。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .score import METERS, Problem, Score

# ---------- 歌词标签 ----------

TAGS = ["Intro", "Verse", "Pre-Chorus", "Chorus", "Bridge", "Interlude", "Outro"]
_TAG_GUESS = {
    "intro": "Intro", "verse": "Verse", "prechorus": "Pre-Chorus", "pre": "Pre-Chorus", "build": "Pre-Chorus",
    "chorus": "Chorus", "hook": "Chorus", "refrain": "Chorus", "bridge": "Bridge",
    "interlude": "Interlude", "break": "Interlude", "solo": "Interlude", "instrumental": "Interlude",
    "outro": "Outro", "ending": "Outro", "end": "Outro", "coda": "Outro",
}


def guess_tag(section: str) -> str | None:
    """verse2 → Verse，pre-chorus → Pre-Chorus，break → Interlude；猜不出返回 None。"""
    key = re.sub(r"[\s_\-]*\d+$", "", section.lower())
    return _TAG_GUESS.get(re.sub(r"[\s_\-]", "", key))


# ---------- 调号和音名拼写 ----------

LETTER_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
LETTERS = "CDEFGAB"
SHARP_ORDER, FLAT_ORDER = "FCGDAEB", "BEADGCF"
MAJOR_KEYS = {"C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "F#": 6, "C#": 7,
              "F": -1, "Bb": -2, "Eb": -3, "Ab": -4, "Db": -5, "Gb": -6, "Cb": -7}
MINOR_KEYS = {"A": 0, "E": 1, "B": 2, "F#": 3, "C#": 4, "G#": 5, "D#": 6, "A#": 7,
              "D": -1, "G": -2, "C": -3, "F": -4, "Bb": -5, "Eb": -6, "Ab": -7}
# 教会调式 → 比同主音大调多几个升号（负数是降号）
MODE_SHIFT = {"major": 0, "ionian": 0, "minor": -3, "aeolian": -3, "dorian": -2, "phrygian": -4,
              "lydian": 1, "mixolydian": -1, "locrian": -5}


@dataclass
class Key:
    abc: str  # K: 字段，如 "Ab"、"F#m"
    sig: dict[str, int]  # 调号里升降的字母
    flats: bool  # 调外音往降号方向拼


def parse_key(text: str) -> Key | None:
    """"Ab major"、"F# minor"、"D dorian"、"Am" → Key；不认识返回 None。
    教会调式写成同调号的大调（YuE2 的 ABC 只认大小调）。"""
    m = re.fullmatch(r"\s*([A-Ga-g])([#b]?)\s*(m(?![a-z])|[A-Za-z]*)\s*", text or "")
    if not m:
        return None
    tonic = m.group(1).upper() + m.group(2)
    mode = {"": "major", "m": "minor", "maj": "major", "min": "minor"}.get(m.group(3).lower(), m.group(3).lower())
    if mode not in MODE_SHIFT:
        return None
    if mode in ("minor", "aeolian"):
        if tonic not in MINOR_KEYS:
            return None
        n, abc = MINOR_KEYS[tonic], tonic + "m"
    else:
        base = MAJOR_KEYS.get(tonic)
        if base is None:
            return None
        n = base + MODE_SHIFT[mode]
        if abs(n) > 7:
            return None
        abc = tonic if mode in ("major", "ionian") else next(k for k, v in MAJOR_KEYS.items() if v == n)
    letters = SHARP_ORDER[:n] if n > 0 else FLAT_ORDER[:-n]
    return Key(abc, {ch: (1 if n > 0 else -1) for ch in letters}, n < 0)


def _spell(midi: int, key: Key) -> tuple[str, int, int]:
    """(字母, 升降, 八度)。调内音按调号拼，调外音按调的升降方向拼。"""
    pc = midi % 12
    for ch in LETTERS:
        if (LETTER_PC[ch] + key.sig.get(ch, 0)) % 12 == pc:
            alter = key.sig.get(ch, 0)
            break
    else:
        # 调外音：能用本位就用本位（F 大调里的 B 是还原 B，不是降 C），否则按调的方向用降号或升号
        ch, alter = next((ch, a) for a in (0, -1 if key.flats else 1) for ch in LETTERS
                         if (LETTER_PC[ch] + a) % 12 == pc)
    return ch, alter, (midi - alter) // 12 - 1


def _pitch_token(midi: int, key: Key, bar_state: dict[str, int]) -> str:
    """ABC 音名。YuE2 的约定：临时记号按字母在整小节内生效（不分八度），到下一小节失效。"""
    ch, alter, octave = _spell(midi, key)
    acc = ""
    if alter != bar_state.get(ch, key.sig.get(ch, 0)):
        acc = {-2: "__", -1: "_", 0: "=", 1: "^", 2: "^^"}[alter]
        bar_state[ch] = alter
    name = ch.lower() + "'" * (octave - 5) if octave >= 5 else ch + "," * (4 - octave)
    return acc + name


UNITS = [48, 32, 24, 16, 12, 8, 6, 4, 3, 2, 1]  # YuE2 支持的时值倍数


def _split(n: int) -> list[int]:
    out = []
    while n > 0:
        u = next(u for u in UNITS if u <= n)
        out.append(u)
        n -= u
    return out


def _voice_bars(notes, bars: int, bar_units: int, chords: dict[int, str], key: Key) -> list[str]:
    """notes: [(起点, 时长, midi)]，单位是 L 的格子、从这一段开头算。返回每小节一段 ABC。"""
    total = bars * bar_units
    grid: list[tuple[int, bool] | None] = [None] * total  # 每格：(midi, 是不是起音)
    for st, du, m in notes:
        for i in range(max(st, 0), min(st + du, total)):
            grid[i] = (m, i == st)
    out = []
    for b in range(bars):
        state: dict[str, int] = {}
        s, i, end = "", b * bar_units, (b + 1) * bar_units
        while i < end:
            j = i + 1
            if grid[i] is None:
                while j < end and j not in chords and grid[j] is None:
                    j += 1
            else:
                while j < end and j not in chords and grid[j] == (grid[i][0], False):
                    j += 1
            s += f'"{chords[i]}"' if i in chords else ""
            if grid[i] is None:
                s += "".join(f"z{u}" if u > 1 else "z" for u in _split(j - i))
            else:
                tok = _pitch_token(grid[i][0], key, state)
                s += "-".join(f"{tok}{u}" if u > 1 else tok for u in _split(j - i))
                if j < total and grid[j] == (grid[i][0], False):
                    s += "-"  # 延续到下一个和弦或下一小节
            i = j
        out.append(s)
    return out


# ---------- 歌词 ----------

_SMALL_KANA = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ")


def syllables(text: str) -> int:
    """估算音节数：汉字、韩文一字一个；假名一拍一个（小写的ゃゅょ不算）；英文按元音组估。"""
    n = 0
    for tok in re.findall(r"[A-Za-z']+|[぀-ヿ]|[一-鿿가-힯]", text):
        if tok[0].isascii():
            w = tok.lower().strip("'")
            k = len(re.findall(r"[aeiouy]+", w))
            if w.endswith("e") and not w.endswith(("le", "ee")) and k > 1:
                k -= 1
            n += max(k, 1)
        elif tok not in _SMALL_KANA:
            n += 1
    return n


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in str(text).splitlines() if ln.strip()]


# ---------- 导出 ----------

VOCAL_LOW, VOCAL_HIGH = 40, 81  # E2 ~ A5：超出这个范围，男声女声都很难唱


@dataclass
class VocalPlan:
    abc: str
    lyrics: str
    style: str
    seed: int
    sections: list[dict]
    warnings: list[Problem] = field(default_factory=list)


def plan(score: Score) -> tuple[VocalPlan | None, list[Problem]]:
    """乐谱 → YuE2 的输入。返回 (结果, 问题)；有问题时结果为 None。"""
    v = score.vocal
    if not v:
        return None, [Problem("no_vocal", "vocal", "乐谱里没有 vocal 段。写上 track（唱哪条旋律）、style、lyrics，"
                                                   "格式见 onbgm schema 或 skill 文档", None)]
    problems: list[Problem] = []
    warnings: list[Problem] = []
    names = [s.name for s in score.sections]
    track = next((t for t in score.tracks if t.name == v["track"]), None)
    if track is None:
        problems.append(Problem("unknown_track", "vocal.track",
                                f"没有声部 {v['track']!r}（现有: {', '.join(t.name for t in score.tracks) or '无'}）"))
    elif not track.melody:
        problems.append(Problem("not_a_melody", "vocal.track", f"{track.name} 没有用 notes 逐音写旋律，人声没法照着唱"))
    lyrics = v["lyrics"]
    for sec in lyrics:
        if sec not in names:
            problems.append(Problem("unknown_section", f"vocal.lyrics.{sec}", f"没有这个段落（现有: {', '.join(names)}）"))
    tags = {}
    for sec in v.get("tags") or {}:
        if sec not in names:
            problems.append(Problem("unknown_section", f"vocal.tags.{sec}", f"没有这个段落（现有: {', '.join(names)}）"))
    for s in score.sections:
        tag = (v.get("tags") or {}).get(s.name) or guess_tag(s.name)
        if tag is None:
            problems.append(Problem("unknown_tag", f"vocal.tags.{s.name}",
                                    f"看不出段落 {s.name} 在歌里是什么角色；在 vocal.tags 里写明（可用: {', '.join(TAGS)}）"))
        tags[s.name] = tag
    key = parse_key(score.key)
    if key is None:
        problems.append(Problem("bad_key", "meta.key", f"导出 ABC 需要调性，当前 {score.key!r} 认不出"
                                                       "（写成 \"Ab major\"、\"F# minor\"、\"D dorian\" 这样）"))
    transpose = int(v.get("transpose", 0))
    melody = track.melody if track else []
    if problems:
        return None, problems

    bpb = score.beats_per_bar
    res = 4 if all(abs(x * 4 - round(x * 4)) < 1e-6 for n in melody for x in (n.start, n.dur)) else 8
    bar_units = round(bpb * res)
    by_sec: dict[str, list] = {s.name: [] for s in score.sections}
    for n in melody:
        by_sec[score.section_of(n.start).name].append(n)
    for sec in lyrics:
        if not by_sec[sec]:
            problems.append(Problem("no_melody_here", f"vocal.lyrics.{sec}",
                                    f"{track.name} 在 {sec} 段没写旋律，人声没有可唱的音。先在 tracks.{track.name}.notes.{sec} "
                                    f"写旋律，或者去掉这段歌词"))
    if problems:
        return None, problems

    bpm = round(score.bpm)
    if any(s.tempo is not None for s in score.sections):
        warnings.append(Problem("tempo_ignored", "sections", f"ABC 只能写一个速度（{bpm} BPM），段落速度和渐慢渐快不会传给 YuE2"))
    meter = score.meter
    lines = ["X:1", "T:", f"M:{meter}", f"L:1/{res * 4}", f"Q:1/4={bpm}",
             'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"',
             'V: Ins clef=treble name="Ins Melody" snm="Inst."', f"K:{key.abc}"]
    lyric_text: list[str] = []
    report = []
    sung = []
    for s in score.sections:
        voice = "vocal" if s.name in lyrics else "ins" if by_sec[s.name] else None
        notes = [(round((n.start - s.start) * res), max(1, round(n.dur * res)), n.pitch + transpose) for n in by_sec[s.name]]
        chords = {round((c.start - s.start) * res): c.chord.symbol for c in score.chords if c.section == s.name}
        vb = _voice_bars(notes if voice == "vocal" else [], s.bars, bar_units, chords, key)
        ib = _voice_bars(notes if voice == "ins" else [], s.bars, bar_units, {}, key)
        lines.append(f"% {tags[s.name].lower()}")
        for g in range(0, s.bars, 4):
            k = min(4, s.bars - g)
            lines += ["V: Vocal", "|".join(vb[g:g + k]) + "|", "V: Ins"]
            ins = ib[g:g + k]
            lines.append((f"Z{k}|" if k > 1 else "Z|") if voice != "ins" or all(re.fullmatch(r"(z\d*)+", x) for x in ins)
                         else "|".join(ins) + "|")
        text = _lines(lyrics.get(s.name, ""))
        lyric_text += [f"[{tags[s.name]}]", *text, ""]
        row = {"section": s.name, "tag": tags[s.name], "bars": s.bars, "voice": voice, "notes": len(notes)}
        if voice == "vocal":
            syl = syllables(" ".join(text))
            row |= {"lines": len(text), "syllables": syl,
                    "range": [min(n[2] for n in notes), max(n[2] for n in notes)]}
            sung += [n[2] for n in notes]
            if syl > len(notes) * 1.25 + 2:
                warnings.append(Problem("too_many_syllables", f"vocal.lyrics.{s.name}",
                                        f"歌词约 {syl} 个音节，旋律只有 {len(notes)} 个音：模型会把几个字挤在一个音上，或者自己改旋律"))
            elif syl < len(notes) * 0.6:
                warnings.append(Problem("too_few_syllables", f"vocal.lyrics.{s.name}",
                                        f"歌词约 {syl} 个音节，旋律有 {len(notes)} 个音：多出来的音会被拖长或者唱成衬词"))
        report.append(row)
    if sung and (min(sung) < VOCAL_LOW or max(sung) > VOCAL_HIGH):
        from .theory import pitch_name

        lo, hi = min(sung), max(sung)
        shift = -12 if hi > VOCAL_HIGH and lo - 12 >= VOCAL_LOW else 12 if lo < VOCAL_LOW and hi + 12 <= VOCAL_HIGH else 0
        warnings.append(Problem("out_of_vocal_range", "vocal.transpose",
                                f"人声要唱 {pitch_name(lo)}~{pitch_name(hi)}，超出了常用人声音域（{pitch_name(VOCAL_LOW)}~"
                                f"{pitch_name(VOCAL_HIGH)}）" + (f"；transpose 改成 {transpose + shift} 正好落进去" if shift else ""),
                                {"transpose": transpose + shift} if shift else None))
    vp = VocalPlan("\n".join(lines) + "\n", "\n".join(lyric_text).rstrip() + "\n", v["style"].strip(),
                   int(v.get("seed", 831001)), report, warnings)
    return vp, []


# ---------- 演唱：调用 YuE2 ----------

ENGINE_URL = "https://huggingface.co/ahmadw/YuE2-3B-MLX"
ENGINE_FIX = (f"onbgm sing 需要本机的 YuE2 引擎（Apple Silicon，MLX 移植版 {ENGINE_URL}，约 5GB）：\n"
              "  hf download ahmadw/YuE2-3B-MLX --local-dir ~/.cache/onbgm/yue2-mlx\n"
              "  python3 -m venv ~/.cache/onbgm/yue2-mlx/.venv && "
              "~/.cache/onbgm/yue2-mlx/.venv/bin/pip install mlx numpy tiktoken regex\n"
              "放在别处就用 --engine 或环境变量 ONBGM_YUE2 指过去")


def default_engine() -> Path:
    return Path(os.environ.get("ONBGM_YUE2") or Path.home() / ".cache" / "onbgm" / "yue2-mlx").expanduser()


@dataclass
class Engine:
    python: Path
    script: Path
    model: Path


def find_engine(path: str | Path | None = None, model: str | None = None) -> Engine:
    """找 YuE2 引擎：目录里要有 generate.py 和一个量化版本（8bit 优先）。找不到抛 RuntimeError（带安装方法）。"""
    root = Path(path).expanduser() if path else default_engine()
    script = root / "generate.py"
    if not script.exists():
        raise RuntimeError(f"{root} 里没有 YuE2 引擎（generate.py）。\n{ENGINE_FIX}")
    if model:
        candidates = [root / model]
    else:
        candidates = [root / q for q in ("8bit", "4bit", "bf16")] + sorted(p for p in root.iterdir() if p.is_dir())
    found = next((c for c in candidates if (c / "config.json").exists() and (c / "model.safetensors").exists()), None)
    if found is None:
        raise RuntimeError(f"{root} 里没有模型权重（{model or '8bit / 4bit / bf16'} 目录）。\n{ENGINE_FIX}")
    python = root / ".venv" / "bin" / "python"
    return Engine(python if python.exists() else Path(sys.executable), script, found)


class _FileLock:
    """环境变量 ONBGM_LOCK_FILE 指向一个文件时，跑模型前先对它加独占锁：和机器上别的重任务（渲染视频等）排队，不并行。"""

    def __enter__(self):
        path = os.environ.get("ONBGM_LOCK_FILE")
        self.handle = None
        if path:
            import fcntl

            self.handle = open(path, "a")
            t = time.time()
            fcntl.flock(self.handle, fcntl.LOCK_EX)
            self.waited = time.time() - t
        return self

    def __exit__(self, *exc):
        if self.handle:
            import fcntl

            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()


def write_inputs(vp: VocalPlan, out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = {"abc": out / "song.abc", "lyrics": out / "lyrics.txt", "style": out / "style.txt"}
    files["abc"].write_text(vp.abc, encoding="utf-8")
    files["lyrics"].write_text(vp.lyrics, encoding="utf-8")
    files["style"].write_text(vp.style + "\n", encoding="utf-8")
    return files


def sing(vp: VocalPlan, out: Path, engine: Engine, log=lambda line: None) -> dict:
    """跑 YuE2，输出 song.wav 和 song.mp3。log 收到引擎的每一行输出。失败抛 RuntimeError。"""
    import numpy as np
    import soundfile as sf
    from pedalboard.io import AudioFile

    files = write_inputs(vp, out)
    wav = out / "song.wav"
    cmd = [str(engine.python), str(engine.script), "--model", str(engine.model), "--style", vp.style,
           "--lyrics-file", str(files["lyrics"]), "--abc-file", str(files["abc"]), "--cot", "full",
           "--seed", str(vp.seed), "--out", str(wav)]
    t0 = time.time()
    with _FileLock() as lock:
        waited = getattr(lock, "waited", 0.0)
        t1 = time.time()
        proc = subprocess.Popen(cmd, cwd=engine.script.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        tail: list[str] = []
        for line in proc.stdout:
            line = line.rstrip()
            tail = (tail + [line])[-20:]
            log(line)
        code = proc.wait()
        gen_s = time.time() - t1
    if code != 0 or not wav.exists():
        raise RuntimeError(f"YuE2 出错（退出码 {code}），最后几行输出:\n" + "\n".join(tail))
    files["abc"].write_text(vp.abc, encoding="utf-8")  # 引擎会把它用的 ABC 写回同名文件，这里保持导出的原样
    audio, sr = sf.read(wav, dtype="float32", always_2d=True)
    files["audio"] = wav
    files["preview"] = out / "song.mp3"
    with AudioFile(str(files["preview"]), "w", sr, audio.shape[1], quality="V2") as f:
        f.write(np.ascontiguousarray(audio.T))
    latents = wav.with_suffix(".latents.npy")
    if latents.exists():
        files["latents"] = latents
    import pyloudnorm

    lufs = pyloudnorm.Meter(sr).integrated_loudness(audio)
    return {"files": {k: str(p) for k, p in files.items()}, "duration_s": round(len(audio) / sr, 2),
            "lufs": round(float(lufs), 1), "generate_s": round(gen_s), "lock_wait_s": round(waited),
            "total_s": round(time.time() - t0), "engine": str(engine.model)}

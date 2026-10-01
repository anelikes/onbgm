"""乐谱 DSL：读取 YAML、校验，并解出时间轴。

错误信息写给 LLM 看：指出位置、原因和改法，方便它自己修正。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .theory import Chord, parse_chord, parse_pitch

BEATS_PER_BAR = 4
NOTE_TOLERANCE = 0.05  # 拍；人性化会把音符起点挪动几毫秒，判断段落归属时按谱面位置留出容差
FX = {"lofi": "lo-fi 质感：磁带抖动、低通、轻微失真、黑胶底噪（0~1）"}


class ScoreError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(f"- {p}" for p in problems))
        self.problems = problems


@dataclass
class Section:
    name: str
    bars: int
    energy: tuple[float, float]  # 段首、段尾的能量，0~1
    at: float | None  # 时间约束（秒）
    start_bar: int

    @property
    def start(self) -> float:
        return self.start_bar * BEATS_PER_BAR

    @property
    def end(self) -> float:
        return (self.start_bar + self.bars) * BEATS_PER_BAR


@dataclass
class ChordSpan:
    chord: Chord
    start: float  # 绝对拍
    dur: float
    section: str

    @property
    def end(self) -> float:
        return self.start + self.dur


@dataclass
class MelodyNote:
    start: float
    dur: float
    pitch: int


@dataclass
class Track:
    name: str
    sound: str
    texture: str
    sections: list[str]
    melody: list[MelodyNote]
    options: dict


@dataclass
class Score:
    title: str
    bpm: float
    key: str
    loop: bool
    seed: int
    sections: list[Section]
    chords: list[ChordSpan]
    tracks: list[Track]
    swing: float = 0.5  # 0.5 为平直；0.58 左右是 lo-fi 常见的十六分音符摇摆
    swing_unit: int = 16  # 16：十六分音符摇摆；8：八分音符摇摆（shuffle）
    fx: dict | None = None  # 整体效果，如 {"lofi": 0.7}
    tail: float = 0.0  # 不循环时，乐谱结束后再留几秒余音

    @property
    def total_beats(self) -> float:
        return self.sections[-1].end

    def seconds(self, beat: float) -> float:
        return beat * 60.0 / self.bpm

    def section(self, name: str) -> Section:
        return next(s for s in self.sections if s.name == name)

    def section_of(self, beat: float) -> Section:
        """音符属于哪个段落：按谱面位置算，被人性化提前了几毫秒的音符仍属于它写在的那一段。"""
        return self.section_at(beat + NOTE_TOLERANCE)

    def section_at(self, beat: float) -> Section:
        for s in self.sections:
            if beat < s.end:
                return s
        return self.sections[-1]

    def energy_at(self, beat: float) -> float:
        s = self.section_at(beat)
        a, b = s.energy
        frac = min(max((beat - s.start) / (s.end - s.start), 0.0), 1.0)
        return a + (b - a) * frac

    def chord_at(self, beat: float) -> ChordSpan:
        for c in self.chords:
            if c.start - 1e-6 <= beat < c.end - 1e-6:
                return c
        return self.chords[-1]

    def spans_in(self, names: list[str]) -> list[ChordSpan]:
        return [c for c in self.chords if c.section in names]


def parse_time(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", str(v).strip())
    if not m:
        raise ValueError(f"无法解析时间 {v!r}（格式如 \"0:32\" 或 \"1:04.5\"）")
    return int(m.group(1) or 0) * 60 + float(m.group(2))


def fmt_time(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:05.2f}"


def _parse_energy(v, where: str, problems: list[str]) -> tuple[float, float]:
    try:
        if isinstance(v, (int, float)):
            a = b = float(v)
        else:
            a, b = (float(x) for x in str(v).split("->"))
    except ValueError:
        problems.append(f"{where}: 无法解析能量 {v!r}（写 0.6 或 \"0.4->0.9\"）")
        return (0.5, 0.5)
    if not (0 <= a <= 1 and 0 <= b <= 1):
        problems.append(f"{where}: 能量须在 0~1 之间，当前 {v!r}")
    return (a, b)


def _solve_bpm(meta: dict, sections: list[Section], problems: list[str]) -> float:
    """根据时间约束确定或校验 BPM。"""
    constraints = [(f"sections.{s.name}.at", s.start, s.at) for s in sections if s.at is not None]
    if "length" in meta:
        constraints.append(("meta.length", sections[-1].end, parse_time(meta["length"])))
    bpm = meta.get("bpm", "auto" if constraints else None)
    if bpm is None:
        problems.append("缺少 meta.bpm：写一个数字（如 90），或写 auto 并用 meta.length 或段落的 at 约束时间")
        return 90.0
    if bpm == "auto":
        usable = [c for c in constraints if c[1] > 0 and c[2] > 0]
        if not usable:
            problems.append("meta.bpm 为 auto 时需要至少一个时间约束（meta.length 或某个段落的 at）")
            return 90.0
        _, beats, secs = usable[0]
        bpm = beats * 60 / secs
    bpm = float(bpm)
    for where, beats, secs in constraints:
        actual = beats * 60 / bpm
        if abs(actual - secs) > 0.01:
            msg = f"{where} 要求 {fmt_time(secs)}，但在 {bpm:g} BPM 下实际是 {fmt_time(actual)}。"
            if secs > 0:
                msg += f"可以把 bpm 改为 {beats * 60 / secs:.2f}（或 auto），或调整它之前的小节数，或删掉这个约束。"
            problems.append(msg)
    return bpm


_NOTE_RE = re.compile(r"(r|[A-Ga-g][#b]*-?\d)/(1|2|4|8|16)(\.?)(~?)")
_VALUE_BEATS = {"1": 4.0, "2": 2.0, "4": 1.0, "8": 0.5, "16": 0.25}


def _parse_melody(text: str, section: Section, where: str, problems: list[str]) -> list[MelodyNote]:
    """旋律记法：D5/4 = 四分音符，F#4/8. = 附点八分，r/2 = 二分休止，A4/2~ = 连到下一个同音。"""
    bars = [b.strip() for b in str(text).split("|") if b.strip()]
    if section.bars % len(bars):
        problems.append(f"{where}: 写了 {len(bars)} 小节，但段落 {section.name} 有 {section.bars} 小节")
        return []
    bars = bars * (section.bars // len(bars))
    notes: list[MelodyNote] = []
    tie = False
    for i, bar in enumerate(bars):
        pos = 0.0
        for tok in bar.split():
            m = _NOTE_RE.fullmatch(tok)
            if not m:
                problems.append(f"{where} 第 {i + 1} 小节: 无法解析 {tok!r}（格式如 D5/4、F#4/8.、r/2）")
                return []
            dur = _VALUE_BEATS[m.group(2)] * (1.5 if m.group(3) else 1.0)
            start = section.start + i * BEATS_PER_BAR + pos
            if m.group(1) != "r":
                pitch = parse_pitch(m.group(1))
                prev = notes[-1] if notes else None
                if tie and prev and prev.pitch == pitch and abs(prev.start + prev.dur - start) < 1e-6:
                    prev.dur += dur
                else:
                    notes.append(MelodyNote(start, dur, pitch))
            tie = bool(m.group(4))
            pos += dur
        if abs(pos - BEATS_PER_BAR) > 1e-6:
            problems.append(f"{where} 第 {i + 1} 小节时值合计 {pos:g} 拍，应为 {BEATS_PER_BAR} 拍: \"{bar}\"")
    return notes


def load(path: str | Path) -> Score:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    problems: list[str] = []
    meta = raw.get("meta") or {}

    sections: list[Section] = []
    bar = 0
    for i, s in enumerate(raw.get("sections") or []):
        name = s.get("name") or f"section{i + 1}"
        bars = s.get("bars")
        if not isinstance(bars, int) or bars <= 0:
            problems.append(f"sections.{name}.bars 须为正整数，当前 {bars!r}")
            bars = 1
        energy = _parse_energy(s.get("energy", 0.5), f"sections.{name}.energy", problems)
        at = None
        if "at" in s:
            try:
                at = parse_time(s["at"])
            except ValueError as e:
                problems.append(f"sections.{name}.at: {e}")
        sections.append(Section(name, bars, energy, at, bar))
        bar += bars
    if not sections:
        raise ScoreError(["至少需要一个 section"])
    names = [s.name for s in sections]
    for n in {n for n in names if names.count(n) > 1}:
        problems.append(f"段落名 {n} 重复了")

    bpm = _solve_bpm(meta, sections, problems)

    chords: list[ChordSpan] = []
    harmony = raw.get("harmony") or {}
    for key in harmony:
        if key not in names:
            problems.append(f"harmony.{key}: 没有这个段落（现有: {', '.join(names)}）")
    for s in sections:
        prog = harmony.get(s.name)
        if prog is None:
            problems.append(f"harmony 里缺少段落 {s.name} 的和弦进行")
            continue
        bar_texts = [b.strip() for b in str(prog).split("|") if b.strip()]
        if s.bars % len(bar_texts):
            problems.append(f"harmony.{s.name}: 写了 {len(bar_texts)} 小节，但段落有 {s.bars} 小节（须相等或能整除以便重复）")
            continue
        for i, bt in enumerate(bar_texts * (s.bars // len(bar_texts))):
            symbols = bt.split()
            step = BEATS_PER_BAR / len(symbols)
            for j, sym in enumerate(symbols):
                try:
                    chord = parse_chord(sym)
                except ValueError as e:
                    problems.append(f"harmony.{s.name} 第 {i + 1} 小节: {e}")
                    continue
                chords.append(ChordSpan(chord, s.start + i * BEATS_PER_BAR + j * step, step, s.name))

    tracks: list[Track] = []
    for name, cfg in (raw.get("tracks") or {}).items():
        cfg = cfg or {}
        melody: list[MelodyNote] = []
        notes = cfg.get("notes") or {}
        for sec_name, text in notes.items():
            if sec_name not in names:
                problems.append(f"tracks.{name}.notes.{sec_name}: 没有这个段落")
                continue
            melody += _parse_melody(text, sections[names.index(sec_name)], f"tracks.{name}.notes.{sec_name}", problems)
        in_ = cfg.get("in", list(notes) if notes else "all")
        track_sections = names if in_ == "all" else list(in_)
        for n in track_sections:
            if n not in names:
                problems.append(f"tracks.{name}.in: 没有段落 {n}（现有: {', '.join(names)}）")
        options = {k: v for k, v in cfg.items() if k not in ("sound", "texture", "in", "notes")}
        texture = cfg.get("texture", "melody" if notes else None)
        tracks.append(Track(name, cfg.get("sound"), texture, track_sections, melody, options))

    swing = meta.get("swing", 0.5)
    if not isinstance(swing, (int, float)) or not 0.5 <= swing <= 0.75:
        problems.append(f"meta.swing 须在 0.5（平直）到 0.75 之间，当前 {swing!r}")
        swing = 0.5
    swing_unit = meta.get("swing_unit", 16)
    if swing_unit not in (8, 16):
        problems.append(f"meta.swing_unit 须为 8 或 16，当前 {swing_unit!r}")
        swing_unit = 16
    tail = 0.0
    if "tail" in meta:
        try:
            tail = parse_time(meta["tail"])
        except ValueError as e:
            problems.append(f"meta.tail: {e}")
        if meta.get("loop"):
            problems.append("meta.tail 只用于不循环的曲子（loop: false）；循环时余音会自动叠回开头")
    fx = raw.get("fx") or {}
    for k, v in fx.items():
        if k not in FX:
            problems.append(f"fx.{k}: 不认识（可用: {', '.join(FX)}）")
        elif not isinstance(v, (int, float)) or not 0 <= v <= 1:
            problems.append(f"fx.{k} 须在 0~1 之间，当前 {v!r}")

    if problems:
        raise ScoreError(problems)
    return Score(
        title=raw.get("title", Path(path).stem),
        bpm=bpm,
        key=meta.get("key", ""),
        loop=bool(meta.get("loop", False)),
        seed=int(meta.get("seed", 0)),
        sections=sections,
        chords=chords,
        tracks=tracks,
        swing=float(swing),
        swing_unit=swing_unit,
        fx=fx,
        tail=tail,
    )

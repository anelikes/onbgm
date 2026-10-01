"""乐谱 DSL：读取 YAML、校验，并解出时间轴。

错误信息写给 LLM 看：指出位置、原因和改法，方便它自己修正。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import tempo
from .tempo import TempoMap
from .theory import Chord, parse_chord, parse_pitch

BEATS_PER_BAR = 4
NOTE_TOLERANCE = 0.05  # 拍；人性化会把音符起点挪动几毫秒，判断段落归属时按谱面位置留出容差
FX = {"lofi": "lo-fi 质感：磁带抖动、低通、轻微失真、黑胶底噪（0~1）"}


@dataclass
class Problem:
    """一条写给 agent 看的问题。code 便于程序处理；path 指向乐谱里出问题的位置；
    message 是说明和改法；fix 是可以直接套用的修正值（有的话）。"""
    code: str
    path: str
    message: str
    fix: dict | None = None

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message

    def to_dict(self) -> dict:
        d = {"code": self.code, "path": self.path, "message": self.message}
        if self.fix:
            d["fix"] = self.fix
        return d


class ScoreError(Exception):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        super().__init__("\n".join(f"- {p}" for p in problems))


@dataclass
class Section:
    name: str
    bars: int
    energy: tuple[float, float]  # 段首、段尾的能量，0~1
    at: float | None  # 时间约束（秒）
    start_bar: int
    tempo: tuple[float | None, float] | None = None  # 这一段自己的速度（起, 止）；None 用基准速度

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
    art: str = ""  # "staccato" 或空
    accent: bool = False


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
    sfx: list = field(default_factory=list)  # 音效时间线（onbgm.sfx.SfxEvent）
    tempo_map: TempoMap | None = None

    @property
    def total_beats(self) -> float:
        return self.sections[-1].end

    def seconds(self, beat: float) -> float:
        return self.tempo_map.seconds(beat) if self.tempo_map else beat * 60.0 / self.bpm

    def beat_at(self, seconds: float) -> float:
        return self.tempo_map.beat_at(seconds) if self.tempo_map else seconds * self.bpm / 60

    def bpm_at(self, beat: float) -> float:
        return self.tempo_map.bpm_at(beat) if self.tempo_map else self.bpm

    def duration(self, start: float, beats: float) -> float:
        """从 start 拍开始、长 beats 拍的一段有多少秒（变速时不能直接用 beats × 60 / bpm）。"""
        return self.seconds(start + beats) - self.seconds(start)

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


def _parse_energy(v, where: str, problems: list[Problem]) -> tuple[float, float]:
    try:
        if isinstance(v, (int, float)):
            a = b = float(v)
        else:
            a, b = (float(x) for x in str(v).split("->"))
    except ValueError:
        problems.append(Problem("bad_format", where, f"无法解析能量 {v!r}（写 0.6 或 \"0.4->0.9\"）"))
        return (0.5, 0.5)
    if not (0 <= a <= 1 and 0 <= b <= 1):
        problems.append(Problem("out_of_range", where, f"能量须在 0~1 之间，当前 {v!r}"))
    return (a, b)


def _solve_tempo(meta: dict, sections: list[Section], problems: list[Problem]) -> tuple[float, TempoMap]:
    """确定或校验基准速度，并建出速度图。设了 tempo 的段落用自己的速度，其余段落用基准速度。"""
    bounds = [(s.start, s.end) for s in sections]
    specs = [s.tempo for s in sections]
    constraints = [(f"sections.{s.name}.at", s.start, s.at) for s in sections if s.at is not None]
    if "length" in meta:
        constraints.append(("meta.length", sections[-1].end, parse_time(meta["length"])))
    bpm = meta.get("bpm", "auto" if constraints else None)
    if bpm is None:
        problems.append(Problem("missing_bpm", "meta.bpm",
                                "缺少速度：写一个数字（如 90），或写 auto 并用 meta.length 或段落的 at 约束时间"))
        bpm = 90.0
    auto = bpm == "auto"
    if auto:
        # 依次尝试每个时间约束，用第一个基准速度影响得到的那个来反推
        solved = next((b for _, beats, secs in constraints if beats > 0 and secs > 0
                       for b in [tempo.solve_base(bounds, specs, beats, secs)] if b is not None), None)
        if solved is None:
            problems.append(Problem("no_time_constraint", "meta.bpm",
                                    "为 auto 时需要至少一个基准速度影响得到的时间约束（meta.length 或某个段落的 at；"
                                    "如果它之前的段落都设了自己的 tempo，基准速度就影响不到它）"))
            return 90.0, tempo.build(bounds, specs, 90.0)
        bpm = solved
    base = float(bpm)
    tmap = tempo.build(bounds, specs, base)
    for where, beats, secs in constraints:
        actual = tmap.seconds(beats)
        if abs(actual - secs) <= 0.01:
            continue
        msg = f"要求 {fmt_time(secs)}，但在 {base:g} BPM 下实际是 {fmt_time(actual)}。"
        if auto:
            msg += "几个时间约束在同一个主速度下互相矛盾（主速度已按第一个约束算出）。"
        msg += "可以："
        options, fix = [], {}
        if where == "meta.length" and 0 < secs - actual <= 4:
            # 视频结尾通常是停留画面：差几秒用余音补齐最自然，也不会打乱前面的时间点
            fix["tail"] = round(secs - actual, 3)
            options.append(f"如果结尾是停留画面，删掉 meta.length、写 meta.tail: {secs - actual:.2f}，用余音补齐（推荐，不影响其他时间点）")
        need_base = tempo.solve_base(bounds, specs, beats, secs) if secs > 0 and not auto else None
        if need_base is not None:
            fix["bpm"] = round(need_base, 2)
            options.append(f"把 bpm 改为 {need_base:.2f}（会改变其他时间点）")
        # 保持基准速度：前面需要多少小节（设了 tempo 的段落时长不变）
        before = [(s, seg) for s, seg in zip(sections, tmap.segments) if s.end <= beats]
        fixed_s = sum(seg.seconds_into(seg.end) for s, seg in before if s.tempo is not None)
        fixed_beats = sum(s.end - s.start for s, _ in before if s.tempo is not None)
        if secs - fixed_s > 0:
            span = "总共" if where == "meta.length" else "它之前一共"
            need = ((secs - fixed_s) * base / 60 + fixed_beats) / BEATS_PER_BAR
            now = beats / BEATS_PER_BAR
            if abs(need - round(need)) < 0.02 and round(need) > 0:
                fix["bars" if where == "meta.length" else "bars_before"] = round(need)
                options.append(f"保持 {base:g} BPM，让{span} {round(need)} 小节（现在是 {now:g}）")
            else:
                options.append(f"保持 {base:g} BPM 的话，{span}需要 {need:.2f} 小节，不是整数")
        options.append("给它之前的某一段单独设 tempo，略快或略慢一点（onbgm fit 可以算出每段该用多少）")
        options.append("删掉这个约束")
        problems.append(Problem("time_constraint_mismatch", where,
                                msg + "；".join(f"({i + 1}) {o}" for i, o in enumerate(options)), fix or None))
    return base, tmap


_NOTE_RE = re.compile(r"(r|[A-Ga-g][#b]*-?\d)/(1|2|4|8|16)(\.?)([>']*)(~?)")
_VALUE_BEATS = {"1": 4.0, "2": 2.0, "4": 1.0, "8": 0.5, "16": 0.25}


def _parse_melody(text: str, section: Section, where: str, problems: list[Problem]) -> list[MelodyNote]:
    """旋律记法：D5/4 = 四分音符，F#4/8. = 附点八分，r/2 = 二分休止，A4/2~ = 连到下一个同音，
    D5/16' = 断奏，D5/8> = 重音（可以合写成 D5/8'>）。"""
    bars = [b.strip() for b in str(text).split("|") if b.strip()]
    if section.bars % len(bars):
        problems.append(Problem("melody_bar_count", where,
                                f"写了 {len(bars)} 小节，但段落 {section.name} 有 {section.bars} 小节（须相等或能整除）"))
        return []
    bars = bars * (section.bars // len(bars))
    notes: list[MelodyNote] = []
    tie = False
    for i, bar in enumerate(bars):
        pos = 0.0
        for tok in bar.split():
            m = _NOTE_RE.fullmatch(tok)
            if not m:
                problems.append(Problem("bad_note", where,
                                        f"第 {i + 1} 小节: 无法解析 {tok!r}（格式如 D5/4、F#4/8.、r/2、D5/16'、D5/8>）"))
                return []
            dur = _VALUE_BEATS[m.group(2)] * (1.5 if m.group(3) else 1.0)
            start = section.start + i * BEATS_PER_BAR + pos
            if m.group(1) != "r":
                pitch = parse_pitch(m.group(1))
                prev = notes[-1] if notes else None
                if tie and prev and prev.pitch == pitch and abs(prev.start + prev.dur - start) < 1e-6:
                    prev.dur += dur
                else:
                    notes.append(MelodyNote(start, dur, pitch, "staccato" if "'" in m.group(4) else "",
                                            ">" in m.group(4)))
            tie = bool(m.group(5))
            pos += dur
        if abs(pos - BEATS_PER_BAR) > 1e-6:
            problems.append(Problem("bar_length", where,
                                    f"第 {i + 1} 小节时值合计 {pos:g} 拍，应为 {BEATS_PER_BAR} 拍: \"{bar}\""))
    return notes


def _parse_sfx(items: list, sections: list[Section], tmap: TempoMap, tail: float, problems: list[Problem]) -> list:
    """音效时间线：at 可以是段落名（该段开头）或时间；按音效类型检查起止是否越界。"""
    from .sfx import SFX_TYPES, SfxEvent

    names = {s.name: s for s in sections}
    total = sections[-1].end
    events = []
    for i, item in enumerate(items):
        where = f"sfx.{i}"
        kind, at_raw = item["type"], item["at"]
        spec = SFX_TYPES[kind]
        if isinstance(at_raw, str) and at_raw in names:
            at = names[at_raw].start
        else:
            try:
                at = tmap.beat_at(parse_time(at_raw))
            except ValueError:
                problems.append(Problem("unknown_section", f"{where}.at",
                                        f"{at_raw!r} 既不是段落名也不是时间（段落: {', '.join(names)}；时间写 \"0:12\" 或 12）"))
                continue
        if "length" in item and spec.length is None:
            problems.append(Problem("field_not_for_sfx", f"{where}.length", f"{kind} 的长度由音效本身决定，不用写 length"))
            continue
        ev = SfxEvent(kind, at, item.get("length", spec.length) or 0.0, item.get("level", 0.8), item.get("duck", 0.0), where)
        start, end = ev.span(spec.natural * tmap.bpm_at(at) / 60)
        if start < -1e-6:
            problems.append(Problem("sfx_out_of_range", f"{where}.at",
                                    f"{kind} 长 {ev.length:g} 拍、{'在 at 结束' if spec.anchor == 'end' else '最响处在 at'}，"
                                    f"起点会早于乐谱开头 {fmt_time(-tmap.seconds(start))}；把 length 缩短到 "
                                    f"{(ev.at if spec.anchor == 'end' else 2 * ev.at):g} 拍以内，或把 at 往后挪",
                                    {"length": round(ev.at if spec.anchor == "end" else 2 * ev.at, 3)}))
        elif tmap.seconds(at) > tmap.seconds(total) + tail + 1e-6:
            problems.append(Problem("sfx_out_of_range", f"{where}.at",
                                    f"at 在 {fmt_time(tmap.seconds(at))}，超出了乐谱（到 {fmt_time(tmap.seconds(total))}）"))
        else:
            events.append(ev)
    return events


def load(path: str | Path) -> Score:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ScoreError([Problem("yaml_syntax", "", f"YAML 语法错误: {e}")]) from None
    return from_dict(raw, Path(path).stem)


def from_dict(raw: dict, default_title: str = "untitled") -> Score:
    """从已经解析好的数据构造乐谱（load 读文件后调用它；引擎内部也用它生成参考片段）。"""
    from .schema import validate

    if problems := validate(raw):
        raise ScoreError(problems)
    problems: list[Problem] = []
    meta = raw.get("meta") or {}

    sections: list[Section] = []
    bar = 0
    for i, s in enumerate(raw.get("sections") or []):
        name = s.get("name") or f"section{i + 1}"
        bars = s.get("bars")
        if not isinstance(bars, int) or bars <= 0:
            problems.append(Problem("out_of_range", f"sections.{name}.bars", f"须为正整数，当前 {bars!r}"))
            bars = 1
        energy = _parse_energy(s.get("energy", 0.5), f"sections.{name}.energy", problems)
        section_tempo = None
        if "tempo" in s:
            try:
                section_tempo = tempo.parse_tempo(s["tempo"])
            except ValueError as e:
                problems.append(Problem("bad_format", f"sections.{name}.tempo", str(e)))
        at = None
        if "at" in s:
            try:
                at = parse_time(s["at"])
            except ValueError as e:
                problems.append(Problem("bad_format", f"sections.{name}.at", str(e)))
        sections.append(Section(name, bars, energy, at, bar, section_tempo))
        bar += bars
    if not sections:
        raise ScoreError([Problem("no_sections", "sections", "至少需要一个段落")])
    names = [s.name for s in sections]
    for n in {n for n in names if names.count(n) > 1}:
        problems.append(Problem("duplicate_section", "sections", f"段落名 {n} 重复了"))

    bpm, tmap = _solve_tempo(meta, sections, problems)

    chords: list[ChordSpan] = []
    harmony = raw.get("harmony") or {}
    for key in harmony:
        if key not in names:
            problems.append(Problem("unknown_section", f"harmony.{key}", f"没有这个段落（现有: {', '.join(names)}）"))
    for s in sections:
        prog = harmony.get(s.name)
        if prog is None:
            problems.append(Problem("missing_harmony", f"harmony.{s.name}", f"缺少段落 {s.name} 的和弦进行"))
            continue
        bar_texts = [b.strip() for b in str(prog).split("|") if b.strip()]
        if s.bars % len(bar_texts):
            problems.append(Problem("harmony_bar_count", f"harmony.{s.name}",
                                    f"写了 {len(bar_texts)} 小节，但段落有 {s.bars} 小节（须相等或能整除以便重复）"))
            continue
        for i, bt in enumerate(bar_texts * (s.bars // len(bar_texts))):
            symbols = bt.split()
            step = BEATS_PER_BAR / len(symbols)
            for j, sym in enumerate(symbols):
                try:
                    chord = parse_chord(sym)
                except ValueError as e:
                    problems.append(Problem("unknown_chord", f"harmony.{s.name}", f"第 {i + 1} 小节: {e}"))
                    continue
                chords.append(ChordSpan(chord, s.start + i * BEATS_PER_BAR + j * step, step, s.name))

    tracks: list[Track] = []
    for name, cfg in (raw.get("tracks") or {}).items():
        cfg = cfg or {}
        melody: list[MelodyNote] = []
        notes = cfg.get("notes") or {}
        for sec_name, text in notes.items():
            if sec_name not in names:
                problems.append(Problem("unknown_section", f"tracks.{name}.notes.{sec_name}",
                                        f"没有这个段落（现有: {', '.join(names)}）"))
                continue
            melody += _parse_melody(text, sections[names.index(sec_name)], f"tracks.{name}.notes.{sec_name}", problems)
        in_ = cfg.get("in", list(notes) if notes else "all")
        track_sections = names if in_ == "all" else list(in_)
        for n in track_sections:
            if n not in names:
                problems.append(Problem("unknown_section", f"tracks.{name}.in", f"没有段落 {n}（现有: {', '.join(names)}）"))
        options = {k: v for k, v in cfg.items() if k not in ("sound", "texture", "in", "notes")}
        texture = cfg.get("texture", "melody" if notes else None)
        tracks.append(Track(name, cfg.get("sound"), texture, track_sections, melody, options))

    swing = meta.get("swing", 0.5)
    if not isinstance(swing, (int, float)) or not 0.5 <= swing <= 0.75:
        problems.append(Problem("out_of_range", "meta.swing", f"须在 0.5（平直）到 0.75 之间，当前 {swing!r}"))
        swing = 0.5
    swing_unit = meta.get("swing_unit", 16)
    if swing_unit not in (8, 16):
        problems.append(Problem("unknown_value", "meta.swing_unit", f"须为 8 或 16，当前 {swing_unit!r}"))
        swing_unit = 16
    tail = 0.0
    if "tail" in meta:
        try:
            tail = parse_time(meta["tail"])
        except ValueError as e:
            problems.append(Problem("bad_format", "meta.tail", str(e)))
        if meta.get("loop"):
            problems.append(Problem("tail_with_loop", "meta.tail", "只用于不循环的曲子（loop: false）；循环时余音会自动叠回开头"))
    sfx_events = _parse_sfx(raw.get("sfx") or [], sections, tmap, tail, problems)
    fx = raw.get("fx") or {}
    for k, v in fx.items():
        if k not in FX:
            problems.append(Problem("unknown_field", f"fx.{k}", f"不认识（可用: {', '.join(FX)}）"))
        elif not isinstance(v, (int, float)) or not 0 <= v <= 1:
            problems.append(Problem("out_of_range", f"fx.{k}", f"须在 0~1 之间，当前 {v!r}"))

    if problems:
        raise ScoreError(problems)
    return Score(
        title=raw.get("title", default_title),
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
        sfx=sfx_events,
        tempo_map=tmap,
    )

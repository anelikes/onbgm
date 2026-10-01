"""把乐谱展开成具体音符。

LLM 在乐谱里只描述意图（和弦、织体、能量、旋律），这里负责乐器惯用写法和演奏细节：
力度起伏、微小的时间偏差、踏板、弦乐表情（CC11）等。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import math

from .score import BEATS_PER_BAR, ChordSpan, Score, ScoreError, Section, Track
from .theory import Chord, guitar_voicing, pitch_name, place, voice_lead


@dataclass(frozen=True)
class Sound:
    program: int  # General MIDI 音色号（0 起）
    lo: int  # 乐器实际音域，用于越界检查
    hi: int
    reverb: float  # 混响发送量
    sustained: bool = False  # 持续音乐器，用 CC11 控制表情
    drum: bool = False


SOUNDS: dict[str, Sound] = {
    "piano": Sound(0, 21, 108, 0.22),
    "epiano": Sound(4, 28, 103, 0.2),
    "harp": Sound(46, 24, 103, 0.3),
    "strings": Sound(48, 28, 96, 0.35, sustained=True),
    "strings-slow": Sound(49, 28, 96, 0.4, sustained=True),
    "tremolo": Sound(44, 28, 96, 0.35, sustained=True),
    "pizzicato": Sound(45, 28, 96, 0.25),
    "cello": Sound(42, 36, 76, 0.3, sustained=True),
    "contrabass": Sound(43, 28, 67, 0.15, sustained=True),
    "acoustic-bass": Sound(32, 28, 67, 0.1),
    "timpani": Sound(47, 36, 57, 0.3),
    "horn": Sound(60, 34, 77, 0.35, sustained=True),
    "brass": Sound(61, 34, 84, 0.3, sustained=True),
    "flute": Sound(73, 60, 96, 0.3, sustained=True),
    "oboe": Sound(68, 58, 91, 0.3, sustained=True),
    "choir": Sound(52, 40, 84, 0.4, sustained=True),
    "vibraphone": Sound(11, 53, 89, 0.3),
    "marimba": Sound(12, 45, 96, 0.25),
    "glockenspiel": Sound(9, 72, 108, 0.3),
    "guitar-nylon": Sound(24, 40, 88, 0.2),
    "guitar-steel": Sound(25, 40, 88, 0.2),
    "electric-bass": Sound(33, 28, 67, 0.08),
    "whistle": Sound(78, 60, 96, 0.3, sustained=True),
    # 鼓组：program 是 GM/GS 鼓组号
    "drums": Sound(0, 27, 87, 0.25, drum=True),
    "drums-room": Sound(8, 27, 87, 0.2, drum=True),
    "drums-electronic": Sound(24, 27, 87, 0.15, drum=True),
    "drums-808": Sound(25, 27, 87, 0.15, drum=True),
    "drums-jazz": Sound(32, 27, 87, 0.2, drum=True),
    "drums-brush": Sound(40, 27, 87, 0.2, drum=True),
}

DRUM_KEYS = {
    "kick": 36, "rim": 37, "snare": 38, "clap": 39, "hat": 42, "pedal-hat": 44, "open-hat": 46,
    "tom-low": 45, "tom-mid": 47, "tom-high": 50, "crash": 49, "ride": 51, "ride-bell": 53,
    "tambourine": 54, "cowbell": 56, "conga-high": 62, "conga-low": 64, "cabasa": 69, "shaker": 70,
}
STEP_COUNTS = (8, 12, 16, 24, 32)


@dataclass
class Note:
    start: float  # 拍
    dur: float
    pitch: int
    vel: int
    art: str = ""  # 演奏法，如 "roll"（滚奏）；渲染器没有对应采样时自行展开


@dataclass
class Part:
    name: str
    sound_name: str
    sound: Sound
    gain_db: float
    notes: list[Note] = field(default_factory=list)
    ccs: list[tuple[float, int, int]] = field(default_factory=list)  # (拍, 控制器, 值)
    texture: str = ""


class Performer:
    """演奏者：给机械的音符加上人的误差和轻重。

    随机量由（声部, 段落, 段内位置, 音高）决定，而不是按顺序抽取：
    改动某一段或某个声部，不会让其他地方的演奏细节跟着变。
    """

    def __init__(self, score: Score, track: str):
        self.score, self.prefix = score, f"{score.seed}:{track}"

    def _rng(self, beat: float, pitch: int, kind: str) -> random.Random:
        s = self.score.section_at(beat)
        return random.Random(f"{self.prefix}:{s.name}:{beat - s.start:.3f}:{pitch}:{kind}")

    def time(self, beat: float, pitch: int, spread: float) -> float:
        return max(0.0, beat + self._rng(beat, pitch, "t").gauss(0, spread))

    def vel(self, base: float, beat: float, pitch: int, spread: float = 4.0, accent: bool = True) -> int:
        pos = round(beat % BEATS_PER_BAR, 3)
        bonus = {0: 8, 2: 4}.get(pos, 0 if pos == int(pos) else -5) if accent else 0
        return int(min(127, max(1, round(base + bonus + self._rng(beat, pitch, "v").gauss(0, spread)))))


def regions(spans: list[ChordSpan]) -> list[tuple[float, float, list[ChordSpan]]]:
    """把和弦按时间上是否连续分组。"""
    out: list[tuple[float, float, list[ChordSpan]]] = []
    for s in spans:
        if out and abs(out[-1][1] - s.start) < 1e-6:
            out[-1] = (out[-1][0], s.end, out[-1][2] + [s])
        else:
            out.append((s.start, s.end, [s]))
    return out


def expression(score: Score, part: Part, start: float, end: float, floor: int = 35) -> None:
    """CC11 表情跟随能量曲线，进入时 2 拍淡入。"""
    b = start
    while b < end:
        v = floor + (127 - floor) * score.energy_at(b)
        v *= min(1.0, 0.35 + 0.65 * (b - start) / 2)
        part.ccs.append((b, 11, int(v)))
        b += 0.25


# ---------- 织体 ----------

def t_arpeggio(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """流行/抒情钢琴分解和弦（1-5-8-9-10-9-8-5）。能量低用四分音符，高了用八分音符。"""
    lo = track.options.get("low", 36)
    spans = score.spans_in(track.sections)
    for span in spans:
        c = span.chord
        e = score.energy_at(span.start)
        bass = place(c.bass, lo)
        root = place(c.root, bass)
        third, fifth = root + 12 + c.third, root + c.fifth
        if c.is_dominant:
            pattern = [bass, fifth, root + 10, root + 12, third, root + 12, root + 10, fifth]
        else:
            color = root + 14 if (14 in c.intervals or c.third in (3, 4)) else fifth + 12
            pattern = [bass, fifth, root + 12, color, third, color, root + 12, fifth]
        step = 1.0 if e < 0.35 else 0.5
        if step == 1.0:
            pattern = [bass, fifth, root + 12, third]
        base = 44 + 42 * e
        for i in range(int(round(span.dur / step))):
            b, p = span.start + i * step, pattern[i % len(pattern)]
            part.notes.append(Note(perf.time(b, p, 0.008), step * 1.6, p, perf.vel(base + (6 if i == 0 else 0), b, p)))
        # 换和弦时抬踏板再踩下，避免和声糊在一起
        part.ccs.append((max(0.0, span.start - 0.03), 64, 0))
        part.ccs.append((span.start + 0.08, 64, 127))
    for _, end, _ in regions(spans):
        part.ccs.append((end, 64, 0))


def t_pad(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """持续和弦铺底，四声部就近进行，共同音保持不断。

    每个段落从固定的起始排法开始，段内才就近进行：这样改动某一段不会让其他段的排法漂移。
    """
    lo, hi = track.options.get("range", [48, 72])
    for start, end, spans in regions(score.spans_in(track.sections)):
        prev: tuple[int, ...] | None = None
        held: dict[int, Note] = {}
        for i, span in enumerate(spans):
            if i and span.section != spans[i - 1].section:
                prev = None
            voicing = voice_lead(span.chord, prev, lo, hi)
            e = score.energy_at(span.start)
            new_held = {}
            for p in voicing:
                n = held.get(p)
                if n is not None:
                    n.dur = span.end - n.start
                else:
                    n = Note(perf.time(span.start, p, 0.02), span.dur, p,
                             perf.vel(50 + 40 * e, span.start, p, spread=3, accent=False))
                    part.notes.append(n)
                new_held[p] = n
            held, prev = new_held, voicing
        expression(score, part, start, end)


def t_bass_sustain(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """长音低音，每个和弦一个根音（斜杠和弦用斜杠后的低音）。"""
    lo = track.options.get("low", 28)
    for start, end, spans in regions(score.spans_in(track.sections)):
        for span in spans:
            e = score.energy_at(span.start)
            p = place(span.chord.bass, lo)
            part.notes.append(Note(perf.time(span.start, p, 0.01), span.dur - 0.02, p, perf.vel(60 + 35 * e, span.start, p)))
        expression(score, part, start, end, floor=45)


def t_bass_pizz(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """拨弦低音：根-根-五-八的跳跃节奏。"""
    lo = track.options.get("low", 36)
    for span in score.spans_in(track.sections):
        c = span.chord
        e = score.energy_at(span.start)
        r = place(c.bass, lo)
        second = r + c.fifth if c.bass == c.root else place(c.root, r + 1)
        hits = [(0, r), (1.5, r), (2, second), (3, r + 12)] if span.dur >= 4 else [(0, r), (1, second)]
        for off, p in hits:
            b = span.start + off
            part.notes.append(Note(perf.time(b, p, 0.008), 0.6, p, perf.vel(58 + 50 * e, b, p)))


def t_timpani(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """定音鼓：每小节重拍敲根音；roll_into 指定段落前一小节做渐强滚奏。"""
    lo = track.options.get("low", 38)
    for span in score.spans_in(track.sections):
        if span.start % BEATS_PER_BAR:
            continue
        e = score.energy_at(span.start)
        p = place(span.chord.root, lo)
        for off, base in ((0, 85 + 35 * e), (2, 60 + 25 * e))[: 2 if e >= 0.8 else 1]:
            b = span.start + off
            part.notes.append(Note(perf.time(b, p, 0.005), 1.5, p, perf.vel(base, b, p, spread=3, accent=False)))
    target = track.options.get("roll_into")
    if target:
        sec = score.section(target)
        before = [c for c in score.chords if c.start < sec.start]
        if before:
            # 一个滚奏音符 + CC11 渐强；高潮第一拍把表情拉回满值
            p = place(before[-1].chord.root, lo)
            start = sec.start - BEATS_PER_BAR
            part.notes.append(Note(start, BEATS_PER_BAR, p, 100, art="roll"))
            part.ccs.append((0.0, 11, 127))
            steps = 32
            for i in range(steps):
                part.ccs.append((start + i * BEATS_PER_BAR / steps, 11, int(22 + 105 * (i / (steps - 1)) ** 1.6)))
            part.ccs.append((sec.start, 11, 127))


def t_crash(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """在段落开头敲一下吊镲。"""
    for name in track.sections:
        start = score.section(name).start
        part.notes.append(Note(start, 4.0, 49, perf.vel(108, start, 49, spread=2, accent=False)))


def t_melody(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """逐音写好的旋律；力度随能量和音高轮廓起伏。"""
    gate = track.options.get("gate", 1.0 if part.sound.sustained else 0.95)
    center = sum(n.pitch for n in track.melody) / len(track.melody)
    for n in track.melody:
        e = score.energy_at(n.start)
        base = 56 + 36 * e + (n.pitch - center) * 0.8
        part.notes.append(Note(perf.time(n.start, n.pitch, 0.01), n.dur * gate, n.pitch,
                               perf.vel(base, n.start, n.pitch, spread=3)))
    if part.sound.sustained:
        for name in track.sections:
            s = score.section(name)
            expression(score, part, s.start, s.end)


# ---------- 节奏型织体：每小节 8/12/16/24/32 个字符，一个字符一步，| 分小节 ----------

def _pattern(track: Track, section: str, key: str):
    """取某段的节奏型：by_section 里写了就用它，否则用默认的。"""
    override = (track.options.get("by_section") or {}).get(section) or {}
    return override.get(key, track.options.get(key))


def steps(section: Section, text: str, allowed: str, where: str) -> list[tuple[float, float, str]]:
    """把节奏型字符串展开成 [(绝对拍, 每步拍数, 字符)]。空格只为好读，会被忽略。"""
    bars = [b.replace(" ", "") for b in str(text).split("|") if b.strip()]
    if not bars or section.bars % len(bars):
        raise ScoreError([f"{where}: 写了 {len(bars)} 小节，段落 {section.name} 有 {section.bars} 小节（须相等或能整除）"])
    out = []
    for i, bar in enumerate(bars * (section.bars // len(bars))):
        if len(bar) not in STEP_COUNTS:
            raise ScoreError([f"{where} 第 {i + 1} 小节有 {len(bar)} 步，每小节须为 "
                              f"{'/'.join(map(str, STEP_COUNTS))} 步: \"{bar}\""])
        if bad := set(bar) - set(allowed):
            raise ScoreError([f"{where} 第 {i + 1} 小节有不认识的字符 {''.join(sorted(bad))}（可用: {allowed}）"])
        step = BEATS_PER_BAR / len(bar)
        out += [(section.start + i * BEATS_PER_BAR + j * step, step, c) for j, c in enumerate(bar)]
    return out


def _events(score: Score, track: Track, key: str, allowed: str) -> list[tuple[float, float, str, ChordSpan]]:
    """展开成 (起点, 时长, 字符, 和弦)：'-' 延长前一个，'.' 休止；延长途中换和弦就在新和弦上重新起音。"""
    out: list[list] = []
    for name in track.sections:
        text = _pattern(track, name, key)
        if text is None:
            raise ScoreError([f"tracks.{track.name}: {track.texture} 织体需要 {key}（可写在 by_section.{name} 里）"])
        cur = None
        for b, step, c in steps(score.section(name), text, allowed, f"tracks.{track.name}.{key}"):
            span = score.chord_at(b)
            if c == "-" and cur is not None:
                if cur[3] is span:
                    cur[1] += step
                    continue
                c = cur[2]
            if c in ".-":
                cur = None
                continue
            cur = [b, step, c, span]
            out.append(cur)
    return [tuple(e) for e in out]


def _degree(chord: Chord, deg: str, lo: int) -> int:
    """级数 → 音高：1 是低音（斜杠和弦取斜杠后的音），8 是高八度；没有七音的和弦，7 弹高八度根音。"""
    bass = place(chord.bass, lo)
    root = place(chord.root, bass)
    return {"1": bass, "8": bass + 12, "3": root + chord.third, "5": root + chord.fifth,
            "7": root + (chord.seventh or 12)}[deg]


def t_groove(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """鼓组：pattern 里每件鼓一行。X 重音、x 普通、g 轻声（ghost note）、. 不打。
    laid_back：军鼓/拍手往后拖的拍数，做出 lo-fi 那种慵懒的感觉。"""
    laid_back = track.options.get("laid_back", 0.0)
    for name in track.sections:
        pattern = _pattern(track, name, "pattern")
        if not isinstance(pattern, dict):
            raise ScoreError([f"tracks.{track.name}: groove 织体需要 pattern，每件鼓一行（可用: {', '.join(DRUM_KEYS)}）"])
        for drum, text in pattern.items():
            if drum not in DRUM_KEYS:
                raise ScoreError([f"tracks.{track.name}.pattern.{drum}: 不认识这件鼓（可用: {', '.join(DRUM_KEYS)}）"])
            key = DRUM_KEYS[drum]
            for b, step, c in steps(score.section(name), text, "Xxg.", f"tracks.{track.name}.pattern.{drum}"):
                if c == ".":
                    continue
                base = {"X": 104, "x": 84, "g": 40}[c] * (0.75 + 0.3 * score.energy_at(b))
                t = b + (laid_back if key in (37, 38, 39) else 0.0)
                part.notes.append(Note(perf.time(t, key, 0.006), min(step, 0.25), key,
                                       perf.vel(base, b, key, spread=5, accent=False)))


def t_bassline(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """节奏型低音：rhythm 里 1/3/5/7/8 是当前和弦的级数，- 延长，. 休止。"""
    lo = track.options.get("low", 28)
    for b, dur, c, span in _events(score, track, "rhythm", "13578-."):
        p = _degree(span.chord, c, lo)
        e = score.energy_at(b)
        part.notes.append(Note(perf.time(b, p, 0.006), dur * 0.92, p, perf.vel(70 + 35 * e, b, p, spread=4)))


def t_comp(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """和弦伴奏型：x 弹和弦、X 重音、- 延长、. 休止。四声部就近进行（每段重新起排）。
    roll：从低到高依次拨开的间隔（拍），0 为齐奏。"""
    lo, hi = track.options.get("range", [48, 67])  # 默认垫在旋律下面
    roll = track.options.get("roll", 0.0)
    prev, prev_section = None, None
    for b, dur, c, span in _events(score, track, "rhythm", "Xx-."):
        if span.section != prev_section:
            prev, prev_section = None, span.section
        voicing = voice_lead(span.chord, prev, lo, hi)
        prev = voicing
        base = 52 + 40 * score.energy_at(b) + (12 if c == "X" else 0)
        for i, p in enumerate(voicing):
            t = b + i * roll
            part.notes.append(Note(perf.time(t, p, 0.008), max(0.1, dur * 0.95 - i * roll), p,
                                   perf.vel(base, b, p, spread=4, accent=False)))


def t_strum(score: Score, track: Track, part: Part, perf: Performer) -> None:
    """吉他扫弦：D 下扫、U 上扫（只扫高音的 4 根弦）、d/u 轻扫、x 闷音切音、. 让弦继续响。
    按法由 guitar_voicing 在真实的标准调弦上搜索；spread 是相邻两根弦的时间差（秒）。"""
    spread = track.options.get("spread", 0.012) * score.bpm / 60
    for name in track.sections:
        sec = score.section(name)
        ringing: dict[int, Note] = {}

        def stop(at: float, only=None) -> None:
            for i in list(ringing):
                if only is None or i in only:
                    ringing[i].dur = max(0.05, at - ringing[i].start)
                    del ringing[i]

        last_span = None
        for b, step, c in steps(sec, _pattern(track, name, "pattern") or "", "DUdux.", f"tracks.{track.name}.pattern"):
            if c == ".":
                continue
            span = score.chord_at(b)
            voicing = guitar_voicing(span.chord)
            strings = [i for i, p in enumerate(voicing) if p is not None]
            e = score.energy_at(b)
            if c == "x":
                stop(b)
                for i in strings[1:5]:
                    part.notes.append(Note(perf.time(b, voicing[i], 0.004), 0.05, voicing[i],
                                           perf.vel(38 + 20 * e, b, voicing[i], spread=4, accent=False)))
                continue
            down = c in "Dd"
            hit = strings if down else strings[-4:][::-1]
            if span is not last_span:  # 换了和弦：这次没扫到的弦也要按新把位，旧音停掉
                stop(b, only=[i for i in ringing if i not in hit])
            last_span = span
            base = (70 + 35 * e) * (1.0 if c.isupper() else 0.62) - (0 if down else 8)
            for k, i in enumerate(hit):
                t = b + k * spread * (1.0 if down else 0.8)
                if i in ringing:
                    stop(t, only=[i])
                n = Note(perf.time(t, voicing[i], 0.003), sec.end - t, voicing[i],
                         perf.vel(base - 2 * k, b, voicing[i], spread=4, accent=False))
                part.notes.append(n)
                ringing[i] = n
        stop(sec.end)


def swing_beat(score: Score, b: float) -> float:
    """平直拍点 → 摇摆后的拍点：每对音符里后一个往后挪，比例为 swing（0.5 不变，0.67 为三连音感）。"""
    s = score.swing
    if s == 0.5:
        return b
    pair = 8 / score.swing_unit
    base = math.floor(b / pair + 1e-9) * pair
    u = (b - base) / pair
    u = u * 2 * s if u < 0.5 else s + (u - 0.5) * 2 * (1 - s)
    return base + u * pair


TEXTURES = {
    "arpeggio": t_arpeggio,
    "pad": t_pad,
    "bass-sustain": t_bass_sustain,
    "bass-pizz": t_bass_pizz,
    "timpani": t_timpani,
    "crash": t_crash,
    "melody": t_melody,
    "groove": t_groove,
    "bassline": t_bassline,
    "comp": t_comp,
    "strum": t_strum,
}

# 不同织体的默认音量平衡（dB），可在乐谱里用 gain 覆盖


def _fix_overlaps(part: Part) -> None:
    """同音高的音符重叠时截断前一个，避免 MIDI note-off 错乱。"""
    part.notes.sort(key=lambda n: (n.pitch, n.start))
    for a, b in zip(part.notes, part.notes[1:]):
        if a.pitch == b.pitch and a.start + a.dur > b.start - 0.01:
            a.dur = max(0.05, b.start - a.start - 0.01)
    part.notes.sort(key=lambda n: n.start)


def arrange(score: Score) -> tuple[list[Part], list[str]]:
    problems = []
    for t in score.tracks:
        if t.sound not in SOUNDS:
            problems.append(f"tracks.{t.name}.sound: 不认识 {t.sound!r}（可用: {', '.join(SOUNDS)}）")
        if t.texture not in TEXTURES:
            problems.append(f"tracks.{t.name}.texture: 不认识 {t.texture!r}（可用: {', '.join(TEXTURES)}）")
        elif t.texture == "melody" and not t.melody:
            problems.append(f"tracks.{t.name}: melody 织体需要 notes")
        if "roll_into" in t.options and t.options["roll_into"] not in [s.name for s in score.sections]:
            problems.append(f"tracks.{t.name}.roll_into: 没有段落 {t.options['roll_into']}")
    if problems:
        raise ScoreError(problems)

    parts, warnings = [], []
    for t in score.tracks:
        sound = SOUNDS[t.sound]
        part = Part(t.name, t.sound, sound, float(t.options.get("gain", 0.0)), texture=t.texture)
        TEXTURES[t.texture](score, t, part, Performer(score, t.name))
        if score.swing != 0.5:
            for n in part.notes:
                end = swing_beat(score, n.start + n.dur)
                n.start = swing_beat(score, n.start)
                n.dur = max(0.02, end - n.start)
            part.ccs = [(swing_beat(score, b), cc, v) for b, cc, v in part.ccs]
        _fix_overlaps(part)
        if not sound.drum:
            bad = [n for n in part.notes if not sound.lo <= n.pitch <= sound.hi]
            if bad:
                n = bad[0]
                warnings.append(f"{t.name}: {len(bad)} 个音超出 {t.sound} 音域 "
                                f"{pitch_name(sound.lo)}–{pitch_name(sound.hi)}（例如 {pitch_name(n.pitch)}，"
                                f"第 {int(n.start // BEATS_PER_BAR) + 1} 小节）")
        parts.append(part)
    return parts, warnings

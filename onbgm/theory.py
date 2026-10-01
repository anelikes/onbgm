"""乐理基础：音高、和弦解析、声部进行。"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from functools import lru_cache

_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def parse_pc(name: str) -> int:
    pc = _PC[name[0].upper()]
    pc += name[1:].count("#") - name[1:].count("b")
    return pc % 12


def parse_pitch(token: str) -> int:
    """'C#5' -> 73，约定 C4 = 60。"""
    m = re.fullmatch(r"([A-Ga-g])([#b]*)(-?\d)", token)
    if not m:
        raise ValueError(f"无法解析音高 {token!r}")
    pc = _PC[m.group(1).upper()] + m.group(2).count("#") - m.group(2).count("b")
    return 12 * (int(m.group(3)) + 1) + pc


def pitch_name(p: int) -> str:
    return f"{_NAMES[p % 12]}{p // 12 - 1}"


QUALITIES: dict[str, tuple[int, ...]] = {
    "": (0, 4, 7),
    "m": (0, 3, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "6": (0, 4, 7, 9),
    "m6": (0, 3, 7, 9),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "m7b5": (0, 3, 6, 10),
    "dim7": (0, 3, 6, 9),
    "7sus4": (0, 5, 7, 10),
    "add9": (0, 4, 7, 14),
    "madd9": (0, 3, 7, 14),
    "9": (0, 4, 7, 10, 14),
    "maj9": (0, 4, 7, 11, 14),
    "m9": (0, 3, 7, 10, 14),
    "m11": (0, 3, 7, 10, 14, 17),
    "13": (0, 4, 7, 10, 14, 21),
    "7b9": (0, 4, 7, 10, 13),
    "7#9": (0, 4, 7, 10, 15),
    "maj7#11": (0, 4, 7, 11, 18),
    "6add9": (0, 4, 7, 9, 14),
}


@dataclass(frozen=True)
class Chord:
    symbol: str
    root: int  # pitch class
    intervals: tuple[int, ...]
    bass: int  # pitch class，斜杠和弦时与 root 不同

    @property
    def pcs(self) -> set[int]:
        return {(self.root + i) % 12 for i in self.intervals}

    @property
    def third(self) -> int:
        """决定和弦色彩的音；sus 和弦用 4 度 / 2 度代替。"""
        return next(i for i in (3, 4, 5, 2) if i in self.intervals)

    @property
    def fifth(self) -> int:
        return next((i for i in (7, 6, 8) if i in self.intervals), 7)

    @property
    def seventh(self) -> int | None:
        return next((i for i in (10, 11) if i in self.intervals), None)

    @property
    def is_dominant(self) -> bool:
        return self.seventh == 10 and self.third in (4, 5)


def parse_chord(symbol: str) -> Chord:
    m = re.fullmatch(r"([A-G][#b]?)([^/]*)(?:/([A-G][#b]?))?", symbol)
    if not m or m.group(2) not in QUALITIES:
        known = ", ".join(q or "(大三)" for q in QUALITIES)
        raise ValueError(f"不认识的和弦 {symbol!r}（支持的类型: {known}）")
    root = parse_pc(m.group(1))
    bass = parse_pc(m.group(3)) if m.group(3) else root
    return Chord(symbol, root, QUALITIES[m.group(2)], bass)


def place(pc: int, lo: int) -> int:
    """把音级放到 [lo, lo+11] 内。"""
    return lo + (pc - lo) % 12


GUITAR_STRINGS = (40, 45, 50, 55, 59, 64)  # 标准调弦 E2 A2 D3 G3 B3 E4


@lru_cache(maxsize=None)
def guitar_voicing(chord: Chord) -> tuple[int | None, ...]:
    """在标准调弦的吉他上找一个按得出来的和弦把位，返回每根弦的音高（None 表示不弹）。

    规则：只允许最低的一两根弦闷掉；最低音必须是和弦的低音；必须有根音和三音（七和弦还要有七音）；
    不重复同一个音；按弦的品位跨度不超过 3 品，手指不超过 4 根（横按算一根），横按范围内不能有空弦。偏好：弦多、空弦多、把位低（像真吉他手会选的开放和弦）。
    """
    options = []
    for open_pitch in GUITAR_STRINGS:
        frets = [f for f in range(0, 8) if (open_pitch + f) % 12 in chord.pcs]
        options.append(frets)
    required = {chord.root % 12, (chord.root + chord.third) % 12}
    if chord.seventh is not None:
        required.add((chord.root + chord.seventh) % 12)
    best, best_cost = None, float("inf")
    for muted in range(0, 3):
        for frets in itertools.product(*options[muted:]):
            pitches = [GUITAR_STRINGS[muted + i] + f for i, f in enumerate(frets)]
            if pitches[0] % 12 != chord.bass or pitches[0] != min(pitches):
                continue
            if not required <= {p % 12 for p in pitches}:
                continue
            if len(set(pitches)) < len(pitches):  # 同一个音弹两遍（同度重复）不自然
                continue
            pressed = [f for f in frets if f > 0]
            if pressed:
                low = min(pressed)
                if max(pressed) - low > 3:
                    continue
                if len(pressed) > 4:  # 手指不够，必须用食指横按最低品位
                    if sum(f != low for f in pressed) + 1 > 4:
                        continue
                    barred = [i for i, f in enumerate(frets) if f == low]
                    if 0 in frets[barred[0]:barred[-1]]:  # 横按会压住中间的空弦
                        continue
            span = max(pressed) - min(pressed) if pressed else 0
            cost = (-2.0 * len(pitches) - 0.7 * frets.count(0) + 0.5 * max(frets) + 0.8 * span
                    - 0.3 * len({p % 12 for p in pitches}))
            if cost < best_cost:
                best, best_cost = tuple([None] * muted + pitches), cost
    if best is None:
        raise ValueError(f"找不到 {chord.symbol} 的吉他按法")
    return best


def voice_lead(chord: Chord, prev: tuple[int, ...] | None, lo: int, hi: int, n: int = 4) -> tuple[int, ...]:
    """在 [lo, hi] 内为和弦选 n 个声部，使其与上一个和弦的总移动距离最小。"""
    tones = [p for p in range(lo, hi + 1) if p % 12 in chord.pcs]
    required = {(chord.root + chord.third) % 12}
    if chord.seventh is not None:
        required.add((chord.root + chord.seventh) % 12)
    else:
        required.add(chord.root)
    best, best_cost = None, float("inf")
    for combo in itertools.combinations(tones, n):
        pcs = {p % 12 for p in combo}
        if not required <= pcs:
            continue
        if any(b - a < 2 for a, b in zip(combo, combo[1:])):  # 避免小二度挤在一起
            continue
        if combo[-1] - combo[0] > 19:
            continue
        if prev:
            cost = sum(abs(a - b) for a, b in zip(combo, prev))
        else:
            cost = abs(sum(combo) / n - (lo + hi) / 2)
        cost += 0.5 * (n - len(pcs))  # 同样距离下偏好更多不同的和弦音
        if cost < best_cost:
            best, best_cost = combo, cost
    if best is None:
        raise ValueError(f"无法在 {pitch_name(lo)}–{pitch_name(hi)} 内为 {chord.symbol} 排出 {n} 声部")
    return best

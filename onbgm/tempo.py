"""速度图：每个段落可以有自己的速度，也可以在段内渐变（渐慢、渐快）。

拍和秒之间的换算都在这里。渐变段的速度随拍子线性变化，时间是它的积分，有解析解：
    bpm(b) = b0 + k·(b − s)，   t(b) = 60/k · ln(bpm(b) / b0)
给视频配乐时，几个画面时间点很少能在同一个速度下都落在小节线上；每段用略有不同的速度（差 1~3%，几乎听不出），
就能让每个点都精确落在小节第一拍上，这是电影配乐的通行做法（tempo map）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass
class Segment:
    start: float  # 拍
    end: float
    bpm0: float
    bpm1: float
    t0: float = 0.0  # 起点的秒数

    def seconds_into(self, beat: float) -> float:
        """从段首到 beat（可以超出段尾，按段尾速度延伸）经过的秒数。"""
        x = beat - self.start
        if beat > self.end:
            return self.seconds_into(self.end) + (beat - self.end) * 60 / self.bpm1
        if abs(self.bpm1 - self.bpm0) < 1e-9:
            return x * 60 / self.bpm0
        k = (self.bpm1 - self.bpm0) / (self.end - self.start)
        return 60 / k * math.log((self.bpm0 + k * x) / self.bpm0)

    def beat_after(self, seconds: float) -> float:
        """seconds_into 的反函数。"""
        span = self.seconds_into(self.end)
        if seconds > span:
            return self.end + (seconds - span) * self.bpm1 / 60
        if abs(self.bpm1 - self.bpm0) < 1e-9:
            return self.start + seconds * self.bpm0 / 60
        k = (self.bpm1 - self.bpm0) / (self.end - self.start)
        return self.start + (self.bpm0 * math.exp(k * seconds / 60) - self.bpm0) / k

    def bpm_at(self, beat: float) -> float:
        if abs(self.bpm1 - self.bpm0) < 1e-9 or beat >= self.end:
            return self.bpm1
        return self.bpm0 + (self.bpm1 - self.bpm0) * max(0.0, beat - self.start) / (self.end - self.start)


class TempoMap:
    def __init__(self, segments: list[Segment]):
        self.segments = segments
        t = 0.0
        for s in segments:
            s.t0 = t
            t += s.seconds_into(s.end)

    def _segment(self, beat: float) -> Segment:
        for s in self.segments:
            if beat < s.end:
                return s
        return self.segments[-1]

    def seconds(self, beat: float) -> float:
        if beat < 0:
            return beat * 60 / self.segments[0].bpm0
        s = self._segment(beat)
        return s.t0 + s.seconds_into(beat)

    def beat_at(self, seconds: float) -> float:
        if seconds < 0:
            return seconds * self.segments[0].bpm0 / 60
        for s in self.segments:
            if seconds < s.t0 + s.seconds_into(s.end):
                return s.beat_after(seconds - s.t0)
        last = self.segments[-1]
        return last.beat_after(seconds - last.t0)

    def bpm_at(self, beat: float) -> float:
        return self._segment(beat).bpm_at(beat)

    @property
    def varies(self) -> bool:
        return any(abs(s.bpm0 - s.bpm1) > 1e-9 or abs(s.bpm0 - self.segments[0].bpm0) > 1e-9 for s in self.segments)


_RAMP = re.compile(r"^\s*(\d+(?:\.\d+)?)?\s*->\s*(\d+(?:\.\d+)?)\s*$")


def parse_tempo(value) -> tuple[float | None, float]:
    """段落的 tempo 字段：120 → (120, 120)；"120->90" → (120, 90)；"->70" → (None, 70)，起点沿用上一段的速度。"""
    if isinstance(value, (int, float)):
        return (float(value), float(value))
    m = _RAMP.match(str(value))
    if not m:
        raise ValueError(f"无法解析速度 {value!r}（写 96、\"120->90\" 或 \"->70\"）")
    return (float(m.group(1)) if m.group(1) else None, float(m.group(2)))


def build(bounds: list[tuple[float, float]], specs: list[tuple[float | None, float] | None], base: float) -> TempoMap:
    """bounds 是每段的 (起拍, 止拍)，specs 是每段的速度设置（None 表示用基准速度）。"""
    segments, prev = [], base
    for (start, end), spec in zip(bounds, specs):
        b0, b1 = (base, base) if spec is None else (prev if spec[0] is None else spec[0], spec[1])
        segments.append(Segment(start, end, b0, b1))
        prev = b1
    return TempoMap(segments)


def solve_base(bounds, specs, beat: float, seconds: float) -> float | None:
    """找一个基准速度，使 beat 正好落在 seconds。基准速度越快时间越短，所以用二分法；
    beat 之前的段落都不用基准速度时，基准速度影响不到它，返回 None。"""
    def at(base: float) -> float:
        return build(bounds, specs, base).seconds(beat)

    lo, hi = 10.0, 1000.0
    if abs(at(lo) - at(hi)) < 1e-9 or not at(hi) <= seconds <= at(lo):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if at(mid) > seconds:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2

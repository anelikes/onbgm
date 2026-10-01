"""给定视频的画面节点，算出速度和段落结构，让每个节点都精确落在小节第一拍上。

做法和电影配乐的 tempo map 一样：先选一个基准速度，每两个节点之间取最接近的整数小节；
凑不准的那一段单独设一个略快或略慢的速度（偏差通常 1~3%，几乎听不出），节点就能精确对上。
评分偏好：变速段越少越好、偏差越小越好；小节数是 2、4、8 这类整齐的数；基准速度是整数。
"""
from __future__ import annotations

from dataclasses import dataclass

from .score import BEATS_PER_BAR, fmt_time
from .tempo import build

EXACT = 0.0005  # 速度偏差小于 0.05% 视为不用变速


@dataclass
class Cue:
    seconds: float
    name: str


def _bar_penalty(bars: int) -> float:
    if bars in (2, 4, 8, 16):
        return 0.0
    if bars in (1, 6, 12):
        return 0.2
    return 0.4


def _deviation_cost(dev: float) -> float:
    """变速的代价按听感分级：3% 以内几乎听不出，3~6% 略有感觉，6% 以上明显。"""
    dev = abs(dev)
    if dev < EXACT:
        return 0.0
    if dev <= 0.03:
        return 1 + 30 * dev
    if dev <= 0.06:
        return 2 + 60 * dev
    return 10 + 100 * dev


def _plan(base: float, bounds: list[tuple[float, float]], ends_at_length: bool) -> tuple[list[dict], float]:
    """在这个基准速度下给每段定小节数和速度，返回 (段落, 代价)。
    最后一段如果是到视频结尾，不必卡准：按基准速度写完整小节，剩下的交给余音（meta.tail），通常比变速自然。"""
    rows, cost = [], 0.0
    for i, (start, end) in enumerate(bounds):
        d = end - start
        bar_s = 60 * BEATS_PER_BAR / base
        bars = max(1, round(d / bar_s))
        tempo = 60 * BEATS_PER_BAR * bars / d
        dev = tempo / base - 1
        row = {"bars": bars, "tempo": tempo, "deviation": dev, "tail": 0.0}
        option = _deviation_cost(dev) + _bar_penalty(bars)
        if ends_at_length and i == len(bounds) - 1:
            whole = int(d / bar_s + 1e-9)
            tail = d - whole * bar_s
            if whole >= 1 and tail <= 4.0:
                tail_cost = (0.3 + 0.1 * tail if tail > 0.01 else 0.0) + _bar_penalty(whole)
                if tail_cost < option:
                    row, option = {"bars": whole, "tempo": base, "deviation": 0.0, "tail": tail}, tail_cost
        rows.append(row)
        cost += option
    if abs(base - round(base)) > 1e-6:
        cost += 0.3
    return rows, cost


def fit(cues: list[Cue], length: float | None, bpm_range: tuple[float, float], hits: list[float] = (),
        top: int = 3) -> dict:
    """返回 {"plans": 按代价排序的方案, "suggestion": 放宽速度范围能明显更好时的提示}。"""
    plans = _search(cues, length, bpm_range, hits, top)
    suggestion = None
    if plans and plans[0]["max_deviation_pct"] > 3:
        wide = _search(cues, length, (60.0, 180.0), hits, 1)
        if wide and wide[0]["max_deviation_pct"] < plans[0]["max_deviation_pct"] - 1:
            w = wide[0]
            suggestion = (f"速度范围放宽到 {w['bpm']:g} BPM 的话，最大偏差只有 {w['max_deviation_pct']:g}%"
                          + ("，所有节点都正好落在小节线上" if w["max_deviation_pct"] == 0 else "")
                          + f"（用 --bpm {max(60, int(w['bpm']) - 5)}-{int(w['bpm']) + 5} 查看）")
    return {"plans": plans, "suggestion": suggestion}


def _search(cues, length, bpm_range, hits, top) -> list[dict]:
    times = [0.0] + [c.seconds for c in cues] + ([length] if length else [])
    names = ["intro"] + [c.name for c in cues]
    bounds = list(zip(times, times[1:]))
    if not bounds:
        return []
    lo, hi = bpm_range
    seen, scored = set(), []
    # 候选基准速度：范围内每 0.25 BPM 一个，加上让某一段正好整数小节的速度
    candidates = {round(lo + i * 0.25, 2) for i in range(int((hi - lo) / 0.25) + 1)}
    for start, end in bounds:
        for bars in range(1, 65):
            bpm = 60 * BEATS_PER_BAR * bars / (end - start)
            if lo <= bpm <= hi:
                candidates.add(round(bpm, 4))
    for base in sorted(candidates):
        rows, cost = _plan(base, bounds, bool(length))
        key = tuple(r["bars"] for r in rows)
        scored.append((cost, abs(base - (lo + hi) / 2) / 1000, base, rows, key))
    scored.sort(key=lambda x: (x[0], x[1]))
    plans = []
    for cost, _, base, rows, key in scored:
        if key in seen:
            continue  # 同样的小节划分只保留代价最低的那个
        seen.add(key)
        plans.append(_describe(base, rows, bounds, names, cost, hits, bool(length), times[-1]))
        if len(plans) == top:
            break
    return plans


def _describe(base, rows, bounds, names, cost, hits, has_length, last_cue) -> dict:
    sections = []
    for i, ((start, end), r) in enumerate(zip(bounds, rows)):
        exact = abs(r["deviation"]) < EXACT
        sections.append({
            "name": names[i] if i < len(names) else f"s{i + 1}",
            "start_s": round(start, 3), "end_s": round(end, 3), "bars": r["bars"],
            "tempo": None if exact else round(r["tempo"], 2),
            "deviation_pct": 0.0 if exact else round(100 * r["deviation"], 2),
            "tail_s": round(r["tail"], 3),
        })
    if not has_length:  # 最后一个节点之后的段落没有终点：按基准速度先给 4 小节
        sections.append({"name": names[-1], "start_s": round(last_cue, 3),
                         "end_s": round(last_cue + 4 * BEATS_PER_BAR * 60 / base, 3), "bars": 4,
                         "tempo": None, "deviation_pct": 0.0, "tail_s": 0.0})
    plan = {"bpm": round(base, 2), "cost": round(cost, 3), "sections": sections,
            "max_deviation_pct": max(abs(s["deviation_pct"]) for s in sections)}
    tmap = build([(sum(s["bars"] for s in sections[:i]) * BEATS_PER_BAR,
                   sum(s["bars"] for s in sections[:i + 1]) * BEATS_PER_BAR) for i in range(len(sections))],
                 [None if s["tempo"] is None else (s["tempo"], s["tempo"]) for s in sections], plan["bpm"])
    plan["hits"] = []
    for t in hits:
        beat = tmap.beat_at(t)
        frac = beat % 1
        on = ("downbeat" if beat % BEATS_PER_BAR < 0.02 or beat % BEATS_PER_BAR > BEATS_PER_BAR - 0.02 else
              "beat" if frac < 0.02 or frac > 0.98 else "eighth" if abs(frac - 0.5) < 0.02 else "off")
        plan["hits"].append({"time_s": round(t, 3), "bar": int(beat // BEATS_PER_BAR) + 1,
                             "beat": round(beat % BEATS_PER_BAR + 1, 2), "on": on})
    plan["skeleton"] = skeleton(plan, has_length)
    return plan


def skeleton(plan: dict, has_length: bool) -> str:
    """一份可以直接 check 的乐谱骨架：段落、小节数、时间约束和必要的段落速度都已填好，和弦待写。"""
    tail = plan["sections"][-1]["tail_s"]
    lines = ["title: My BGM", "meta:", f"  bpm: {plan['bpm']:g}", "  loop: false"]
    if tail > 0.01:
        lines.append(f"  tail: {tail:g}   # 音乐在 {fmt_time(plan['sections'][-1]['end_s'] - tail)} 结束，余音到视频结尾")
    lines += ["", "sections:"]
    for s in plan["sections"]:
        fields = [f"name: {s['name']}", f"bars: {s['bars']}", "energy: 0.5"]
        if s["start_s"] > 0:
            fields.append(f'at: "{fmt_time(s["start_s"])}"')
        if s["tempo"] is not None:
            fields.append(f"tempo: {s['tempo']:g}")
        note = f"{fmt_time(s['start_s'])}–{fmt_time(s['end_s'])}"
        if s["tempo"] is not None:
            note += f"，速度比基准{'快' if s['deviation_pct'] > 0 else '慢'} {abs(s['deviation_pct']):g}%"
        if s["tail_s"] > 0.01:
            note += f"，之后余音 {s['tail_s']:g} 秒"
        lines.append(f"  - {{ {', '.join(fields)} }}   # {note}")
    lines += ["", "harmony:   # TODO：每段换成真正的和弦进行（小节数要等于段落小节数或能整除）"]
    for s in plan["sections"]:
        lines.append(f'  {s["name"]}: "C"')
    if not has_length:
        lines.insert(4, "  # 没有给 --length：最后一段先按基准速度给了 4 小节，没有时间约束")
    return "\n".join(lines) + "\n"

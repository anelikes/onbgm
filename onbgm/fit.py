"""给定视频的画面节点，算出速度和段落结构，让每个节点都精确落在小节第一拍上。

做法和电影配乐的 tempo map 一样：每两个节点之间取整数小节，各段的速度因此略有不同；
用得最多的那个速度是主速度，其余段落单独设速度。人耳听到的是相邻两段之间的速度跳变，
所以评分看相邻跳变：3% 以内几乎听不出，3~6% 略有感觉，6% 以上明显。排序先看明显的跳变有几处，
再看最大跳变，最后看总代价（小节数偏好 2、4、8 这类整齐的数）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .score import BEATS_PER_BAR, fmt_time
from .tempo import Segment, build

EXACT = 0.0005  # 速度差小于 0.05% 视为同一个速度
MAX_TAIL = 4.0  # 结尾留给余音的最长秒数


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


def _jump_cost(jump: float) -> float:
    """相邻两段速度跳变的代价，按听感分级。"""
    jump = abs(jump)
    if jump < EXACT:
        return 0.0
    if jump <= 0.03:
        return 1 + 30 * jump
    if jump <= 0.06:
        return 2 + 60 * jump
    return 10 + 100 * jump


def _ramp_seconds(bars: int, start_bpm: float, end_bpm: float) -> float:
    return Segment(0, bars * BEATS_PER_BAR, start_bpm, end_bpm).seconds_into(bars * BEATS_PER_BAR)


def _plan(base: float, bounds: list[tuple[float, float]], ends_at_length: bool, ending_tempo: float | None):
    """按候选速度 base 给每段定小节数和速度。返回 (每段, 主速度, 相邻跳变, 代价)。
    最后一段如果到视频结尾：不必卡准，写完整小节，剩下交给余音（meta.tail）；
    要渐慢收尾时，最后一段从上一段的速度渐变到 ending_tempo，按渐变后的实际时长选小节数。"""
    rows, cost, prev = [], 0.0, base
    for i, (start, end) in enumerate(bounds):
        d = end - start
        last = ends_at_length and i == len(bounds) - 1
        if last and ending_tempo:
            fits = [n for n in range(1, 65) if 0 <= d - _ramp_seconds(n, prev, ending_tempo) <= MAX_TAIL]
            if fits:
                n = max(fits)
                tail = d - _ramp_seconds(n, prev, ending_tempo)
                rows.append({"bars": n, "tempo": prev, "tail": tail, "ramp_to": ending_tempo})
                cost += (0.3 + 0.1 * tail if tail > 0.01 else 0.0) + _bar_penalty(n)
                continue
        bar_s = 60 * BEATS_PER_BAR / base
        bars = max(1, round(d / bar_s))
        row = {"bars": bars, "tempo": 60 * BEATS_PER_BAR * bars / d, "tail": 0.0, "ramp_to": None}
        if last:
            whole = int(d / bar_s + 1e-9)
            tail = d - whole * bar_s
            exact_cost = _jump_cost(row["tempo"] / prev - 1)
            if whole >= 1 and tail <= MAX_TAIL and 0.3 + 0.1 * tail < exact_cost:
                row = {"bars": whole, "tempo": base, "tail": tail, "ramp_to": None}
                cost += 0.3 + 0.1 * tail if tail > 0.01 else 0.0
        rows.append(row)
        cost += _bar_penalty(row["bars"])
        prev = row["tempo"]
    # 主速度：用的小节最多的那个速度
    weight: dict[float, int] = {}
    for r in rows:
        key = next((k for k in weight if abs(r["tempo"] / k - 1) < EXACT), r["tempo"])
        weight[key] = weight.get(key, 0) + r["bars"]
    main = max(weight, key=weight.get)
    jumps = [0.0] + [rows[i]["tempo"] / rows[i - 1]["tempo"] - 1 for i in range(1, len(rows))]
    cost += sum(_jump_cost(j) for j in jumps)
    return rows, main, jumps, cost


def fit(cues: list[Cue], length: float | None, bpm_range: tuple[float, float], hits: list[float] = (),
        top: int = 3, ending_tempo: float | None = None) -> dict:
    """返回 {"plans": 排好序的方案, "suggestion": 放宽速度范围能明显更好时的提示}。"""
    plans = _search(cues, length, bpm_range, hits, top, ending_tempo)
    suggestion = None
    if plans and plans[0]["max_jump_pct"] > 3:
        wide = _search(cues, length, (60.0, 180.0), hits, 1, ending_tempo)
        if wide and wide[0]["max_jump_pct"] < plans[0]["max_jump_pct"] - 1:
            w = wide[0]
            suggestion = (f"速度范围放宽到 {w['bpm']:g} BPM 左右的话，相邻段落的最大速度跳变只有 {w['max_jump_pct']:g}%"
                          + ("，所有节点都在同一个速度下正好落在小节线上" if w["max_jump_pct"] == 0 else "")
                          + f"（用 --bpm {max(60, int(w['bpm']) - 5)}-{int(w['bpm']) + 5} 查看）")
    return {"plans": plans, "suggestion": suggestion}


def _search(cues, length, bpm_range, hits, top, ending_tempo=None) -> list[dict]:
    times = [0.0] + [c.seconds for c in cues] + ([length] if length else [])
    names = ["intro"] + [c.name for c in cues]
    bounds = list(zip(times, times[1:]))
    if not bounds:
        return []
    lo, hi = bpm_range
    # 候选速度：范围内每 0.25 BPM 一个，加上让某一段正好整数小节的速度
    candidates = {round(lo + i * 0.25, 2) for i in range(int((hi - lo) / 0.25) + 1)}
    for start, end in bounds:
        for bars in range(1, 65):
            bpm = 60 * BEATS_PER_BAR * bars / (end - start)
            if lo <= bpm <= hi:
                candidates.add(round(bpm, 4))
    scored = []
    for base in sorted(candidates):
        rows, main, jumps, cost = _plan(base, bounds, bool(length), ending_tempo)
        obvious = sum(abs(j) > 0.06 for j in jumps)
        worst = max(abs(j) for j in jumps)
        key = tuple((r["bars"], round(r["tail"], 2)) for r in rows)
        scored.append(((obvious, round(worst, 4) if worst > 0.03 else 0.0, round(cost, 4), abs(main - (lo + hi) / 2)),
                       key, rows, main, jumps, cost))
    scored.sort(key=lambda x: x[0])
    plans, seen = [], set()
    for _, key, rows, main, jumps, cost in scored:
        if key in seen:
            continue  # 同样的小节划分（速度也就一样）只保留一个
        seen.add(key)
        plans.append(_describe(main, rows, jumps, bounds, names, cost, hits, bool(length), times[-1], ending_tempo))
        if len(plans) == top:
            break
    return plans


def _describe(main, rows, jumps, bounds, names, cost, hits, has_length, last_cue, ending_tempo) -> dict:
    sections = []
    for i, ((start, end), r) in enumerate(zip(bounds, rows)):
        dev = r["tempo"] / main - 1
        own = abs(dev) >= EXACT and not r["ramp_to"]
        sections.append({
            "name": names[i] if i < len(names) else f"s{i + 1}",
            "start_s": round(start, 3), "end_s": round(end, 3), "bars": r["bars"],
            "tempo": round(r["tempo"], 3) if own else None,
            "deviation_pct": round(100 * dev, 2) if abs(dev) >= EXACT else 0.0,
            "jump_pct": round(100 * jumps[i], 2) if abs(jumps[i]) >= EXACT else 0.0,
            "tail_s": round(r["tail"], 3),
            "ramp_to": r["ramp_to"],
        })
    if not has_length:  # 最后一个节点之后没有终点：先给 4 小节，没有时间约束
        sections.append({"name": names[-1], "start_s": round(last_cue, 3),
                         "end_s": round(last_cue + 4 * BEATS_PER_BAR * 60 / rows[-1]["tempo"], 3), "bars": 4,
                         "tempo": None, "deviation_pct": 0.0, "jump_pct": 0.0, "tail_s": 0.0, "ramp_to": ending_tempo})
    plan = {"bpm": round(main, 3), "cost": round(cost, 3), "sections": sections,
            "max_jump_pct": max(abs(s["jump_pct"]) for s in sections),
            "max_deviation_pct": max(abs(s["deviation_pct"]) for s in sections)}
    tmap = build([(sum(s["bars"] for s in sections[:i]) * BEATS_PER_BAR,
                   sum(s["bars"] for s in sections[:i + 1]) * BEATS_PER_BAR) for i in range(len(sections))],
                 [(None, s["ramp_to"]) if s["ramp_to"] else None if s["tempo"] is None else (s["tempo"], s["tempo"])
                  for s in sections], plan["bpm"])
    plan["hits"] = []
    for t in hits:
        beat = tmap.beat_at(t)
        frac, pos = beat % 1, beat % BEATS_PER_BAR
        on = ("downbeat" if pos < 0.02 or pos > BEATS_PER_BAR - 0.02 else
              "beat" if frac < 0.02 or frac > 0.98 else "eighth" if abs(frac - 0.5) < 0.02 else "off")
        plan["hits"].append({"time_s": round(t, 3), "bar": int(round(beat, 2) // BEATS_PER_BAR) + 1,
                             "beat": round(round(beat, 2) % BEATS_PER_BAR + 1, 2), "on": on})
    plan["skeleton"] = skeleton(plan, has_length)
    return plan


def skeleton(plan: dict, has_length: bool) -> str:
    """一份可以直接 check 的乐谱骨架：段落、小节数、时间约束和必要的段落速度都已填好，和弦待写。"""
    tail = plan["sections"][-1]["tail_s"]
    lines = ["title: My BGM", "meta:", f"  bpm: {plan['bpm']:g}   # 主速度：大多数段落用这个速度", "  loop: false"]
    if tail > 0.01:
        lines.append(f"  tail: {tail:g}   # 音乐在 {fmt_time(plan['sections'][-1]['end_s'] - tail)} 结束，余音到视频结尾")
    lines += ["", "sections:"]
    for s in plan["sections"]:
        fields = [f"name: {s['name']}", f"bars: {s['bars']}", "energy: 0.5"]
        if s["start_s"] > 0:
            fields.append(f'at: "{fmt_time(s["start_s"])}"')
        if s["ramp_to"]:
            fields.append(f'tempo: "->{s["ramp_to"]:g}"')
        elif s["tempo"] is not None:
            fields.append(f"tempo: {s['tempo']:g}")
        notes = [f"{fmt_time(s['start_s'])}–{fmt_time(s['end_s'])}"]
        if s["jump_pct"]:
            notes.append(f"比上一段{'快' if s['jump_pct'] > 0 else '慢'} {abs(s['jump_pct']):g}%")
        if s["ramp_to"]:
            notes.append(f"渐慢到 {s['ramp_to']:g} BPM")
        if s["tail_s"] > 0.01:
            notes.append(f"之后余音 {s['tail_s']:g} 秒")
        lines.append(f"  - {{ {', '.join(fields)} }}   # {'，'.join(notes)}")
    lines += ["", "harmony:   # TODO：每段换成真正的和弦进行（小节数要等于段落小节数或能整除）"]
    for s in plan["sections"]:
        lines.append(f'  {s["name"]}: "C"')
    if not has_length:
        lines.insert(4, "  # 没有给 --length：最后一段先给了 4 小节，没有时间约束")
    return "\n".join(lines) + "\n"

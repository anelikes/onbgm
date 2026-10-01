"""渲染后的检查：LLM 听不见，所以把"听"的结果变成它能读的文字和图。"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pyloudnorm as pyln

from .arrange import DRUM_KEYS, Part
from .render import SR
from .score import BEATS_PER_BAR, Score, fmt_time


def _r(x: float, nd: int = 2) -> float:
    return round(float(x), nd)


def timeline_data(score: Score) -> dict:
    """时间轴：每段从第几秒开始、每小节是什么和弦、哪些声部在哪些段落出现。agent 不用自己算时间。"""
    return {
        "title": score.title,
        "bpm": _r(score.bpm, 3),
        "key": score.key,
        "loop": score.loop,
        "bars": int(score.total_beats // BEATS_PER_BAR),
        "duration_s": _r(score.seconds(score.total_beats), 3),
        "seconds_per_bar": _r(score.seconds(BEATS_PER_BAR), 4),
        "sections": [
            {"name": s.name, "bars": [s.start_bar + 1, s.start_bar + s.bars],
             "start_s": _r(score.seconds(s.start), 3), "end_s": _r(score.seconds(s.end), 3),
             "energy": [s.energy[0], s.energy[1]]}
            for s in score.sections
        ],
        "chords": [
            {"bar": int(c.start // BEATS_PER_BAR) + 1, "beat": _r(c.start % BEATS_PER_BAR + 1, 3),
             "start_s": _r(score.seconds(c.start), 3), "chord": c.chord.symbol, "section": c.section}
            for c in score.chords
        ],
        "tracks": [{"name": t.name, "sound": t.sound, "texture": t.texture, "sections": t.sections}
                   for t in score.tracks],
    }


def locate(score: Score, seconds: float) -> dict:
    """某个时间点在乐谱里的位置：第几小节第几拍、哪一段、什么和弦。"""
    beat = seconds * score.bpm / 60
    end = score.seconds(score.total_beats)
    if 0 <= seconds - end < score.tail:
        return {"time_s": _r(seconds, 3), "in_tail": True,
                "note": f"在乐谱结束（{fmt_time(end)}）之后的余音（meta.tail）里，这里不会有新的音符起音"}
    if not 0 <= beat < score.total_beats:
        return {"time_s": _r(seconds, 3),
                "error": f"超出乐谱范围（0 ~ {fmt_time(end)}{f'，加余音到 {fmt_time(end + score.tail)}' if score.tail else ''}）"}
    sec, chord = score.section_at(beat), score.chord_at(beat)
    return {"time_s": _r(seconds, 3), "bar": int(beat // BEATS_PER_BAR) + 1, "beat": _r(beat % BEATS_PER_BAR + 1, 3),
            "section": sec.name, "bar_in_section": int((beat - sec.start) // BEATS_PER_BAR) + 1,
            "chord": chord.chord.symbol, "energy": _r(score.energy_at(beat), 3)}


def timeline(score: Score) -> str:
    d = timeline_data(score)
    lines = [f"《{d['title']}》 {score.bpm:g} BPM · {d['key']} · {d['bars']} 小节 · {fmt_time(d['duration_s'])}"
             + (" · 可无缝循环" if d["loop"] else "")]
    for s in d["sections"]:
        a, b = s["energy"]
        energy = f"{a:.2f}" if a == b else f"{a:.2f}→{b:.2f}"
        lines.append(f"  {s['name']:<8} 第 {s['bars'][0]:>2}–{s['bars'][1]:<2} 小节  {fmt_time(s['start_s'])}  能量 {energy}")
    return "\n".join(lines)


def loudness_data(score: Score, audio: np.ndarray) -> dict:
    """各段实测响度 vs 目标能量，检查"该响的地方是否真的响"（能量差小于 0.15 的两段不比较）。"""
    meter = pyln.Meter(SR)
    rows = []
    for s in score.sections:
        seg = audio[round(score.seconds(s.start) * SR): round(score.seconds(s.end) * SR)]
        rows.append({"section": s.name, "energy": _r(sum(s.energy) / 2, 3), "lufs": _r(meter.integrated_loudness(seg), 1)})
    issues = [f"{a['section']} 目标能量比 {b['section']} 高，实测却更轻"
              for a in rows for b in rows if a["energy"] > b["energy"] + 0.15 and a["lufs"] < b["lufs"]]
    return {"integrated_lufs": _r(meter.integrated_loudness(audio), 1), "sections": rows,
            "follows_energy": not issues, "issues": issues}


def loudness_report(score: Score, audio: np.ndarray) -> str:
    d = loudness_data(score, audio)
    loudest = max(r["lufs"] for r in d["sections"])
    lines = ["各段响度（目标能量 → 实测）:"]
    for r in d["sections"]:
        bar = "█" * max(1, round((r["lufs"] - loudest + 24) / 1.2))
        lines.append(f"  {r['section']:<8} 能量 {r['energy']:.2f} → {r['lufs']:6.1f} LUFS  {bar}")
    lines.append("  ✓ 响度走向与能量曲线一致" if d["follows_energy"] else "  ✗ " + "；".join(d["issues"]))
    return "\n".join(lines)


def balance_data(score: Score, stems: dict[str, np.ndarray], parts: list[Part], section: str) -> list[dict]:
    """某一段里各声部的响度（从响到轻），检查旋律有没有被伴奏盖住。"""
    s = score.section(section)
    a, b = round(score.seconds(s.start) * SR), round(score.seconds(s.end) * SR)
    meter = pyln.Meter(SR)
    levels = []
    for p in parts:
        seg = stems[p.name][a:b] * 10 ** (p.gain_db / 20)
        if np.abs(seg).max() > 1e-4 and np.isfinite(lufs := meter.integrated_loudness(seg)):
            levels.append({"track": p.name, "lufs": _r(lufs, 1)})
    return sorted(levels, key=lambda x: -x["lufs"])


def balance_report(score: Score, stems: dict[str, np.ndarray], parts: list[Part], section: str) -> str:
    rows = balance_data(score, stems, parts, section)
    return f"{section} 段声部平衡: " + "  ".join(f"{r['track']} {r['lufs']:.1f}" for r in rows)


def pianoroll(score: Score, parts: list[Part], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(16, 7), dpi=110)
    colors = plt.get_cmap("tab10")
    pitched = [p for p in parts if not p.sound.drum]
    top = max(n.pitch for p in pitched for n in p.notes) + 3
    # 鼓每件一行，画在音高区上方，方便看清节奏型和摇摆
    drum_names = {v: k for k, v in DRUM_KEYS.items()}
    keys = sorted({n.pitch for p in parts if p.sound.drum for n in p.notes})
    row = {k: top + 2 + 1.6 * i for i, k in enumerate(keys)}
    for k, y in row.items():
        ax.text(0.15, y, drum_names.get(k, str(k)), fontsize=7, va="center", color="0.3")
    top = top + 2 + 1.6 * len(keys)
    for i, p in enumerate(parts):
        c = colors(i % 10)
        if p.sound.drum:
            ax.scatter([score.seconds(n.start) for n in p.notes], [row[n.pitch] for n in p.notes],
                       s=[8 + 40 * n.vel / 127 for n in p.notes], marker="|", color=c, label=p.name, zorder=3)
            continue
        for n in p.notes:
            ax.add_patch(plt.Rectangle((score.seconds(n.start), n.pitch - 0.4), score.seconds(n.dur), 0.8,
                                       color=c, alpha=0.35 + 0.6 * n.vel / 127, linewidth=0))
        ax.plot([], [], color=c, linewidth=6, label=p.name)
    for s in score.sections:
        x = score.seconds(s.start)
        ax.axvline(x, color="k", linewidth=0.8, linestyle="--", alpha=0.5)
        ax.text(x + 0.2, top + 2, f"{s.name}\n{fmt_time(x)}", fontsize=9, va="bottom")
    total = score.seconds(score.total_beats)
    ax.set_xlim(0, total)
    ax.set_ylim(min(n.pitch for p in pitched for n in p.notes) - 3, top + 7)
    ax.set_xlabel("seconds")
    ax.set_ylabel("MIDI pitch")
    beats = np.linspace(0, score.total_beats, 400)
    ax2 = ax.twinx()
    ax2.plot([score.seconds(b) for b in beats], [score.energy_at(b) for b in beats], color="crimson", linewidth=2)
    ax2.set_ylim(0, 1.6)
    ax2.set_ylabel("energy", color="crimson")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.09), ncol=len(parts), fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)

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


def timeline(score: Score) -> str:
    total = score.seconds(score.total_beats)
    bars = int(score.total_beats // BEATS_PER_BAR)
    lines = [f"《{score.title}》 {score.bpm:g} BPM · {score.key} · {bars} 小节 · {fmt_time(total)}"
             + (" · 可无缝循环" if score.loop else "")]
    for s in score.sections:
        a, b = s.energy
        energy = f"{a:.2f}" if a == b else f"{a:.2f}→{b:.2f}"
        lines.append(f"  {s.name:<8} 第 {s.start_bar + 1:>2}–{s.start_bar + s.bars:<2} 小节  "
                     f"{fmt_time(score.seconds(s.start))}  能量 {energy}")
    return "\n".join(lines)


def loudness_report(score: Score, audio: np.ndarray) -> str:
    """各段实测响度 vs 目标能量，检查"该响的地方是否真的响"。"""
    meter = pyln.Meter(SR)
    rows = []
    for s in score.sections:
        seg = audio[round(score.seconds(s.start) * SR): round(score.seconds(s.end) * SR)]
        rows.append((s, sum(s.energy) / 2, meter.integrated_loudness(seg)))
    loudest = max(r[2] for r in rows)
    lines = ["各段响度（目标能量 → 实测）:"]
    for s, target, lufs in rows:
        bar = "█" * max(1, round((lufs - loudest + 24) / 1.2))
        lines.append(f"  {s.name:<8} 能量 {target:.2f} → {lufs:6.1f} LUFS  {bar}")
    issues = [f"{a[0].name} 目标能量比 {b[0].name} 高，实测却更轻"
              for a in rows for b in rows if a[1] > b[1] + 0.15 and a[2] < b[2]]
    lines.append("  ✓ 响度走向与能量曲线一致" if not issues else "  ✗ " + "；".join(issues))
    return "\n".join(lines)


def balance_report(score: Score, stems: dict[str, np.ndarray], parts: list[Part], section: str) -> str:
    """某一段里各声部的响度，检查旋律有没有被伴奏盖住。"""
    s = score.section(section)
    a, b = round(score.seconds(s.start) * SR), round(score.seconds(s.end) * SR)
    meter = pyln.Meter(SR)
    levels = []
    for p in parts:
        seg = stems[p.name][a:b] * 10 ** (p.gain_db / 20)
        if np.abs(seg).max() > 1e-4 and np.isfinite(meter.integrated_loudness(seg)):
            levels.append((meter.integrated_loudness(seg), p.name))
    levels.sort(reverse=True)
    return f"{section} 段声部平衡: " + "  ".join(f"{name} {lufs:.1f}" for lufs, name in levels)


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

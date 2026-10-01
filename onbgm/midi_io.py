"""把声部写成 MIDI 文件。"""
from __future__ import annotations

from pathlib import Path

import mido

from .arrange import Note, Part
from .score import BEATS_PER_BAR, Score

TPB = 480


def _tick(beat: float) -> int:
    return max(0, round(beat * TPB))


def _finish(track: mido.MidiTrack, events: list[tuple[int, int, mido.Message]], end: int) -> None:
    # 同一 tick 上：先 note_off，再控制器，最后 note_on
    events.sort(key=lambda e: (e[0], e[1]))
    now = 0
    for tick, _, msg in events:
        track.append(msg.copy(time=tick - now))
        now = tick
    track.append(mido.MetaMessage("end_of_track", time=max(0, end - now)))


def _expand(notes: list[Note]) -> list[Note]:
    """MIDI 里没有"滚奏"，展开成 32 分音符连击（渐强由 CC11 负责）。"""
    out = []
    for n in notes:
        if n.art == "roll":
            k = max(1, round(n.dur * 8))
            out += [Note(n.start + i * n.dur / k, n.dur / k * 0.9, n.pitch, n.vel) for i in range(k)]
        else:
            out.append(n)
    return out


def write_midi(path: Path, score: Score, parts: list[Part], tail_beats: float = 0.0) -> None:
    mid = mido.MidiFile(type=1, ticks_per_beat=TPB)
    end = _tick(score.total_beats + tail_beats)

    conductor = mido.MidiTrack()
    events = [
        (0, 0, mido.MetaMessage("track_name", name=score.title)),
        (0, 0, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(score.bpm))),
        (0, 0, mido.MetaMessage("time_signature", numerator=BEATS_PER_BAR, denominator=4)),
    ]
    events += [(_tick(s.start), 1, mido.MetaMessage("marker", text=s.name)) for s in score.sections]
    _finish(conductor, events, end)
    mid.tracks.append(conductor)

    channels = [c for c in range(16) if c != 9]
    for i, part in enumerate(parts):
        ch = 9 if part.sound.drum else channels[i % len(channels)]
        track = mido.MidiTrack()
        events = [(0, 0, mido.MetaMessage("track_name", name=part.name))]
        events.append((0, 1, mido.Message("program_change", channel=ch, program=part.sound.program)))  # 鼓组通道上是选鼓组
        for cc, v in ((7, 100), (11, 127), (91, 0), (93, 0)):
            events.append((0, 1, mido.Message("control_change", channel=ch, control=cc, value=v)))
        for n in _expand(part.notes):
            events.append((_tick(n.start), 2, mido.Message("note_on", channel=ch, note=n.pitch, velocity=n.vel)))
            events.append((_tick(n.start + n.dur), 0, mido.Message("note_off", channel=ch, note=n.pitch)))
        for beat, cc, v in part.ccs:
            events.append((_tick(beat), 1, mido.Message("control_change", channel=ch, control=cc, value=v)))
        # 末尾放一个无害事件，确保渲染器把混响/释音尾巴也渲染出来
        events.append((end, 1, mido.Message("control_change", channel=ch, control=7, value=100)))
        _finish(track, events, end)
        mid.tracks.append(track)

    mid.save(path)

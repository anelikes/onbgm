"""音色库：同一个音色名在不同音源下的实现方式，以及每件乐器的混音参数。

乐谱只写 `sound: strings`；用 GeneralUser GS（gm）还是高质量采样（hq）在这里决定，
hq 缺哪件乐器就自动退回 gm，所以没下载大音源也能跑。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pedalboard import HighpassFilter, HighShelfFilter, PeakFilter

from .sampler import Instrument, load_instrument, pitched_hits, unpitched

SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"
GM_SF2 = "GeneralUser-GS.sf2"


@dataclass(frozen=True)
class SF2:
    file: str
    program: int | None = None  # None 表示沿用 GM 音色号


@dataclass
class Mix:
    pan: float = 0.0  # -1 左 … 1 右（乐团座位）
    width: float = 1.0  # 立体声宽度，0 为单声道
    eq: list = field(default_factory=list)


# 乐团座位：小提琴在左，中提琴居中偏右，大提琴和低音提琴在右，打击乐在后方
MIX = {
    "piano": Mix(0.0, 0.9, [HighpassFilter(35), HighShelfFilter(9000, -1.5)]),
    "strings": Mix(-0.3, 0.7, [HighpassFilter(160), PeakFilter(2800, -1.5, 1.0), HighShelfFilter(7000, -3)]),
    "strings-slow": Mix(0.0, 1.0, [HighpassFilter(70), PeakFilter(320, -2.5, 0.8), HighShelfFilter(7000, -3)]),
    "cello": Mix(0.3, 0.6, [HighpassFilter(55), PeakFilter(300, -1.5, 1.0)]),
    "contrabass": Mix(0.35, 0.4, [HighpassFilter(30), PeakFilter(220, -2, 1.0)]),
    "pizzicato": Mix(0.3, 0.5, [HighpassFilter(40)]),
    "timpani": Mix(-0.15, 0.6, [HighpassFilter(35)]),
    "drums": Mix(0.15, 0.8, [HighpassFilter(250)]),
}


def mix_for(sound: str) -> Mix:
    return MIX.get(sound, Mix())


def _vel_layer(f: Path) -> int:
    return int(re.search(r"_v(\d+)", f.name).group(1))


def _dyn_layer(f: Path) -> int:
    return {"pp": 0, "mp": 1, "mf": 2, "ff": 3}[re.search(r"_(pp|mp|mf|ff)_", f.name).group(1)]


@lru_cache(maxsize=None)
def _hq_instruments(root: Path) -> dict[str, Instrument]:
    v = root / "vsco2"
    s = v / "Strings"
    insts = {
        "violins": load_instrument("violins", s / "Violin Section/susVib", pan=-0.35, release=0.3),
        "violas": load_instrument("violas", s / "Viola Section/susvib", pan=0.1, release=0.3),
        "celli": load_instrument("celli", s / "Cello Section/susvib", pan=0.3, release=0.3),
        "basses": load_instrument("basses", s / "Solo Contrabass/SusVib", release=0.3),
        "cello-pizz": load_instrument("cello-pizz", s / "Cello Section/pizzT", sustain=False),
        "bass-pizz": load_instrument("bass-pizz", s / "Solo Contrabass/Pizz", sustain=False),
        "timpani": pitched_hits("timpani", sorted((v / "Percussion/Timpani").glob("Timpani*_Hit_*.wav")),
                                layer_of=_vel_layer),
        "timpani-roll": pitched_hits("timpani-roll", sorted((v / "Percussion/Timpani/Rolls").glob("*.wav")),
                                     layer_of=_vel_layer, sustain=True, release=0.5),
        "crash": unpitched("crash", sorted((v / "Percussion").glob("cymbal-crash1_*.wav")), layer_of=_dyn_layer),
        "glock": load_instrument("glock", v / "Percussion/Glock", sustain=False),
    }
    if insts["timpani"] and insts["timpani-roll"]:
        insts["timpani"].articulations = {"roll": insts["timpani-roll"]}
    # 断奏改用跳弓（spiccato）采样：长音采样起音慢、释音长，快速短音会糊成一片
    for legato, folder in [("violins", "Violin Section/Spic"), ("violas", "Viola Section/spic"),
                           ("celli", "Cello Section/spic"), ("basses", "Solo Contrabass/Spic")]:
        spic = load_instrument(f"{legato}-spic", s / folder, sustain=False, line_choke=True,
                               pan=insts[legato].pan if insts[legato] else 0.0)
        if insts[legato] and spic:
            insts[legato].articulations = {"staccato": spic}
    return {k: i for k, i in insts.items() if i is not None}


# 高质量实现：SF2 文件，或采样乐器名，或 [(最高音, 乐器名), ...] 按音区分给不同乐器
HQ = {
    "piano": SF2("SalamanderGrandPiano.sf2", 0),
    "strings": "violins",
    "strings-slow": [(55, "celli"), (62, "violas"), (127, "violins")],  # 铺底按音区分给三个声部
    "cello": "celli",
    "contrabass": "basses",
    "pizzicato": [(35, "bass-pizz"), (127, "cello-pizz")],
    "timpani": "timpani",
    "drums": {49: "crash", 57: "crash"},
    "glockenspiel": "glock",
}


def resolve(sound: str, soundset: str, pitches: set[int] = frozenset(), root: Path = SOUNDS_DIR):
    """返回 SF2(...)，或 route(pitch) -> Instrument。鼓组只有用到的每件鼓都有采样时才用采样。"""
    if soundset == "hq" and sound in HQ:
        spec = HQ[sound]
        if isinstance(spec, SF2):
            if (root / spec.file).exists():
                return spec
        else:
            insts = _hq_instruments(root)
            if isinstance(spec, str) and spec in insts:
                return lambda pitch, i=insts[spec]: i
            if isinstance(spec, list) and all(name in insts for _, name in spec):
                return lambda pitch: insts[next(name for top, name in spec if pitch <= top)]
            if isinstance(spec, dict) and pitches <= set(spec) and all(name in insts for name in spec.values()):
                return lambda pitch: insts[spec.get(pitch, next(iter(spec.values())))]
    return SF2(GM_SF2)

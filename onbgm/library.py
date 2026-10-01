"""音色库：同一个音色名在不同音源下的实现方式，以及每件乐器的混音参数。

乐谱只写 `sound: strings`；用 GeneralUser GS（gm）还是高质量采样（hq）在这里决定，
hq 缺哪件乐器就自动退回 gm，所以没下载大音源也能跑。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pedalboard import HighpassFilter, HighShelfFilter, PeakFilter

from .sampler import Instrument, load_instrument, load_sfz, pitched_hits, unpitched

def _sounds_dir() -> Path:
    """音源目录：环境变量 ONBGM_SOUNDS 优先；在源码仓库里开发时用仓库下的 sounds/；
    作为工具安装（uv tool install 等）时用 ~/.onbgm/sounds。"""
    if env := os.environ.get("ONBGM_SOUNDS"):
        return Path(env).expanduser()
    repo = Path(__file__).resolve().parent.parent
    if (repo / "pyproject.toml").exists():
        return repo / "sounds"
    return Path.home() / ".onbgm" / "sounds"


SOUNDS_DIR = _sounds_dir()
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
    delay_beats: float | None = None  # 跟着速度走的延迟（拍），如 0.75 = 附点八分
    delay_feedback: float = 0.3
    delay_mix: float = 0.2


# 乐团座位：小提琴在左，中提琴居中偏右，大提琴和低音提琴在右，打击乐在后方
MIX = {
    "piano": Mix(0.0, 0.9, [HighpassFilter(35), HighShelfFilter(9000, -1.5)]),
    "strings": Mix(-0.3, 0.7, [HighpassFilter(160), PeakFilter(2800, -1.5, 1.0), HighShelfFilter(7000, -3)]),
    "strings-slow": Mix(0.0, 1.0, [HighpassFilter(70), PeakFilter(320, -2.5, 0.8), HighShelfFilter(7000, -3)]),
    "cello": Mix(0.3, 0.6, [HighpassFilter(55), PeakFilter(300, -1.5, 1.0)]),
    "contrabass": Mix(0.35, 0.4, [HighpassFilter(30), PeakFilter(220, -2, 1.0)]),
    "pizzicato": Mix(0.3, 0.5, [HighpassFilter(40)]),
    "timpani": Mix(-0.15, 0.6, [HighpassFilter(35)]),
    "drums": Mix(0.15, 0.8, [HighpassFilter(250)]),  # 单独的镲轨（crash 织体），管弦乐里的位置
    "drum-kit": Mix(0.0, 0.9, [HighpassFilter(30)]),  # 完整鼓组：每件鼓的左右位置由采样库定
    "synth-pluck": Mix(0.0, 0.8, [HighpassFilter(120)], delay_beats=0.75, delay_feedback=0.35, delay_mix=0.25),
    "synth-pad": Mix(0.0, 1.0, [HighpassFilter(150), PeakFilter(400, -2, 0.8)]),
    "supersaw": Mix(0.0, 1.0, [HighpassFilter(150)]),
    "synth-lead": Mix(0.0, 0.5, [HighpassFilter(150)], delay_beats=0.75, delay_feedback=0.25, delay_mix=0.15),
    "synth-bass": Mix(0.0, 0.2, [HighpassFilter(30)]),
    "sub-bass": Mix(0.0, 0.0, [HighpassFilter(25)]),
    "bass-808": Mix(0.0, 0.0, [HighpassFilter(25)]),
    "synth-bell": Mix(0.1, 0.8, [HighpassFilter(200)], delay_beats=0.75, delay_feedback=0.3, delay_mix=0.2),
    "fm-epiano": Mix(0.0, 0.7, [HighpassFilter(60)]),
    "drums-synth": Mix(0.0, 1.0, [HighpassFilter(25)]),
}


KIT_SOUNDS = ("drums", "drums-room", "drums-jazz")


def mix_for(sound: str, texture: str = "") -> Mix:
    if sound in KIT_SOUNDS and texture == "groove":
        return MIX["drum-kit"]
    return MIX.get(sound, Mix())


def _vel_layer(f: Path) -> int:
    return int(re.search(r"_v(\d+)", f.name).group(1))


def _dyn_layer(f: Path) -> int:
    return {"pp": 0, "mp": 1, "mf": 2, "ff": 3}[re.search(r"_(pp|mp|mf|ff)_", f.name).group(1)]


# 我们的鼓名（GM 键位）→ AVL 鼓组的键位。AVL 只有一个落地嗵鼓和一个嗵鼓：高嗵鼓用嗵鼓升 3 个半音；
# 沙锤和卡巴萨都用库里的沙锤。conga 没有，用到时整套鼓退回 GM。
AVL_KIT = {36: 36, 37: 37, 38: 38, 39: 39, 42: 42, 44: 44, 46: 46, 45: 41, 47: 45, 50: (45, 3),
           49: 49, 51: 51, 53: 53, 54: 54, 56: 56, 69: 61, 70: 61}


def _sfz_instruments(root: Path) -> dict[str, Instrument]:
    from .fetch import SFZ_LIBS, sfz_path

    libs = {lib.name: sfz_path(lib, root) for lib in SFZ_LIBS}
    insts = {
        "jrhodes": load_sfz("jrhodes", libs["jrhodes"]),
        "fss-guitar": load_sfz("fss-guitar", libs["fss-guitar"]),
        # 这个库按贝斯的记谱习惯映射（比实际发声高一个八度），我们的音高是实际发声的音高
        "black-and-blue": load_sfz("black-and-blue", libs["black-and-blue"], key_offset=-12),
        "black-pearl": load_sfz("black-pearl", libs["black-pearl"], remap=AVL_KIT, kit=True),
        "blonde-bop": load_sfz("blonde-bop", libs["blonde-bop"], remap=AVL_KIT, kit=True),
    }
    return {k: i for k, i in insts.items() if i is not None}


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
    return {k: i for k, i in insts.items() if i is not None} | _sfz_instruments(root)


# 高质量实现：SF2 文件，或采样乐器名，或 [(最高音, 乐器名), ...] 按音区分给不同乐器，
# 或 {音高: 乐器名}（只有用到的音高都在表里时才用），或 (方案, 方案, ...) 依次尝试
HQ = {
    "piano": SF2("SalamanderGrandPiano.sf2", 0),
    "strings": "violins",
    "strings-slow": [(55, "celli"), (62, "violas"), (127, "violins")],  # 铺底按音区分给三个声部
    "cello": "celli",
    "contrabass": "basses",
    "pizzicato": [(35, "bass-pizz"), (127, "cello-pizz")],
    "timpani": "timpani",
    "drums": ({49: "crash", 57: "crash"}, "black-pearl"),  # 只有镲时用管弦乐的镲，否则用整套鼓
    "drums-room": "black-pearl",
    "drums-jazz": "blonde-bop",
    "glockenspiel": "glock",
    "epiano": "jrhodes",
    "guitar-steel": "fss-guitar",
    "electric-bass": "black-and-blue",
}


def _route(spec, insts: dict[str, Instrument], pitches: set[int]):
    """按 HQ 里的写法返回 route(pitch) -> Instrument；需要的乐器不全、或鼓组缺用到的鼓时返回 None。"""
    if isinstance(spec, tuple):
        return next((r for s in spec if (r := _route(s, insts, pitches)) is not None), None)
    if isinstance(spec, str) and spec in insts:
        inst = insts[spec]
        if inst.kit and not all(any(z.keys[0] <= p <= z.keys[1] for z in inst.zones) for p in pitches):
            return None
        return lambda pitch, i=inst: i
    if isinstance(spec, list) and all(name in insts for _, name in spec):
        return lambda pitch: insts[next(name for top, name in spec if pitch <= top)]
    if isinstance(spec, dict) and pitches <= set(spec) and all(name in insts for name in spec.values()):
        return lambda pitch: insts[spec.get(pitch, next(iter(spec.values())))]
    return None


def resolve(sound: str, soundset: str, pitches: set[int] = frozenset(), root: Path = SOUNDS_DIR):
    """返回 SF2(...)、合成器音色（synth.Preset / synth.Kit），或 route(pitch) -> Instrument。
    合成器音色不需要下载，任何档位都直接合成；鼓组只有用到的每件鼓都有采样时才用采样。"""
    from . import synth

    if sound in synth.PRESETS:
        return synth.PRESETS[sound]
    if sound == synth.KIT.name:
        return synth.KIT
    if soundset == "hq" and sound in HQ:
        spec = HQ[sound]
        if isinstance(spec, SF2):
            if (root / spec.file).exists():
                return spec
        elif (route := _route(spec, _hq_instruments(root), set(pitches))) is not None:
            return route
    return SF2(GM_SF2)

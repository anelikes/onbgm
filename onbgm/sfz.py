"""SFZ 解析：免费乐器采样库大多是 SFZ 格式（一个文本映射文件 + 一堆音频）。

只实现常用的那部分规范，足够读大多数免费库：
- 头：<control>（default_path）、<global>、<master>、<group>、<region>，下层继承上层的设定；
- #define 变量替换和 #include；
- 区域选择：key / lokey / hikey、lovel / hivel、pitch_keycenter；轮替：seq_length / seq_position、lorand / hirand；
- 音量和音高：volume（dB）、amplitude（%）、amp_veltrack、tune（音分）、transpose、pitch_keytrack；
- 播放：offset、loop_mode（one_shot / no_loop / loop_continuous）、ampeg_release、trigger（只用 attack，跳过 release 采样）；
- 互斥：group / off_by（踩镲闭合时切断开镲）。
"""
from __future__ import annotations

import re
from pathlib import Path

_NOTE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def note_number(value: str) -> int:
    """SFZ 的音高可以写数字或音名（c4 = 60，c#4、db4 都可以）。"""
    value = str(value).strip().lower()
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    m = re.fullmatch(r"([a-g])([#b]?)(-?\d)", value)
    if not m:
        raise ValueError(f"无法解析音高 {value!r}")
    return 12 * (int(m.group(3)) + 1) + _NOTE[m.group(1)] + {"#": 1, "b": -1, "": 0}[m.group(2)]


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def _preprocess(path: Path, defines: dict[str, str], depth: int = 0) -> str:
    """展开 #include 和 #define（变量以 $ 开头）。"""
    if depth > 8:
        raise ValueError("SFZ #include 嵌套太深")
    out = []
    for line in _strip_comments(path.read_text(encoding="utf-8", errors="ignore")).splitlines():
        stripped = line.strip()
        if m := re.match(r'#define\s+(\$\w+)\s+(.+)', stripped):
            defines[m.group(1)] = m.group(2).strip()
            continue
        if m := re.match(r'#include\s+"([^"]+)"', stripped):
            out.append(_preprocess(path.parent / m.group(1).replace("\\", "/"), defines, depth + 1))
            continue
        for name in sorted(defines, key=len, reverse=True):
            line = line.replace(name, defines[name])
        out.append(line)
    return "\n".join(out)


def _opcodes(chunk: str) -> dict[str, str]:
    """把 'a=1 sample=foo bar.wav b=2' 拆成字典；sample 的值可以含空格，一直到下一个 opcode。"""
    out = {}
    parts = re.split(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", chunk)
    for i in range(1, len(parts) - 1, 2):
        out[parts[i]] = parts[i + 1].strip()
    return out


def parse(path: Path) -> list[dict]:
    """返回每个 region 合并了上层设定之后的 opcode 字典，并补上 _sample（音频的绝对路径）。"""
    text = _preprocess(path, {})
    levels = {"global": {}, "master": {}, "group": {}}
    control: dict[str, str] = {}
    regions = []
    for header, body in re.findall(r"<(\w+)>([^<]*)", text):
        ops = _opcodes(body)
        if header == "control":
            control.update(ops)
        elif header in ("global", "master", "group"):
            levels[header] = ops
            if header == "global":
                levels["master"], levels["group"] = {}, {}
            elif header == "master":
                levels["group"] = {}
        elif header == "region":
            merged = {**{k: v for k, v in control.items() if k.startswith("set_cc")},
                      **levels["global"], **levels["master"], **levels["group"], **ops}
            if "sample" not in merged:
                continue
            sample = (control.get("default_path", "") + merged["sample"]).replace("\\", "/")
            merged["_sample"] = str((path.parent / sample).resolve())
            regions.append(merged)
    return regions


def playable(regions: list[dict]) -> list[dict]:
    """只留下按下琴键时发声的区域，并按控制器的默认状态过滤：
    - trigger=release 之类（松键声、连奏过渡）不用；
    - 有键位开关（sw_last）的库，只用默认那一组演奏法；
    - 按控制器值分组的区域（如踏板踩下 / 抬起两套采样），只用控制器默认值（0，或 set_ccN）对应的那套。"""
    regions = [r for r in regions if r.get("trigger", "attack") in ("attack", "first")]
    switches = sorted({note_number(r["sw_last"]) for r in regions if "sw_last" in r})
    if switches:
        default = next((note_number(r["sw_default"]) for r in regions if "sw_default" in r), switches[0])
        regions = [r for r in regions if "sw_last" not in r or note_number(r["sw_last"]) == default]
    out = []
    for r in regions:
        ok = True
        for op, value in r.items():
            if m := re.fullmatch(r"(lo|hi)cc(\d+)", op):
                cc = float(r.get(f"set_cc{m.group(2)}", 0))
                ok &= cc >= float(value) if m.group(1) == "lo" else cc <= float(value)
        if ok:
            out.append(r)
    return out

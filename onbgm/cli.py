"""命令行：onbgm check <乐谱> / onbgm render <乐谱>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyloudnorm as pyln

from .analyze import balance_report, loudness_report, pianoroll, timeline
from .arrange import arrange
from .fetch import fetch_all
from .library import SOUNDS_DIR
from .render import SR, render
from .schema import score_schema
from .score import ScoreError, load


def main() -> None:
    ap = argparse.ArgumentParser(prog="onbgm", description="用文本乐谱精确控制每个音符的 BGM 引擎")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("check", "只校验乐谱、打印时间轴"), ("render", "渲染成音频")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("score")
    sub.choices["render"].add_argument("-o", "--out", help="输出目录（默认 out/<乐谱名>）")
    sub.choices["render"].add_argument("--soundset", choices=["hq", "gm"], default="hq",
                                       help="hq：高质量采样（缺的乐器自动退回 gm）；gm：只用 GeneralUser GS")
    sub.add_parser("fetch", help="下载音源到 sounds/")
    sub.add_parser("schema", help="输出乐谱格式的 JSON Schema")
    args = ap.parse_args()

    if args.cmd == "fetch":
        fetch_all(SOUNDS_DIR)
        return
    if args.cmd == "schema":
        print(json.dumps(score_schema(), ensure_ascii=False, indent=2))
        return

    try:
        score = load(args.score)
        parts, warnings = arrange(score)
    except ScoreError as e:
        print(f"乐谱有问题:\n{e}", file=sys.stderr)
        sys.exit(1)

    print(timeline(score))
    for w in warnings:
        print(f"  ⚠ {w}")
    if args.cmd == "check":
        return

    out = Path(args.out or Path("out") / Path(args.score).stem)
    result = render(score, parts, out, args.soundset)
    pianoroll(score, parts, out / "pianoroll.png")
    print("\n音源:")
    for name, src in result.sources.items():
        print(f"  {name:<8} {src}")
    gains = [x for x in result.notes if not x.startswith("⚠")]
    if gains:
        print("自动混音: " + "，".join(gains))
    for x in result.notes:
        if x.startswith("⚠"):
            print(x)
    print(f"整体响度: {pyln.Meter(SR).integrated_loudness(result.main):.1f} LUFS，时长 {len(result.main) / SR:.3f} 秒")
    print()
    print(loudness_report(score, result.main))
    loudest = max(score.sections, key=lambda s: sum(s.energy))
    print(balance_report(score, result.stems, parts, loudest.name))
    print(f"\n输出: {out}/")
    for f in result.files + [out / "pianoroll.png"]:
        print(f"  {f.name}")


if __name__ == "__main__":
    main()

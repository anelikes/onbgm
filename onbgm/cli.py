"""命令行。每个命令都支持 --json：输出一个 JSON 对象，方便 agent 程序化处理。

退出码：0 成功；1 乐谱有问题；2 环境有问题（缺 fluidsynth、音源等）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import catalog
from .analyze import (balance_data, balance_report, locate, loudness_data, loudness_report, pianoroll,
                      timeline, timeline_data)
from .arrange import arrange
from .fetch import VSCO_PARTS, fetch_all
from .library import GM_SF2, SOUNDS_DIR
from .schema import score_schema
from .score import Problem, ScoreError, load, parse_time

EXAMPLES = Path(__file__).resolve().parent / "examples"
TEMPLATE = """\
title: My BGM
meta:
  bpm: 96
  key: C major
  loop: true                   # true 输出 loop.wav；false 输出 song.wav

sections:
  - { name: intro, bars: 4, energy: 0.3 }
  - { name: main,  bars: 8, energy: "0.5->0.8" }

harmony:
  intro: "Cmaj7 | Am7 | Dm7 | G7"
  main:  "Cmaj7 | Am7 | Dm7 | G7"

tracks:
  keys:
    sound: epiano
    texture: comp
    rhythm: "x--- --x- ---- ----"
  lead:
    sound: vibraphone
    notes:
      main: "E5/4 G5/4 C6/2 | B5/2 G5/2 | A5/4 F5/4 D5/2 | r/1"
"""


def _emit(data: dict, as_json: bool, text: str, code: int = 0) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif text:
        print(text, file=sys.stderr if code else sys.stdout)
    sys.exit(code)


def _fail(problems: list[Problem], as_json: bool, code: int = 1) -> None:
    body = "\n".join(f"- {p}" for p in problems)
    _emit({"ok": False, "problems": [p.to_dict() for p in problems]}, as_json,
          f"乐谱有问题:\n{body}" if code == 1 else body, code)


def _load(path: str, as_json: bool):
    try:
        score = load(path)
        parts, warnings = arrange(score)
    except ScoreError as e:
        _fail(e.problems, as_json)
    except FileNotFoundError:
        _fail([Problem("file_not_found", "", f"找不到乐谱文件 {path}")], as_json)
    return score, parts, warnings


# ---------- 各命令 ----------

def cmd_check(args) -> None:
    score, parts, warnings = _load(args.score, args.json)
    text = timeline(score) + "".join(f"\n  ⚠ {w}" for w in warnings)
    _emit({"ok": True, "timeline": timeline_data(score), "warnings": [w.to_dict() for w in warnings]}, args.json, text)


def cmd_timeline(args) -> None:
    score, _, _ = _load(args.score, args.json)
    data = timeline_data(score)
    lines = [timeline(score)]
    if args.at:
        data["at"] = [locate(score, parse_time(t)) for t in args.at]
        for x in data["at"]:
            lines.append(f"  {x['time_s']}s → " + (x.get("error") or x.get("note") if "bar" not in x else
                         f"第 {x['bar']} 小节第 {x['beat']:g} 拍（{x['section']} 段第 {x['bar_in_section']} 小节），和弦 {x['chord']}"))
    else:
        lines.append("和弦:")
        for c in data["chords"]:
            lines.append(f"  第 {c['bar']:>2} 小节第 {c['beat']:g} 拍  {c['start_s']:7.2f}s  {c['chord']:<8} ({c['section']})")
    _emit(data, args.json, "\n".join(lines))


def cmd_render(args) -> None:
    from .render import SR, render

    score, parts, warnings = _load(args.score, args.json)
    out = Path(args.out or Path("out") / Path(args.score).stem)
    try:
        result = render(score, parts, out, args.soundset)
    except RuntimeError as e:
        _fail([Problem("missing_dependency", "", str(e))], args.json, code=2)
    pianoroll(score, parts, out / "pianoroll.png")
    files = {k: str(v) for k, v in result.files.items()} | {"pianoroll": str(out / "pianoroll.png")}
    loud = loudness_data(score, result.main)
    loudest = max(score.sections, key=lambda s: sum(s.energy)).name
    all_warnings = warnings + result.warnings
    data = {
        "ok": True,
        "files": files,
        "duration_s": round(len(result.main) / SR, 3),
        "loop": score.loop,
        "lufs": loud["integrated_lufs"],
        "timeline": timeline_data(score),
        "sources": result.sources,
        "automix": result.mix,
        "loudness": loud,
        "balance": [{"section": s.name, "tracks": balance_data(score, result.stems, parts, s.name)}
                    for s in score.sections],
        "warnings": [w.to_dict() for w in all_warnings],
    }
    lines = [timeline(score), "", "音源:"] + [f"  {k:<8} {v}" for k, v in result.sources.items()]
    if result.mix:
        lines.append("自动混音: " + "，".join(f"{m['track']}@{m['section']} {m['gain_db']:+.1f} dB" for m in result.mix))
    lines += [f"⚠ {w}" for w in all_warnings]
    lines += [f"整体响度: {loud['integrated_lufs']:.1f} LUFS，时长 {data['duration_s']:.3f} 秒", "",
              loudness_report(score, result.main), balance_report(score, result.stems, parts, loudest), "",
              f"输出: {out}/"] + [f"  {k:<9} {Path(v).name}" for k, v in files.items()]
    _emit(data, args.json, "\n".join(lines))


def cmd_catalog(args) -> None:
    found = catalog.search(args.query, args.kind, args.limit)
    if args.query is None and args.kind is None:
        by_kind: dict[str, list[str]] = {}
        for x in found:
            by_kind.setdefault(x["kind"], []).append(x["name"])
        text = "\n".join(f"{k:<12} {', '.join(v)}" for k, v in by_kind.items())
        text += "\n\n用法: onbgm catalog -q \"慵懒的 lo-fi 鼓\"   或   onbgm catalog --kind progression"
    else:
        text = catalog.format_items(found) or "没有找到。换个说法，或用 --kind 浏览某一类。"
    _emit({"items": found}, args.json, text)


def cmd_example(args) -> None:
    files = {p.stem: p for p in sorted(EXAMPLES.glob("*.yaml"))}
    if args.name is None:
        rows = [{"name": x["name"], "description": x["description"]} for x in catalog.search(kind="example")]
        _emit({"examples": rows}, args.json,
              "\n".join(f"{r['name']:<16} {r['description']}" for r in rows) + "\n\n用法: onbgm example <名字> > my_score.yaml")
    if args.name not in files:
        _fail([Problem("unknown_value", "", f"没有示例 {args.name!r}（可用: {', '.join(files)}）")], args.json)
    text = files[args.name].read_text(encoding="utf-8")
    _emit({"name": args.name, "score": text}, args.json, text.rstrip("\n"))


def cmd_init(args) -> None:
    target = Path(args.dir)
    score_path = target / "score.yaml"
    if score_path.exists() and not args.force:
        _fail([Problem("file_exists", "", f"{score_path} 已存在（要覆盖就加 --force）")], args.json)
    if args.example:
        src = EXAMPLES / f"{args.example}.yaml"
        if not src.exists():
            names = ", ".join(p.stem for p in sorted(EXAMPLES.glob("*.yaml")))
            _fail([Problem("unknown_value", "", f"没有示例 {args.example!r}（可用: {names}）")], args.json)
        text = src.read_text(encoding="utf-8")
    else:
        text = TEMPLATE
    target.mkdir(parents=True, exist_ok=True)
    score_path.write_text(text, encoding="utf-8")
    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("out/\n", encoding="utf-8")
    steps = [f"onbgm check {score_path}", f"onbgm render {score_path} -o {target / 'out' / 'v1'}"]
    _emit({"ok": True, "score": str(score_path), "next": steps}, args.json,
          f"已创建 {score_path}\n下一步:\n" + "\n".join(f"  {s}" for s in steps))


def doctor_checks() -> list[dict]:
    checks = []

    def add(name, ok, detail, fix, required=True):
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail, "fix": None if ok else fix})

    add("python", sys.version_info >= (3, 10), sys.version.split()[0], "需要 Python 3.10 或更新版本")
    fs = shutil.which("fluidsynth")
    version = ""
    if fs:
        run = subprocess.run([fs, "--version"], capture_output=True, text=True)
        lines = (run.stdout or run.stderr).strip().splitlines()
        version = lines[0] if lines else fs
    add("fluidsynth", fs, version or "未找到",
        "macOS: brew install fluid-synth；Debian/Ubuntu: sudo apt install fluidsynth；Windows: https://www.fluidsynth.org")
    add("sounds_dir", SOUNDS_DIR.exists(), str(SOUNDS_DIR), "运行 onbgm fetch 下载音源（也可以用环境变量 ONBGM_SOUNDS 指定目录）")
    add("gm_soundfont", (SOUNDS_DIR / GM_SF2).exists(), GM_SF2, "运行 onbgm fetch")
    add("hq_piano", (SOUNDS_DIR / "SalamanderGrandPiano.sf2").exists(), "Salamander Grand Piano",
        "运行 onbgm fetch（缺少时钢琴退回 GM）", required=False)
    for part in VSCO_PARTS:
        base = SOUNDS_DIR / "vsco2" / part
        found = list(base.glob("*.wav")) if part.endswith("/") else list(base.parent.glob(base.name + "*.wav"))
        add(f"hq_{part.strip('/').replace(' ', '_').lower()}", found, f"{len(found)} 个采样",
            "运行 onbgm fetch（缺少时这件乐器退回 GM）", required=False)
    return checks


def cmd_doctor(args) -> None:
    checks = doctor_checks()
    ready = all(c["ok"] for c in checks if c["required"])
    hq = all(c["ok"] for c in checks)
    lines = [f"{'✓' if c['ok'] else '✗'} {c['name']:<36} {c['detail']}" + (f"\n    → {c['fix']}" if c["fix"] else "")
             for c in checks]
    lines.append("\n" + ("可以渲染，高质量音源齐全" if hq else "可以渲染，但缺少部分高质量音源，相应乐器会退回 GM" if ready
                         else "还不能渲染：先处理上面标 ✗ 的必需项"))
    _emit({"ok": ready, "hq_complete": hq, "sounds_dir": str(SOUNDS_DIR), "checks": checks}, args.json,
          "\n".join(lines), 0 if ready else 2)


def cmd_schema(args) -> None:
    print(json.dumps(score_schema(), ensure_ascii=False, indent=2))


def cmd_fetch(args) -> None:
    fetch_all(SOUNDS_DIR)


def main() -> None:
    ap = argparse.ArgumentParser(prog="onbgm", description="给 agent 用的纯器乐 BGM 引擎：文本乐谱 → 音频")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        if name not in ("schema", "fetch"):
            p.add_argument("--json", action="store_true", help="以 JSON 输出")
        return p

    add("check", cmd_check, "校验乐谱，打印时间轴").add_argument("score")
    p = add("timeline", cmd_timeline, "时间轴：每段的起止时间、每小节的和弦；--at 查某个时间点在第几小节")
    p.add_argument("score")
    p.add_argument("--at", action="append", help="时间点，如 23.4 或 0:23.4，可重复")
    p = add("render", cmd_render, "渲染成音频")
    p.add_argument("score")
    p.add_argument("-o", "--out", help="输出目录（默认 out/<乐谱名>）")
    p.add_argument("--soundset", choices=["hq", "gm"], default="hq",
                   help="hq：高质量采样（缺的乐器自动退回 gm）；gm：只用 GeneralUser GS")
    p = add("catalog", cmd_catalog, "搜索现成的写法（鼓组、扫弦、低音、和弦伴奏、和弦进行）和可用的构件")
    p.add_argument("-q", "--query", help="用自然语言描述，如 \"慵懒的 lo-fi 鼓\"")
    p.add_argument("--kind", choices=catalog.KINDS, help="只看某一类")
    p.add_argument("--limit", type=int, default=8)
    add("example", cmd_example, "列出示例乐谱，或输出某个示例").add_argument("name", nargs="?")
    p = add("init", cmd_init, "创建项目骨架（score.yaml 和 .gitignore）")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--example", help="用某个示例作为起点")
    p.add_argument("--force", action="store_true", help="覆盖已有的 score.yaml")
    add("doctor", cmd_doctor, "检查环境和音源是否就绪")
    add("schema", cmd_schema, "输出乐谱格式的 JSON Schema")
    add("fetch", cmd_fetch, "下载音源")
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

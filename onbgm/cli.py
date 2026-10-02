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
from .score import Problem, ScoreError, fmt_time, load, parse_time

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
    from .render import ALL_OUTPUTS, SR, render, render_slot

    score, parts, warnings = _load(args.score, args.json)
    out = Path(args.out or Path("out") / Path(args.score).stem)
    try:
        with render_slot():
            result = render(score, parts, out, args.soundset, outputs=frozenset() if args.draft else ALL_OUTPUTS)
    except RuntimeError as e:
        _fail([Problem("missing_dependency", "", str(e))], args.json, code=2)
    pianoroll(score, parts, out / "pianoroll.png")
    files = {k: str(v) for k, v in result.files.items()} | {"pianoroll": str(out / "pianoroll.png")}
    if not args.draft:
        from .player import player_data, write_player

        player = write_player(out, player_data(score, parts, out / ("loop.wav" if score.loop else "song.wav"),
                                               len(result.main) / SR, args.score))
        files |= {k: str(v) for k, v in player.items()}
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
        "sfx": result.sfx,
    }
    lines = [timeline(score), "", "音源:"] + [f"  {k:<8} {v}" for k, v in result.sources.items()]
    if result.mix:
        lines.append("自动混音: " + "，".join(f"{m['track']}@{m['section']} {m['gain_db']:+.1f} dB" for m in result.mix))
    lines += [f"⚠ {w}" for w in all_warnings]
    if result.sfx:
        lines.append("音效音量（比当时的音乐响多少）: " + "，".join(
            f"{x['type']}@{x['at_s']:g}s {x['vs_music_db']:+.1f} dB" for x in result.sfx))
    lines += [f"整体响度: {loud['integrated_lufs']:.1f} LUFS，时长 {data['duration_s']:.3f} 秒", "",
              loudness_report(score, result.main), balance_report(score, result.stems, parts, loudest), "",
              f"输出: {out}/"] + [f"  {k:<9} {Path(v).name}" for k, v in files.items()]
    _emit(data, args.json, "\n".join(lines))


def cmd_fit(args) -> None:
    from .fit import Cue, fit

    try:
        cues = []
        for i, raw in enumerate(args.cues):
            t, _, name = raw.partition("=")
            cues.append(Cue(parse_time(t), (name or f"s{i + 2}").strip().replace(" ", "_")))
        length = parse_time(args.length) if args.length else None
        hits = [parse_time(h) for h in args.hit]
        lo, _, hi = args.bpm.partition("-")
        bpm_range = (float(lo), float(hi or lo))
    except ValueError as e:
        _fail([Problem("bad_format", "", f"{e}（节点写成 0:08 或 0:08=drop；速度范围写成 100-130）")], args.json)
    times = [c.seconds for c in cues]
    if times != sorted(times) or len(set(times)) != len(times) or (times and times[0] <= 0) or \
            (length and times and times[-1] >= length):
        _fail([Problem("out_of_range", "", "节点要按时间递增、大于 0、且都在总时长之内")], args.json)
    result = fit(cues, length, bpm_range, hits, args.top, args.ending_tempo)
    plans = result["plans"]
    if args.write and plans:
        Path(args.write).write_text(plans[0]["skeleton"], encoding="utf-8")
    lines = []
    for i, p in enumerate(plans, 1):
        changes = sum(1 for s in p["sections"] if s["tempo"] is not None)
        lines.append(f"方案 {i}：主速度 {p['bpm']:g} BPM，" + (
            f"{changes} 段单独设速度，相邻段落最大速度跳变 {p['max_jump_pct']:g}%" if changes
            else "所有节点都在同一个速度下正好落在小节线上"))
        for s in p["sections"]:
            extra = f"  速度 {s['tempo']:g}" if s["tempo"] is not None else ""
            if s["jump_pct"]:
                extra += f"（比上一段{'快' if s['jump_pct'] > 0 else '慢'} {abs(s['jump_pct']):g}%）"
            if s["ramp_to"]:
                extra += f"  渐慢到 {s['ramp_to']:g} BPM"
            if s["tail_s"] > 0.01:
                extra += f"  之后余音 {s['tail_s']:g} 秒"
            lines.append(f"  {s['name']:<10} {fmt_time(s['start_s'])}–{fmt_time(s['end_s'])}  {s['bars']:>2} 小节{extra}")
        for h in p["hits"]:
            where = {"downbeat": "小节第一拍", "beat": "拍上", "eighth": "半拍上", "off": "不在拍上（音效照样精确，只是不在节奏点上）"}
            lines.append(f"  音效点 {fmt_time(h['time_s'])} → 第 {h['bar']} 小节第 {h['beat']:g} 拍，{where[h['on']]}")
    if result["suggestion"]:
        lines.insert(0, f"提示：{result['suggestion']}\n")
    if plans:
        lines += ["", "方案 1 的乐谱骨架" + (f"（已写入 {args.write}）" if args.write else "（加 --write 文件名 可以直接写出）") + "：",
                  plans[0]["skeleton"]]
    _emit({"plans": plans, "suggestion": result["suggestion"], "written": args.write if args.write and plans else None},
          args.json, "\n".join(lines))


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
    from .fetch import SAMPLE_LIBS, lib_path
    for lib in SAMPLE_LIBS:
        add(f"hq_{lib.name}", lib_path(lib, SOUNDS_DIR).exists(), f"{lib.title}：{lib.used_for}",
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


def cmd_review(args) -> None:
    from .player import DATA_FILE, ReviewServer, load_comments

    dirs = [Path(d) for d in [args.out] + ([args.compare] if args.compare else [])]
    for d in dirs:
        if not (d / DATA_FILE).exists():
            _fail([Problem("no_player_data", str(d), f"{d} 里没有播放器数据（{DATA_FILE}）。"
                           "要么不是 onbgm render 的输出目录，要么是旧版本渲染的，重新渲染一次即可。")], args.json, code=2)
    if args.list:
        comments = load_comments(dirs[0])
        lines = [f"{c['time_s']:>7.2f}s  " + ("余音" if c.get("in_tail") else f"第 {c.get('bar')} 小节第 {c.get('beat')} 拍 {c.get('section')}")
                 + f"  {c['text']}" for c in comments] or ["还没有意见"]
        _emit({"ok": True, "comments": comments}, args.json, "\n".join(lines))
    try:
        server = ReviewServer(dirs[0], dirs[1] if len(dirs) > 1 else None, args.port)
    except OSError as e:
        _fail([Problem("port_unavailable", "--port", f"端口 {args.port} 用不了（{e.strerror}）。"
                       "不写 --port 会自动选一个空闲端口")], args.json, code=2)
    info = {"ok": True, "url": server.url, "review_file": str(dirs[0] / "review.json")}
    if args.json:
        print(json.dumps(info, ensure_ascii=False), flush=True)
    else:
        print(f"审听页面：{server.url}\n意见会保存到 {info['review_file']}\n按 Ctrl+C 结束", flush=True)
    server.serve(open_browser=not args.no_open)


def cmd_fetch(args) -> None:
    fetch_all(SOUNDS_DIR)


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("onbgm")
    except PackageNotFoundError:  # 直接从源码目录运行、没有安装
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser(prog="onbgm", description="给 agent 用的纯器乐 BGM 引擎：文本乐谱 → 音频")
    ap.add_argument("--version", action="version", version=f"onbgm {_version()}")
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
    p.add_argument("--draft", action="store_true",
                   help="草稿：完整渲染并输出报告和卷帘图，但不写音频文件（反复修改时更快、不占磁盘）")
    p = add("fit", cmd_fit, "根据视频的画面节点算出速度和段落结构，让每个节点都落在小节第一拍")
    p.add_argument("cues", nargs="+", help="段落分界的时间点，可带名字：0:08=drop 0:30=logo")
    p.add_argument("--length", help="总时长，如 0:45")
    p.add_argument("--bpm", default="80-140", help="基准速度范围，如 100-130（默认 80-140）")
    p.add_argument("--hit", action="append", default=[], help="只放音效、不分段的时间点（报告它落在第几拍），可重复")
    p.add_argument("--top", type=int, default=3, help="给出几个方案")
    p.add_argument("--ending-tempo", type=float, help="结尾渐慢到这个速度（如 60），最后一段会按渐慢后的实际时长排")
    p.add_argument("--write", help="把方案 1 的乐谱骨架写到这个文件")
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
    p = add("review", cmd_review, "打开网页播放器审听：看钢琴卷帘、A/B 对比、在时间轴上写意见（存进 review.json）")
    p.add_argument("out", help="onbgm render 的输出目录")
    p.add_argument("--compare", help="另一个输出目录，用来 A/B 对比（比如修改前的版本）")
    p.add_argument("--port", type=int, default=0, help="端口（默认自动选一个空闲端口）")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p.add_argument("--list", action="store_true", help="不启动服务器，只列出已有的意见")
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

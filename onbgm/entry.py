"""命令行入口：先确认 pedalboard 能在这台机器上加载，再进入真正的命令（cli.main）。

pedalboard 的 Linux x86 预编译包（0.9.25 及以前）是按打包机器的 CPU 编译的，换到某些 CPU 上一导入就因为
非法指令崩溃（spotify/pedalboard#454，上游已修复、等待发布）。进程会被直接杀死，来不及给出任何有用的报错。
所以在 Linux x86 上先用子进程试导入一次，结果按（pedalboard 版本, Python, CPU 型号）缓存，之后不再重复检查。
"""
from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PORTABLE_COMMIT = "8dd91c687ae96995ba90f4f928e3baa5e047fe96"  # 上游修复这个问题的提交
# 从上游修复了这个问题的提交编译 pedalboard（需要编译工具，约 4 分钟）。子模块是 SSH 地址，用环境变量临时改走 HTTPS
FIX_COMMANDS = [
    "sudo apt install pkg-config libfreetype6-dev libasound2-dev libx11-dev libxrandr-dev libxinerama-dev "
    "libxrender-dev libxcomposite-dev libxcursor-dev",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.https://github.com/.insteadOf GIT_CONFIG_VALUE_0=git@github.com: "
    f"USE_PORTABLE_SIMD=1 pip install --force-reinstall --no-deps "
    f"\"pedalboard @ git+https://github.com/spotify/pedalboard@{PORTABLE_COMMIT}\"",
]


def _cpu() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            return next((line.split(":", 1)[1].strip() for line in f if line.startswith("model name")), platform.processor())
    except OSError:
        return platform.processor()


def pedalboard_crashes() -> bool:
    """pedalboard 在这台机器上导入时会不会因为非法指令崩溃。只有 Linux x86 上需要检查。"""
    if sys.platform != "linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        return False
    try:
        key = f"{version('pedalboard')}|{sys.executable}|{_cpu()}"
    except PackageNotFoundError:
        return False  # 没装的话，让正常的导入错误去报
    cache = Path(os.environ.get("ONBGM_CACHE", Path.home() / ".cache" / "onbgm")) / "pedalboard-probe.json"
    try:
        known = json.loads(cache.read_text())
    except (OSError, ValueError):
        known = {}
    if key not in known:
        run = subprocess.run([sys.executable, "-c", "import pedalboard"], capture_output=True)
        known[key] = run.returncode == -signal.SIGILL
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(known))
        except OSError:
            pass
    return known[key]


def main() -> None:
    if pedalboard_crashes():
        message = (f"音频处理库 pedalboard 的预编译包不支持这台机器的 CPU（{_cpu()}），一导入就会因为非法指令崩溃。"
                   "这是 pedalboard 的已知问题（spotify/pedalboard#454），上游已修复、等待发布新版本。")
        if "--json" in sys.argv:
            problem = {"code": "pedalboard_illegal_instruction", "path": "", "message": message,
                       "fix": {"commands": FIX_COMMANDS}}
            print(json.dumps({"ok": False, "problems": [problem]}, ensure_ascii=False, indent=2))
        else:
            steps = "\n".join(f"  {c}" for c in FIX_COMMANDS)
            print(f"{message}\n\n从上游修复了这个问题的提交编译 pedalboard（需要编译工具，约 4 分钟）：\n{steps}", file=sys.stderr)
        sys.exit(2)
    from .cli import main as cli_main

    cli_main()

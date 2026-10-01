"""网页播放器和审听：让人一边听、一边看钢琴卷帘，在时间轴上写意见，意见存成 agent 能直接读的 JSON。

- `onbgm render` 在输出目录写 player.json（播放器数据）和 player.html（数据内嵌，双击就能打开，不需要联网）；
- `onbgm review 输出目录 [--compare 另一个输出目录]` 在本机起一个小服务器：新旧两版可以在同一位置无缝切换，
  意见存进输出目录的 review.json，每条带秒数、小节、拍、段落和当时的和弦。
"""
from __future__ import annotations

import json
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from .arrange import DRUM_KEYS, Part
from .score import Score

DATA_FILE = "player.json"
PAGE_FILE = "player.html"
REVIEW_FILE = "review.json"
PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#86bcb6"]


def _r(x: float, n: int = 3) -> float:
    return round(float(x), n)


def player_data(score: Score, parts: list[Part], audio: Path, duration_s: float, score_path: str | None = None) -> dict:
    """播放器需要的一切：段落、每拍的时间点（用来把时间换算成第几小节第几拍）、和弦、每个声部的音符、音效。"""
    drum_names = {v: k for k, v in DRUM_KEYS.items()}
    tracks = []
    for i, p in enumerate(parts):
        drum = bool(p.sound.drum)
        tracks.append({
            "name": p.name, "sound": p.sound_name, "texture": p.texture, "color": PALETTE[i % len(PALETTE)],
            "drum": drum,
            "notes": [[_r(score.seconds(n.start)), _r(max(score.duration(n.start, n.dur), 0.02)), n.pitch, n.vel]
                      for n in sorted(p.notes, key=lambda n: n.start)],
        })
    from .analyze import _sfx_row

    return {
        "title": score.title,
        "score": score_path,
        "bpm": _r(score.bpm, 2),
        "key": score.key,
        "loop": score.loop,
        "audio": audio.name,
        "duration_s": _r(duration_s),
        "music_end_s": _r(score.seconds(score.total_beats)),
        "beats_per_bar": score.beats_per_bar,
        "meter": score.meter,
        "beats": [_r(score.seconds(b)) for b in range(int(score.total_beats) + 1)],
        "sections": [{"name": s.name, "start_s": _r(score.seconds(s.start)), "end_s": _r(score.seconds(s.end)),
                      "bars": [s.start_bar + 1, s.start_bar + s.bars], "energy": list(s.energy)}
                     for s in score.sections],
        "chords": [{"start_s": _r(score.seconds(c.start)), "chord": c.chord.symbol} for c in score.chords],
        "tracks": tracks,
        "drum_names": {str(k): v for k, v in drum_names.items()},
        "sfx": [_sfx_row(score, ev) for ev in score.sfx],
    }


def _template() -> str:
    return resources.files("onbgm").joinpath("player.html").read_text(encoding="utf-8")


def page(a: dict, b: dict | None = None, server: bool = False, labels: tuple[str, str] = ("A", "B")) -> str:
    """播放器页面：数据直接写进页面，所以不联网、双击打开也能用。"""
    boot = {"a": a, "b": b, "server": server, "labels": list(labels)}
    blob = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")  # 防止数据里的 </script> 提前结束脚本
    return _template().replace("/*__ONBGM_BOOT__*/null", blob)


def write_player(out: Path, data: dict) -> dict[str, Path]:
    (out / DATA_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (out / PAGE_FILE).write_text(page(data, labels=(out.name, "")), encoding="utf-8")
    return {"player": out / PAGE_FILE}


# ---------- 意见 ----------

def load_comments(out: Path) -> list[dict]:
    path = out / REVIEW_FILE
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("comments", [])


def save_comments(out: Path, comments: list[dict]) -> None:
    comments = sorted(comments, key=lambda c: c["time_s"])
    (out / REVIEW_FILE).write_text(json.dumps({"comments": comments}, ensure_ascii=False, indent=2), encoding="utf-8")


def new_comment(body: dict) -> dict:
    text = str(body.get("text", "")).strip()
    if not text:
        raise ValueError("意见不能是空的")
    keep = ("time_s", "bar", "beat", "section", "chord", "version", "in_tail")
    return {"id": uuid.uuid4().hex[:8], **{k: body[k] for k in keep if k in body}, "text": text,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# ---------- 审听服务器 ----------

class ReviewServer:
    """在本机起一个只监听 127.0.0.1 的小服务器：A 是正在审听的版本（意见存在它的目录里），B 是对比的版本。"""

    def __init__(self, a: Path, b: Path | None = None, port: int = 0):
        self.dirs = {"a": a.resolve(), "b": b.resolve() if b else None}
        self.data = {k: json.loads((d / DATA_FILE).read_text(encoding="utf-8")) if d else None
                     for k, d in self.dirs.items()}
        self.lock = threading.Lock()
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/"

    def page(self) -> str:
        labels = (self.dirs["a"].name, self.dirs["b"].name if self.dirs["b"] else "")
        if labels[0] == labels[1]:
            labels = (f"{self.dirs['a'].parent.name}/{labels[0]}", f"{self.dirs['b'].parent.name}/{labels[1]}")
        return page(self.data["a"], self.data["b"], server=True, labels=labels)

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 不往终端刷访问日志
                pass

            def _send(self, code: int, body: bytes, kind: str):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, data):
                self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/":
                    return self._send(200, server.page().encode(), "text/html; charset=utf-8")
                if path == "/api/comments":
                    return self._json(200, load_comments(server.dirs["a"]))
                version, _, name = path.lstrip("/").partition("/")
                base = server.dirs.get(version)
                if base and name:
                    target = (base / name).resolve()
                    if target.is_relative_to(base) and target.is_file():  # 只提供这两个输出目录里的文件
                        kind = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".json": "application/json",
                                ".png": "image/png"}.get(target.suffix, "application/octet-stream")
                        return self._send(200, target.read_bytes(), kind)
                self._json(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/api/comments":
                    return self._json(404, {"error": "not found"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                    comment = new_comment(body)
                except (ValueError, TypeError) as e:
                    return self._json(400, {"error": str(e)})
                with server.lock:
                    comments = load_comments(server.dirs["a"]) + [comment]
                    save_comments(server.dirs["a"], comments)
                self._json(200, comment)

            def do_DELETE(self):
                prefix = "/api/comments/"
                if not self.path.startswith(prefix):
                    return self._json(404, {"error": "not found"})
                cid = self.path[len(prefix):]
                with server.lock:
                    comments = load_comments(server.dirs["a"])
                    kept = [c for c in comments if c["id"] != cid]
                    save_comments(server.dirs["a"], kept)
                self._json(200 if len(kept) < len(comments) else 404, {"deleted": len(comments) - len(kept)})

        return Handler

    def serve(self, open_browser: bool = True) -> None:
        if open_browser:
            threading.Timer(0.3, lambda: webbrowser.open(self.url)).start()
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.httpd.server_close()

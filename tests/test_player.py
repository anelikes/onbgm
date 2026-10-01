import json
import threading
import urllib.request

import pytest

from onbgm.arrange import arrange
from onbgm.player import REVIEW_FILE, ReviewServer, page, player_data, write_player
from onbgm.score import load


@pytest.fixture
def rendered(write_score, tmp_path):
    """一个"渲染输出目录"：只放播放器需要的数据和一个假的音频文件，不真的渲染。"""
    score = load(write_score("""
        meta: {bpm: 120}
        sections:
          - {name: intro, bars: 2, energy: 0.3}
          - {name: drop, bars: 2, energy: 0.9}
        harmony: {intro: "C | Am", drop: "F | G"}
        tracks:
          keys: {sound: piano, texture: pad}
          drums: {sound: drums, texture: groove, pattern: {kick: "x... .... x... ...."}}
        sfx:
          - {type: riser, at: "drop"}
    """))
    parts, _ = arrange(score)
    out = tmp_path / "out"
    out.mkdir()
    (out / "song.wav").write_bytes(b"RIFF....WAVE")
    write_player(out, player_data(score, parts, out / "song.wav", 8.5, "score.yaml"))
    return out


def test_player_data_has_beats_sections_notes_and_sfx(rendered):
    data = json.loads((rendered / "player.json").read_text())
    assert data["audio"] == "song.wav" and data["duration_s"] == 8.5 and data["music_end_s"] == 8.0
    assert data["beats"][:3] == [0.0, 0.5, 1.0] and len(data["beats"]) == 17  # 4 小节 × 4 拍，加上终点
    assert [s["name"] for s in data["sections"]] == ["intro", "drop"] and data["sections"][1]["start_s"] == 4.0
    drums = next(t for t in data["tracks"] if t["name"] == "drums")
    assert drums["drum"] and {n[2] for n in drums["notes"]} == {36}
    assert data["sfx"][0]["type"] == "riser" and data["sfx"][0]["at_s"] == 4.0
    assert data["chords"][1] == {"start_s": 2.0, "chord": "Am"}


def test_page_embeds_data_safely(rendered):
    data = json.loads((rendered / "player.json").read_text())
    data["title"] = "</script><script>alert(1)</script>"
    html = page(data)
    assert "/*__ONBGM_BOOT__*/" not in html and "</script><script>alert" not in html
    assert (rendered / "player.html").read_text().count("const BOOT = {") == 1


def test_review_server_saves_and_deletes_comments(rendered, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "player.json").write_text((rendered / "player.json").read_text())
    server = ReviewServer(rendered, other)
    threading.Thread(target=server.httpd.serve_forever, daemon=True).start()
    try:
        def call(method, path, body=None):
            req = urllib.request.Request(server.url.rstrip("/") + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        status, html = call("GET", "/")
        assert status == 200 and '"server": true' in html.decode() and "other" in html.decode()
        assert call("GET", "/a/song.wav") == (200, b"RIFF....WAVE")
        assert call("GET", "/a/../other/player.json")[0] == 404  # 不能读输出目录以外的文件
        assert call("POST", "/api/comments", {"text": "  "})[0] == 400
        status, body = call("POST", "/api/comments", {"time_s": 4.2, "bar": 3, "beat": 1.4, "section": "drop",
                                                      "chord": "F", "version": "out", "text": "鼓太吵", "evil": 1})
        comment = json.loads(body)
        assert status == 200 and comment["text"] == "鼓太吵" and "evil" not in comment
        saved = json.loads((rendered / REVIEW_FILE).read_text())["comments"]
        assert [c["section"] for c in saved] == ["drop"]
        assert json.loads(call("GET", "/api/comments")[1])[0]["id"] == comment["id"]
        assert call("DELETE", f"/api/comments/{comment['id']}")[0] == 200
        assert json.loads((rendered / REVIEW_FILE).read_text())["comments"] == []
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()

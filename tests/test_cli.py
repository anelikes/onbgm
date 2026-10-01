import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import needs_audio
from onbgm import catalog
from onbgm.arrange import arrange
from onbgm.score import load

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "onbgm" / "examples" / "night_rain_v1.yaml"


def run(*args, cwd=None):
    """运行 onbgm --json，返回 (退出码, 解析后的 JSON)。"""
    proc = subprocess.run([sys.executable, "-m", "onbgm", *args, "--json"], capture_output=True, text=True, cwd=cwd)
    return proc.returncode, json.loads(proc.stdout)


def test_check_json_reports_timeline():
    code, data = run("check", str(EXAMPLE))
    assert code == 0 and data["ok"]
    assert [s["name"] for s in data["timeline"]["sections"]] == ["intro", "build", "climax", "outro"]
    assert data["timeline"]["sections"][2]["start_s"] == 32.0


def test_check_json_problems_have_code_path_and_fix(write_score):
    path = write_score("""
        meta: {bpm: 90}
        sections: [{name: a, bars: 2}]
        harmony: {a: "C | G"}
        tracks:
          k: {sound: piano, texture: comp, rythm: "x--- ---- ---- ----"}
    """)
    code, data = run("check", str(path))
    assert code == 1 and not data["ok"]
    assert data["problems"] == [{
        "code": "unknown_field", "path": "tracks.k.rythm",
        "message": "不认识这个字段，是不是想写 'rhythm'？（可用: by_section, gain, in, range, rhythm, roll, sound, texture）",
        "fix": {"replace_with": "rhythm"}}]


def test_time_constraint_problem_carries_machine_readable_fix(write_score):
    path = write_score("""
        meta: {bpm: 168}
        sections: [{name: run, bars: 12}, {name: surge, bars: 8, at: "0:20"}]
        harmony: {run: "Am", surge: "Am"}
    """)
    _, data = run("check", str(path))
    assert data["problems"][0]["code"] == "time_constraint_mismatch"
    assert data["problems"][0]["fix"] == {"bpm": 144.0, "bars_before": 14}


def test_timeline_at_locates_seconds_in_bars():
    code, data = run("timeline", str(EXAMPLE), "--at", "0:32", "--at", "23.4")
    assert code == 0
    assert data["at"][0] | {} == {"time_s": 32.0, "bar": 13, "beat": 1.0, "section": "climax",
                                  "bar_in_section": 1, "chord": "Bb", "energy": 1.0}
    assert data["at"][1]["section"] == "build" and data["at"][1]["bar"] == 9


def test_catalog_search_finds_relevant_patterns():
    _, data = run("catalog", "-q", "慵懒的 lo-fi 鼓")
    assert data["items"][0]["name"] == "boom-bap"
    _, data = run("catalog", "-q", "追逐 紧张")
    assert {x["name"] for x in data["items"]} == {"cinematic-drive", "syncopated-332"}


@pytest.mark.parametrize("item", [x for x in catalog.items() if "snippet" in x], ids=lambda x: x["name"])
def test_every_catalog_snippet_can_be_pasted_into_a_score(write_score, item):
    snippet = yaml.safe_load(item["snippet"])
    if item["kind"] == "progression":
        bars = len(snippet.split("|"))
        doc = {"meta": {"bpm": 100}, "sections": [{"name": "a", "bars": bars}], "harmony": {"a": snippet},
               "tracks": {"keys": {"sound": "piano", "texture": "comp", "rhythm": "x--- ---- ---- ----"}}}
    else:
        doc = {"meta": {"bpm": 100}, "sections": [{"name": "a", "bars": 2}], "harmony": {"a": "C | G"},
               "tracks": {"t": snippet}}
    arrange(load(write_score(yaml.safe_dump(doc, allow_unicode=True))))


def test_example_list_and_init(tmp_path):
    _, data = run("example")
    assert "night_rain_v1" in [x["name"] for x in data["examples"]]
    code, data = run("init", str(tmp_path / "proj"), "--example", "late_study")
    assert code == 0 and (tmp_path / "proj" / "score.yaml").exists()
    load(tmp_path / "proj" / "score.yaml")
    code, data = run("init", str(tmp_path / "proj"))
    assert code == 1 and data["problems"][0]["code"] == "file_exists"


def test_doctor_reports_checks():
    code, data = run("doctor")
    assert {"python", "fluidsynth", "gm_soundfont"} <= {c["name"] for c in data["checks"]}
    assert code == (0 if data["ok"] else 2)


@needs_audio
def test_render_json_lists_outputs(tmp_path):
    code, data = run("init", str(tmp_path / "p"))
    code, data = run("render", str(tmp_path / "p" / "score.yaml"), "-o", str(tmp_path / "p" / "out"), "--soundset", "gm")
    assert code == 0 and data["ok"]
    assert data["duration_s"] == data["timeline"]["duration_s"]
    for key in ("audio", "preview", "midi", "stems", "pianoroll"):
        assert Path(data["files"][key]).exists(), key
    assert data["files"]["audio"].endswith("loop.wav") and data["files"]["preview"].endswith(".mp3")
    assert data["automix"] and [b["section"] for b in data["balance"]] == ["intro", "main"]
    assert all(b["tracks"] for b in data["balance"])


def test_timeline_at_inside_tail_is_explained(write_score):
    path = write_score("""
        meta: {bpm: 120, tail: 2}
        sections: [{name: a, bars: 2}]
        harmony: {a: "C"}
    """)
    _, data = run("timeline", str(path), "--at", "4.5", "--at", "7")
    assert data["at"][0]["in_tail"] is True
    assert "加余音到 0:06.00" in data["at"][1]["error"]

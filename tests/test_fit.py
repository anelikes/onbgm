import json
import subprocess
import sys

import pytest

from onbgm.fit import Cue, fit
from onbgm.score import load


def test_finds_the_tempo_where_every_cut_lands_on_a_bar_line():
    """第四轮测试的需求：3 秒间隔的节点。80 BPM 下一小节正好 3 秒，所有节点和音效点都落在小节第一拍。"""
    result = fit([Cue(12, "transition"), Cue(15, "hero"), Cue(27, "outro")], 30, (70, 140), hits=[3, 6, 9])
    best = result["plans"][0]
    assert best["max_deviation_pct"] == 0 and best["bpm"] == 80
    assert all(h["on"] == "downbeat" for h in best["hits"])


def test_last_section_ends_with_a_tail_instead_of_a_tempo_change():
    best = fit([Cue(8, "drop"), Cue(30, "logo")], 45, (100, 130))["plans"][0]
    assert best["bpm"] == 120 and best["max_deviation_pct"] == 0
    assert best["sections"][-1]["bars"] == 7 and best["sections"][-1]["tail_s"] == 1.0


def test_irregular_cuts_use_small_section_tempo_changes_and_the_skeleton_validates(tmp_path):
    best = fit([Cue(7.3, "reveal"), Cue(21.8, "feature"), Cue(38.5, "logo")], 45, (100, 130))["plans"][0]
    assert 0 < best["max_jump_pct"] <= 3  # 相邻段落的速度跳变在 3% 以内，几乎听不出
    path = tmp_path / "s.yaml"
    path.write_text(best["skeleton"], encoding="utf-8")
    score = load(path)  # 骨架里的 at 约束在速度图下全部满足（否则 load 会报错）
    for name, t in [("reveal", 7.3), ("feature", 21.8), ("logo", 38.5)]:
        assert abs(score.seconds(score.section(name).start) - t) < 0.005


def test_suggests_a_wider_range_when_it_is_much_better():
    result = fit([Cue(12, "a"), Cue(15, "b"), Cue(27, "c")], 30, (100, 130))
    assert result["plans"][0]["max_jump_pct"] > 3
    assert result["suggestion"] and "最大速度跳变只有 0%" in result["suggestion"]


def test_fit_cli_json_and_timeline_snaps_to_bar_lines(tmp_path):
    out = tmp_path / "s.yaml"
    proc = subprocess.run([sys.executable, "-m", "onbgm", "fit", "0:07.3=reveal", "0:21.8=feature", "--length", "0:30",
                           "--bpm", "100-130", "--write", str(out), "--json"], capture_output=True, text=True)
    data = json.loads(proc.stdout)
    assert proc.returncode == 0 and data["written"] == str(out) and len(data["plans"]) >= 1
    proc = subprocess.run([sys.executable, "-m", "onbgm", "timeline", str(out), "--at", "0:21.8", "--json"],
                          capture_output=True, text=True)
    at = json.loads(proc.stdout)["at"][0]
    assert at["section"] == "feature" and at["bar_in_section"] == 1 and at["beat"] == 1.0


def test_ranking_prefers_the_smallest_audible_jump_over_a_phantom_base():
    """回归测试：第五轮测试里，跳变 14.8% 的方案曾排在 7% 的前面；而且"基准速度"可能没有任何段落在用。"""
    cues = [Cue(5.4, "people"), Cue(16.9, "montage"), Cue(19.2, "c2"), Cue(21.5, "c3"), Cue(23.8, "c4"), Cue(28.3, "slow")]
    plans = fit(cues, 38, (80, 140), ending_tempo=60)["plans"]
    def audibility(p):  # 先比明显跳变（> 6%）有几处，再比最大跳变
        return (sum(abs(s["jump_pct"]) > 6 for s in p["sections"]), p["max_jump_pct"])
    assert audibility(plans[0]) == min(audibility(p) for p in plans)
    best = plans[0]
    assert any(s["tempo"] is None for s in best["sections"])  # 主速度确实被某些段落使用


def test_ending_ritardando_fits_the_video_length(tmp_path):
    """回归测试：fit 曾按基准速度算渐慢段的时长，实际渐慢从上一段的速度开始，乐谱比视频短了。"""
    cues = [Cue(5.4, "people"), Cue(16.9, "montage"), Cue(28.3, "slow")]
    best = fit(cues, 38, (80, 140), ending_tempo=60)["plans"][0]
    assert best["sections"][-1]["ramp_to"] == 60
    path = tmp_path / "s.yaml"
    path.write_text(best["skeleton"], encoding="utf-8")
    score = load(path)
    assert abs(score.seconds(score.total_beats) + score.tail - 38.0) < 0.01
    assert score.bpm_at(score.total_beats - 1e-6) == pytest.approx(60, abs=0.5)

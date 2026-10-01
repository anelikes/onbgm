import pytest

from onbgm.score import ScoreError, load


def test_bpm_auto_solves_time_constraint(write_score):
    score = load(write_score("""
        meta: {bpm: auto}
        sections:
          - {name: verse, bars: 6}
          - {name: chorus, bars: 4, at: "0:12"}
        harmony: {verse: "G", chorus: "C"}
    """))
    assert score.bpm == pytest.approx(120)
    assert score.seconds(score.section("chorus").start) == pytest.approx(12)


def test_time_constraint_mismatch_suggests_a_fix(write_score):
    with pytest.raises(ScoreError) as err:
        load(write_score("""
            meta: {bpm: 90}
            sections:
              - {name: a, bars: 4}
              - {name: b, bars: 4, at: "0:32"}
            harmony: {a: "C", b: "C"}
        """))
    assert "bpm 改为" in str(err.value)


def test_melody_bar_with_wrong_length_is_located(write_score):
    with pytest.raises(ScoreError) as err:
        load(write_score("""
            meta: {bpm: 90}
            sections: [{name: a, bars: 1}]
            harmony: {a: "C"}
            tracks:
              lead: {sound: piano, notes: {a: "C5/2 D5/4"}}
        """))
    assert "第 1 小节时值合计 3 拍" in str(err.value)


def test_section_of_tolerates_humanize_jitter(write_score):
    score = load(write_score("""
        meta: {bpm: 100}
        sections: [{name: a, bars: 2}, {name: b, bars: 2}]
        harmony: {a: "C", b: "C"}
    """))
    b_start = score.section("b").start
    assert score.section_of(b_start - 0.003).name == "b"  # 被提前几毫秒的音仍属于 b
    assert score.section_of(b_start - 0.25).name == "a"  # 真正写在前一段的弱起音属于 a


def test_tail_is_rejected_for_loops(write_score):
    with pytest.raises(ScoreError) as err:
        load(write_score("""
            meta: {bpm: 90, loop: true, tail: 2}
            sections: [{name: a, bars: 1}]
            harmony: {a: "C"}
        """))
    assert "tail" in str(err.value)


def test_time_constraint_error_offers_bar_count_for_the_requested_tempo(write_score):
    """回归测试：用户要"更快但时间点不变"时，报错只建议把 bpm 改回去，没说要几小节。"""
    with pytest.raises(ScoreError) as err:
        load(write_score("""
            meta: {bpm: 168}
            sections:
              - {name: run, bars: 12}
              - {name: surge, bars: 8, at: "0:20"}
            harmony: {run: "Am", surge: "Am"}
        """))
    assert "保持 168 BPM，让它之前一共 14 小节（现在是 12）" in str(err.value)

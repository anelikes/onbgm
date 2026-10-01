import json
from pathlib import Path

import pytest

from onbgm.arrange import TEXTURES
from onbgm.schema import TEXTURE_FIELDS, score_schema
from onbgm.score import ScoreError, load

ROOT = Path(__file__).resolve().parent.parent


def test_exported_schema_is_up_to_date():
    exported = json.loads((ROOT / "schema" / "score.schema.json").read_text(encoding="utf-8"))
    assert exported == score_schema(), "schema 改了但没重新导出：运行 onbgm schema > schema/score.schema.json"


def test_texture_field_table_covers_every_texture():
    assert set(TEXTURE_FIELDS) == set(TEXTURES)


@pytest.mark.parametrize("path", sorted((ROOT / "onbgm" / "examples").glob("*.yaml")) + sorted((ROOT / "experiments").glob("*/*.yaml")))
def test_examples_and_agent_scores_validate(path):
    load(path)


def _problems(write_score, text):
    with pytest.raises(ScoreError) as err:
        load(write_score(text))
    return err.value.problems


BASE = """
    title: t
    meta: {bpm: 100}
    sections: [{name: verse, bars: 2}, {name: chorus, bars: 2}]
    harmony: {verse: "C", chorus: "G"}
"""


def test_mistakes_fresh_agents_made_get_actionable_errors(write_score):
    problems = _problems(write_score, BASE.replace("meta: {bpm: 100}", "meta: {bpm: 100, title: x}") + """
    tracks:
      lead: {sound: glockenspiel, notes: "G5/4 E5/8"}
    """)
    assert any("'title' 应该写在顶层" in p for p in problems)
    assert any(p.startswith("tracks.lead.notes: 应为键值对") and "chorus" in p for p in problems)


def test_typos_get_suggestions(write_score):
    problems = _problems(write_score, BASE + """
    tracks:
      keys: {sound: epaino, texture: comp, rythm: "x--- ---- ---- ----"}
      drums: {sound: drums, texture: groove, pattern: {kik: "x... ...."}}
    """)
    assert any("是不是想写 epiano" in p for p in problems)
    assert any("是不是想写 'rhythm'" in p for p in problems)
    assert any("是不是想写 kick" in p for p in problems)


def test_field_from_another_texture_names_the_right_textures(write_score):
    problems = _problems(write_score, BASE + """
    tracks:
      drums: {sound: drums, texture: groove, rhythm: "x... ...."}
    """)
    assert problems == ["tracks.drums: groove 织体不使用字段 'rhythm'（bassline、comp 才用）；"
                        "groove 可用: by_section, gain, in, laid_back, pattern, sound, texture"]


def test_by_section_must_name_real_sections_and_overridable_fields(write_score):
    problems = _problems(write_score, BASE + """
    tracks:
      bass:
        sound: electric-bass
        texture: bassline
        rhythm: "1--- ---- ---- ----"
        by_section: {bridge: {rhythm: "1---"}, chorus: {low: 30}}
    """)
    assert any("by_section.bridge: 没有这个段落" in p for p in problems)
    assert any("by_section.chorus: bassline 的段落覆盖里不能写 'low'" in p for p in problems)


def _doc_scores():
    """skill 文档里完整的乐谱示例（含 sections 的 yaml 代码块）。"""
    import re
    for md in sorted((ROOT / "skills").rglob("*.md")):
        for block in re.findall(r"```yaml\n(.*?)```", md.read_text(encoding="utf-8"), re.S):
            if "sections:" in block:
                yield pytest.param(block, id=md.name)


@pytest.mark.parametrize("text", list(_doc_scores()))
def test_scores_in_skill_docs_are_valid(write_score, text):
    from onbgm.arrange import arrange
    arrange(load(write_score(text)))

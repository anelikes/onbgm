import shutil
import textwrap

import pytest

from onbgm.library import GM_SF2, SOUNDS_DIR

needs_audio = pytest.mark.skipif(
    not shutil.which("fluidsynth") or not (SOUNDS_DIR / GM_SF2).exists(),
    reason="需要 fluidsynth 和 GeneralUser GS（运行 onbgm fetch）",
)


@pytest.fixture
def write_score(tmp_path):
    """把一段 YAML 写成乐谱文件，返回路径。"""
    def write(text: str, name: str = "score.yaml"):
        path = tmp_path / name
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path
    return write

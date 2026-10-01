import numpy as np
import soundfile as sf

from conftest import needs_audio
from onbgm.arrange import arrange
from onbgm.render import SR, render
from onbgm.score import load

PIECE = """
    meta: {{bpm: 120, loop: {loop}{tail}}}
    sections: [{{name: a, bars: 4}}, {{name: b, bars: 4}}]
    harmony: {{a: "C | Am | F | G", b: "C | Am | F | G"}}
    tracks:
      keys: {{sound: piano, texture: comp, rhythm: "x--- ---- x--- ----"}}
      bass: {{sound: acoustic-bass, texture: bassline, rhythm: "1--- 5--- 1--- 5---"}}
      lead: {{sound: {lead}, notes: {{b: "E5/2 G5/2 | A5/1 | F5/2 A5/2 | G5/1"}}}}
"""


def _render(write_score, tmp_path, name, **kw):
    path = write_score(PIECE.format(**{"loop": "false", "tail": "", "lead": "piano", **kw}), f"{name}.yaml")
    score = load(path)
    parts, _ = arrange(score)
    return score, render(score, parts, tmp_path / name, soundset="gm")


@needs_audio
def test_non_loop_output_is_exactly_the_score_length(write_score, tmp_path):
    score, result = _render(write_score, tmp_path, "plain")
    audio, _ = sf.read(tmp_path / "plain" / "song.wav")
    assert len(audio) == round(16.0 * SR)  # 8 小节 @120 BPM
    _, result = _render(write_score, tmp_path, "tail", tail=", tail: 1.5")
    assert len(sf.read(tmp_path / "tail" / "song.wav")[0]) == round(17.5 * SR)


@needs_audio
def test_loop_output_is_exact_and_seamless(write_score, tmp_path):
    _, result = _render(write_score, tmp_path, "loop", loop="true")
    audio, _ = sf.read(tmp_path / "loop" / "loop.wav")
    assert len(audio) == round(16.0 * SR)
    typical = np.percentile(np.abs(np.diff(audio, axis=0)).max(axis=1), 99)
    assert np.abs(audio[0] - audio[-1]).max() < typical


@needs_audio
def test_changing_melody_sound_leaves_earlier_sections_untouched(write_score, tmp_path):
    """回归测试：曾经按整首平均响度归一化，只换了旋律音色，前面没有旋律的段落音量也跟着变。"""
    _, r1 = _render(write_score, tmp_path, "v1", lead="piano")
    _, r2 = _render(write_score, tmp_path, "v2", lead="vibraphone")
    cut = round(7.9 * SR)  # b 段从第 8 秒开始
    # 卷积混响用 FFT 整首一起算，浮点舍入会摊到每个采样上（约 1e-8），所以比较到 -120 dB 而不是逐位相等
    assert np.abs(r1.main[:cut] - r2.main[:cut]).max() < 1e-6
    for name in ["keys", "bass"]:
        assert np.array_equal(r1.stems[name], r2.stems[name])

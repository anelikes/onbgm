# onbgm 命令行参考

每个命令都支持 `--json`（`schema` 本身就是 JSON；`fetch` 只打印进度）。用 `--json` 时，标准输出只有一个 JSON 对象，最好优先使用。

退出码：
- `0`：成功
- `1`：乐谱有问题，或参数不对
- `2`：环境有问题（缺 fluidsynth、缺音源等）

## 命令

| 命令 | 作用 |
|---|---|
| `onbgm doctor` | 检查 Python、fluidsynth、音源是否就绪；缺什么就给出安装命令 |
| `onbgm init <目录> [--example 名字]` | 在目录里创建 `score.yaml`（默认是一份最小模板）和 `.gitignore` |
| `onbgm example [名字]` | 不带名字时列出示例；带名字时输出完整乐谱 |
| `onbgm catalog -q "描述"` | 用自然语言搜现成的写法：鼓组、扫弦、低音、和弦伴奏、和弦进行，返回可以直接粘贴的片段 |
| `onbgm catalog --kind 类别` | 浏览某一类：`drums strum bassline comp progression texture sound drum chord example` |
| `onbgm check score.yaml` | 校验乐谱，输出时间轴 |
| `onbgm timeline score.yaml [--at 秒 …]` | 输出每段的起止时间和每小节的和弦；`--at 0:23.4` 查某个时间点在第几小节第几拍 |
| `onbgm render score.yaml -o 目录 [--soundset gm]` | 渲染 |
| `onbgm schema` | 输出乐谱格式的 JSON Schema |
| `onbgm fetch` | 下载音源 |

## JSON 输出

**出错时**（`check`、`timeline`、`render` 都一样）：

```json
{
  "ok": false,
  "problems": [
    {"code": "unknown_field", "path": "tracks.keys.rythm",
     "message": "不认识这个字段，是不是想写 'rhythm'？（可用: …）",
     "fix": {"replace_with": "rhythm"}}
  ]
}
```

- `path` 指向乐谱里出问题的位置，比如 `tracks.lead.notes.chorus`、`sections.climax.at`。
- `fix` 只在能自动给出修正时出现：
  - `replace_with`：拼错的字段或取值，换成这个；
  - 时间约束冲突时给 `bpm`（满足约束需要的速度），以及 `bars_before` 或 `bars`（保持现有速度时需要的小节数）。

**`check` 成功**：`{"ok": true, "timeline": {…}, "warnings": [{code, path, message}]}`

**`timeline`**：

```json
{
  "bpm": 90.0, "bars": 24, "duration_s": 64.0, "seconds_per_bar": 2.6667, "loop": true,
  "sections": [{"name": "climax", "bars": [13, 20], "start_s": 32.0, "end_s": 53.333, "energy": [1.0, 1.0]}],
  "chords": [{"bar": 13, "beat": 1.0, "start_s": 32.0, "chord": "Bb", "section": "climax"}],
  "tracks": [{"name": "lead", "sound": "strings", "texture": "melody", "sections": ["climax"]}],
  "at": [{"time_s": 23.4, "bar": 9, "beat": 4.1, "section": "build", "bar_in_section": 5, "chord": "Gm9", "energy": 0.68}]
}
```

**`render`**：

```json
{
  "ok": true,
  "files": {"audio": "out/v1/loop.wav", "preview": "out/v1/preview.mp3", "midi": "out/v1/song.mid",
            "stems": "out/v1/stems", "pianoroll": "out/v1/pianoroll.png"},
  "duration_s": 64.0, "loop": true, "lufs": -16.9,
  "timeline": {…},
  "sources": {"lead": "vsco2: violins", "piano": "SalamanderGrandPiano.sf2"},
  "automix": [{"track": "lead", "section": "climax", "gain_db": 4.1}],
  "loudness": {"integrated_lufs": -16.9, "follows_energy": true, "issues": [],
               "sections": [{"section": "climax", "energy": 1.0, "lufs": -12.9}]},
  "balance": {"section": "climax", "tracks": [{"track": "lead", "lufs": -14.6}, {"track": "piano", "lufs": -18.1}]},
  "warnings": []
}
```

- `audio` 是交付用的文件：循环曲是 `loop.wav`，不循环是 `song.wav`。
- `automix` 里的 `gain_db` 是相对原始渲染电平的增益，数值大（比如 +20）也正常。

## 错误码

| 类别 | 错误码 |
|---|---|
| 格式和结构 | `yaml_syntax` `unknown_field` `misplaced_field` `missing_field` `wrong_type` `unknown_value` `bad_format` `out_of_range` `wrong_length` `invalid`（其他格式问题） |
| 段落和时间 | `no_sections` `duplicate_section` `unknown_section` `missing_bpm` `no_time_constraint` `time_constraint_mismatch` `tail_with_loop` |
| 和声 | `missing_harmony` `harmony_bar_count` `unknown_chord` |
| 旋律 | `melody_bar_count` `bar_length` `bad_note` `missing_notes` |
| 声部和织体 | `missing_texture` `field_not_for_texture` `not_overridable` `missing_pattern` `unknown_drum` `pattern_bar_count` `pattern_step_count` `pattern_bad_char` |
| 渲染时的警告 | `note_out_of_range` `automix_outlier` `heavy_limiting` |
| 环境和文件 | `missing_dependency` `file_not_found` `file_exists` |

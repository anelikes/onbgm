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
| `onbgm catalog --kind 类别` | 浏览某一类：`drums strum bassline comp arp progression sfx texture sound drum chord example` |
| `onbgm fit 0:08=drop 0:30=logo --length 0:45 [--bpm 100-130] [--hit 0:03] [--ending-tempo 60] [--write score.yaml]` | 给视频配乐：根据画面节点算出速度和每段的小节数，必要时给段落设略有偏差的速度，让每个节点都落在小节第一拍；`--write` 直接写出乐谱骨架 |
| `onbgm check score.yaml` | 校验乐谱，输出时间轴 |
| `onbgm timeline score.yaml [--at 秒 …]` | 输出每段的起止时间和每小节的和弦；`--at 0:23.4` 查某个时间点在第几小节第几拍 |
| `onbgm render score.yaml -o 目录 [--soundset gm] [--draft]` | 渲染。`--draft` 只输出报告和卷帘图、不写音频文件，反复修改时用它更快 |
| `onbgm review out/版本 [--compare out/旧版本] [--port N] [--no-open]` | 启动审听页面（只监听本机）：钢琴卷帘、A/B 无缝切换、在时间轴上写意见。会一直运行，请在后台运行；`--json` 时第一行输出 `{"url": …, "review_file": …}` |
| `onbgm review out/版本 --list` | 不启动页面，列出用户已经写的意见 |
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

**`fit`**：

```json
{
  "plans": [{
    "bpm": 100.599, "max_jump_pct": 1.3, "max_deviation_pct": 1.96,
    "sections": [{"name": "intro", "start_s": 0.0, "end_s": 7.3, "bars": 3, "tempo": 98.63, "deviation_pct": -1.96,
                  "jump_pct": 0.0, "tail_s": 0.0, "ramp_to": null}],
    "hits": [{"time_s": 3.0, "bar": 2, "beat": 1.0, "on": "downbeat"}],
    "skeleton": "title: …"
  }],
  "suggestion": null,
  "written": "score.yaml"
}
```

- 方案按听感排序：先比相邻段落之间明显的速度跳变（超过 6%）有几处，再比最大跳变，最后比总代价（小节数偏好 2、4、8 这类整齐的数）。
- `bpm` 是主速度（用的小节最多的那个速度）。`tempo` 为 null 表示这一段用主速度。
- `jump_pct` 是这一段比上一段快或慢多少，这是听得到的那个量：3% 以内几乎听不出，3–6% 略有感觉，6% 以上明显。`deviation_pct` 是相对主速度的差。
- `ramp_to` 不为空表示这一段渐慢（或渐快）到这个速度（来自 `--ending-tempo`）；`tail_s` 是乐谱结束后留给余音的秒数。
- `hits[].on` 表示音效点落在哪里：`downbeat`（小节第一拍）、`beat`（拍上）、`eighth`（半拍上）、`off`（不在节奏点上；音效照样精确，只是不在节奏点上）。
- `suggestion` 不为空时，说明放宽速度范围能得到明显更好的方案。

**`check` 成功**：`{"ok": true, "timeline": {…}, "warnings": [{code, path, message}]}`

**`timeline`**：

```json
{
  "bpm": 90.0, "bars": 24, "duration_s": 64.0, "seconds_per_bar": 2.6667, "loop": true,
  "sections": [{"name": "climax", "bars": [13, 20], "start_s": 32.0, "end_s": 53.333, "energy": [1.0, 1.0], "bpm": [90.0, 90.0]}],
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
  "balance": [{"section": "climax", "tracks": [{"track": "lead", "lufs": -14.6}, {"track": "piano", "lufs": -18.1}]}],
  "warnings": [],
  "sfx": [{"type": "riser", "at_s": 8.0, "start_s": 4.0, "end_s": 8.0, "level_db": -15.9, "vs_music_db": 2.4, "path": "sfx.0"}]
}
```

- `audio` 是交付用的文件：循环曲是 `loop.wav`，不循环是 `song.wav`。
- `automix` 里的 `gain_db` 是相对原始渲染电平的增益，数值大（比如 +20）也正常。
- `balance` 列出每一段里各声部的响度，从响到轻排列，可以检查旋律有没有被伴奏盖住。
- `review --list --json` 返回 `{"comments": [...]}`。每条意见的字段：
  - `time_s`：秒数；
  - `bar`、`beat`：第几小节第几拍；
  - `section`、`chord`：所在段落和当时的和弦；
  - `in_tail`：是否落在乐谱结束后的余音里；
  - `version`：用户当时听的是哪个输出目录的版本；
  - `text`：意见内容；
  - `created`：写下的时间。
  改完一版后，用 `--compare` 让用户对比新旧两版。
- `timeline --at` 查到乐谱结束后的余音（`meta.tail`）里时，返回 `{"in_tail": true, "note": …}`。
- `files.stems` 目录里有每个声部的分轨；有音效时还有 `sfx.wav`（所有音效，不含混响）。分轨是自动定音量之后、`gain` 和声像之前的声音。
- `render` 的 `sfx` 里，`level_db` 是音效自己的音量（K 计权后最响 100ms），`vs_music_db` 是它比当时的音乐响多少 dB。用户说"再轻一点"时，可以用它核对改了多少。
- `loudness` 里的各段响度是最终混音的响度，包含音效，也包含从上一段延续过来的余音。
- `timeline` 和 `render` 都会列出 `sfx`：每个音效的对准点 `at_s` 和实际起止 `start_s` / `end_s`。`timeline` 里还有 `anchor`，表示怎么对准：`end` 在 at 结束，`start` 从 at 开始，`center` 最响处在 at。

## 环境变量

- `ONBGM_SOUNDS`：音源目录。
- `ONBGM_CACHE`：缓存目录（默认 `~/.cache/onbgm`）。
- `ONBGM_MAX_RENDERS`：最多同时进行几个渲染，多出来的排队。批量生成、很多 agent 同时渲染时设置，避免把机器占满。

## 错误码

| 类别 | 错误码 |
|---|---|
| 格式和结构 | `yaml_syntax` `unknown_field` `misplaced_field` `missing_field` `wrong_type` `unknown_value` `bad_format` `out_of_range` `wrong_length` `invalid`（其他格式问题） |
| 段落和时间 | `no_sections` `duplicate_section` `unknown_section` `missing_bpm` `no_time_constraint` `time_constraint_mismatch` `tail_with_loop` |
| 和声 | `missing_harmony` `harmony_bar_count` `unknown_chord` |
| 旋律 | `melody_bar_count` `bar_length` `bad_note` `missing_notes` |
| 音效 | `sfx_out_of_range`（起点早于开头或超出结尾，`fix.length` 给出能放下的最大长度） `field_not_for_sfx` |
| 声部和织体 | `missing_texture` `field_not_for_texture` `not_overridable` `missing_pattern` `unknown_drum` `pattern_bar_count` `pattern_step_count` `pattern_bad_char` |
| 渲染时的警告 | `note_out_of_range` `automix_outlier` `heavy_limiting` |
| 环境和文件 | `missing_dependency` `file_not_found` `file_exists` `pedalboard_illegal_instruction`（见 README 常见问题） |
| 审听 | `no_player_data`（目录不是 render 的输出，或者是旧版本渲染的，重新渲染即可） `port_unavailable` |

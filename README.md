# onbgm

让 LLM 直接写乐谱、控制每一个音符来生成纯器乐 BGM。和 Suno 这类端到端音频模型不同，这里的音乐是一份可读、可 diff 的文本：
BPM、段落、"第几秒进高潮"、循环点都是精确的，对话里说"高潮提前 4 小节"就只改那 4 小节。

```
LLM ──写/改──▶ 乐谱 YAML ──▶ 校验 + 时间轴求解 ──▶ 织体展开 + 人性化 ──▶ 音符
                   ▲                                                   │
                   │                        分轨渲染（SF2 → FluidSynth；采样乐器 → 内置采样器）
                   │                                                   │
                   └── 文字报告（响度走向、声部平衡、音域） ◀── 混音（座位声像、EQ、卷积混响）→ 母带
```

## 快速开始

```bash
brew install fluid-synth ffmpeg
uv venv && uv pip install -e .
.venv/bin/onbgm fetch                                # 下载音源到 sounds/（约 650MB）

.venv/bin/onbgm check  examples/night_rain_v1.yaml   # 只校验、打印时间轴
.venv/bin/onbgm render examples/night_rain_v1.yaml   # 输出到 out/night_rain_v1/
.venv/bin/onbgm render examples/night_rain_v1.yaml --soundset gm   # 只用 GM 音源（轻量）
```

## 音源

| 音源 | 协议 | 用于 |
|---|---|---|
| [GeneralUser GS](https://github.com/mrbumpy409/GeneralUser-GS) | 自由使用 | 所有音色的保底实现（`--soundset gm`） |
| [Salamander Grand Piano](https://freepats.zenvoid.org/Piano/acoustic-grand-piano.html) | CC-BY 3.0 | 钢琴 |
| [VSCO 2 Community Edition](https://github.com/sgossner/VSCO-2-CE) | CC0 | 弦乐分部、低音提琴、拨弦、定音鼓、镲 |

VSCO 的采样由内置采样器（`onbgm/sampler.py`）直接读取：用 soxr 高质量重采样变调，加载时裁掉开头静音、对齐各采样响度，
并用频谱检测每个采样的真实音高（VSCO 文件名的八度比实际低一个）。`hq` 缺哪件乐器就自动退回 GM。

输出：`loop.wav`（精确到采样的无缝循环段）、`preview.m4a`（循环播放两遍再自然结束，可以听接缝）、
`song.mid`（带段落标记，可导入 DAW）、`stems/`（分轨）、`pianoroll.png`。

## 乐谱格式

示例：`examples/night_rain_v1.yaml`（电影感钢琴弦乐）、`examples/late_study.yaml`（lo-fi）、
`examples/sunny_walk.yaml`（木吉他 vlog）。要点：

- `sections`：段落、小节数、能量（`0.6` 或渐变 `"0.4->0.9"`），可选 `at: "0:32"` 时间约束
- `meta.bpm: auto` + `meta.length: "1:00"` 或段落 `at`：由引擎反推 BPM
- `meta.swing`：0.5 平直，0.58 左右是 lo-fi 的晃动感；`meta.swing_unit`：16（默认）或 8
- `harmony`：每段一行和弦，`|` 分小节，同一小节写多个和弦则平分
- `tracks`：`sound`（音色）+ `texture`（织体）+ `in`（出现在哪些段），或用 `notes` 逐音写旋律；
  `by_section: {段落名: {...}}` 可以让某一段换一套节奏型
- `fx.lofi`：0~1，磁带抖动、低通、轻微失真、黑胶底噪

**旋律记法**：`D5/4` 四分音符，`F#4/8.` 附点八分，`r/2` 二分休止，`A4/2~` 连到下一个同音。每小节必须正好 4 拍。

**节奏型记法**：每小节 8/12/16/24/32 个字符（16 个就是一个字符一个十六分音符），`|` 分小节，空格只为好读。

| 织体 | 字段 | 字符 |
|---|---|---|
| `groove` 鼓组 | `pattern: {kick: ..., snare: ..., hat: ...}` | `X` 重音 `x` 普通 `g` 轻声 `.` 不打 |
| `bassline` 节奏型低音 | `rhythm` | `1 3 5 7 8` 当前和弦的级数，`-` 延长，`.` 休止 |
| `comp` 和弦伴奏型 | `rhythm` | `X`/`x` 弹和弦，`-` 延长，`.` 休止 |
| `strum` 吉他扫弦 | `pattern` | `D`/`U` 下扫/上扫，`d`/`u` 轻扫，`x` 闷音切音，`.` 让弦继续响 |

鼓件：`kick rim snare clap hat pedal-hat open-hat tom-low tom-mid tom-high crash ride ride-bell tambourine cowbell conga-high conga-low cabasa shaker`

其他织体：

| 织体 | 作用 |
|---|---|
| `arpeggio` | 钢琴分解和弦，能量低时四分音符、高时八分音符，自动换踏板 |
| `pad` | 四声部持续和弦，段内就近进行 |
| `bass-sustain` / `bass-pizz` | 长音低音 / 拨弦低音 |
| `timpani` | 重拍定音鼓，`roll_into: 段落名` 在该段前做渐强滚奏 |
| `crash` | 段落开头的吊镲 |
| `melody` | 逐音写的旋律 |

吉他扫弦的按法由引擎在标准调弦的六弦吉他上搜索（G = 320003，D/F# = 200232，F = 133211 …），
规则是真实可按：品位跨度、手指数、横按范围内不能有空弦。

音色：`piano epiano harp vibraphone marimba glockenspiel guitar-nylon guitar-steel strings strings-slow tremolo
pizzicato cello contrabass acoustic-bass electric-bass timpani horn brass flute oboe whistle choir`；
鼓组：`drums drums-room drums-jazz drums-brush drums-808 drums-electronic`

## 设计原则

- **LLM 描述意图，引擎负责演奏**：力度、踏板、CC11 表情、微小的时间偏差都由织体代码生成
- **局部修改不外溢**：人性化随机量按"声部 + 段落 + 段内位置 + 音高"确定，声部进行按段落重置，
  改一段不会让别的段落发生变化
- **LLM 不用管混音**：伴奏按角色定音量（实测每个声部在自己段落里的响度，对齐到该角色的目标电平），
  换音源、换织体密度都不打乱平衡；旋律音量按每段伴奏自动决定（至少比最响的伴奏高 3 dB，且不低于伴奏总和 1.5 dB）；
  母带统一到 -16 LUFS，瞬态多的曲子由预读限幅器最多压 4 dB
- **报错写给 LLM 看**：指出位置、原因和改法

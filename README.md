# onbgm

为 AI agent 设计的纯器乐 BGM 生成工具：agent 逐音写一份文本乐谱（YAML），onbgm 负责编排、混音和渲染成音频。

和 Suno 这类端到端音频模型不同，这里的音乐是一份可读、可 diff 的文本：
- 速度、段落、"第几秒进高潮"、循环点都是精确的；
- 在对话里说"高潮提前 4 小节"，就只改那 4 小节，其余部分逐音不变；
- 同一份乐谱永远渲染出同样的结果。

```
agent ──写/改──▶ 乐谱 YAML ──▶ 严格校验 + 时间轴求解 ──▶ 织体展开 + 人性化 ──▶ 音符
                    ▲                                                      │
                    │                       分轨渲染（SF2 → FluidSynth；采样乐器 → 内置采样器）
                    │                                                      │
                    └── 报告：响度走向、声部平衡、警告、卷帘图 ◀── 混音（角色电平、座位声像、卷积混响）→ 母带
```

## 给 agent

agent 用的说明在 [`skills/onbgm/`](skills/onbgm/SKILL.md)，按 skill 格式组织：
- 入口 [`SKILL.md`](skills/onbgm/SKILL.md)：工作流程、怎么读报告、规则
- [`references/cli.md`](skills/onbgm/references/cli.md)：命令、`--json` 的输出结构、错误码
- [`references/score-format.md`](skills/onbgm/references/score-format.md)：完整的乐谱格式
- [`references/arranging.md`](skills/onbgm/references/arranging.md)：怎么把 BGM 写好，以及各风格的配方

机器可读的格式规范是 [`schema/score.schema.json`](schema/score.schema.json)，也可以用 `onbgm schema` 输出。

## 快速开始

```bash
brew install fluid-synth                  # Debian/Ubuntu: sudo apt install fluidsynth
uv tool install git+https://github.com/anelikes/onbgm
onbgm fetch                               # 下载音源到 ~/.onbgm/sounds（约 1.2GB；可用 ONBGM_SOUNDS 指定位置）
onbgm doctor                              # 检查环境

onbgm init my_bgm --example late_study    # 从示例起步
onbgm catalog -q "慵懒的 lo-fi 鼓"          # 搜现成的写法
onbgm check my_bgm/score.yaml             # 校验、打印时间轴
onbgm fit 0:07.3=reveal 0:21.8=feature --length 0:45 --write video.yaml   # 给视频配乐：按画面节点算速度和结构
onbgm timeline my_bgm/score.yaml --at 0:20
onbgm render my_bgm/score.yaml -o my_bgm/out/v1               # 渲染
onbgm render my_bgm/score.yaml -o my_bgm/out/v1 --soundset gm # 只用 GM 音源（快、轻量）
```

每个命令都支持 `--json`。

开发时：`uv venv && uv pip install -e ".[dev]"`。在源码仓库里，音源会下载到仓库下的 `sounds/`。

输出文件：
- `loop.wav`：循环曲的精确循环段，首尾无缝；不循环的曲子输出 `song.wav`，时长精确等于乐谱时长；
- `preview.mp3`：试听；
- `song.mid`：MIDI；
- `stems/`：分轨；
- `pianoroll.png`：卷帘图。

## 示例

| 示例 | 风格 |
|---|---|
| `night_rain_v1` / `v2` | 电影感钢琴弦乐；v2 演示对话式修改（高潮提前 4 小节、贝斯换成拨弦） |
| `late_study` | lo-fi hip hop：爵士和弦、摇摆、黑胶质感 |
| `sunny_walk` | 木吉他 vlog：扫弦、拍手、钟琴 |
| `cafe_jazz` | 咖啡馆爵士钢琴三重奏 |
| `vlog_intro` | 20 秒不循环片头 |
| `product_video` | 45 秒产品宣传片配乐：全部合成器音色，按视频时间点对齐，带音效时间线（上升音、冲击、whoosh、logo 提示音） |

`cafe_jazz` 和 `vlog_intro` 是两个只读过文档的全新 agent 写的。`experiments/` 里存着这类可用性测试的原始记录。

## 音源

| 音源 | 协议 | 用于 |
|---|---|---|
| [GeneralUser GS](https://github.com/mrbumpy409/GeneralUser-GS) | 自由使用 | 所有音色的保底实现（`--soundset gm`） |
| [Salamander Grand Piano](https://freepats.zenvoid.org/Piano/acoustic-grand-piano.html) | CC-BY 3.0 | 钢琴 |
| [VSCO 2 Community Edition](https://github.com/sgossner/VSCO-2-CE) | CC0 | 弦乐分部（含跳弓）、低音提琴、拨弦、定音鼓、镲、钟琴 |
| [jRhodes（GM 版）](https://github.com/sfzinstruments/Discord-SFZ-GM-Bank)，Jeff Learman | CC0 | 电钢琴 |
| [FSS Steel String Guitar](https://freepats.zenvoid.org/Guitar/steel-acoustic-guitar.html)，Gary Campion / FreePats | GPL-3.0+，附例外：用它做的音乐不受 GPL 约束 | 钢弦木吉他 |
| [Black And Blue Basses](https://github.com/sfzinstruments/karoryfer.black-and-blue-basses)，Karoryfer Samples | CC0 | 电贝斯 |
| [AVL Drumkits](https://www.bandshed.net/avldrumkits/)（Black Pearl、Blonde Bop），Glen MacArthur | CC-BY-SA 3.0，用它做的音乐不需要署名 | 标准鼓组、爵士鼓组 |
| [Swirly Drums](https://github.com/sfzinstruments/karoryfer.swirly-drums)，Karoryfer Samples | CC0 | 刷子鼓组（只用干声） |
| [Versilian Community Sample Library](https://github.com/sgossner/VCSL)，Versilian Studios | CC0 | 颤音琴、马林巴 |
| 内置合成器（`onbgm/synth.py`） | — | 合成器音色（pluck、pad、supersaw、lead、bass、808、FM 钟声和电钢琴）和合成鼓组，不需要下载 |

采样由内置采样器（`onbgm/sampler.py`）直接读取，用 soxr 做高质量重采样来变调。
- VSCO：加载时裁掉开头的静音，对齐各采样的响度，用频谱检测每个采样的真实音高。
- SFZ 采样库（`onbgm/sfz.py`）：区域、力度层、轮替、包络、循环点和互斥组（闭镲切断开镲）都按 SFZ 文件来。另外做两项校准：
  - 有的库在起音前留了预备音（比如贝斯的手指声），播放时提前这么多，让真正的起音落在拍子上；
  - 相邻力度层在交界处的音量对齐，避免力度多一点音量反而突变；同一力度的几个轮替采样之间，响度差超过 ±1.5 dB 的部分拉回来。
  - 按控制器默认值折算调制参数（有的库靠它来定每件鼓的声像和调音），也支持只用来触发互斥组的无声区域。
- 下载的采样库都固定到具体版本，作者更新不会改变已有乐谱的声音。

`hq` 音源缺哪件乐器，就自动退回 GM。

## 设计原则

- **agent 描述意图，引擎负责演奏**：力度、踏板、表情、微小的时间偏差都由织体代码生成。
- **局部修改不外溢**：
  - 人性化的随机量按"声部 + 段落 + 段内位置 + 音高"来确定；
  - 声部进行在每个段落重新开始；
  - 母带用固定增益，不按整首的平均响度归一化。
  所以改一处，只影响那一处。
- **agent 不用管混音**：
  - 伴奏按角色定音量：用这个声部的音色和织体渲染一段标准参考片段，测响度后对齐到角色的目标电平。
    只看配置、不看乐谱内容，所以改某一段不会让这个声部在其他段落变响或变轻；
  - 旋律音量按每段的伴奏自动决定；
  - 瞬态多的曲子由预读限幅器控制峰值。
- **报错写给 agent 看**：写明位置、原因和改法，拼错了会猜"是不是想写 xx"，不认识的字段一律报错，不会被悄悄忽略。

## 开发

```bash
.venv/bin/python -m pytest      # 需要音频的测试在没装 fluidsynth 或音源时会自动跳过
```

当前状态：阶段 1（打地基）。定位、范围决策和后续阶段见 [ROADMAP.md](ROADMAP.md)。

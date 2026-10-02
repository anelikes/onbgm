# 参与开发

onbgm 是给 AI agent 用的配乐工具：agent 写文本乐谱，onbgm 渲染成纯器乐。改动之前请先读 README 的「设计原则」和 [ROADMAP.md](ROADMAP.md)。

安全问题或者不方便公开讨论的事，请发邮件到 [contact@onbgm.com](mailto:contact@onbgm.com)。

## 环境

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
brew install fluid-synth            # Debian/Ubuntu: sudo apt install fluidsynth
.venv/bin/onbgm fetch               # 下载音源，约 1.2GB（在源码仓库里会放到 sounds/）
.venv/bin/onbgm doctor              # 检查环境
.venv/bin/python -m pytest -q
```

没装 fluidsynth 或音源时，需要音频的测试会自动跳过。CI 只下载 GM 音源，所以需要高质量采样的测试只在本地跑；改了采样相关的代码，请在本地完整跑一遍。

## 几条硬约定

- **局部修改不外溢**：改乐谱的某一段，其他段落的声音必须逐采样不变。
  - 新增的随机量要按"声部 + 段落 + 段内位置 + 音高"这类局部信息确定；
  - 新增的统计量（响度、平均值等）不能跨段落计算；
  - 碰到这类改动，加一条回归测试：改一段，断言其他段落的音频完全相同。
- **可复现**：同一份乐谱永远渲染出同样的结果，不用全局随机数，不依赖运行环境。
- **报错写给 agent 看**：用 `Problem(code, path, message, fix)`。写明位置、原因和具体改法，能给出可以直接套用的修正值就放进 `fix`。
- **乐谱格式改动要一起改**：
  - `schema.py`（JSON Schema 和字段说明）；
  - `score.py`（解析和校验）；
  - `skills/onbgm/` 下 agent 读的文档；
  - 需要的话再加一个示例或写法库条目（`catalog.yaml`）。
- **代码风格**：注释用中文，密度和写法跟周围的代码保持一致；不轻易引入新依赖。

## 新增音色或采样库

1. **协议**：必须允许用它做的音乐自由使用，包括商用。CC0 最好；CC-BY、或作者明确写了"用它做的音乐不受限制"也可以。只允许非商用（NC）的不行。
2. **登记**：在 `onbgm/fetch.py` 的 `SAMPLE_LIBS` 里登记。来源在 GitHub 上的，要固定到具体提交；只下载用得到的文件。`onbgm doctor` 会自动检查。
3. **接入**：在 `onbgm/library.py` 里建乐器，并接到音色名上。
   - SFZ 格式用 `load_sfz`；
   - 文件名里写了音高的用 `load_instrument`；
   - 缺这件乐器时会自动退回 GM。
4. **实测，不要只靠耳朵**：
   - 起音是否准时（有的库在起音前留了预备音）；
   - 力度层交界处的音量是否连续；
   - 实际音高是否和映射一致（有的贝斯库按记谱音高映射，比实际发声高一个八度）。
5. **对比试听**：用示例乐谱渲染新旧两版做 A/B 对比，并检查声部平衡和限幅器的报告。
6. **文档**：在 README（英文 `README.md` 和中文 `README.zh-CN.md` 两份）的音源表和 `skills/onbgm/references/score-format.md` 的音色表里写上它。

## 可用性测试

文档是写给 agent 看的。检验方法：让一个全新的 agent 只读 `skills/onbgm/`，按一个具体需求写曲、修改，记录它遇到的每个问题。`experiments/` 里存着之前几轮的原始记录。

## 提交

- 测试全部通过，CI 是绿的；
- 新功能和修复都带测试；
- 用户能感知的变化，同时更新 `skills/onbgm/` 和两份 README（英文和中文）。

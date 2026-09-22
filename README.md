# AI 自动剪辑（单产品广告）

把同一个产品的几段原视频丢进一个文件夹，自动分析镜头、识别产品、制定剪辑方案、写英文字幕、审核，输出一条竖屏商品广告。

**你只需要准备两样东西：一个 AI API Key，和几段同一产品的原视频。** 背景音乐和品牌片尾是可选的，不配也能出片。

镜头理解、选片、文案和审片调用 AI API；裁切、拼接、烧字幕、导出全部在本地用 FFmpeg 完成，素材不出本机（发给模型的是抽帧图片）。

---

## 快速开始（Windows）

### 1. 装两个前置软件

| 软件 | 说明 |
|------|------|
| [Python 3.11 或 3.12](https://www.python.org/downloads/) | 安装时勾选 "Add Python to PATH" |
| [FFmpeg](https://www.gyan.dev/ffmpeg/builds/) | 下载 release build，解压后把 `bin` 目录加入 PATH |

装完在 PowerShell 里确认这三条命令都有输出：

```powershell
python --version
ffmpeg -version
ffprobe -version
```

### 2. 下载本项目并安装

下载仓库（`Code → Download ZIP` 解压，或 `git clone`），然后**双击 `安装.cmd`**。
它会自动建虚拟环境、装依赖、生成 `.env` 和 `auto-cut.settings.json`。

### 3. 填入你的 API Key

用记事本打开项目根目录的 `.env`，把 Key 换成你自己的：

```ini
MINIMAX_API_KEY=你的key
MINIMAX_BASE_URL=https://api.minimaxi.com/v1
MINIMAX_MODEL=MiniMax-M3
```

模型账号需要支持**图片输入**和**工具调用（function calling）**。也可以改用 OpenAI 兼容接口，见下方「换一个模型服务商」。

### 4. 出片

把一个产品的素材文件夹**拖到 `拖入素材文件夹.cmd` 上**（或双击它再粘贴路径）。

```text
素材/
└─ 产品1/          ← 拖这一层
   ├─ a.mp4
   ├─ b.mp4
   └─ c.mp4
```

同一个文件夹里只放**同一个产品**的视频，支持 MP4、MOV、MKV、AVI、WebM、M4V。不要把装着多个产品的总目录丢进去。

命令行等价写法：

```powershell
.\.venv\Scripts\python.exe .\auto_cut.py ".\素材\产品1"
```

---

## 你会拿到什么

输出默认在素材文件夹**上一级**的 `自动剪辑成片\product-日期时间-编号\`（用 `--output-root` 可改）：

| 文件 | 含义 |
|------|------|
| `成品.mp4` | 粗剪、成片视觉审核、技术检查**全部通过** |
| `待检查预览.mp4` | 没过审，保留可用版本供人工看，**不要当成品直接投放** |
| `product_profile.json` | AI 判断出的产品是什么、卖点、情绪基调 |
| `footage_index.json` | 每段素材的分镜、画面内容分析 |
| `edit_plan.json` | 最终采用的剪辑方案（用了哪段的哪几秒） |
| `review_report.json` / `output_manifest.json` | 审核问题清单、任务状态、各产物路径 |

默认规格：1080×1920、30fps、正文约 15 秒（±2 秒），英文字幕，原速硬切，使用完整镜头。

---

## BGM 和字幕，到底要不要自己准备？

这是最常被问的两件事，结论：

| | 要不要你准备 | 说明 |
|---|---|---|
| **字幕** | **不用** | AI 看完整条片子统一写英文文案，按镜头切分，自动烧进画面。字体 Lato 已随仓库附带（SIL OFL 许可），白字黑描边、自动避让两行安全区 |
| **背景音乐** | **可选** | 不配置 → 正文是静音的（片子照样出，QC 不会因此判失败，只留一条提醒）。配置了 → AI 按情绪关键词从你的音乐库里选一首，自动淡入淡出 |
| **品牌片尾** | **可选** | 不配置 → 只输出正文。配置了 → 自动缩放到成片尺寸并拼在正文后面，保留片尾自己的音效 |

**为什么音乐不随仓库附带**：能商用的音乐都有各自的授权条款，打包进开源仓库会把授权风险转嫁给每个用户。所以这里只做接口，曲子你自己放。

### 想加音乐和片尾（推荐，成片质感差别很大）

在项目里建 `assets` 目录：

```text
assets/
├─ end-card.mp4        品牌片尾：竖屏、自带音轨、一般 3~4 秒
└─ music/              无人声音乐，放 .mp3 或 .wav
   ├─ warm-piano.mp3
   ├─ upbeat-guitar.mp3
   └─ calm-ambient.mp3
```

然后编辑根目录的 `auto-cut.settings.json`：

```json
{
  "end_card": "assets/end-card.mp4",
  "bgm_library": "assets/music",
  "body_seconds": 15,
  "width": 1080,
  "height": 1920
}
```

选曲逻辑是**按文件名匹配情绪词**的：AI 为这条片子给出 `warm` / `calm` / `elegant` / `emotional` / `upbeat` 之类关键词，程序挑文件名（或所在子目录名）命中最多的那首。所以文件名里带上情绪词最管用，例如 `warm-piano-loop.mp3`；也可以按目录分类 `music/warm/piano-01.mp3`。

音乐必须**无人声**（人声会和字幕打架），并且确认你有商用授权。免费可商用的常见来源：Pixabay Music、YouTube Audio Library、Free Music Archive（逐首确认许可证）。

片尾**必须自带音轨**，否则会直接报错；分辨率会自动缩放，时长不计入正文的 15 秒。

更多说明见 `assets/README.md`。

---

## 换一个模型服务商

程序走 OpenAI 兼容协议，`.env` 里配哪组 Key 就用哪个：

```ini
# 方式一：MiniMax（默认）
MINIMAX_API_KEY=...
MINIMAX_BASE_URL=https://api.minimaxi.com/v1
MINIMAX_MODEL=MiniMax-M3

# 方式二：OpenAI
OPENAI_API_KEY=...
OPENAI_TEXT_MODEL=gpt-4o
```

两组都填时默认用 MiniMax，可以用 `--provider openai` 或 `--model` 覆盖（传给 `run_v2.py`）。
模型可用性与计费以服务商账户为准；本项目不统计 token 费用，用量请查服务商账单。

---

## 常用命令

```powershell
# 只检查配置和素材、生成任务，不花钱调 AI
.\.venv\Scripts\python.exe .\auto_cut.py ".\素材\产品1" --prepare-only

# 素材分析或“首次方案生成”失败：复用已完成的镜头缓存续跑
.\.venv\Scripts\python.exe .\auto_cut.py --resume ".\data\auto-requests\任务编号.json"

# 分析已完成但没过审：在新目录重剪，不覆盖旧任务
.\.venv\Scripts\python.exe .\auto_cut.py --retry ".\data\auto-requests\任务编号.json"
```

`--retry` 要求原任务保留了完整分析结果和 `sources/` 素材副本，并会校验原视频内容没变。同一个任务不要同时跑两次。

---

## 它怎么工作的

```text
素材文件夹
   ↓ Analyzer    分镜、抽帧、视觉理解（结果带缓存）
   ↓ Product     识别这是什么产品、卖点、情绪基调
   ↓ Director    AI 出剪辑方案：用哪段的哪几秒、怎么排
   ↓ Validator   时间、动作连续性、方案合法性校验
   ↓ Renderer    FFmpeg 粗剪 → 字幕 → 音乐 → 片尾
   ↓ Reviewer    每 0.5 秒抽一帧给 AI 审片；不过就重剪（最多 2 次）
   ↓ QC          时长、分辨率、黑帧、冻结画面、音频峰值
成品.mp4 / 待检查预览.mp4
```

粗剪最多重剪两次，字幕最多生成两轮。审核会区分“同一产品换场景展示”和“同一镜头重复播放”；流程保留每一轮结果，优先选通过或严重问题更少的版本，**不会靠降低分数门槛强行过审**。

### 代码结构

```text
auto_cut.py                  文件夹入口：配置、续跑、重试
run_v2.py                    JSON 任务入口（更底层，见 --help）
services/video-worker/
├─ analyzer/                 分镜、抽帧、视觉分析和缓存
├─ director/                 AI 剪辑方案生成
├─ validator/                时间、角色、动作和方案检查
└─ v2/                       产品识别、主流程、渲染、审片
schemas/                     五份 JSON 数据契约
tests/                       本地回归测试（不调用真实 AI）
fonts/                       字幕字体 Lato 及其 OFL 许可证
```

跑测试（需要 FFmpeg）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

---

## 已知限制

- 字幕目前只做**英文**（`brief.language` 必须为 `en`）。
- 转场只有硬切，原速播放，不做变速、抠像、字幕逐字动画和节拍对齐。
- AI 按每 0.5 秒一帧抽样审核，**不能保证发现所有瞬间瑕疵**，也不判断音频内容。投放前请完整看一遍成片。
- 素材太少、模型返回异常、网络或渲染故障，都可能导致连预览都没有。
- 实际验证过摆件和节日挂件品类，其他品类需要用真实素材验证。

## 隐私与授权

`.gitignore` 已排除密钥、本地配置、输入输出媒体、任务记录和缓存。素材、音乐、品牌片尾和 API Key 都不在仓库内，请使用你有权使用的文件。

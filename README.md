# AI 自动剪辑工具 (AI Automated Cutting)

> 将多个AI生成的基础素材，自动剪辑成带有BGM、字幕、公司商标的成品视频

---

## 📋 项目概述

给AI喂入多个基础素材（720×1280, ~15秒），自动剪辑成**2-3个**成品视频（2160×3840, 16-22秒），供人工挑选。

### 成品规格
| 参数 | 基础素材 | 成品 |
|------|----------|------|
| 分辨率 | 720×1280 | 2160×3840 (4K竖屏) |
| 时长 | ~15秒 | 16-22秒主体 + 3.37秒片尾 |
| 音频 | 有原声 | BGM替换（无人声）+ 片尾打字音效 |
| 字幕 | 无 | AI视觉生成英文文案（GPT-4o）/ Whisper回退 |

---

## 🚀 快速开始

### 1. 环境准备

```bash
# Python 3.9+
pip install opencv-python scenedetect whisper openai numpy

# FFmpeg 7.0+（需在系统PATH中）
ffmpeg -version
```

### 2. 准备素材

```
素材目录/
├── 素材1.mp4    # 720×1280 竖屏视频
├── 素材2.mp4
└── ...
```

### 3. 准备片尾视频

将 `平销片尾.mp4`（1080×1920, 3.37秒, 含打字音效）放在脚本同目录下。

### 4. 运行

```bash
# 预设品类（自动推导输入输出目录）
python run_workflow.py --category 别针 --allow-heuristic-fallback

# 或自定义目录
python run_workflow.py --input "D:\素材目录" --output "D:\输出目录" --allow-heuristic-fallback
```

### 5. 启用 AI 视觉字幕（可选）

```bash
# 设置 OpenAI API Key 后，自动使用 GPT-4o 看画面生成英文字幕
# 不设置则自动回退到 Whisper 语音识别
set OPENAI_API_KEY=sk-你的Key       # Windows
export OPENAI_API_KEY=sk-你的Key     # Linux/Mac

python run_workflow.py --category 别针 --allow-heuristic-fallback
```

---

## ⚙️ 参数说明

| 参数 | 必选 | 说明 |
|------|------|------|
| `--category` | 三选一 | 摆件/别针/航海贴/衣服/露营贴 |
| `--input` | 三选一 | 自定义输入目录 |
| `--output` | 配对input | 自定义输出目录 |
| `--allow-heuristic-fallback` | ✅ | 启用启发式规划 |
| `--candidates` | 否 | 候选数（2或3，默认3） |
| `--whisper-model` | 否 | Whisper模型（默认base） |
| `--language` | 否 | 字幕语言（默认自动识别） |
| `--overwrite` | 否 | 覆盖已有输出 |
| `--keep-temp` | 否 | 保留中间文件 |
| `--dry-run` | 否 | 只分析不渲染 |

---

## 🔄 工作流程

```
Stage 1: 扫描素材目录 → 收集视频文件
    ↓
Stage 2: PySceneDetect 场景检测 → 识别镜头边界
    ↓
Stage 3: 启发式剪辑规划 → 生成2-3个候选方案（每个16-22秒）
    ↓
Stage 4: FFmpeg视频合成 → 裁剪片段 + 拼接 + 4K放大
    ↓
Stage 5: 字幕生成
    ├─ 有API Key → GPT-4o看帧生成英文广告文案
    └─ 无API Key → Whisper语音识别生成字幕
    ↓
Stage 5.5: BGM替换 → 用BGM替换视频音轨（去除原声人声）
    ↓
Stage 6: 片尾拼接 → 拼接商标片尾（保留打字音效）
    ↓
输出: 2-3个候选成品视频
```

---

## 📁 输出文件

```
输出目录/
├── candidate_01.mp4              # 成品1（最终版）
├── candidate_02.mp4              # 成品2
├── candidate_01_clean.mp4        # 中间：干净视频（无字幕无BGM）
├── candidate_01_subtitle.mp4     # 中间：带字幕（无BGM无片尾）
├── candidate_01_bgm.mp4          # 中间：带字幕+BGM（无片尾）
└── manifest.json                 # 方案信息
```

---

## 🛠 技术栈

| 模块 | 技术 | 说明 |
|------|------|------|
| 场景检测 | PySceneDetect | 镜头边界识别 |
| 视频处理 | FFmpeg | 裁剪、拼接、放大、字幕烧录、音轨替换 |
| 字幕-AI | GPT-4o Vision | 看画面生成英文广告文案 |
| 字幕-回退 | Whisper | 语音识别生成字幕 |
| 质量评估 | OpenCV | 清晰度/亮度/对比度打分 |

---

## 📝 注意事项

1. **片尾视频**：`平销片尾.mp4` 需单独准备（不在Git仓库中），放在脚本同目录
2. **BGM音乐库**：默认读取 `C:/Users/Lenovo/Desktop/music experiment/` 目录，可修改 `BGM_LIBRARY_PATH`
3. **API Key 安全**：`OPENAI_API_KEY` 通过环境变量传入，不写入代码，推送到GitHub不会泄露
4. **中文字幕路径**：FFmpeg concat 不支持中文路径，脚本内部已用temp短路径处理

---

## 📚 相关文档

- [交接文档](./handoff.md) - 完整开发历史和问题记录
- [Prompt模板](./prompt_templates.md) - AI决策的Prompt设计

---

*本项目正在开发中*

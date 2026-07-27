# AI 自动剪辑工具 (AI Automated Cutting)

> 将多个AI生成的15秒基础素材，自动剪辑成带有BGM、字幕、公司商标的成品视频

---

## 📋 项目概述

### 目标
给AI喂入多个15秒左右的基础素材（由AI生成的优质素材），自动剪辑成**2-3个**15秒成品视频，供人工挑选最佳版本。

### 成品要求
- ✅ 合适的背景音乐 (BGM)
- ✅ 同步字幕
- ✅ 公司商标视频（商标部分不带BGM，固定拼接在最后）

### 核心痛点
- 多个素材如何挑选、如何排序
- 镜头之间的过渡如何处理
- BGM和字幕如何自动匹配
- **所有这些决策都依赖 Prompt Engineering**

---

## 🔄 工作流程

```
┌─────────────────────────────────────────────────────────────┐
│                         输入层                               │
│     多个15s基础素材 + 固定商标视频 + 背景音乐库                │
└─────────────────────────────────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
    ┌──────────┐     ┌──────────┐     ┌──────────┐
    │ Stage 1  │     │ Stage 2  │     │ Stage 3  │
    │ 素材分析  │     │ 智能剪辑  │     │ 视频合成  │
    └──────────┘     └──────────┘     └──────────┘
          │                 │                 │
          ▼                 ▼                 ▼
    场景检测             剪辑规划           视频拼接
    质量评估             镜头选择           转场添加
    瑕疵过滤             顺序优化           字幕烧录
          │                 │                 │
          └─────────────────┼─────────────────┘
                            ▼
                    ┌──────────────┐
                    │   成品视频    │
                    │  (2-3个候选)  │
                    └──────────────┘
```

### Stage 1: 素材分析
**输入**: 多个15s基础素材
**处理**:
1. 场景检测 - 使用 PySceneDetect 检测镜头切换
2. 质量评估 - AI评估每个镜头的清晰度、稳定性、曝光等
3. 瑕疵过滤 - 剔除质量不达标的镜头

**关键Prompt**: `prompt_templates.md` - 镜头质量评估

### Stage 2: 智能剪辑规划
**输入**: 合格镜头列表 + 质量评分
**处理**:
1. 镜头描述 - AI描述每个镜头的画面内容/情绪
2. 剪辑决策 - GPT分析镜头逻辑关系，生成剪辑方案
3. 时序优化 - 确定镜头顺序、转场效果

**关键Prompt**: `prompt_templates.md` - 剪辑脚本生成

### Stage 3: 视频合成
**输入**: 剪辑脚本 + 原始素材
**处理**:
1. 视频拼接 - MoviePy 按脚本拼接
2. 字幕生成 - Whisper 提取音频生成SRT
3. 字幕烧录 - FFmpeg 将字幕压入视频

**关键Prompt**: `prompt_templates.md` - 字幕样式设计

### Stage 4: 后期处理
**输入**: 带字幕视频
**处理**:
1. BGM匹配 - 根据视频情绪推荐适合的音乐
2. 混音合成 - FFmpeg 混音（人声为主，BGM为辅）
3. 商标叠加 - 将固定商标视频拼接在最后（商标部分不加BGM）

**关键Prompt**: `prompt_templates.md` - 音乐推荐

### Stage 5: 成品输出
**输出**: 2-3个15秒成品视频，供人工选择最佳版本

---

## 🛠 技术栈

| 模块 | 技术 | 说明 |
|------|------|------|
| 场景检测 | PySceneDetect | OpenCV-based 镜头检测 |
| 视频处理 | FFmpeg + MoviePy | 视频拼接、字幕烧录、混音 |
| 字幕生成 | Whisper (OpenAI) | 音频→文字→SRT |
| AI决策 | GPT-4o / Claude | Prompt Engineering 核心 |
| 视频合成 | MoviePy | Python 视频编辑 |
| BGM匹配 | GPT推荐 + 规则 | 根据情绪节奏匹配 |

---

## 📁 项目结构

```
AI-automated-cutting/
├── README.md                      # 本文件
├── 竞品调研报告.md                  # 开源项目调研
├── prompt_templates.md            # Prompt 模板库
├── 视频自动剪辑workflow学习笔记.md   # Prompt工程学习过程
├── agents.md                      # 子窗口协作规则
│
├── config/
│   ├── settings.py                # 配置文件
│   └── prompts/                   # Prompt 模板文件
│       ├── quality_assessment.md
│       ├── clip_planning.md
│       ├── shot_description.md
│       ├── bgm_matching.md
│       └── subtitle_design.md
│
├── core/
│   ├── video_analyzer.py          # 场景检测+质量评估
│   ├── clip_planner.py            # AI剪辑规划
│   ├── video_composer.py          # 视频合成
│   ├── subtitle_generator.py      # 字幕生成
│   ├── bgm_matcher.py             # BGM匹配
│   └── final_renderer.py          # 最终渲染+商标
│
├── assets/
│   ├── logo_video.mp4             # 固定商标视频
│   └── bgm/                       # BGM音乐库
│
├── workflows/
│   └── main_workflow.py           # 主工作流
│
└── output/
    └── candidates/                # 候选成品视频
```

---

## 🔑 Prompt Engineering 核心

本项目的灵魂是 **Prompt Engineering**，所有AI决策都依赖精心设计的Prompt。

### 关键原则
1. **角色设定** - 给AI定义一个专业角色（"资深剪辑师"）
2. **输入清晰** - 提供足够的上下文和约束
3. **输出格式** - 指定明确的输出格式（JSON/Markdown）
4. **评估标准** - 给出判断好坏的具体标准

### Prompt 迭代流程
```
编写Prompt → 测试效果 → 收集反馈 → 分析偏差 → 优化Prompt → 循环
```

详见: [视频自动剪辑workflow学习笔记.md](./视频自动剪辑workflow学习笔记.md)

---

## 🚀 快速开始

### 环境要求
- Python 3.9+
- FFmpeg
- OpenAI API Key (用于 GPT)

### 安装依赖
```bash
pip install -r requirements.txt
```

### 运行
```bash
python workflows/main_workflow.py --input ./assets/raw_videos --output ./output/candidates
```

---

## 📝 子窗口协作

如果需要新建 Claude Code 窗口专门做 Prompt Engineering 研究，
参考: [agents.md](./agents.md)

---

## 📚 相关资源

- [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) - 场景检测
- [MoviePy](https://github.com/jeromegrosse/moviepy) - 视频编辑
- [Whisper](https://github.com/openai/whisper) - 字幕生成
- [open-chat-video-editor](https://github.com/SCUTlihaoyu/open-chat-video-editor) - 参考项目

---

*本项目正在开发中，方案确认后开始编码*

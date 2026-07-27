# AI 自动剪辑项目 - 交接文档

> 最后更新: 2026-07-27 13:30
> 本文件记录所有交互内容，供无上下文的新对话查看

---

## 一、项目背景

### 目标
做一个 **AI 自动剪辑工具**，核心功能：
- 输入：多个15秒左右的AI生成基础素材
- 处理：AI自动剪辑 + 加BGM + 加字幕 + 叠加公司商标（商标部分不带BGM）
- 输出：2-3个15秒成品视频，供人工挑选

### 成品要求
- ✅ 合适的背景音乐 (BGM)
- ✅ 同步字幕
- ✅ 公司商标视频（商标部分不带BGM，固定拼接在最后）

### 核心痛点
所有AI决策都依赖 **Prompt Engineering**，这是项目的技术核心。

---

## 二、已完成的工作

### 2.1 竞品调研

在 GitHub 上搜索并分析了高 stars 的相关开源项目：

| 项目 | Stars | 分析结论 |
|------|-------|----------|
| [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | 5.1k | ✅ 直接采用 - 场景检测 |
| [MoviePy](https://github.com/jeromegrosse/moviepy) | 主流 | ✅ 直接采用 - 视频拼接 |
| [Whisper](https://github.com/openai/whisper) | - | ✅ 直接采用 - 字幕生成 |
| [open-chat-video-editor](https://github.com/SCUTlihaoyu/open-chat-video-editor) | 2.8k | ⚠️ 参考架构 - 生成式非剪辑式 |

详见: `竞品调研报告.md`

### 2.2 技术方案设计

设计了5阶段工作流：

```
Stage 1: 素材分析 (PySceneDetect + AI质量评估)
Stage 2: 智能剪辑规划 (GPT Prompt Engineering)
Stage 3: 视频合成 (MoviePy)
Stage 4: 字幕生成 (Whisper + FFmpeg)
Stage 5: 后期处理 (BGM匹配 + 混音 + 商标叠加)
```

### 2.3 Prompt 模板库

创建了核心 Prompt 模板，覆盖：
- 镜头质量评估 (1.1 单镜头质量评估, 1.2 批量镜头筛选)
- 剪辑规划 (2.1 剪辑脚本生成, 2.2 镜头描述生成)
- BGM匹配 (3.1 音乐推荐, 3.2 音乐与视频同步分析)
- 字幕生成 (4.1 字幕风格设计)
- 质量检验 (5.1 成品检验)
- 迭代优化 (6.1 效果反馈分析)

详见: `prompt_templates.md`

### 2.4 GitHub 仓库

- 仓库地址: https://github.com/Kunzyyy/AI-automated-cutting
- 已推送文件: README.md, 竞品调研报告.md, prompt_templates.md, agents.md

### 2.5 子窗口协作规则

创建了 agents.md，记录如何新建 Claude Code 子窗口专门做 Prompt Engineering 研究。

---

## 三、当前状态 (正在干什么)

**当前阶段：环境准备 + 验证核心组件**

正准备带用户跑一遍核心开源组件（PySceneDetect、MoviePy、Whisper），验证它们能正常工作，然后用他的素材做一个最小化的演示。

### 用户需要完成的前置任务
1. 安装 **FFmpeg** (视频处理必备)
2. 安装 **ImageMagick** (MoviePy 需要)

### 下一步计划
1. 用户装好 FFmpeg 和 ImageMagick 后
2. 我写一个"快速验证脚本"，包含 PySceneDetect + MoviePy + Whisper 的最小化使用
3. 用户跑脚本，用真实素材测试
4. 验证通过后进入 Prompt 工程阶段

---

## 四、待确认事项 (需要用户回答)

1. **基础素材的瑕疵类型** - 主要是哪些问题？（抖动、模糊、曝光、噪点？）
2. **BGM音乐库** - 是否有现成的音乐库？还是需要从零构建？
3. **商标视频** - 时长多长？放在最后还是片头？
4. **是否需要配音旁白**？还是只要字幕就够了？
5. **FFmpeg 和 ImageMagick 装好了吗？**

---

## 五、文件清单

| 文件 | 说明 |
|------|------|
| `handoff.md` | 本文件 - 交接文档 |
| `README.md` | 项目介绍 |
| `竞品调研报告.md` | 开源项目调研 + 运行指南 |
| `prompt_templates.md` | Prompt 模板库 |
| `agents.md` | 子窗口协作规则 |
| `视频自动剪辑workflow学习笔记.md` | ⚠️ 待删除 - 本地学习笔记不需要 |

---

## 六、快速开始（对新对话的建议）

如果要继续这个项目，按以下步骤：

1. **阅读 handoff.md** 了解项目背景
2. **确认环境** - 用户是否装好了 FFmpeg 和 ImageMagick
3. **运行验证脚本** - 用真实素材测试核心组件
4. **迭代 Prompt** - 根据测试结果优化 Prompt
5. **开发完整工作流** - 串联所有模块

---

*交接完成，随时可以继续*

# AI 自动剪辑项目 - 交接文档

> 最后更新: 2026-07-31（MiniMax 接入完成 + 新叙事方向讨论）
> 本文件记录所有交互内容，供无上下文的新对话查看

---

## 零、离开前最新状态（新对话优先阅读）

### 0.1 一句话结论

MiniMax-M3 已接入成功，字幕可正常生成。但当前字幕逻辑仍有根本限制：每个 clip 只看到自己段的3帧拼图，看不到完整的 clip 序列和整体叙事。需要改为"脚本做骨架、AI做创意决策"的新架构。

### 0.2 本轮完成的事情

1. MiniMax 环境变量设置（通过 `setx` 写入 Windows 用户注册表）
2. MiniMax-M3 连通测试、文字请求、多模态理解、结构化 JSON 全部验证通过
3. `run_workflow.py` 新增 Provider Adapter：
   - `get_minimax_credentials()` — 从 Windows 注册表读取 `MINIMAX_API_KEY` 和 `MINIMAX_BASE_URL`
   - `get_ai_client()` — MiniMax 优先，OpenAI 次之，返回 (client, model_name)
4. Stage 3/5 的 AI 判断逻辑同时支持 MiniMax 和 OpenAI
5. 完整运行别针品类 3 个候选视频，AI 字幕真实生效
6. **问题1修复**：字幕段数从固定4段改为每个 clip 对应一段
7. **问题2修复**：每段从1帧改为3帧（0.2/0.5/0.8时间点）横向拼接后发给AI
8. **问题3修复**：样式优化（字号200/描边8/统一顶部位置/淡紫新配色）

### 0.3 当前 API 配置

**Windows 注册表路径**：`HKEY_CURRENT_USER\Environment`
- `MINIMAX_API_KEY`：用户的 MiniMax API Key
- `MINIMAX_BASE_URL`：`https://api.minimaxi.com/v1`

**API 读取方式**：不再依赖环境变量（Git Bash 读不到），脚本通过 `winreg` 模块直接从注册表读取。

**Provider 优先级**：MiniMax → OpenAI（都有则用 MiniMax）

### 0.4 待解决问题

**核心问题**：当前字幕逻辑的架构性限制

现状：
- `build_subtitle_segments()` 按 clip 边界生成字幕段
- 每个段发给 MiniMax-M3 时只看到自己的 3 帧拼图
- AI 不知道前后的 clip 是什么，无法做全局叙事规划

后果：
- 字幕文案可能与相邻 clip 脱节
- 叙事逻辑无法跨 clip 统一
- 角色分配（emotion_hook/quality/memory/customization）循环复用，AI 无法理解"这个 clip 为什么是这个角色"

### 0.5 新架构方向（已讨论，待实现）

**核心思路：脚本做骨架，AI 做创意决策**

```
素材
  ↓
脚本 Stage 1-2：扫描 + 场景检测
  ↓
脚本 Stage 3：生成 clip 序列（时间范围、来源文件）
  ↓
MiniMax-M3 一次性理解完整 clip 序列 + 关键帧
  ↓
AI 输出：每个 clip 的字幕文案 + 镜头角色 + 颜色 + 位置
  ↓
脚本 Stage 4-6：按 AI 决策渲染视频（FFmpeg 烧字幕+BGM+片尾）
```

**关键变化**：
- 不再让 AI 逐段生成字幕然后脚本拼凑
- 一次性把完整 clip 序列发给 AI（包含时间戳、来源、每个 clip 的关键帧摘要）
- AI 做全局叙事规划：哪段是什么角色、文案应该是什么
- 脚本只负责执行，不做创意判断

### 0.6 本轮测试记录

**单元测试**：9项全部通过（`python test_workflow.py`）

**完整运行**：别针品类，3个候选，18-19秒/个，2160×3840，AI字幕生效

---

## 一、项目现状

### 当前阶段
**MiniMax 已接入，字幕可工作，但架构有根本限制** — 下一步应转向"脚本做骨架、AI做创意"的新架构。

### 已修复的问题（2026-07-31）
1. ✅ **字幕段数 = 镜头数**：每个 clip 对应一个字幕段，角色循环复用 SUBTITLE_ROLES
2. ✅ **每段3帧拼接**：每段取0.2/0.5/0.8时间点，横向拼成一张图发给MiniMax
3. ✅ **样式优化**：字号200/描边8/统一顶部位置/淡紫配色

### 核心未解问题
- AI 看不到完整的 clip 序列，无法做真正的叙事规划
- 角色分配是循环复用，不是 AI 理解内容后动态决定

---

## 二、本次交互完整记录

### 2.1 MiniMax 接入

**环境变量设置**：通过 `setx` 命令写入 Windows 用户注册表，Git Bash 环境变量不通用。

**Provider Adapter**：
```python
def get_minimax_credentials() -> tuple[Optional[str], Optional[str]]:
    """从 Windows 注册表读取 MiniMax 用户变量。"""
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ)
    api_key, _ = winreg.QueryValueEx(key, "MINIMAX_API_KEY")
    base_url, _ = winreg.QueryValueEx(key, "MINIMAX_BASE_URL")
    winreg.CloseKey(key)
    return (api_key, base_url)

def get_ai_client():
    """返回可用的 AI client（MiniMax 优先，OpenAI 次之）。"""
    minimax_key, minimax_url = get_minimax_credentials()
    if minimax_key and minimax_url:
        return OpenAI(api_key=minimax_key, base_url=minimax_url), "MiniMax-M3"
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key and openai_available:
        return OpenAI(api_key=openai_key), "gpt-4o"
    return None, None
```

### 2.2 三处代码修改

**修改1：环境变量检查 + AI规划判断**
```python
# 旧
has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))
# 新
ai_client, ai_model = get_ai_client()
has_ai = ai_client is not None
```

**修改2：build_subtitle_segments 重写**
```python
# 旧：按时间等分4段
# 新：每个 clip 对应一段，角色循环复用 SUBTITLE_ROLES
def build_subtitle_segments(plan, total_duration, category):
    segments = []
    cursor = 0.0
    for index, clip in enumerate(plan.clips):
        role = SUBTITLE_ROLES[index % len(SUBTITLE_ROLES)]
        segments.append(SubtitleSegment(
            start=round(cursor, 2),
            end=round(cursor + float(clip.duration), 2),
            text=get_subtitle_copy(category, role),
            role=role,
            position=SUBTITLE_ROLE_POSITION[role],
            color=SUBTITLE_ROLE_COLOR[role],
        ))
        cursor += float(clip.duration)
    return segments
```

**修改3：apply_transcript_role_evidence 修复**
```python
# 旧：当 segments > 4 时 remaining_roles.pop(0) 会 IndexError
# 新：超过4个时循环复用 SUBTITLE_ROLES
remaining_roles = [role for role in SUBTITLE_ROLES if role not in used_roles]
role_cycle = itertools.cycle(SUBTITLE_ROLES)
next_role = next(role_cycle)
for index in range(len(segments)):
    if index not in assignments:
        if remaining_roles:
            assignments[index] = remaining_roles.pop(0)
        else:
            while next_role in used_roles:
                next_role = next(role_cycle)
            assignments[index] = next_role
            next_role = next(role_cycle)
```

### 2.3 新架构讨论

**现状架构问题**：
- 字幕是"脚本生成4段时间 → AI逐段填内容"的拼凑模式
- AI 看不到完整 clip 序列，无法做全局叙事规划
- 角色分配是预设循环，不是 AI 理解内容后动态决定

**新架构方向**：
```
脚本 Stage 3 输出 clip 序列（时间+来源）
    ↓
MiniMax-M3 一次性理解完整序列（包含关键帧摘要）
    ↓
AI 输出每个 clip 的字幕文案 + 角色 + 颜色 + 位置
    ↓
脚本 Stage 4-6 渲染
```

**MiniMax-M3 提示词思路**（待实现）：
```
You are a short-form product ad editor. I will give you a sequence of clips with their timestamps, source files, and keyframe descriptions.

Clip sequence:
1. [0.0-1.6s] source: 素材A.mp4 - 女士手拿别针产品，白色背景
2. [1.6-3.1s] source: 素材A.mp4 - 特写别针细节，金属质感
...

Based on this sequence, plan the narrative and return:
{
  "clips": [
    {"index": 0, "role": "emotion_hook", "caption": "Keep their love...", "color": "white", "position": "top"},
    ...
  ],
  "overall_narrative": "从情感钩子开始，逐步展示品质，最终引导定制"
}

Requirements:
- Each clip gets one caption of 2-10 words
- The narrative should flow naturally across clips
- Use emotional, lifestyle ad language
- Return valid JSON only
```

---

## 三、已完成的工作

### 3.1 核心脚本

**文件：** `run_workflow.py`

**已实现的功能：**
| 阶段 | 功能 | 验证状态 |
|------|------|---------|
| Stage 1 | 扫描基础素材目录 | ✅ |
| Stage 2 | PySceneDetect场景检测 | ✅ |
| Stage 3 | 启发式剪辑规划 | ✅ |
| Stage 4 | FFmpeg视频合成+upscale | ✅ 720p→2160p |
| Stage 5 | AI视觉字幕（MiniMax-M3 / GPT-4o）/ Whisper回退 | ✅ |
| Stage 5.5 | BGM替换音轨 | ✅ |
| Stage 6 | 商标片尾拼接（保留打字音效） | ✅ |

### 3.2 验证结果

**MiniMax API 测试**：
- 文字请求：✅ Model: MiniMax-M3
- 多模态理解：✅ 正确识别产品/人物/动作/场景
- 结构化JSON：✅ 正确输出

**完整运行**：别针 3 候选，18-19秒/个，2160×3840

---

## 四、踩过的坑

### 4.1 环境变量在 Git Bash 里读不到
- **原因**：Windows 用户变量和 Git Bash 是两套环境
- **解决**：脚本通过 `winreg` 模块直接从 Windows 注册表读取

### 4.2 apply_transcript_role_evidence 超过4段会崩溃
- **原因**：`remaining_roles.pop(0)` 在列表为空时抛出 IndexError
- **解决**：超过4个时循环复用 SUBTITLE_ROLES

---

## 五、使用方法

### 5.1 环境准备

```bash
# 基础依赖
pip install opencv-python scenedetect whisper openai

# FFmpeg 需 7.0+，需在系统PATH中

# API Key（通过 Windows 注册表，不需要手动设置环境变量）
# MINIMAX_API_KEY 和 MINIMAX_BASE_URL 已通过 setx 写入
```

### 5.2 运行命令

```bash
# 方式1：预设品类
python run_workflow.py --category 别针 --allow-heuristic-fallback --overwrite

# 方式2：自定义目录
python run_workflow.py --input "D:\素材目录" --output "D:\输出目录" --allow-heuristic-fallback --overwrite
```

### 5.3 参数说明

| 参数 | 必选 | 说明 |
|------|------|------|
| `--category` | 三选一 | 摆件/别针/航海贴/衣服/露营贴 |
| `--input` | 三选一 | 自定义输入目录 |
| `--output` | 配对input | 自定义输出目录 |
| `--allow-heuristic-fallback` | ✅必须 | 启用启发式规划（无AI时） |
| `--candidates` | 否 | 候选数（默认3） |
| `--overwrite` | 否 | 覆盖已有输出 |
| `--keep-temp` | 否 | 保留中间文件 |
| `--dry-run` | 否 | 只分析不渲染 |

---

## 六、目录结构

```
视频自动剪辑workflow/
├── run_workflow.py             # 主脚本
├── 平销片尾.mp4                 # 商标视频
├── handoff.md                  # 本文件
├── README.md                   # 项目说明
├── prompt_templates.md         # Prompt模板
├── quality_exclusions.json     # 质量排除配置
├── test_workflow.py            # 测试脚本
└── fonts/Lato-Bold.ttf         # 字幕字体
```

---

## 七、快速检查清单

新对话接手时，检查：

- [ ] 脚本能否运行？（`python run_workflow.py --help`）
- [ ] MiniMax 注册表变量是否存在？（`python -c "import winreg; k=winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Environment',0,winreg.KEY_READ); print(winreg.QueryValueEx(k,'MINIMAX_API_KEY')[0][:20])"`）
- [ ] 单元测试通过？（`python test_workflow.py`）
- [ ] AI字幕是否生效？（看日志是否出现 `MiniMax-M3 Vision`）

---

*交接完成，新对话只需阅读本文档即可接手任务*

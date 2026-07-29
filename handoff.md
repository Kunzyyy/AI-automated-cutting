# AI 自动剪辑项目 - 交接文档

> 最后更新: 2026-07-29
> 本文件记录所有交互内容，供无上下文的新对话查看

---

## 一、项目现状

### 当前阶段
**功能基本完成阶段** - 4个核心问题已修复，AI视觉字幕功能已实现（待实测）。脚本可运行，2个候选成品可正常生成。

### 已修复的问题（2026-07-29）
1. ✅ **AI人声去除** - 不再提取/混合源视频原音频，只使用BGM作为音轨
2. ✅ **静态字幕** - 不再逐字出现，每条字幕整句一次性显示
3. ✅ **字幕样式打磨** - 字号72、描边4、阴影2、位置上移（MarginV=150）
4. ✅ **片尾打字音效保留** - 片尾有音频轨时保留原始音频（打字音效），无音频才补静音
5. ⚠️ **AI视觉字幕** - GPT-4o看帧生成英文文案，代码已写好，本机无Key未实测

### 待办事项
- [ ] AI视觉字幕在公司电脑上实测（需设置 `OPENAI_API_KEY` 环境变量）
- [ ] 字幕位置动态切换（顶部/中部/底部）—— 当前仍为固定底部
- [ ] 主体时长优化（当前22s左右，目标16-22s，基本达标）
- [ ] **跨素材形态不一致的穿模检测**（见下文 2026-07-29 下午段）

---

## 二、本次交互完整记录

### 2.1 交互概述
用户要求改进 `run_workflow.py`，解决4个问题 + 新增AI视觉字幕功能。

### 2.2 用户提出的4个问题

#### Issue 1: 去除AI人声
- **用户描述**：成品里有AI人声/旁白，需要完全去掉
- **根因**：`stage5_mix_bgm()` 会提取源视频原音频并通过 `amix` 与BGM混合，源视频里的人声被保留了下来
- **修复**：删除提取原音频和amix混合的步骤，改为直接用BGM替换视频音轨（`-map 1:a`）
- **改动位置**：`run_workflow.py` 第1007-1042行（Step 3和Step 4合并为一个替换命令）

#### Issue 2: 静态字幕
- **用户描述**：字幕不需要逐字出现，直接放一段文字就行，每1-2个镜头切换一条
- **根因**：`progressive_ass_events()` 函数把每句话拆成逐词/逐字前缀，创建重叠的ASS事件，形成karaoke式逐字显示
- **修复**：在 `stage5_generate_subtitle()` 的ASS事件写入循环中，不再调用 `progressive_ass_events()`，改为每条字幕直接写一行完整文本的 `Dialogue`
- **改动位置**：`run_workflow.py` 第834-842行

#### Issue 3: 字幕位置/字体/颜色
- **用户描述**：字幕位置不应该只在最下面，参考手工成品是顶部/中部/底部都会用
- **当前修复**：基础样式打磨——字号从84改为72，描边从1改为4，阴影从1改为2，MarginV从260改为150（位置稍上移）
- **未完成**：动态位置切换（随镜头变化顶部/中部/底部）—— 需要后续增强，当前仍为固定底部居中
- **改动位置**：`run_workflow.py` ASS Style 行

#### Issue 4: 片尾打字音效
- **用户描述**：片尾视频 `平销片尾.mp4` 有一段打字音效，但成品里听不到
- **根因**：`stage6_append_logo()` 中用 `anullsrc`（静音）替换了片尾原始音频
- **修复**：增加分支判断——如果片尾视频有音频轨，保留原始音频（`-c:a aac`）；只有无音频时才用 `anullsrc` 补静音
- **改动位置**：`run_workflow.py` 第1112-1141行

### 2.3 AI视觉字幕功能（新增）

#### 用户需求
- 字幕文案是英文
- AI"看"每个镜头的画面，理解内容后生成合适的字幕
- 字幕时间轴不固定，根据镜头内容动态决定
- 参考帧在 `D:\doyobest\自动剪辑研究\别针\temp_frames\` 等目录

#### 用户选择的方案
- API: GPT-4o（支持视觉理解）
- 文案: AI完全自由生成（无品牌调性约束）
- 执行顺序: 先修4个基础问题，再做AI字幕

#### 实现细节
**新函数 `generate_ai_subtitles()`**：
1. 从合成视频提取每个镜头的中间帧（JPEG）
2. 按2-4秒一段切分时间轴（对齐到 `plan.clips` 的边界）
3. 每帧发给 GPT-4o Vision API（`detail: "low"` 省token）
4. Prompt: "Look at the product image and write ONE short English subtitle line (max 8 words). Style: elegant, emotional, lifestyle ad captions."
5. 收集所有文案，生成ASS文件
6. FFmpeg烧录字幕 + 像素验证

**回退逻辑**：
- 主函数中先尝试AI视觉字幕
- 如果 `openai` 库未安装 / `OPENAI_API_KEY` 未设置 / API调用失败 → 自动回退到Whisper流程
- 回退对用户透明，不影响出片

**改动位置**：
- `run_workflow.py` 第34-44行：新增 `from openai import OpenAI` 导入检查
- `run_workflow.py` 第669-810行：新增 `generate_ai_subtitles()` 函数
- `run_workflow.py` 第1497-1512行：主函数中Stage 5的调用逻辑

#### 未实测原因
本机没有设置 `OPENAI_API_KEY` 环境变量，所以AI字幕路径被跳过，走了Whisper回退。代码逻辑上应该没问题，但没实测不能100%确认。

### 2.4 关于 API Key 的决策
- 用户计划把代码git到GitHub，在公司电脑上运行
- API Key 通过环境变量 `OPENAI_API_KEY` 传入，**不写入代码**
- 代码运行时检查环境变量，有则用GPT-4o，无则回退Whisper
- 这样推到GitHub不会泄露Key，公司电脑上设好环境变量即可

### 2.5 Git 推送
- 仓库: https://github.com/Kunzyyy/AI-automated-cutting
- 提交: `7fb9435` - "修复4个基础问题 + 新增AI视觉字幕功能"
- 推送了6个文件：run_workflow.py, handoff.md, README.md, prompt_templates.md, quality_exclusions.json, test_workflow.py
- **未推送**: `平销片尾.mp4`（56MB太大），公司电脑需单独拷贝

### 2.6 跨镜头穿模检测（2026-07-29 下午）

#### 用户反馈的问题
candidate_01 在 5-7s 有穿模：
- 5s：横别针（手展示）
- 6s：撕开塑料袋，里面的别针是斜的 ← 与 5s 形态不一致
- 7s：女士毛衣上的别针（又是横的）

属于"跨素材的产品形态不一致"——脚本的启发式规划从不同源素材里选了产品形态不同的 clip 拼在一起。

#### 实际新增的实现
- 函数 `extract_clip_keypoints()`：用 cv2 saliency 检测主体区域 + ORB 关键点
- 函数 `compute_clip_match_rate()`：BFMatcher 计算匹配率
- 函数 `filter_inconsistent_adjacent_clips()`：对候选方案内的相邻 clip 做一致性检查
- 集成到 `stage3_heuristic_planning()`：每个候选方案生成后调用

#### 设计细节
- **match_threshold=0.2**（初版 0.7 误杀太多，调到 0.2 才平衡）
- **同源限制**：`same_source` 判断，跨源素材的相邻 clip 跳过检测
  - 原因：不同源素材的产品形态本就允许不同
  - 副作用：5-7s 这种跨素材形态不一致**漏检**
- 只丢 clip 不补选（用户选择"宁可少出不能穿模"）

#### 验证结果
- 提交 `bcaedab` 推送成功
- 3个候选全部生成（19.35s / 18.76s / 19.62s）
- 匹配率 0.14 的同源穿模被正确丢弃
- **跨素材形态不一致（5-7s）仍然漏检**——确认未修复

#### 未解决：跨素材形态不一致
**根因**：`same_source` 限制让跨源 clip 跳过检测。

**未来方案**（待用户决定）：
1. **全局聚类**：先对所有 clip 做主体特征聚类（k-means），每个候选方案只从同一聚类里选
2. **去掉 same_source 限制**：保留跨素材检测，但阈值需要更精细（比如 0.05-0.1），可能误杀
3. **人工黑名单兜底**：在 `quality_exclusions.json` 里手动标注问题时段（已经支持）

**提交记录**：
- `bcaedab` - 新增跨镜头主体一致性检查（同源内 ORB+显著性）

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
| Stage 5 | AI视觉字幕（GPT-4o）/ Whisper回退 | ⚠️ 回退路径已验证，AI路径待测 |
| Stage 5.5 | BGM替换音轨（不再混合原音频） | ✅ |
| Stage 6 | 商标片尾拼接（保留打字音效） | ✅ |

### 3.2 验证结果

**已验证的品类：**
- 别针：2个候选成品生成，22.89s / 22.35s，2160×3840，H264+AAC(48kHz立体声)
- 字幕烧录验证通过（像素对比法）
- 片尾音频轨保留成功

**输出文件：**
```
{输出目录}\
├── candidate_01.mp4           # 成品1（最终版）
├── candidate_02.mp4
├── candidate_01_clean.mp4     # 中间：干净视频（无字幕无BGM）
├── candidate_01_subtitle.mp4 # 中间：带字幕（无BGM无片尾）
├── candidate_01_bgm.mp4      # 中间：带字幕+BGM（无片尾）
└── manifest.json
```

---

## 四、踩过的坑（不要再碰）

### 4.1 PySceneDetect API变更
```
错误：from scenedetect import VideoManager  # 旧版API已废弃
正确：from scenedetect import open_video, SceneManager, ContentDetector
```

### 4.2 Stage 4 不能去掉音频
```
错误：ffmpeg ... -an  # 去掉音频导致Whisper无法识别
正确：ffmpeg ... -c:a aac -b:a 128k  # 保留并转码音频
```

### 4.3 Whisper 需要直接音频文件
```
错误：model.transcribe(video_path)  # 不能直接读取视频
正确：先用ffmpeg提取音频到wav，再whisper识别
```

### 4.4 字体名必须是系统存在的
```
错误：Source Han Sans SC  # 可能不存在
正确：Microsoft YaHei / SimHei / Arial
```

### 4.5 FFmpeg concat 不支持中文路径
```
错误：file 'D:/路径/中文.mp4'  # 失败
正确：先复制到temp短路径，用相对路径拼接
```

### 4.6 片尾音频不能被anullsrc覆盖
```
错误：一律用 anullsrc 替换片尾音频  # 打字音效丢失
正确：先检查片尾是否有音频轨，有则保留原始音频，无才补anullsrc
```

### 4.7 BGM混合不能保留源视频人声
```
错误：提取原视频音频 + amix混合BGM  # 源视频人声残留
正确：直接用BGM替换音轨，-map 1:a
```

---

## 五、使用方法

### 5.1 环境准备

```bash
# 基础依赖
pip install opencv-python scenedetect whisper openai

# FFmpeg 需 7.0+，需在系统PATH中

# 可选：设置 OpenAI API Key 启用 AI 视觉字幕
# 不设置则自动回退到 Whisper 语音识别
set OPENAI_API_KEY=sk-你的Key       # Windows
export OPENAI_API_KEY=sk-你的Key     # Linux/Mac
```

### 5.2 运行命令

```bash
# 方式1：预设品类
python run_workflow.py --category 别针 --allow-heuristic-fallback

# 方式2：自定义目录
python run_workflow.py --input "D:\素材目录" --output "D:\输出目录" --allow-heuristic-fallback

# 方式3：覆盖已有输出
python run_workflow.py --category 别针 --allow-heuristic-fallback --overwrite
```

### 5.3 参数说明

| 参数 | 必选 | 说明 |
|------|------|------|
| `--category` | 三选一 | 摆件/别针/航海贴/衣服/露营贴 |
| `--input` | 三选一 | 自定义输入目录 |
| `--output` | 配对input | 自定义输出目录 |
| `--allow-heuristic-fallback` | ✅必须 | 启用启发式规划（无AI时） |
| `--candidates` | 否 | 候选数（2或3，默认3） |
| `--whisper-model` | 否 | Whisper模型（默认base） |
| `--language` | 否 | 字幕语言（默认自动识别） |
| `--overwrite` | 否 | 覆盖已有输出 |
| `--keep-temp` | 否 | 保留中间文件 |
| `--dry-run` | 否 | 只分析不渲染 |

---

## 六、目录结构

```
视频自动剪辑workflow/
├── run_workflow.py             # 主脚本（全部逻辑在这一个文件里）
├── 平销片尾.mp4                 # 商标视频（1080×1920, 3.37s, 含打字音效）
├── handoff.md                  # 本文件
├── README.md                   # 项目说明
├── prompt_templates.md         # Prompt模板
├── quality_exclusions.json     # 质量排除配置
└── test_workflow.py            # 测试脚本
```

---

## 七、成品规格

| 参数 | 目标值 | 当前实际 |
|------|--------|---------|
| 输出分辨率 | 2160×3840 | ✅ 2160×3840 |
| 主体时长 | 16-22秒 | ✅ ~22s |
| 总时长 | 主体+3.37s片尾 | ✅ ~26s |
| 字幕 | 白字黑描边、静态整句显示 | ✅ |
| 字幕文案 | AI看画面生成英文 | ⚠️ 待实测 |
| 片尾音频 | 保留打字音效 | ✅ |
| BGM | 替换音轨，无人声 | ✅ |

---

## 八、快速检查清单

新对话接手时，检查以下问题：

- [ ] 脚本能否运行？（`python run_workflow.py --help`）
- [ ] 字幕是否烧入视频？（播放candidate_01.mp4检查）
- [ ] 片尾是否有打字音效？（播放最后3秒）
- [ ] 成品是否有人声？（应该是纯BGM）
- [ ] 设置 `OPENAI_API_KEY` 后AI字幕是否生效？

---

*交接完成，新对话只需阅读本文档即可接手任务*

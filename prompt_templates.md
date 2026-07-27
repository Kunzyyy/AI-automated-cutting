# AI视频剪辑 Prompt 模板库

> 本文件收录AI视频剪辑工作流中使用的核心Prompt模板
> 配合 `prompt_engineering学习过程.md` 阅读效果更佳

---

## 一、镜头质量评估 Prompt

### 1.1 单镜头质量评估

```markdown
你是一个专业的视频质量评估专家。请评估以下视频镜头的质量。

【镜头信息】
- 镜头编号: {shot_id}
- 时长: {duration}s
- 场景类型: {scene_type}
- 预估情绪: {emotion}

【评估维度】
请从以下维度评估(每个维度1-10分):
1. 画面清晰度: 主体是否清晰可见?
2. 曝光正常度: 是否有曝光过度或不足?
3. 画面稳定性: 是否有明显抖动?
4. 构图美感: 构图是否符合审美?
5. 内容价值: 画面内容是否有意义/有趣?

【输出要求】
请以JSON格式输出:
{
  "shot_id": "镜头编号",
  "scores": {
    "clarity": 分数,
    "exposure": 分数,
    "stability": 分数,
    "composition": 分数,
    "content_value": 分数
  },
  "total_score": 总分,
  "issues": ["问题1", "问题2"],
  "verdict": "PASS/FAIL",
  "reason": "简要理由"
}

【决策标准】
- total_score >= 35: PASS(合格)
- total_score < 35: FAIL(不合格)
- 如果有任何维度得分<=2，直接FAIL
```

### 1.2 批量镜头筛选

```markdown
你是一个专业的视频剪辑师。现在有{num_shots}个候选镜头，需要选出最优质的{N}个。

【镜头列表】
{shot_list}

【筛选标准】
1. 画面质量(无抖动、无模糊、曝光正常)
2. 内容相关性(与目标主题相关)
3. 情绪价值(有表现力、感染力)
4. 构图美感(符合三分法、视觉焦点)
5. 多样性(不同角度、不同景别)

【任务】
请选出最优质的{N}个镜头，并给出排序。

【输出格式】
{
  "selected_shots": [
    {"rank": 1, "shot_id": "xxx", "reason": "理由"},
    {"rank": 2, "shot_id": "xxx", "reason": "理由"},
    ...
  ],
  "rejected_shots": [
    {"shot_id": "xxx", "reason": "淘汰理由"},
    ...
  ]
}
```

---

## 二、剪辑规划 Prompt

### 2.1 剪辑脚本生成

```markdown
你是一个资深视频剪辑师，有{years}年从业经验。请为以下素材设计剪辑方案。

【素材信息】
- 目标时长: {target_duration}秒
- 目标情绪: {target_emotion}
- 视频类型: {video_type}

【可用镜头】
{available_shots}

【剪辑要求】
1. 时长精确控制在{target_duration}秒
2. 情绪曲线需要有起伏(引入→发展→高潮→结尾)
3. 逻辑连贯，有故事性
4. 注意镜头间的转场自然
5. 充分利用每个镜头的最佳部分

【转场建议】
- 同场景短镜头: 跳切
- 不同场景: 淡入淡出或交叉叠化
- 情绪转折: 硬切

【输出格式】
{
  "plan_id": "plan_001",
  "total_duration": 总时长,
  "shots": [
    {
      "shot_id": "镜头ID",
      "start_time": 开始时间,
      "end_time": 结束时间,
      "duration": 持续时长,
      "transition": "转场方式",
      "rationale": "选择理由"
    }
  ],
  "emotion_curve": "情绪曲线描述",
  "highlights": ["亮点1", "亮点2"]
}
```

### 2.2 镜头描述生成(用于AI理解素材)

```markdown
你是一个视频内容分析师。请为以下镜头生成描述，供后续剪辑参考。

【镜头信息】
- 时长: {duration}
- 视频路径: {video_path}

【分析要求】
1. 描述画面主体和背景
2. 判断场景类型(室内/室外/人像/风景/动作等)
3. 评估情绪基调(欢快/紧张/平静/悬疑等)
4. 标注视觉亮点(特殊角度、构图、色彩)
5. 识别音频内容(人声/音乐/环境音)

【输出格式】
{
  "shot_id": "镜头ID",
  "visual_description": "画面描述",
  "scene_type": "场景类型",
  "emotion_tone": "情绪基调",
  "visual_highlights": ["亮点1", "亮点2"],
  "audio_content": "音频内容",
  "suitability": ["适用场景1", "适用场景2"]
}
```

---

## 三、BGM匹配 Prompt

### 3.1 音乐推荐

```markdown
你是一个专业的音乐总监。请为以下视频推荐最合适的背景音乐。

【视频信息】
- 时长: {duration}秒
- 情绪基调: {emotion}
- 视频类型: {video_type}
- 节奏快慢: {pace}(快/中/慢)

【可选音乐库】
{bgm_list}

【匹配原则】
1. 情绪匹配: 音乐情绪与视频情绪一致
2. 节奏匹配: 音乐节奏与视频节奏协调
3. 文化匹配: 音乐风格与目标受众匹配
4. 版权安全: 优先选择无版权音乐

【输出要求】
推荐前3首音乐，并说明理由。

【输出格式】
{
  "recommendations": [
    {
      "rank": 1,
      "music_id": "音乐ID",
      "music_name": "音乐名称",
      "match_score": 匹配度(0-100),
      "reason": "推荐理由"
    },
    ...
  ]
}
```

### 3.2 音乐与视频同步分析

```markdown
你是一个视频音乐同步专家。请分析音乐与视频的同步点。

【视频信息】
- 镜头列表: {shot_list}
- 总时长: {duration}秒

【音乐信息】
- 音乐名称: {music_name}
- 音乐时长: {music_duration}秒
- BPM: {bpm}
- 关键节拍点: {beat_points}

【任务】
1. 识别视频中的关键动作/情绪点
2. 将这些点与音乐节拍对齐
3. 给出调整建议(加快/放慢/裁剪)

【输出格式】
{
  "sync_points": [
    {
      "video_moment": "视频时刻",
      "music_beat": "对应节拍",
      "adjustment": "调整建议"
    }
  ],
  "overall_assessment": "整体评估",
  "suggestions": ["建议1", "建议2"]
}
```

---

## 四、字幕生成 Prompt

### 4.1 字幕风格设计

```markdown
你是一个专业字幕设计师。请为以下视频设计字幕样式。

【视频信息】
- 视频类型: {video_type}
- 目标受众: {audience}
- 视频风格: {style}

【设计要求】
1. 字体: 清晰易读
2. 颜色: 与视频背景对比明显
3. 位置: 不遮挡主体
4. 动画: 简洁大方
5. 标点: 合理断句

【输出格式】
{
  "font": {
    "family": "字体名称",
    "size": "字号",
    "color": "颜色",
    "stroke": "描边设置"
  },
  "position": {
    "horizontal": "水平位置",
    "vertical": "垂直位置"
  },
  "animation": {
    "type": "动画类型",
    "duration": "持续时间"
  },
  "style_notes": "风格说明"
}
```

---

## 五、质量检验 Prompt

### 5.1 成品检验

```markdown
你是一个视频质量审核专家。请审核以下成品视频。

【视频信息】
- 视频路径: {video_path}
- 目标时长: {target_duration}秒
- 要求: {requirements}

【审核清单】
1. 时长是否正确(误差<0.5秒)?
2. 画面是否清晰流畅?
3. 字幕是否正确同步?
4. BGM是否合适且音量适中?
5. 商标是否正确添加?
6. 整体观感是否专业?

【输出格式】
{
  "passed": true/false,
  "issues": [
    {
      "category": "问题类别",
      "description": "问题描述",
      "severity": "严重程度(HIGH/MEDIUM/LOW)",
      "fix_suggestion": "修复建议"
    }
  ],
  "overall_score": 综合评分(0-100),
  "summary": "总结"
}
```

---

## 六、迭代优化 Prompt

### 6.1 效果反馈分析

```markdown
你是一个AI工作流优化专家。请分析以下执行结果，提出改进建议。

【任务目标】
{objective}

【实际结果】
{actual_result}

【用户反馈】
{user_feedback}

【分析要求】
1. 识别与目标的偏差
2. 分析可能的原因
3. 提出具体的Prompt优化建议
4. 建议是否需要调整工作流

【输出格式】
{
  "deviation_analysis": "偏差分析",
  "root_causes": ["原因1", "原因2"],
  "prompt_improvements": [
    {
      "prompt_name": "Prompt名称",
      "current_version": "当前版本",
      "improved_version": "改进版本",
      "improvement_rationale": "改进理由"
    }
  ],
  "workflow_adjustments": ["调整1", "调整2"]
}
```

---

*持续更新中...*

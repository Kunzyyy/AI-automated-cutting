# assets

这个目录放你自己的素材，仓库不收录里面的媒体文件（`.gitignore` 已排除）。

两样东西都是**可选**的，不放也能出片：

```text
assets/
├─ end-card.mp4        品牌片尾：竖屏，自带音轨，一般 3~4 秒
└─ music/              无人声背景音乐库，放 .mp3 或 .wav
   ├─ warm-piano.mp3
   ├─ upbeat-guitar.mp3
   └─ calm-ambient.mp3
```

放好之后，在项目根目录的 `auto-cut.settings.json` 里填上路径：

```json
{
  "end_card": "assets/end-card.mp4",
  "bgm_library": "assets/music"
}
```

## 音乐文件怎么命名

选曲是按文件名匹配的：AI 给出当条广告的情绪关键词（如 `warm`、`calm`、`elegant`、`emotional`、`upbeat`），
程序在库里找文件名或所在子目录名命中关键词最多的那首。所以文件名里写上情绪词最有效，
例如 `warm-piano-loop.mp3`、`upbeat-pop.mp3`；也可以用子目录分类：`music/warm/piano-01.mp3`。

音乐必须无人声（人声会和字幕打架），并确认你拥有商用授权。免费可商用的常见来源：
Pixabay Music、Free Music Archive（注意逐首确认许可证）、YouTube Audio Library。

## 片尾要求

竖屏、**自带音轨**（没有音轨会直接报错），分辨率会自动缩放到成片尺寸。
片尾时长不计入正文的 15 秒目标。

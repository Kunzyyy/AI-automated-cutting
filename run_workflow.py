#!/usr/bin/env python3
"""
AI视频自动剪辑工作流 - 通用脚本
功能：将基础素材自动剪辑成2-3个4K竖屏候选成品
不支持BGM（本次版本）
"""

import argparse
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List

# 依赖检查
import cv2
import numpy as np
cv2_available = cv2 is not None

try:
    from scenedetect import open_video, SceneManager, ContentDetector
    scenedetect_available = True
except ImportError:
    scenedetect_available = False

try:
    import whisper
    whisper_available = True
except ImportError:
    whisper_available = False


# ============== 配置 ==============
DEFAULT_CATEGORIES = {
    "摆件": "D:/doyobest/自动剪辑研究/摆件",
    "别针": "D:/doyobest/自动剪辑研究/别针",
    "航海贴": "D:/doyobest/自动剪辑研究/航海贴",
    "衣服": "D:/doyobest/自动剪辑研究/衣服",
    "露营贴": "D:/doyobest/自动剪辑研究/露营贴",
}

LOGO_FILE = "D:/doyobest/视频自动剪辑workflow/平销片尾.mp4"
BGM_LIBRARY_PATH = "C:/Users/Lenovo/Desktop/music experiment"  # TODO: 后续改为相对路径
OUTPUT_WIDTH = 2160
OUTPUT_HEIGHT = 3840
TARGET_DURATION_MIN = 16
TARGET_DURATION_MAX = 22
LOGO_DURATION = 3.37
CLIP_DURATION_TARGET = 2.0

# 品类偏好BGM类型（逗号分隔，越靠前优先级越高）
BGM_PREFERENCE_BY_CATEGORY = {
    "摆件": "warm,elegant,emotional,calm,upbeat",
    "别针": "elegant,calm,warm,emotional,upbeat",
    "航海贴": "calm,emotional,warm,elegant,upbeat",
    "衣服": "upbeat,warm,calm,elegant,emotional",
    "露营贴": "calm,warm,emotional,elegant,upbeat",
}


# ============== 数据结构 ==============
@dataclass
class VideoInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


@dataclass
class Scene:
    index: int
    start_time: float
    end_time: float
    duration: float
    quality_score: float = 0.0
    is_suspicious: bool = False


@dataclass
class Clip:
    source_file: str
    start_time: float
    end_time: float
    duration: float


@dataclass
class CandidatePlan:
    id: int
    clips: list
    total_duration: float
    emotion_curve: str = ""


# ============== 日志配置 ==============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)


# ============== FFmpeg工具函数 ==============
def run_ffmpeg(cmd: list, description: str = "FFmpeg") -> bool:
    """运行FFmpeg命令，返回是否成功"""
    logger.info(f"{description}: {' '.join(str(x) for x in cmd[:5])}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"{description} 失败: {result.stderr[-500:]}")
        return False
    return True


def get_video_info(video_path: str) -> Optional[VideoInfo]:
    """使用FFprobe获取视频信息"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
        "-show_entries", "stream=width,height,codec_type,r_frame_rate",
        "-of", "json",
        video_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"FFprobe失败: {video_path}")
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.error(f"FFprobe JSON解析失败: {video_path}")
        return None

    streams = data.get("streams", [])
    format_info = data.get("format", {})
    duration = float(format_info.get("duration", 0))

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video_stream:
        logger.error(f"无视频流: {video_path}")
        return None

    width = video_stream.get("width", 0)
    height = video_stream.get("height", 0)

    fps_str = video_stream.get("r_frame_rate", "30/1")
    if "/" in fps_str:
        fps_parts = fps_str.split("/")
        fps = float(fps_parts[0]) / float(fps_parts[1]) if fps_parts[1] != "0" else 30.0
    else:
        fps = float(fps_str)

    return VideoInfo(
        path=video_path,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        has_audio=audio_stream is not None
    )


def extract_frame(video_path: str, time_sec: float, output_path: str) -> bool:
    """提取单帧图像"""
    cmd = [
        "ffmpeg", "-y", "-ss", str(time_sec),
        "-i", video_path,
        "-vframes", "1",
        "-update", "1",
        output_path
    ]
    return run_ffmpeg(cmd, f"提取帧 {time_sec}s")


def detect_frame_glitch(video_path: str, start: float, end: float) -> bool:
    """检测帧间差异是否过大（穿模检测），返回是否可疑"""
    temp_frame1 = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    temp_frame2 = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    temp_frame1.close()
    temp_frame2.close()

    try:
        # 提取起始帧和结束帧
        if not extract_frame(video_path, start, temp_frame1.name):
            return False
        if not extract_frame(video_path, end, temp_frame2.name):
            return False

        # 读取两帧
        img1 = cv2.imread(temp_frame1.name)
        img2 = cv2.imread(temp_frame2.name)
        if img1 is None or img2 is None:
            return False

        # 调整到相同尺寸
        if img1.shape != img2.shape:
            img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))

        # 计算像素差异
        diff = cv2.absdiff(img1, img2)
        diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)

        # 计算变化像素比例
        total_pixels = diff_gray.size
        changed_pixels = np.count_nonzero(diff_gray > 30)
        change_ratio = changed_pixels / total_pixels

        # 如果变化超过30%，标记为可疑穿模
        SUSPICIOUS_THRESHOLD = 0.30
        if change_ratio > SUSPICIOUS_THRESHOLD:
            logger.info(f"  检测到可疑穿模: 变化比例 {change_ratio:.2%}")
            return True

        return False
    except Exception as e:
        logger.warning(f"穿模检测异常: {e}")
        return False
    finally:
        try:
            os.unlink(temp_frame1.name)
        except:
            pass
        try:
            os.unlink(temp_frame2.name)
        except:
            pass


def calculate_scene_quality(video_path: str, start: float, end: float) -> tuple[float, bool]:
    """计算场景质量分数（基于亮度、对比度）和穿模检测，返回(分数, 是否可疑)"""
    temp_frame = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    temp_frame.close()

    try:
        # 提取中间帧
        mid_time = (start + end) / 2
        if not extract_frame(video_path, mid_time, temp_frame.name):
            return (5.0, False)  # 默认分数

        # 读取图像
        img = cv2.imread(temp_frame.name)
        if img is None:
            return (5.0, False)

        # 计算质量指标
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 亮度（避免过曝或欠曝）
        brightness = gray.mean() / 255.0
        brightness_score = 1.0 - abs(brightness - 0.5) * 2

        # 对比度
        contrast = gray.std() / 255.0
        contrast_score = min(contrast * 3, 1.0)

        # 最终分数
        score = (brightness_score * 0.4 + contrast_score * 0.6) * 10

        # 穿模检测
        is_suspicious = detect_frame_glitch(video_path, start, end)

        return (round(score, 2), is_suspicious)
    except Exception as e:
        logger.warning(f"质量评估异常: {e}")
        return (5.0, False)
    finally:
        try:
            os.unlink(temp_frame.name)
        except:
            pass


# ============== Stage 1: 扫描素材 ==============
def stage1_scan_materials(input_dir: str) -> list[VideoInfo]:
    """扫描输入目录，返回视频文件列表"""
    logger.info(f"[Stage 1] 扫描素材目录: {input_dir}")

    input_path = Path(input_dir)
    if not input_path.exists():
        logger.error(f"目录不存在: {input_dir}")
        return []

    video_extensions = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}
    video_files = []

    for file in sorted(input_path.iterdir()):
        if file.suffix.lower() in video_extensions:
            info = get_video_info(str(file))
            if info and info.duration > 0:
                video_files.append(info)
                logger.info(f"  发现视频: {file.name} ({info.duration:.2f}s, {info.width}x{info.height})")

    if not video_files:
        logger.error("未发现有效视频文件")

    return video_files


# ============== Stage 2: 场景检测 ==============
def stage2_detect_scenes(video_info: VideoInfo, threshold: float = 30.0) -> list[Scene]:
    """使用PySceneDetect检测场景"""
    logger.info(f"[Stage 2] 场景检测: {Path(video_info.path).name}")

    if not scenedetect_available:
        logger.error("PySceneDetect 未安装")
        return [Scene(0, 0, video_info.duration, video_info.duration)]

    try:
        from scenedetect import open_video, SceneManager, ContentDetector

        video = open_video(video_info.path)
        scene_manager = SceneManager()
        scene_manager.add_detector(ContentDetector(threshold=threshold))

        scene_manager.detect_scenes(video)
        scene_list = scene_manager.get_scene_list()
        # 新版API不需要release()

        scenes = []
        for i, scene in enumerate(scene_list):
            start = scene[0].get_seconds()
            end = scene[1].get_seconds()
            duration = end - start

            if duration < 0.5:
                continue

            # 计算质量分数和穿模检测
            quality, is_suspicious = calculate_scene_quality(video_info.path, start, end)

            scenes.append(Scene(
                index=i,
                start_time=start,
                end_time=end,
                duration=duration,
                quality_score=quality,
                is_suspicious=is_suspicious
            ))

        logger.info(f"  检测到 {len(scenes)} 个有效场景")
        return scenes

    except Exception as e:
        logger.error(f"场景检测失败: {e}")
        return [Scene(0, 0, video_info.duration, video_info.duration)]


# ============== Stage 3: 剪辑规划（启发式） ==============
def stage3_heuristic_planning(
    materials: list[tuple[VideoInfo, list[Scene]]],
    num_candidates: int = 3
) -> list[CandidatePlan]:
    """
    启发式剪辑规划（无AI时使用）
    策略：按质量分数选择片段，确保多样性和时长约束
    """
    logger.info(f"[Stage 3] 启发式剪辑规划 ({num_candidates} 个候选)")

    all_clips = []

    # 收集所有可用片段（排除穿模片段）
    for video_info, scenes in materials:
        for scene in scenes:
            if scene.duration >= 1.0 and scene.quality_score >= 5.0 and not scene.is_suspicious:
                # 计算最佳切割点（目标2秒）
                num_cuts = max(1, int(scene.duration / CLIP_DURATION_TARGET))
                for i in range(num_cuts):
                    clip_start = scene.start_time + i * CLIP_DURATION_TARGET
                    clip_end = min(clip_start + CLIP_DURATION_TARGET, scene.end_time)
                    clip_duration = clip_end - clip_start

                    if clip_duration >= 1.0:
                        all_clips.append(Clip(
                            source_file=video_info.path,
                            start_time=clip_start,
                            end_time=clip_end,
                            duration=clip_duration
                        ))

    logger.info(f"  收集到 {len(all_clips)} 个候选片段")

    # 生成多个候选方案
    candidates = []
    for cand_id in range(num_candidates):
        plan_clips = []
        total_duration = 0.0
        # 选择片段，确保多样性
        last_source = None
        consecutive_count = 0

        # 打乱片段顺序，增加多样性
        import random
        shuffled_clips = all_clips.copy()
        random.shuffle(shuffled_clips)

        for clip in shuffled_clips:
            if total_duration >= TARGET_DURATION_MIN:
                break

            # 允许最多2个连续同源片段，避免视觉跳太大
            if clip.source_file == last_source:
                consecutive_count += 1
                if consecutive_count >= 2:
                    continue
            else:
                consecutive_count = 0
                last_source = clip.source_file

            # 添加片段
            plan_clips.append(clip)
            total_duration += clip.duration

        # 确保时长在目标范围内
        if TARGET_DURATION_MIN <= total_duration <= TARGET_DURATION_MAX:
            candidates.append(CandidatePlan(
                id=cand_id + 1,
                clips=plan_clips,
                total_duration=total_duration,
                emotion_curve="引入→发展→高潮→结尾"
            ))

    # 如果生成不足3个，降低要求重试
    if len(candidates) < num_candidates:
        logger.warning("  启发式规划片段不足，降低时长要求重试")
        for cand_id in range(len(candidates), num_candidates):
            plan_clips = []
            total_duration = 0.0
            last_source = None
            consecutive_count = 0

            # 打乱片段顺序，增加多样性
            shuffled_clips = all_clips.copy()
            random.shuffle(shuffled_clips)

            for clip in shuffled_clips:
                if total_duration >= 12.0:  # 降低到12秒
                    break
                # 允许最多2个连续同源片段
                if clip.source_file == last_source:
                    consecutive_count += 1
                    if consecutive_count >= 2:
                        continue
                else:
                    consecutive_count = 0
                    last_source = clip.source_file
                plan_clips.append(clip)
                total_duration += clip.duration

            if plan_clips:
                candidates.append(CandidatePlan(
                    id=cand_id + 1,
                    clips=plan_clips,
                    total_duration=total_duration,
                    emotion_curve="引入→发展→结尾"
                ))

    logger.info(f"  生成 {len(candidates)} 个候选方案")
    for i, c in enumerate(candidates):
        logger.info(f"    方案{i+1}: {c.total_duration:.2f}s, {len(c.clips)} 个片段")

    return candidates


# ============== Stage 4: 视频合成 ==============
def stage4_video_compose(
    plan: CandidatePlan,
    output_path: str,
    keep_temp: bool = False
) -> bool:
    """FFmpeg视频合成和4K放大"""
    logger.info(f"[Stage 4] 视频合成: 方案{plan.id}")

    temp_dir = Path(tempfile.mkdtemp(prefix="video_workflow_"))
    clips_file = temp_dir / "clips.txt"

    try:
        # 准备片段文件列表
        with open(clips_file, "w") as f:
            for i, clip in enumerate(plan.clips):
                segment_path = temp_dir / f"segment_{i:03d}.mp4"

                # 裁剪并标准化片段
                scale_filter = f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1"

                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(clip.start_time),
                    "-i", clip.source_file,
                    "-t", str(clip.duration),
                    "-vf", scale_filter,
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "23",
                    "-c:a", "aac",  # 保留音频并转码为AAC
                    "-b:a", "128k",
                    "-r", "30",
                    "-pix_fmt", "yuv420p",
                    str(segment_path)
                ]

                if not run_ffmpeg(cmd, f"  裁剪片段{i+1}"):
                    return False

                f.write(f"file '{segment_path}'\n")

        # 拼接片段（使用filter_complex方式处理音频）
        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(clips_file),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(temp_dir / "concatenated.mp4")
        ]

        if not run_ffmpeg(concat_cmd, "  拼接片段"):
            return False

        # 获取主体视频时长
        main_info = get_video_info(str(temp_dir / "concatenated.mp4"))
        main_duration = main_info.duration if main_info else plan.total_duration

        final_path = Path(output_path)

        # 复制到最终路径
        cmd = [
            "ffmpeg", "-y",
            "-i", str(temp_dir / "concatenated.mp4"),
            "-c", "copy",
            "-movflags", "+faststart",
            str(final_path)
        ]

        if not run_ffmpeg(cmd, "  生成最终视频"):
            return False

        logger.info(f"  视频合成完成: {final_path}")
        return True

    except Exception as e:
        logger.error(f"视频合成异常: {e}")
        return False
    finally:
        if not keep_temp:
            import shutil
            try:
                shutil.rmtree(temp_dir)
            except:
                pass


# ============== Stage 5: 字幕生成 ==============
def stage5_generate_subtitle(
    video_path: str,
    output_path: str,
    whisper_model: str = "base",
    language: Optional[str] = None
) -> tuple[bool, str]:
    """Whisper字幕生成和烧录，返回(是否成功, 字幕文本)"""
    logger.info(f"[Stage 5] 字幕生成: {Path(video_path).name}")

    if not whisper_available:
        logger.error("Whisper 未安装")
        return False

    try:
        # 先提取音频到临时文件（Whisper需要直接音频文件）
        temp_dir = Path(tempfile.mkdtemp(prefix="whisper_audio_"))
        audio_path = temp_dir / "audio.wav"

        # 提取音频
        extract_cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vn",  # 不要视频
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",  # 单声道
            str(audio_path)
        ]

        if not run_ffmpeg(extract_cmd, "  提取音频"):
            shutil.copy(video_path, output_path)
            return True

        # 加载模型
        model = whisper.load_model(whisper_model, device="cpu")

        # 语音识别
        logger.info("  Whisper 识别中...")
        options = {"task": "transcribe"}
        if language:
            options["language"] = language

        result = model.transcribe(str(audio_path), **options)

        # 清理音频临时文件
        try:
            shutil.rmtree(temp_dir)
        except:
            pass

        if not result["segments"]:
            logger.warning("  未识别到语音")
            shutil.copy(video_path, output_path)
            return (True, "")

        # 收集字幕文本供BGM选择使用
        subtitle_text = " ".join(seg["text"].strip() for seg in result["segments"] if seg["text"].strip())

        # 生成ASS字幕
        ass_path = Path(output_path).with_suffix(".ass")

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write("[Script Info]\n")
            f.write("Title: 动态字幕\n")
            f.write("PlayResX: 2160\n")
            f.write("PlayResY: 3840\n\n")

            f.write("[V4+ Styles]\n")
            f.write("Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n")
            f.write('Style: Default, Microsoft YaHei, 72, &H00FFFFFF, &H00000000, -1, 0, 1, 2, 2, 5, 30, 30, 150\n\n')

            f.write("[Events]\n")
            f.write("Format: Layer, Start, End, Style, Text\n")

            for segment in result["segments"]:
                start = segment["start"]
                end = segment["end"]
                text = segment["text"].strip()

                if not text:
                    continue

                # 转换时间格式
                start_fmt = format_ass_time(start)
                end_fmt = format_ass_time(end)

                # 逐字出现效果（karaoke）
                # 简单处理：直接显示
                f.write(f"Dialogue: 0,{start_fmt},{end_fmt},Default,{text}\n")

        logger.info(f"  ASS字幕生成: {ass_path}")

        # 烧录字幕
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", f"ass={ass_path}",
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path
        ]

        if not run_ffmpeg(cmd, "  烧录字幕"):
            return (False, "")

        # 验证字幕流是否存在
        verify_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "json",
            output_path
        ]
        result = subprocess.run(verify_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"  FFprobe验证失败: {result.stderr[-500:]}")
            return (False, "")

        try:
            probe_data = json.loads(result.stdout)
            streams = probe_data.get("streams", [])
            has_subtitle = any(s.get("codec_type") == "subtitle" for s in streams)
            if not has_subtitle:
                logger.error(f"  字幕流烧录失败: 输出视频无字幕流，请检查FFmpeg是否支持ASS字幕滤镜")
                return (False, "")
        except json.JSONDecodeError:
            logger.error(f"  FFprobe JSON解析失败")
            return (False, "")

        logger.info(f"  字幕烧录完成: {output_path}")
        return (True, subtitle_text)

    except Exception as e:
        logger.error(f"字幕生成异常: {e}")
        shutil.copy(video_path, output_path)
        return (True, "")


def format_ass_time(seconds: float) -> str:
    """秒数转换为ASS时间格式"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


# ============== BGM工具函数 ==============
def select_bgm_by_keywords(subtitle_text: str, preferred_types: List[str]) -> str:
    """根据字幕文字关键词选择BGM类型"""
    # 关键词映射
    keyword_map = {
        "warm": ["love", "loved", "precious", "memory", "dear", "warm", "sweet", "heart"],
        "elegant": ["elegant", "sophisticated", "luxury", "beautiful", "graceful", "fine"],
        "calm": ["peace", "calm", "gentle", "quiet", "serene", "soft", "tranquil"],
        "upbeat": ["energy", "positive", "exciting", "wonderful", "amazing", "bright"],
        "emotional": ["emotion", "feel", "touch", "heart", "soul", "meaningful", "moment"]
    }

    text_lower = subtitle_text.lower()

    # 统计每种类型匹配的关键词数量
    scores = {}
    for bgm_type, keywords in keyword_map.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        scores[bgm_type] = score

    logger.info(f"  BGM类型匹配分数: {scores}")

    # 按preferred_types顺序，结合分数选择
    for preferred in preferred_types:
        if scores.get(preferred, 0) > 0:
            logger.info(f"  选择BGM类型: {preferred} (匹配分数: {scores[preferred]})")
            return preferred

    # fallback：使用第一个偏好类型
    fallback_type = preferred_types[0] if preferred_types else "calm"
    logger.info(f"  BGM fallback到: {fallback_type}")
    return fallback_type


def get_bgm_files(bgm_type: str) -> List[str]:
    """获取指定类型的BGM文件列表"""
    bgm_dir = Path(BGM_LIBRARY_PATH) / bgm_type
    if not bgm_dir.exists():
        logger.warning(f"  BGM目录不存在: {bgm_dir}")
        return []

    mp3_files = list(bgm_dir.glob("*.mp3"))
    return [str(f) for f in mp3_files]


def stage5_mix_bgm(
    video_path: str,
    subtitle_text: str,
    category: str,
    output_path: str
) -> bool:
    """混合BGM到视频"""
    logger.info(f"[Stage 5] BGM混合")

    try:
        # 获取品类的BGM偏好类型列表
        preference_str = BGM_PREFERENCE_BY_CATEGORY.get(category, "calm,warm,elegant,upbeat,emotional")
        preferred_types = [t.strip() for t in preference_str.split(",")]

        # 根据字幕关键词选择BGM类型
        bgm_type = select_bgm_by_keywords(subtitle_text, preferred_types)

        # 获取该类型的BGM文件
        bgm_files = get_bgm_files(bgm_type)
        if not bgm_files:
            # fallback：尝试其他类型
            logger.warning(f"  类型{bgm_type}无BGM文件，尝试其他类型")
            for t in preferred_types:
                if t != bgm_type:
                    bgm_files = get_bgm_files(t)
                    if bgm_files:
                        bgm_type = t
                        break

        if not bgm_files:
            logger.error(f"  所有BGM类型都没有文件")
            # 直接复制原视频
            shutil.copy(video_path, output_path)
            return True

        # 随机选一首
        bgm_file = random.choice(bgm_files)
        logger.info(f"  选择BGM: {Path(bgm_file).name}")

        # 获取视频时长
        video_info = get_video_info(video_path)
        if not video_info:
            shutil.copy(video_path, output_path)
            return True

        main_duration = video_info.duration
        temp_dir = Path(tempfile.mkdtemp(prefix="bgm_mix_"))

        # Step 1: 裁剪BGM到视频时长 + 淡入淡出
        bgm_trimmed = temp_dir / "bgm_trimmed.mp3"
        fade_duration = min(0.5, main_duration * 0.05)  # 淡入0.5s或5%时长
        fade_out_start = main_duration - min(1.0, main_duration * 0.05)  # 淡出1s或5%时长

        trim_cmd = [
            "ffmpeg", "-y",
            "-i", bgm_file,
            "-ss", "0",
            "-t", str(main_duration),
            "-af", f"afade=t=in:st=0:d={fade_duration},afade=t=out:st={fade_out_start}:d=1",
            str(bgm_trimmed)
        ]
        if not run_ffmpeg(trim_cmd, "  裁剪BGM"):
            shutil.copy(video_path, output_path)
            return True

        # Step 2: 降低BGM音量（-20dB ≈ 10%音量，避免盖过语音）
        bgm_quiet = temp_dir / "bgm_quiet.mp3"
        volume_cmd = [
            "ffmpeg", "-y",
            "-i", str(bgm_trimmed),
            "-af", "volume=-20dB",
            str(bgm_quiet)
        ]
        if not run_ffmpeg(volume_cmd, "  调整BGM音量"):
            shutil.copy(video_path, output_path)
            return True

        # Step 3: 提取原视频音频（保留语音）
        audio_original = temp_dir / "audio_original.aac"
        extract_cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vn",
            "-c:a", "aac",
            "-b:a", "128k",
            str(audio_original)
        ]
        if not run_ffmpeg(extract_cmd, "  提取原音频"):
            shutil.copy(video_path, output_path)
            return True

        # Step 4: 混合原音频 + BGM
        mix_cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", str(audio_original),
            "-i", str(bgm_quiet),
            "-filter_complex",
            "[1:a][2:a]amix=inputs=2:duration=longest:dropout_transition=2[aout]",
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            output_path
        ]

        if not run_ffmpeg(mix_cmd, "  混合音频"):
            shutil.copy(video_path, output_path)
            return True

        # 清理临时目录
        try:
            shutil.rmtree(temp_dir)
        except:
            pass

        logger.info(f"  BGM混合完成: {output_path}")
        return True

    except Exception as e:
        logger.error(f"BGM混合异常: {e}")
        shutil.copy(video_path, output_path)
        return True


# ============== Stage 6: 商标片尾拼接 ==============
def stage6_append_logo(
    video_path: str,
    logo_path: str,
    output_path: str
) -> bool:
    """拼接商标片尾"""
    logger.info(f"[Stage 6] 商标片尾拼接")

    try:
        # 获取片尾实际时长
        logo_info = get_video_info(logo_path)
        if not logo_info:
            logger.error("无法获取片尾信息")
            return False

        actual_logo_duration = logo_info.duration
        logger.info(f"  片尾实际时长: {actual_logo_duration:.2f}s")

        # 标准化片尾视频到4K
        temp_dir = Path(tempfile.mkdtemp(prefix="logo_processing_"))
        scaled_logo = temp_dir / "logo.mp4"
        main_video = temp_dir / "main.mp4"

        scale_filter = f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2"

        # 先复制主体视频到临时目录（使用简短路径）
        copy_main_cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-c", "copy",
            str(main_video)
        ]

        if not run_ffmpeg(copy_main_cmd, "  复制主体视频"):
            return False

        # 片尾使用静音并标准化
        cmd = [
            "ffmpeg", "-y",
            "-i", logo_path,
            "-vf", scale_filter,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-an",  # 强制静音
            "-r", "30",
            "-t", str(actual_logo_duration),
            "-pix_fmt", "yuv420p",
            str(scaled_logo)
        ]

        if not run_ffmpeg(cmd, "  标准化片尾"):
            return False

        # 拼接主体和片尾（使用相对路径）
        concat_file = temp_dir / "concat.txt"
        with open(concat_file, "w") as f:
            f.write("file 'main.mp4'\n")
            f.write("file 'logo.mp4'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-movflags", "+faststart",
            output_path
        ]

        if not run_ffmpeg(cmd, "  拼接片尾"):
            return False

        logger.info(f"  片尾拼接完成: {output_path}")

        # 清理临时目录
        try:
            shutil.rmtree(temp_dir)
        except:
            pass

        return True

    except Exception as e:
        logger.error(f"片尾拼接异常: {e}")
        return False


# ============== 主函数 ==============
def main():
    parser = argparse.ArgumentParser(
        description="AI视频自动剪辑工作流 - 通用脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument("--category", choices=list(DEFAULT_CATEGORIES.keys()),
                        help="品类名称，自动推导输入输出目录")
    parser.add_argument("--input", type=str, help="输入目录路径")
    parser.add_argument("--output", type=str, help="输出目录路径")
    parser.add_argument("--candidates", type=int, default=3, choices=[2, 3],
                        help="候选视频数量 (默认: 3)")
    parser.add_argument("--scene-threshold", type=float, default=30.0,
                        help="场景检测阈值 (默认: 30.0)")
    parser.add_argument("--whisper-model", default="base",
                        help="Whisper模型 (默认: base)")
    parser.add_argument("--language", type=str, default=None,
                        help="字幕语言 (默认: 自动识别)")
    parser.add_argument("--allow-heuristic-fallback", action="store_true",
                        help="无AI凭据时使用启发式规划")
    parser.add_argument("--overwrite", action="store_true",
                        help="允许覆盖已有输出")
    parser.add_argument("--keep-temp", action="store_true",
                        help="保留中间文件")
    parser.add_argument("--dry-run", action="store_true",
                        help="只扫描和分析，不进行最终渲染")
    parser.add_argument("--verbose", action="store_true",
                        help="详细输出")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 确定输入输出目录
    if args.category:
        base_dir = DEFAULT_CATEGORIES[args.category]
        input_dir = os.path.join(base_dir, "基础素材")
        output_dir = os.path.join(base_dir, "输出")
    elif args.input and args.output:
        input_dir = args.input
        output_dir = args.output
    else:
        parser.error("必须指定 --category 或同时指定 --input 和 --output")
        return 1

    logger.info(f"输入目录: {input_dir}")
    logger.info(f"输出目录: {output_dir}")

    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)

    # 检查商标文件
    if not os.path.exists(LOGO_FILE):
        logger.error(f"商标文件不存在: {LOGO_FILE}")
        return 1

    # 检查凭据
    has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))

    if not has_openai_key and not args.allow_heuristic_fallback:
        logger.error("未配置 OpenAI API Key，必须使用 --allow-heuristic-fallback 启用启发式规划")
        return 1

    # ===== Stage 1: 扫描素材 =====
    materials = stage1_scan_materials(input_dir)
    if not materials:
        logger.error("未发现有效素材")
        return 1

    # ===== Stage 2: 场景检测 =====
    materials_with_scenes = []
    for video_info in materials:
        scenes = stage2_detect_scenes(video_info, args.scene_threshold)
        materials_with_scenes.append((video_info, scenes))

    if args.dry_run:
        logger.info("Dry run 完成")
        return 0

    # ===== Stage 3: 剪辑规划 =====
    if has_openai_key:
        logger.info("使用AI规划 (需要OpenAI SDK)")
        # TODO: 实现AI规划
        candidates = stage3_heuristic_planning(materials_with_scenes, args.candidates)
    else:
        logger.info("使用启发式规划")
        candidates = stage3_heuristic_planning(materials_with_scenes, args.candidates)

    if not candidates:
        logger.error("未能生成剪辑方案")
        return 1

    # ===== Stage 4-6: 为每个候选方案执行 =====
    for plan in candidates:
        logger.info(f"\n{'='*50}")
        logger.info(f"处理候选方案 {plan.id}/{len(candidates)}")
        logger.info(f"{'='*50}")

        candidate_name = f"candidate_{plan.id:02d}"
        clean_video = os.path.join(output_dir, f"{candidate_name}_clean.mp4")
        subtitle_video = os.path.join(output_dir, f"{candidate_name}_subtitle.mp4")
        final_video = os.path.join(output_dir, f"{candidate_name}.mp4")

        # 检查是否已存在
        if os.path.exists(final_video) and not args.overwrite:
            logger.info(f"输出文件已存在，跳过: {final_video}")
            continue

        # Stage 4: 视频合成
        if not stage4_video_compose(plan, clean_video, args.keep_temp):
            logger.error(f"方案{plan.id} 视频合成失败")
            continue

        # Stage 5: 字幕生成
        subtitle_success, subtitle_text = stage5_generate_subtitle(
            clean_video, subtitle_video, args.whisper_model, args.language
        )
        if not subtitle_success:
            logger.error(f"方案{plan.id} 字幕生成失败")
            continue

        # Stage 5.5: BGM混合
        bgm_video = subtitle_video  # 覆盖subtitle_video
        if not stage5_mix_bgm(subtitle_video, subtitle_text, args.category, bgm_video):
            logger.error(f"方案{plan.id} BGM混合失败")
            continue

        # Stage 6: 片尾拼接
        if not stage6_append_logo(bgm_video, LOGO_FILE, final_video):
            logger.error(f"方案{plan.id} 片尾拼接失败")
            continue

        # 验证最终视频
        final_info = get_video_info(final_video)
        if final_info:
            logger.info(f"✓ 方案{plan.id} 完成: {final_info.duration:.2f}s, {final_info.width}x{final_info.height}")
        else:
            logger.error(f"方案{plan.id} 验证失败")

    # 生成报告
    manifest = {
        "run_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "input_dir": input_dir,
        "output_dir": output_dir,
        "materials_count": len(materials),
        "candidates_planned": len(candidates),
        "candidates_completed": len([p for p in candidates if os.path.exists(os.path.join(output_dir, f"candidate_{p.id:02d}.mp4"))]),
    }

    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    logger.info(f"\n工作流完成！输出目录: {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

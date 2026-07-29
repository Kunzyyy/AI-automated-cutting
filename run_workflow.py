#!/usr/bin/env python3
"""
AI视频自动剪辑工作流 - 通用脚本
功能：将基础素材自动剪辑成2-3个4K竖屏候选成品
不支持BGM（本次版本）
"""

import argparse
import json
import logging
import math
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

try:
    from openai import OpenAI
    openai_available = True
except ImportError:
    openai_available = False


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
QUALITY_EXCLUSIONS_FILE = Path(__file__).with_name("quality_exclusions.json")
OUTPUT_WIDTH = 2160
OUTPUT_HEIGHT = 3840
TARGET_DURATION_MIN = 16
TARGET_DURATION_MAX = 22
LOGO_DURATION = 3.37
CLIP_DURATION_TARGET = 2.0
MIN_STABLE_SCENE_DURATION = 1.35
MIN_CLIP_DURATION = 1.35
SCENE_EDGE_PADDING = 0.15
MIN_QUALITY_SCORE = 4.0

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
    quality_score: float = 0.0


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
def run_ffmpeg(
    cmd: list,
    description: str = "FFmpeg",
    cwd: Optional[str] = None,
    log_command: bool = True,
) -> bool:
    """运行FFmpeg命令，返回是否成功"""
    log_method = logger.info if log_command else logger.debug
    log_method(f"{description}: {' '.join(str(x) for x in cmd[:5])}...")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
        )
    except Exception as e:
        logger.error(f"{description} 执行异常: {e}")
        return False
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
    return run_ffmpeg(cmd, f"提取帧 {time_sec}s", log_command=False)


def detect_frame_glitch(video_path: str, start: float, end: float) -> bool:
    """检测片段内部的突变、闪黑和单帧异常；正常运镜不应被判为穿模。"""
    duration = end - start
    if duration < MIN_CLIP_DURATION:
        return True

    sample_count = max(5, min(9, int(math.ceil(duration * 4))))
    edge = min(0.10, duration * 0.08)
    sample_times = np.linspace(start + edge, end - edge, sample_count)
    frame_paths: list[str] = []
    frames: list[np.ndarray] = []

    try:
        for sample_time in sample_times:
            temp_frame = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            temp_frame.close()
            frame_paths.append(temp_frame.name)
            if not extract_frame(video_path, float(sample_time), temp_frame.name):
                continue
            image = cv2.imread(temp_frame.name)
            if image is None:
                continue
            gray = cv2.cvtColor(cv2.resize(image, (160, 284)), cv2.COLOR_BGR2GRAY)
            frames.append(gray)

        if len(frames) < 4:
            logger.warning("  片段多帧预检取样不足，按可疑片段处理")
            return True

        brightness = np.array([frame.mean() / 255.0 for frame in frames])
        if np.any(brightness < 0.04) or np.any(brightness > 0.97):
            logger.info("  检测到可疑闪黑/过曝帧")
            return True

        change_ratios = []
        histogram_correlations = []
        for previous, current in zip(frames, frames[1:]):
            diff = cv2.absdiff(previous, current)
            change_ratios.append(float(np.mean(diff > 35)))
            previous_hist = cv2.calcHist([previous], [0], None, [32], [0, 256])
            current_hist = cv2.calcHist([current], [0], None, [32], [0, 256])
            cv2.normalize(previous_hist, previous_hist)
            cv2.normalize(current_hist, current_hist)
            histogram_correlations.append(
                float(cv2.compareHist(previous_hist, current_hist, cv2.HISTCMP_CORREL))
            )

        changes = np.array(change_ratios)
        correlations = np.array(histogram_correlations)
        median_change = float(np.median(changes))
        abrupt_transition = bool(
            np.any((changes > max(0.62, median_change + 0.28)) & (correlations < 0.35))
        )
        brightness_jump = bool(np.max(np.abs(np.diff(brightness))) > 0.38)

        if abrupt_transition or brightness_jump:
            logger.info(
                "  检测到片段内部异常突变: 最大变化 %.2f%%, 最低直方图相关度 %.2f",
                float(np.max(changes)) * 100,
                float(np.min(correlations)),
            )
            return True

        return False
    except Exception as e:
        logger.warning(f"穿模检测异常: {e}")
        return True
    finally:
        for frame_path in frame_paths:
            try:
                os.unlink(frame_path)
            except OSError:
                pass


def extract_clip_keypoints(clip: Clip) -> Optional[tuple[np.ndarray, list]]:
    """
    提取 clip 中间帧的显著性区域 ORB 关键点。
    返回 (gray_image, keypoints_descriptors) 或 None。
    """
    mid = (clip.start_time + clip.end_time) / 2
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tmp = tf.name
    try:
        if not extract_frame(clip.source_file, mid, tmp):
            return None
        img = cv2.imread(tmp)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 显著性检测：cv2 内置的 StaticSaliencyFineGrained
        try:
            saliency = cv2.saliency.StaticSaliencyFineGrained_create()
            ok, sal_map = saliency.computeSaliency(img)
        except Exception:
            sal_map = None

        if sal_map is not None and ok:
            # 取显著图前 30% 区域作为主体掩码
            sal_u8 = (sal_map * 255).astype("uint8") if sal_map.max() <= 1.0 else sal_map.astype("uint8")
            _, mask = cv2.threshold(sal_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            # 收缩到中心 60% 范围，避免把背景全包进来
            h, w = mask.shape
            cx0, cx1 = int(w * 0.2), int(w * 0.8)
            cy0, cy1 = int(h * 0.2), int(h * 0.8)
            center_mask = np.zeros_like(mask)
            center_mask[cy0:cy1, cx0:cx1] = 255
            mask = cv2.bitwise_and(mask, center_mask)
        else:
            mask = None

        orb = cv2.ORB_create(nfeatures=500)
        if mask is not None and mask.sum() > 1000:
            kps, des = orb.detectAndCompute(gray, mask)
        else:
            kps, des = orb.detectAndCompute(gray, None)

        if des is None or len(kps) < 8:
            return None
        return gray, kps, des
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def compute_clip_match_rate(des1, des2) -> float:
    """计算两个 clip 的 ORB 描述符匹配率。"""
    if des1 is None or des2 is None:
        return 0.0
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)
    if not matches:
        return 0.0
    # 用距离排序，取前 50 个匹配中距离 < 50 的比例
    matches = sorted(matches, key=lambda m: m.distance)
    top = matches[: min(50, len(matches))]
    good = sum(1 for m in top if m.distance < 50)
    return good / max(1, len(top))


def filter_inconsistent_adjacent_clips(
    plan_clips: list,
    match_threshold: float = 0.2,
) -> list:
    """
    对候选方案内的相邻 clip 做主体一致性检查。
    只对**同一源素材**的相邻 clip 做检测（跨素材产品形态本就允许不同）。
    匹配率 < match_threshold 则丢掉后面那个 clip。
    """
    if len(plan_clips) < 2:
        return list(plan_clips)

    # 预计算所有 clip 的关键点
    kp_cache: dict[int, Optional[tuple]] = {}
    for idx, clip in enumerate(plan_clips):
        kp_cache[idx] = extract_clip_keypoints(clip)

    kept: list = []
    last_kp = None
    last_source: Optional[str] = None
    dropped_count = 0
    for idx, clip in enumerate(plan_clips):
        cur = kp_cache.get(idx)
        same_source = (last_source is not None and clip.source_file == last_source)
        if same_source and last_kp is not None and cur is not None:
            rate = compute_clip_match_rate(last_kp[2], cur[2])
            if rate < match_threshold:
                logger.info(
                    "  跨镜头一致性: 片段%d→%d 匹配率 %.2f < %.2f，丢弃片段%d",
                    idx, idx + 1, rate, match_threshold, idx + 1,
                )
                dropped_count += 1
                continue
        kept.append(clip)
        if cur is not None:
            last_kp = cur
        last_source = clip.source_file

    if dropped_count > 0:
        logger.info("  跨镜头一致性: 丢弃 %d 个不一致片段，保留 %d 个", dropped_count, len(kept))
    return kept


def calculate_scene_quality(video_path: str, start: float, end: float) -> tuple[float, bool]:
    """计算场景静态画质；动态异常在切成约2秒片段后另行检测。"""
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

        sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
        sharpness_score = min(sharpness / 180.0, 1.0)

        score = (
            brightness_score * 0.30
            + contrast_score * 0.30
            + sharpness_score * 0.40
        ) * 10
        is_suspicious = (end - start) < MIN_STABLE_SCENE_DURATION

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
            start = scene[0].seconds
            end = scene[1].seconds
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

        suspicious_count = sum(scene.is_suspicious for scene in scenes)
        logger.info(
            "  检测到 %d 个有效场景（%d 个短促过渡已标记）",
            len(scenes),
            suspicious_count,
        )
        return scenes

    except Exception as e:
        logger.error(f"场景检测失败: {e}")
        return [Scene(0, 0, video_info.duration, video_info.duration)]


# ============== Stage 3: 剪辑规划（启发式） ==============
def load_quality_exclusions(category: Optional[str]) -> list[dict]:
    """加载人工质检确认的语义坏片段，作为自动画质检测的可审计兜底。"""
    if not category or not QUALITY_EXCLUSIONS_FILE.exists():
        return []
    try:
        data = json.loads(QUALITY_EXCLUSIONS_FILE.read_text(encoding="utf-8"))
        rules = data.get(category, [])
        valid_rules = [
            rule
            for rule in rules
            if isinstance(rule, dict)
            and rule.get("file")
            and float(rule.get("end", 0.0)) > float(rule.get("start", 0.0))
        ]
        if valid_rules:
            logger.info("  已加载 %d 条人工质检排除区间", len(valid_rules))
        return valid_rules
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        logger.warning("质检排除配置读取失败: %s", error)
        return []


def stage3_heuristic_planning(
    materials: list[tuple[VideoInfo, list[Scene]]],
    num_candidates: int = 3,
    excluded_ranges: Optional[list[dict]] = None,
) -> list[CandidatePlan]:
    """
    启发式剪辑规划（无AI时使用）
    策略：按质量分数选择片段，确保多样性和时长约束
    """
    logger.info(f"[Stage 3] 启发式剪辑规划 ({num_candidates} 个候选)")

    all_clips: list[Clip] = []
    rejected_glitches = 0
    rejected_manual = 0
    excluded_ranges = excluded_ranges or []

    # 先避开场景边缘，再把长场景均匀切成约2秒片段并逐段预检。
    for video_info, scenes in materials:
        for scene in scenes:
            if scene.is_suspicious or scene.quality_score < MIN_QUALITY_SCORE:
                continue

            usable_start = scene.start_time + SCENE_EDGE_PADDING
            usable_end = min(scene.end_time, video_info.duration) - SCENE_EDGE_PADDING
            usable_duration = usable_end - usable_start
            if usable_duration < MIN_CLIP_DURATION:
                continue

            num_cuts = max(1, int(round(usable_duration / CLIP_DURATION_TARGET)))
            if usable_duration / num_cuts > 2.25:
                num_cuts = int(math.ceil(usable_duration / 2.25))
            while num_cuts > 1 and usable_duration / num_cuts < MIN_CLIP_DURATION:
                num_cuts -= 1

            clip_duration = usable_duration / num_cuts
            for clip_index in range(num_cuts):
                clip_start = usable_start + clip_index * clip_duration
                clip_end = usable_start + (clip_index + 1) * clip_duration
                overlaps_exclusion = any(
                    Path(rule.get("file", "")).name == Path(video_info.path).name
                    and clip_start < float(rule.get("end", 0.0))
                    and clip_end > float(rule.get("start", 0.0))
                    for rule in excluded_ranges
                )
                if overlaps_exclusion:
                    rejected_manual += 1
                    continue
                if detect_frame_glitch(video_info.path, clip_start, clip_end):
                    rejected_glitches += 1
                    continue
                all_clips.append(Clip(
                    source_file=video_info.path,
                    start_time=clip_start,
                    end_time=clip_end,
                    duration=clip_end - clip_start,
                    quality_score=scene.quality_score,
                ))

    logger.info(
        "  收集到 %d 个候选片段，过滤 %d 个异常过渡片段、%d 个质检黑名单片段",
        len(all_clips),
        rejected_glitches,
        rejected_manual,
    )
    if not all_clips:
        return []

    candidates: list[CandidatePlan] = []
    source_count = len({clip.source_file for clip in all_clips})
    for cand_id in range(num_candidates):
        plan_clips: list[Clip] = []
        total_duration = 0.0
        target_duration = min(20.0, 18.0 + cand_id * 0.75)
        rng = random.Random(20260728 + cand_id)
        shuffled_clips = all_clips.copy()
        rng.shuffle(shuffled_clips)
        shuffled_clips.sort(
            key=lambda clip: clip.quality_score + rng.uniform(-1.25, 1.25),
            reverse=True,
        )

        last_source: Optional[str] = None
        consecutive_count = 0

        for clip in shuffled_clips:
            if total_duration >= target_duration:
                break
            if total_duration + clip.duration > TARGET_DURATION_MAX + 0.01:
                continue

            if source_count > 1 and clip.source_file == last_source:
                if consecutive_count >= 2:
                    continue
                consecutive_count += 1
            else:
                last_source = clip.source_file
                consecutive_count = 1

            plan_clips.append(clip)
            total_duration += clip.duration

        # 多样性约束导致未满16秒时，第二遍只放宽连续同源限制，不放宽质量和时长。
        if total_duration < TARGET_DURATION_MIN:
            selected_ids = {id(clip) for clip in plan_clips}
            for clip in shuffled_clips:
                if total_duration >= TARGET_DURATION_MIN:
                    break
                if id(clip) in selected_ids:
                    continue
                if total_duration + clip.duration > TARGET_DURATION_MAX + 0.01:
                    continue
                plan_clips.append(clip)
                total_duration += clip.duration

        if not (TARGET_DURATION_MIN <= total_duration <= TARGET_DURATION_MAX):
            logger.error(
                "  方案%d只有 %.2fs，未达到16秒硬性下限，拒绝生成",
                cand_id + 1,
                total_duration,
            )
            continue

        # 跨镜头主体一致性过滤
        plan_clips = filter_inconsistent_adjacent_clips(plan_clips)
        total_duration = sum(c.duration for c in plan_clips)
        if not (TARGET_DURATION_MIN <= total_duration <= TARGET_DURATION_MAX):
            logger.error(
                "  方案%d一致性过滤后只有 %.2fs，拒绝生成",
                cand_id + 1,
                total_duration,
            )
            continue

        candidates.append(CandidatePlan(
            id=cand_id + 1,
            clips=plan_clips,
            total_duration=total_duration,
            emotion_curve="引入→发展→高潮→结尾",
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

def generate_ai_subtitles(
    video_path: str,
    plan: CandidatePlan,
    output_path: str,
) -> tuple[bool, str]:
    """
    用 GPT-4o Vision 看每个镜头关键帧，生成英文广告文案字幕。
    返回 (是否成功, 字幕文本)。
    """
    if not openai_available:
        logger.warning("  openai 库未安装，AI字幕跳过")
        return (False, "")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("  OPENAI_API_KEY 未设置，AI字幕跳过")
        return (False, "")

    logger.info("  [AI字幕] GPT-4o Vision 生成中...")
    client = OpenAI(api_key=api_key)

    temp_dir = Path(tempfile.mkdtemp(prefix="ai_subtitle_"))
    try:
        # 1. 计算字幕时间轴：每 2-4 秒一条，对齐到镜头边界
        main_info = get_video_info(video_path)
        if not main_info:
            return (False, "")
        total_duration = main_info.duration

        # 用 plan.clips 的边界做分割点，每 2-4 秒一条
        boundaries = [0.0]
        current = 0.0
        for clip in plan.clips:
            current += clip.duration
            if current - boundaries[-1] >= 2.0:
                boundaries.append(round(current, 2))
        if boundaries[-1] < total_duration - 0.5:
            boundaries.append(round(total_duration, 2))
        # 确保最后一条不超过视频时长
        boundaries[-1] = min(boundaries[-1], total_duration)

        subtitle_segments = []
        for i in range(len(boundaries) - 1):
            start = boundaries[i]
            end = boundaries[i + 1]
            mid = (start + end) / 2
            subtitle_segments.append({"start": start, "end": end, "mid": mid})

        # 2. 提取每段中间帧
        frames = []
        for seg in subtitle_segments:
            frame_path = temp_dir / f"frame_{len(frames):03d}.jpg"
            if extract_frame(video_path, seg["mid"], str(frame_path)):
                frames.append(str(frame_path))
            else:
                logger.warning(f"  帧提取失败: {seg['mid']:.2f}s")

        if not frames:
            return (False, "")

        # 3. 批量发给 GPT-4o（每帧一条文案）
        subtitle_texts = []
        for i, frame_path in enumerate(frames):
            try:
                with open(frame_path, "rb") as f:
                    image_data = f.read()
                import base64
                b64 = base64.b64encode(image_data).decode("utf-8")

                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a short-form video ad copywriter. "
                                "Look at the product image and write ONE short English subtitle line "
                                "(max 8 words). Style: elegant, emotional, lifestyle ad captions. "
                                "Examples: 'Carry love wherever you go', 'Small things, big feelings', "
                                "'Every detail tells a story'. Reply with ONLY the subtitle text, no quotes."
                            ),
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{b64}",
                                        "detail": "low",
                                    },
                                }
                            ],
                        },
                    ],
                    max_tokens=30,
                    temperature=0.8,
                )
                text = response.choices[0].message.content.strip().strip('"').strip("'")
                if text:
                    subtitle_texts.append(text)
                    logger.info(f"  [{i+1}/{len(frames)}] {text}")
                else:
                    subtitle_texts.append("")
            except Exception as e:
                logger.warning(f"  GPT-4o 第{i+1}帧失败: {e}")
                subtitle_texts.append("")

        # 4. 生成 ASS 文件
        ass_path = temp_dir / "subtitle.ass"
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write("[Script Info]\n")
            f.write("Title: subtitle\n")
            f.write("Original Script: \n")
            f.write("PlayResX: 2160\n")
            f.write("PlayResY: 3840\n")
            f.write("ScriptType: v4.00+\n")
            f.write("WrapStyle: 0\n\n")

            f.write("[V4+ Styles]\n")
            f.write("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
            f.write("Style: Default,Microsoft YaHei,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,2,2,80,80,150,1\n\n")

            f.write("[Events]\n")
            f.write("Format: Layer, Start, End, Style, Text\n")

            for i, seg in enumerate(subtitle_segments):
                text = subtitle_texts[i] if i < len(subtitle_texts) else ""
                if not text:
                    continue
                f.write(
                    f"Dialogue: 0,{format_ass_time(seg['start'])},"
                    f"{format_ass_time(seg['end'])},Default,{escape_ass_text(text)}\n"
                )

        logger.info(f"  AI字幕ASS生成: {ass_path}")

        # 5. 烧录字幕
        cmd = [
            "ffmpeg", "-y",
            "-i", os.path.abspath(video_path),
            "-vf", f"ass={ass_path.name}",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "copy",
            "-movflags", "+faststart",
            os.path.abspath(output_path),
        ]

        if not run_ffmpeg(cmd, "  烧录AI字幕", cwd=str(temp_dir)):
            return (False, "")

        # 6. 验证字幕可见
        fake_segments = [{"start": s["start"], "end": s["end"]} for s in subtitle_segments]
        if not verify_burned_subtitles(video_path, output_path, fake_segments):
            return (False, "")

        subtitle_text = " ".join(t for t in subtitle_texts if t)
        logger.info(f"  AI字幕烧录完成: {output_path}")
        return (True, subtitle_text)

    except Exception as e:
        logger.error(f"AI字幕生成异常: {e}")
        return (False, "")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def escape_ass_text(text: str) -> str:
    """转义用户文本，避免花括号或换行破坏ASS事件。"""
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\n", r"\N")
    )


def progressive_ass_events(text: str, start: float, end: float) -> list[tuple[float, float, str]]:
    """把一句话展开为逐词/逐字出现且互不重叠的ASS事件。"""
    words = text.split()
    if len(words) > 1:
        prefixes = [" ".join(words[:index]) for index in range(1, len(words) + 1)]
    else:
        characters = list(text)
        prefixes = ["".join(characters[:index]) for index in range(1, len(characters) + 1)]

    if not prefixes:
        return []
    if len(prefixes) > 20:
        indices = np.linspace(1, len(prefixes), 20, dtype=int)
        prefixes = [prefixes[index - 1] for index in indices]

    duration = max(0.10, end - start)
    step_duration = duration / len(prefixes)
    events = []
    for index, prefix in enumerate(prefixes):
        event_start = start + index * step_duration
        event_end = end if index == len(prefixes) - 1 else start + (index + 1) * step_duration
        events.append((event_start, event_end, escape_ass_text(prefix)))
    return events


def verify_burned_subtitles(
    source_path: str,
    rendered_path: str,
    segments: list[dict],
) -> bool:
    """通过活动字幕区域与顶部对照区的像素变化，验证硬字幕确实可见。"""
    check_dir = Path(tempfile.mkdtemp(prefix="subtitle_verify_"))
    try:
        checked = 0
        for index, segment in enumerate(segments[:3]):
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
            if end - start < 0.10:
                continue
            sample_time = start + (end - start) * 0.75
            source_frame = check_dir / f"source_{index}.png"
            rendered_frame = check_dir / f"rendered_{index}.png"
            if not extract_frame(source_path, sample_time, str(source_frame)):
                continue
            if not extract_frame(rendered_path, sample_time, str(rendered_frame)):
                continue

            source_image = cv2.imread(str(source_frame))
            rendered_image = cv2.imread(str(rendered_frame))
            if source_image is None or rendered_image is None:
                continue
            if source_image.shape != rendered_image.shape:
                rendered_image = cv2.resize(
                    rendered_image,
                    (source_image.shape[1], source_image.shape[0]),
                )

            difference = cv2.cvtColor(
                cv2.absdiff(source_image, rendered_image),
                cv2.COLOR_BGR2GRAY,
            )
            height = difference.shape[0]
            subtitle_band = difference[int(height * 0.55):int(height * 0.95)]
            control_band = difference[:int(height * 0.35)]
            subtitle_change = float(np.mean(subtitle_band > 20))
            control_change = float(np.mean(control_band > 20))
            checked += 1
            if subtitle_change > max(0.0015, control_change * 1.35):
                logger.info(
                    "  字幕像素验证通过: 字幕区 %.3f%% / 对照区 %.3f%%",
                    subtitle_change * 100,
                    control_change * 100,
                )
                return True

        logger.error("  字幕像素验证失败（已检查 %d 个活动时间点）", checked)
        return False
    finally:
        shutil.rmtree(check_dir, ignore_errors=True)


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
        return (False, "")

    temp_dir: Optional[Path] = None
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
            shutil.copy2(video_path, output_path)
            return (True, "")

        # 加载模型
        model = whisper.load_model(whisper_model, device="cpu")

        # 语音识别
        logger.info("  Whisper 识别中...")
        options = {"task": "transcribe"}
        if language:
            options["language"] = language

        result = model.transcribe(str(audio_path), **options)

        if not result["segments"]:
            logger.warning("  未识别到语音")
            shutil.copy2(video_path, output_path)
            return (True, "")

        # 收集字幕文本供BGM选择使用
        subtitle_text = " ".join(seg["text"].strip() for seg in result["segments"] if seg["text"].strip())

        segments = [segment for segment in result["segments"] if segment["text"].strip()]
        ass_path = temp_dir / "subtitle.ass"

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write("[Script Info]\n")
            f.write("Title: subtitle\n")
            f.write("Original Script: \n")
            f.write("PlayResX: 2160\n")
            f.write("PlayResY: 3840\n")
            f.write("ScriptType: v4.00+\n")
            f.write("WrapStyle: 0\n\n")

            f.write("[V4+ Styles]\n")
            f.write("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
            f.write("Style: Default,Microsoft YaHei,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,2,2,80,80,150,1\n")

            f.write("[Events]\n")
            f.write("Format: Layer, Start, End, Style, Text\n")

            for segment in segments:
                text = segment["text"].strip()
                if not text:
                    continue
                start = float(segment["start"])
                end = float(segment["end"])
                # 静态字幕：整句一次性显示，不再逐字出现
                f.write(
                    f"Dialogue: 0,{format_ass_time(start)},"
                    f"{format_ass_time(end)},Default,{escape_ass_text(text)}\n"
                )

        logger.info(f"  ASS字幕生成: {ass_path}")

        cmd = [
            "ffmpeg", "-y",
            "-i", os.path.abspath(video_path),
            "-vf", f"ass={ass_path.name}",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "copy",
            "-movflags", "+faststart",
            os.path.abspath(output_path),
        ]

        if not run_ffmpeg(cmd, "  烧录字幕", cwd=str(temp_dir)):
            return (False, "")

        if not verify_burned_subtitles(video_path, output_path, segments):
            return (False, "")

        logger.info(f"  字幕烧录完成: {output_path}")

        return (True, subtitle_text)

    except Exception as e:
        logger.error(f"字幕生成异常: {e}")
        return (False, "")
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


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

        # Step 3: 只使用BGM作为音轨（丢弃源视频原音频，去除人声）
        # 不再提取/混合原视频音频，直接用BGM替换
        mix_cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", str(bgm_quiet),
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            output_path
        ]

        if not run_ffmpeg(mix_cmd, "  替换音轨为BGM"):
            # 避免SameFileError
            if os.path.abspath(video_path) != os.path.abspath(output_path):
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
        if os.path.abspath(video_path) != os.path.abspath(output_path):
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

    temp_dir: Optional[Path] = None
    try:
        main_info = get_video_info(video_path)
        logo_info = get_video_info(logo_path)
        if not main_info or not logo_info:
            logger.error("无法获取主体或片尾信息")
            return False

        actual_logo_duration = logo_info.duration
        logger.info(f"  片尾实际时长: {actual_logo_duration:.2f}s")

        # 标准化片尾视频到4K
        temp_dir = Path(tempfile.mkdtemp(prefix="logo_processing_"))
        scaled_logo = temp_dir / "logo.mp4"
        main_video = temp_dir / "main.mp4"

        scale_filter = f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2"

        # 主体保留视频，仅把音频统一成48kHz立体声，保证concat流结构一致。
        if main_info.has_audio:
            copy_main_cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-map", "0:v:0", "-map", "0:a:0",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                str(main_video),
            ]
        else:
            copy_main_cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-t", str(main_info.duration), "-shortest",
                str(main_video),
            ]

        if not run_ffmpeg(copy_main_cmd, "  复制主体视频"):
            return False

        # 片尾保留原始音频（打字音效），如果有的话；
        # 如果片尾没有音频轨，才补一条静音AAC流。
        if logo_info.has_audio:
            cmd = [
                "ffmpeg", "-y",
                "-i", logo_path,
                "-vf", scale_filter,
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-ac", "2",
                "-r", "30",
                "-t", str(actual_logo_duration),
                "-shortest",
                "-pix_fmt", "yuv420p",
                str(scaled_logo)
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-i", logo_path,
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-map", "0:v:0", "-map", "1:a:0",
                "-vf", scale_filter,
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-r", "30",
                "-t", str(actual_logo_duration),
                "-shortest",
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

        final_info = get_video_info(output_path)
        expected_duration = main_info.duration + actual_logo_duration
        if (
            not final_info
            or final_info.width != OUTPUT_WIDTH
            or final_info.height != OUTPUT_HEIGHT
            or not final_info.has_audio
            or abs(final_info.duration - expected_duration) > 0.35
        ):
            logger.error(
                "  片尾验收失败: 期望 %.2fs，实际 %s",
                expected_duration,
                f"{final_info.duration:.2f}s" if final_info else "不可读取",
            )
            return False

        logger.info(f"  片尾拼接完成: {output_path}")
        return True

    except Exception as e:
        logger.error(f"片尾拼接异常: {e}")
        return False
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


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

    # ===== Stage 3: 剪辑规划 =====
    quality_exclusions = load_quality_exclusions(args.category)
    if has_openai_key:
        logger.info("使用AI规划 (需要OpenAI SDK)")
        # TODO: 实现AI规划
        candidates = stage3_heuristic_planning(
            materials_with_scenes,
            args.candidates,
            quality_exclusions,
        )
    else:
        logger.info("使用启发式规划")
        candidates = stage3_heuristic_planning(
            materials_with_scenes,
            args.candidates,
            quality_exclusions,
        )

    if not candidates:
        logger.error("未能生成剪辑方案")
        return 1

    if args.dry_run:
        logger.info("Dry run 完成：素材分析与剪辑规划均通过，未渲染视频")
        return 0

    # ===== Stage 4-6: 为每个候选方案执行 =====
    completed_candidate_ids: list[int] = []
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
            existing_info = get_video_info(final_video)
            if existing_info:
                completed_candidate_ids.append(plan.id)
            continue

        # Stage 4: 视频合成
        if not stage4_video_compose(plan, clean_video, args.keep_temp):
            logger.error(f"方案{plan.id} 视频合成失败")
            continue
        clean_info = get_video_info(clean_video)
        if (
            not clean_info
            or not (TARGET_DURATION_MIN <= clean_info.duration <= TARGET_DURATION_MAX + 0.20)
        ):
            logger.error(
                "方案%d 主体时长验收失败: %s",
                plan.id,
                f"{clean_info.duration:.2f}s" if clean_info else "不可读取",
            )
            continue

        # Stage 5: 字幕生成（优先 AI 视觉字幕，回退到 Whisper）
        subtitle_success = False
        subtitle_text = ""
        if openai_available and os.environ.get("OPENAI_API_KEY"):
            subtitle_success, subtitle_text = generate_ai_subtitles(
                clean_video, plan, subtitle_video
            )
            if not subtitle_success:
                logger.info("  AI字幕未成功，回退到Whisper流程")

        if not subtitle_success:
            subtitle_success, subtitle_text = stage5_generate_subtitle(
                clean_video, subtitle_video, args.whisper_model, args.language
            )

        if not subtitle_success:
            logger.error(f"方案{plan.id} 字幕生成失败")
            continue

        # Stage 5.5: BGM混合
        bgm_video = os.path.join(output_dir, f"{candidate_name}_bgm.mp4")
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
            logger.info(f"[OK] 方案{plan.id} 完成: {final_info.duration:.2f}s, {final_info.width}x{final_info.height}")
            completed_candidate_ids.append(plan.id)
        else:
            logger.error(f"方案{plan.id} 验证失败")

    # 生成报告
    manifest = {
        "run_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "input_dir": input_dir,
        "output_dir": output_dir,
        "materials_count": len(materials),
        "candidates_planned": len(candidates),
        "candidates_completed": len(completed_candidate_ids),
        "completed_candidate_ids": completed_candidate_ids,
        "plans": [
            {
                "id": plan.id,
                "main_duration": round(plan.total_duration, 3),
                "clips_count": len(plan.clips),
                "clips": [
                    {
                        "file": Path(clip.source_file).name,
                        "start": round(clip.start_time, 3),
                        "end": round(clip.end_time, 3),
                    }
                    for clip in plan.clips
                ],
            }
            for plan in candidates
        ],
        "quality_exclusions_applied": quality_exclusions,
    }

    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    logger.info(f"\n工作流完成！输出目录: {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

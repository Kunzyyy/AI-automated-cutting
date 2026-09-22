from __future__ import annotations

import math
import re
import shutil
from pathlib import Path

import cv2
from PIL import ImageFont

from .common import ROOT, local_path, probe, run, video_artifact


def check_supported(candidate):
    for clip in candidate["timeline"]:
        if clip.get("speed", 1) != 1 or clip["transition"] != {"type": "cut", "duration_seconds": 0}:
            raise ValueError("V2 renderer requires speed=1 and hard cuts with zero transition duration")
        if clip["crop_strategy"] not in {"fit", "fill_center"}:
            raise ValueError("V2 renderer supports fit/fill_center; subject tracking is not implemented")


def vf(width, height, fps, mode="fit"):
    if width % 2 or height % 2:
        raise ValueError("H.264 output dimensions must be even")
    if mode == "fit":
        # White padding avoids creating black borders that would be confused with black-frame QC.
        scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white"
    else:
        scale = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    return scale + f",setsar=1,fps={fps},format=yuv420p"


def encode():
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", "4", "-pix_fmt", "yuv420p"]


def rough_cut(candidate, footage, directory, delivery):
    check_supported(candidate)
    directory.mkdir(parents=True, exist_ok=True)
    sources = {s["asset_id"]: local_path(s["uri"]) for s in footage["sources"]}
    clips = []
    for index, clip in enumerate(candidate["timeline"]):
        output = directory / f"clip-{index:02}.mp4"
        run(["ffmpeg", "-v", "error", "-y", "-ss", clip["source_in_seconds"], "-i", sources[clip["source_asset_id"]],
             "-t", clip["duration_seconds"], "-an", "-vf", vf(delivery["width"], delivery["height"], delivery["fps"], clip["crop_strategy"]),
             *encode(), output])
        clips.append(output)
    (directory / "concat.txt").write_text("\n".join(f"file '{p.name}'" for p in clips), encoding="utf-8")
    output = directory / "rough.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "rough.mp4"], cwd=directory)
    if abs(video_artifact(output)["duration_seconds"] - candidate["total_body_duration_seconds"]) > len(clips) / delivery["fps"] + .05:
        raise ValueError("Rough cut duration differs from the EditPlan")
    return output


def sample_frames(video, directory, interval=.5):
    directory.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(video))
    info = video_artifact(video)
    frame_rate = info["fps"]
    step = max(1, round(frame_rate * interval))
    images = []
    try:
        index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            timestamp = index / frame_rate
            take = index % step == 0
            index += 1
            if not take:
                continue
            h, w = frame.shape[:2]
            frame = cv2.resize(frame, (int(w * min(1, 640 / h)), min(h, 640)))
            path = directory / f"frame-{index:03}.jpg"
            encoded_ok, encoded = cv2.imencode('.jpg', frame)
            if not encoded_ok:
                raise OSError("Cannot save review frame")
            path.write_bytes(encoded.tobytes())
            images.append((timestamp, path))
        if not images or index / frame_rate < info["duration_seconds"] - .3:
            raise ValueError("Review could not decode the full video")
    finally:
        capture.release()
    return images


def ass_time(seconds):
    value = round(seconds * 100)
    return f"{value // 360000}:{value // 6000 % 60:02}:{value // 100 % 60:02}.{value % 100:02}"


def captions_ass(cues, candidate, footage, output, delivery):
    width, height = delivery["width"], delivery["height"]
    fontsize = round(width * .049)
    font = ImageFont.truetype(str(ROOT / "fonts" / "Lato-Bold.ttf"), fontsize)
    shots = {s["shot_id"]: s for s in footage["shots"]}
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Lato,{fontsize},&H00FFFFFF,&H00FFFFFF,&H00202020,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,8,60,60,100,1
[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines = []
    if len(cues) != len(candidate["timeline"]):
        raise ValueError("Caption count must match the timeline")
    for cue, clip in zip(cues, candidate["timeline"]):
        text = cue["text"].strip()
        if cue["sequence"] != clip["sequence"] or not text or any(c in text for c in "{}\\\r\n"):
            raise ValueError("Invalid caption sequence or unsafe ASS text")
        words, wrapped = text.split(), [""]
        for word in words:
            proposed = (wrapped[-1] + " " + word).strip()
            if font.getlength(proposed) > width * .78:
                wrapped.append(word)
            else:
                wrapped[-1] = proposed
        if len(wrapped) > 2 or any(font.getlength(line) > width * .78 for line in wrapped):
            raise ValueError("Caption cannot fit the two-line safe area")
        regions = shots[clip["shot_id"]]["composition"].get("safe_caption_regions", [])
        if "top" in regions:
            position, alignment = round(height * .12), 8
        elif "bottom" in regions:
            position, alignment = round(height * .80), 2
        else:
            position, alignment = round(height * .12), 8
        start = clip["timeline_in_seconds"]
        caption = r"\N".join(wrapped)
        tags = "{" + f"\\an{alignment}\\pos({width // 2},{position})" + "}"
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(start + clip['duration_seconds'])},Default,,0,0,0,,{tags}{caption}")
    output.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def finalize(rough, captions, music, end_card, delivery):
    """Music and end card are optional; without them the body itself is the deliverable."""
    directory = rough.parent
    body_duration = video_artifact(rough)["duration_seconds"]
    # Copy font next to ASS: avoids Windows drive-letter escaping in filter syntax.
    (directory / "fonts").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "fonts" / "Lato-Bold.ttf", directory / "fonts" / "Lato-Bold.ttf")
    if music:
        audio_input = ["-stream_loop", "-1", "-i", music]
        audio_filter = f"loudnorm=I=-22:TP=-2:LRA=7,afade=t=in:d=0.4,afade=t=out:st={max(0, body_duration-.8)}:d=0.8"
    else:
        # A silent track keeps the body concat-compatible with an end card that carries its own audio.
        audio_input = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        audio_filter = "anull"
    run(["ffmpeg", "-v", "error", "-y", "-i", rough.name, *audio_input,
         "-vf", "ass=captions.ass:fontsdir=fonts", "-af", audio_filter,
         "-map", "0:v:0", "-map", "1:a:0", "-t", body_duration, *encode(), "-c:a", "aac", "-ar", "48000", "-ac", "2", "body.mp4"], cwd=directory)
    if end_card is None:
        run(["ffmpeg", "-v", "error", "-y", "-i", "body.mp4", "-c", "copy", "-movflags", "+faststart", "final.mp4"], cwd=directory)
        return directory / "final.mp4"
    end_info = probe(end_card)
    if not any(s["codec_type"] == "audio" for s in end_info["streams"]):
        raise ValueError("End card must contain its own audio")
    run(["ffmpeg", "-v", "error", "-y", "-i", end_card, "-vf", vf(delivery["width"], delivery["height"], delivery["fps"]),
         *encode(), "-c:a", "aac", "-ar", "48000", "-ac", "2", directory / "end-card.mp4"])
    (directory / "final-concat.txt").write_text("file 'body.mp4'\nfile 'end-card.mp4'\n", encoding="utf-8")
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "final-concat.txt",
         "-c", "copy", "-movflags", "+faststart", "final.mp4"], cwd=directory)
    return directory / "final.mp4"


def technical_qc(video, expected_duration, delivery, expect_audio=True, expect_end_card=True):
    info = video_artifact(video)
    diagnostics = run(["ffmpeg", "-hide_banner", "-i", video, "-vf", "blackdetect=d=0.08:pix_th=0.08,freezedetect=n=-50dB:d=2",
                       "-af", "volumedetect", "-f", "null", "-"], timeout=600)
    log = diagnostics.stderr
    peak = re.search(r"max_volume: ([\-\d.]+) dB", log)
    if not peak:
        raise ValueError("QC cannot measure audio peak")
    black = sum(round(float(d) * info["fps"]) for d in re.findall(r"black_duration:([\d.]+)", log))
    frozen = sum(round(float(d) * info["fps"]) for d in re.findall(r"freeze_duration: ([\d.]+)", log))
    peak_db = float(peak.group(1))
    errors = []
    if abs(info["duration_seconds"] - expected_duration) > .4:
        errors.append("Final duration differs from body plus end card")
    if (info["width"], info["height"]) != (delivery["width"], delivery["height"]):
        errors.append("Unexpected output resolution")
    if black:
        errors.append("Black frames detected")
    if peak_db >= 0:
        errors.append("Audio reaches full scale")
    if expect_audio and peak_db < -55:
        errors.append("Audio is effectively silent")
    warnings = ["Frozen scenes require visual review"] if frozen else []
    if not expect_audio:
        warnings.append("No BGM library configured: the body track is silent")
    if not expect_end_card:
        warnings.append("No brand end card configured: the delivery is body only")
    return dict(passed=not errors, black_frames=black, frozen_frames=frozen, audio_peak_dbfs=peak_db,
                audio_clipping=peak_db >= 0, caption_safe_area=False,
                end_card_present=expect_end_card and abs(info["duration_seconds"]-expected_duration) < .4,
                errors=errors, warnings=warnings)

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[3]


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def validate(value, name):
    Draft202012Validator(read(ROOT / "schemas" / (name + ".schema.json")), format_checker=FormatChecker()).validate(value)


def local_path(uri, base=None):
    parsed = urlparse(str(uri))
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("Only local file URIs are supported")
        return Path(url2pathname(parsed.path)).resolve()
    if parsed.scheme and len(parsed.scheme) != 1:
        raise ValueError("Download media to a mounted directory before submitting a job")
    path = Path(uri)
    return (path if path.is_absolute() else Path(base or Path.cwd()) / path).resolve()


def run(args, cwd=None, timeout=900):
    result = subprocess.run([str(x) for x in args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[-2500:]}")
    return result


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]).stdout)


def video_artifact(path):
    info = probe(path)
    stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    num, den = stream["avg_frame_rate"].split("/")
    return dict(uri=Path(path).resolve().as_uri(), duration_seconds=float(info["format"]["duration"]),
                width=stream["width"], height=stream["height"], fps=float(num) / float(den),
                container="mp4", size_bytes=Path(path).stat().st_size)

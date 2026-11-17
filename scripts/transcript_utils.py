"""Pure utilities for normalizing video caption files (stdlib only)."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TIMESTAMP_RE = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{1,3})")
TAG_RE = re.compile(r"</?(?:c(?:\.[\w-]+)*|v|lang|i|b|u|ruby|rt|font)[^>]*>", re.I)


def clean_text(value: str) -> str:
    value = TAG_RE.sub("", value)
    value = re.sub(r"\{\\[^}]*\}", "", value)
    value = html.unescape(value)
    return " ".join(value.replace("\u200b", "").split())


def to_seconds(value: str) -> float:
    m = TIMESTAMP_RE.fullmatch(value.strip())
    if not m:
        raise ValueError(f"Invalid subtitle timestamp: {value!r}")
    h, minute, second, frac = m.groups()
    return int(h or 0) * 3600 + int(minute) * 60 + int(second) + int(frac.ljust(3, "0")) / 1000


def hms(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def timestamp_url(url: str, seconds: float) -> str:
    parsed = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in ("t", "start")]
    params.append(("t", f"{max(0, int(seconds))}s" if "youtu" in parsed.netloc.lower() else str(max(0, int(seconds)))))
    return urlunparse(parsed._replace(query=urlencode(params)))


def normalize(cues: list[dict]) -> list[dict]:
    result: list[dict] = []
    for cue in cues:
        try:
            start = float(cue["start"])
            end = float(cue["end"])
            text = clean_text(str(cue["text"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not text or start < 0 or end < start:
            continue
        if result and text == result[-1]["text"] and start <= result[-1]["end"] + 0.5:
            result[-1]["end"] = max(result[-1]["end"], end)
        else:
            result.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return sorted(result, key=lambda c: c["start"])


def parse_srt(content: str) -> list[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", content.replace("\r\n", "\n").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_idx = next((i for i, s in enumerate(lines) if "-->" in s), None)
        if timing_idx is None:
            continue
        bounds = lines[timing_idx].split("-->", 1)
        try:
            start = to_seconds(bounds[0].strip())
            end = to_seconds(bounds[1].strip().split()[0])
        except ValueError:
            continue
        cues.append({"start": start, "end": end, "text": " ".join(lines[timing_idx + 1:])})
    return normalize(cues)


def parse_vtt(content: str) -> list[dict]:
    content = content.lstrip("\ufeff").replace("\r\n", "\n")
    content = re.sub(r"(?m)^WEBVTT[^\n]*\n", "", content)
    content = re.sub(r"(?ms)^(?:NOTE|STYLE|REGION)(?:[^\n]*)\n.*?(?=\n\s*\n|\Z)", "", content)
    return parse_srt(content)


def parse_json_captions(content: str) -> list[dict]:
    obj = json.loads(content)
    # Bilibili: {body:[{from,to,content},...]}; alternate JSON arrays.
    if isinstance(obj, list):
        return normalize([{"start": c.get("from", c.get("start", 0)),
                           "end": c.get("to", c.get("end", 0)),
                           "text": c.get("content", c.get("text", ""))}
                          for c in obj if isinstance(c, dict)])
    if isinstance(obj, dict) and isinstance(obj.get("body"), list):
        return parse_json_captions(json.dumps(obj["body"], ensure_ascii=False))
    # YouTube json3: events contain tStartMs, dDurationMs, segs[].utf8.
    cues = []
    for event in obj.get("events", []) if isinstance(obj, dict) else []:
        if not event.get("segs"):
            continue
        start = float(event.get("tStartMs", 0)) / 1000
        duration = float(event.get("dDurationMs", 0)) / 1000
        text = "".join(str(seg.get("utf8", "")) for seg in event["segs"])
        cues.append({"start": start, "end": start + duration, "text": text})
    return normalize(cues)


def parse_file(file: Path) -> list[dict]:
    raw = file.read_text(encoding="utf-8-sig")
    suffix = file.suffix.lower()
    if suffix == ".srt":
        return parse_srt(raw)
    if suffix == ".vtt":
        return parse_vtt(raw)
    if suffix in (".json", ".json3"):
        return parse_json_captions(raw)
    raise ValueError(f"Unsupported caption format {suffix}. Supported: .srt/.vtt/.json/.json3")


def write_cues(cues: list[dict], source_url: str, target: Path, max_chunk_chars: int = 12000) -> int:
    target.mkdir(parents=True, exist_ok=True)
    (target / "transcript.json").write_text(json.dumps(cues, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"[{hms(c['start'])}]({timestamp_url(source_url, c['start'])}) {c['text']}" for c in cues]
    (target / "transcript.md").write_text("# Timestamped transcript\n\n" + "\n\n".join(lines) + "\n", encoding="utf-8")
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) > max_chunk_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append(current)
    part_dir = target / "chunks"
    part_dir.mkdir(exist_ok=True)
    for old in part_dir.glob("chunk-*.md"):
        old.unlink()
    for i, chunk in enumerate(chunks, 1):
        (part_dir / f"chunk-{i:03d}.md").write_text(f"# Transcript segment {i}/{len(chunks)}\n\n" + "\n\n".join(chunk) + "\n", encoding="utf-8")
    return len(chunks)

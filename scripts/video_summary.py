#!/usr/bin/env python3
"""Prepare transcript + metadata for an agent; the agent writes the actual summary.

Usage: python scripts/video_summary.py 'https://www.youtube.com/watch?v=...'
Python 3.10+, yt-dlp[default]. Whisper+FFmpeg optional for subtitle-less videos.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from transcript_utils import parse_file, write_cues

SUFFIXES = (".srt", ".vtt", ".json", ".json3")


def platform_of(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http"):
        raise ValueError("URL must start with https:// or http://")
    if host in ("youtu.be", "youtube.com") or host.endswith(".youtube.com"):
        return "youtube"
    if host in ("bilibili.com", "b23.tv") or host.endswith(".bilibili.com"):
        return "bilibili"
    raise ValueError("Only YouTube and Bilibili links are supported")


def run(cmd: list[str], *, capture: bool = False) -> str:
    try:
        result = subprocess.run(cmd, check=True, text=True, encoding="utf-8", errors="replace",
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None)
    except FileNotFoundError as e:
        raise RuntimeError(f"Missing executable: {cmd[0]}") from e
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or "").strip() if capture else "see console output"
        raise RuntimeError(f"Command failed: {cmd[0]} ({e.returncode}); {msg[-1400:]}") from e
    return result.stdout or ""


def preferred_track(info: dict, platform: str, language: str | None = None) -> tuple[str, str] | None:
    original = str(info.get("language") or "").lower()
    if platform == "bilibili" and not original:
        original = "zh"
    preferred = (language or ("zh" if platform == "bilibili" else "en")).lower()
    items = []
    for origin, key in (("manual", "subtitles"), ("auto", "automatic_captions")):
        for code, formats in (info.get(key) or {}).items():
            lower = str(code).lower()
            if not formats or lower in ("danmaku", "live_chat") or "translate" in lower:
                continue
            # yt-dlp exposes Bilibili AI subtitles among ordinary subtitles.
            is_ai = lower.startswith("ai-")
            real_origin = "auto" if is_ai else origin
            score = 0
            if original and (lower == original or lower.startswith(original + "-") or lower == f"ai-{original}"):
                score += 100
            if lower == preferred or lower.startswith(preferred + "-") or lower == f"ai-{preferred}":
                score += 30
            if real_origin == "manual":
                score += 15
            if lower.endswith("-orig"):
                score += 10
            if "-" in lower and not (lower.startswith("ai-") or lower.endswith("-orig")):
                score -= 2
            items.append((score, code, origin))
    if not items:
        return None
    items.sort(key=lambda x: (-x[0], x[1]))
    _, code, origin = items[0]
    return code, origin


def fetch_subtitle(url: str, yt: list[str], info: dict, work: Path, track: tuple[str, str]) -> Path | None:
    code, origin = track
    video_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(info.get("id", "video")))
    # Use exact language regex; yt-dlp language selectors support regex.
    pattern = "^" + re.escape(str(code)) + "$"
    cmd = yt + ["--no-playlist", "--skip-download", "--no-progress", "--sub-langs", pattern,
                "--sub-format", "srt/vtt/json3/json/best", "-P", str(work), "-o", "%(id)s.%(ext)s"]
    cmd += ["--write-subs" if origin == "manual" else "--write-auto-subs", url]
    try:
        run(cmd)
    except RuntimeError as e:
        print(f"Subtitle download failed: {e}", file=sys.stderr)
        return None
    files = [p for p in work.glob(f"{video_id}.*") if p.suffix.lower() in SUFFIXES]
    # Some short-link resolutions can have a different id; work/ is private per video.
    if not files:
        files = [p for p in work.iterdir() if p.suffix.lower() in SUFFIXES]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)[0] if files else None


def whisper_fallback(url: str, yt: list[str], work: Path, model: str) -> Path:
    print("No usable subtitles: extracting audio for local Whisper transcription.")
    run(yt + ["--no-playlist", "-f", "ba", "-x", "--audio-format", "mp3",
              "-P", str(work), "-o", "speech.%(ext)s", url])
    audio = work / "speech.mp3"
    if not audio.exists():
        alternatives = list(work.glob("speech.*"))
        if not alternatives:
            raise RuntimeError("Audio extraction did not produce a file; check FFmpeg and yt-dlp")
        audio = alternatives[0]
    try:
        run([sys.executable, "-m", "whisper", str(audio), "--model", model,
             "--output_format", "srt", "--output_dir", str(work)])
    except RuntimeError as e:
        raise RuntimeError("Whisper failed; install with 'python -m pip install -U openai-whisper' "
                           "and ensure FFmpeg is on PATH. Audio remains in work/. " + str(e)) from e
    srt = work / f"{audio.stem}.srt"
    if not srt.exists():
        raise RuntimeError("Whisper completed but SRT was not created")
    return srt


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare YouTube/Bilibili video transcript for a summarizing agent")
    ap.add_argument("url", help="YouTube/Bilibili video URL")
    ap.add_argument("--output", help="Output directory; by default, create one per video ID")
    ap.add_argument("--cookies-from-browser", choices=("edge", "chrome", "firefox", "brave", "chromium"),
                    help="Opt in to yt-dlp browser cookies if you need login")
    ap.add_argument("--language", help="Preferred subtitle language code, e.g. en or zh")
    ap.add_argument("--whisper-model", default="small", help="Whisper model if subtitles are missing")
    ap.add_argument("--no-whisper", action="store_true", help="Only use existing subtitles")
    args = ap.parse_args()
    try:
        platform = platform_of(args.url)
        yt = [sys.executable, "-m", "yt_dlp"]
        if args.cookies_from_browser:
            yt += ["--cookies-from-browser", args.cookies_from_browser]
        raw = run(yt + ["--no-playlist", "--dump-single-json", "--skip-download", "--no-warnings", args.url], capture=True)
        info = json.loads(raw)
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(info.get("id") or "video"))
        out = Path(args.output if args.output else f"video-summary-output/{platform}-{safe_id}").expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        metadata = {
            "id": info.get("id"), "title": info.get("title"), "platform": platform,
            "uploader": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"), "language": info.get("language"),
            "url": info.get("webpage_url") or args.url,
            "description": info.get("description"),
        }
        (out / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        work = out / "work"
        work.mkdir(exist_ok=True)
        # Never reuse a previous subtitle file after a failed new download.
        for old in work.iterdir():
            if old.is_file() and old.suffix.lower() in SUFFIXES:
                old.unlink()
        track = preferred_track(info, platform, args.language)
        print(f"Platform: {platform}; subtitle candidate: {track or 'none'}")
        source = "Whisper"
        subfile = fetch_subtitle(args.url, yt, info, work, track) if track else None
        cues = []
        if subfile is not None:
            try:
                cues = parse_file(subfile)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                print(f"Cannot parse caption file {subfile.name}: {e}", file=sys.stderr)
            if cues:
                source = f"yt-dlp {track[0]} ({subfile.name})"
        if not cues:
            if args.no_whisper:
                raise RuntimeError("No usable subtitles. Rerun without --no-whisper to transcribe audio.")
            subfile = whisper_fallback(args.url, yt, work, args.whisper_model)
            cues = parse_file(subfile)
        if not cues:
            raise RuntimeError("Transcript is empty")
        chunks = write_cues(cues, metadata["url"], out)
        metadata["transcript_source"] = source
        metadata["caption_count"] = len(cues)
        metadata["chunk_count"] = chunks
        (out / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"READY: {len(cues)} cues, {chunks} chunks in {out}")
        print(f"Next: have Codex read ALL {chunks} files under {out / 'chunks'} and create an Obsidian note.")
        return 0
    except (RuntimeError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

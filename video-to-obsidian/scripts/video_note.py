import argparse
import html
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from whisper_local import transcribe


LOCAL_NOTE_DIR_FILE = Path(__file__).resolve().parents[1] / 'note-dir.local'
CHUNK_MAX_CHARS = 12_000
YT_DLP = [
    'yt-dlp', '--ignore-config', '--no-cache-dir', '--no-playlist',
    '--socket-timeout', '20', '--retries', '1', '--extractor-retries', '1',
]
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'}
BILIBILI_HOSTS = {'bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv'}
TIMING = re.compile(r'(?m)^((?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s+-->[^\n]*$')


def run(command):
    result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                            env={**os.environ, 'PYTHONUTF8': '1'})
    if result.returncode:
        lines = result.stderr.strip().splitlines() or result.stdout.strip().splitlines()
        raise ValueError(lines[-1] if lines else f'{command[0]} exited with {result.returncode}')
    return result.stdout


def video_identity(url):
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in {'http', 'https'} or parts.username or parts.password or parts.port:
        raise ValueError('请提供 B 站或 YouTube 的 HTTP(S) 视频链接。')
    query = parse_qs(parts.query)
    path = parts.path.rstrip('/').split('/')
    if host in YOUTUBE_HOSTS:
        if host == 'youtu.be' and len(path) == 2:
            video_id = path[1]
        elif len(path) == 3 and path[1] in {'shorts', 'live', 'embed'}:
            video_id = path[2]
        else:
            video_id = query.get('v', [''])[0] if path == ['', 'watch'] else ''
        if re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            return 'youtube', video_id, f'https://www.youtube.com/watch?v={video_id}'
    if host in BILIBILI_HOSTS and len(path) == 3 and path[1] == 'video':
        video_id = path[2]
        page = query.get('p', ['1'])[0]
        if re.fullmatch(r'BV[A-Za-z0-9]{10}', video_id) and re.fullmatch(r'[1-9][0-9]*', page):
            base = f'https://www.bilibili.com/video/{video_id}/'
            return 'bilibili', f'{video_id}-p{page}', f'{base}?p={page}' if 'p' in query else base
    if host == 'b23.tv' and len(path) == 2 and re.fullmatch(r'[A-Za-z0-9]+', path[1]):
        return 'bilibili', None, url
    raise ValueError('请提供单个视频的链接。B 站分 P 使用 ?p=数字。')


def existing_note(directory, key):
    suffix = f' [{key}].md'
    return next((path for path in sorted(directory.glob('*.md')) if path.name.endswith(suffix)), None)


def default_note_dir():
    configured = os.environ.get('OBSIDIAN_VIDEO_NOTES_DIR', '').strip()
    if not configured and LOCAL_NOTE_DIR_FILE.is_file():
        configured = LOCAL_NOTE_DIR_FILE.read_text(encoding='utf-8-sig').strip()
    return Path(configured).expanduser() if configured else None


def timestamp_link(meta, seconds):
    seconds = max(0, math.floor(seconds))
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    label = f'{hour:02d}:{minute:02d}:{second:02d}' if hour else f'{minute:02d}:{second:02d}'
    join = '&' if '?' in meta['url'] else '?'
    timestamp = f'{seconds}s' if meta['platform'] == 'youtube' else str(seconds)
    return f'[{label}]({meta["url"]}{join}t={timestamp})'


def seconds(value):
    total = 0.0
    for component in value.replace(',', '.').split(':'):
        total = total * 60 + float(component)
    return total


def subtitle_cues(path):
    content = path.read_text(encoding='utf-8-sig').replace('\r', '')
    if path.suffix in {'.json', '.json3'}:
        data = json.loads(content)
        if 'body' in data:
            yield from ((float(cue['from']), cue['content']) for cue in data['body'])
        else:
            for event in data.get('events', []):
                yield event.get('tStartMs', 0) / 1000, ''.join(
                    segment.get('utf8', '') for segment in event.get('segs', []))
    else:
        for match in TIMING.finditer(content):
            start = match.end() + 1
            end = content.find('\n\n', start)
            yield seconds(match.group(1)), content[start:end if end != -1 else None]


def write_transcript(path, meta, work):
    if path.suffix.lower() in {'.txt', '.md'}:
        content = path.read_text(encoding='utf-8-sig').strip()
    else:
        rows = []
        previous = ''
        for start, cue in subtitle_cues(path):
            cue = ' '.join(html.unescape(re.sub(r'<[^>]*>', '', cue)).split())
            if cue and cue != previous:
                rows.append(f'{timestamp_link(meta, start)} {cue}')
                previous = cue
        content = '\n'.join(rows)
    if not content:
        raise ValueError('字幕或逐字稿没有可用文字。')
    transcript = content + '\n'
    (work / 'transcript.md').write_text(transcript, encoding='utf-8')
    return write_chunks(transcript, work / 'chunks')


def write_chunks(transcript, directory):
    # Keep each timestamp and its caption together, even if one line exceeds the limit.
    chunks = []
    lines = []
    size = 0
    for line in transcript.splitlines(keepends=True):
        if lines and size + len(line) > CHUNK_MAX_CHARS:
            chunks.append(''.join(lines))
            lines = []
            size = 0
        lines.append(line)
        size += len(line)
    if chunks:
        chunks.append(''.join(lines))
        directory.mkdir(exist_ok=True)
        for index, chunk in enumerate(chunks, 1):
            (directory / f'chunk-{index:03d}.md').write_text(chunk, encoding='utf-8')
    for path in directory.glob('chunk-*.md'):
        if re.fullmatch(r'chunk-[0-9]{3,}\.md', path.name) and int(path.stem[6:]) > len(chunks):
            path.unlink()
    return len(chunks)


def choose_tracks(tracks, original_language):
    # YouTube's auto-translation tracks can number in the thousands.
    candidates = []
    for language, formats in tracks.items():
        if language in {'live_chat', 'danmaku'} or not re.fullmatch(r'[\w-]+', language):
            continue
        if not any(track.get('ext') in {'srt', 'vtt', 'json3', 'json'} for track in formats):
            continue
        first = formats[0].get('url') or ''
        if 'tlang' in parse_qs(urlsplit(first).query) and not language.endswith('-orig'):
            continue
        root = language.lower().removesuffix('-orig').split('-')[0]
        priority = (not language.endswith('-orig'), root != original_language,
                    root not in {'zh', 'ai_zh'}, root not in {'en', 'ai_en'})
        candidates.append((priority, language))
    return [language for _, language in sorted(candidates)][:2]


def prepare(args):
    work = args.work
    work.mkdir(parents=True, exist_ok=False)
    platform, video_id, url = video_identity(args.url)
    downloader = YT_DLP + (['--cookies-from-browser', args.browser] if args.browser else [])
    info = {}
    if video_id and not args.update:
        note = find_existing(args, f'{platform}-{video_id}')
        if note:
            return note_result(note, args.output_dir, 'existing')
    if not args.transcript or not video_id:
        info = inspect_video(downloader, url)
        if not video_id:
            match = re.fullmatch(r'(BV[A-Za-z0-9]{10})(?:_p([1-9][0-9]*))?', info.get('id', ''))
            if (info.get('extractor') or '').lower() != 'bilibili' or not match:
                raise ValueError('B 站短链未解析为具体 BV 视频。')
            bv, actual_page = match.groups()
            page = parse_qs(urlsplit(url).query).get('p', [actual_page])[0]
            resolved = f'https://www.bilibili.com/video/{bv}/'
            platform, video_id, url = video_identity(f'{resolved}?p={page}' if page else resolved)
            if page and page != (actual_page or '1') and not args.transcript:
                info = inspect_video(downloader, url)
    key = f'{platform}-{video_id}'
    if not args.update and (note := find_existing(args, key)):
        return note_result(note, args.output_dir, 'existing')
    meta = dict(platform=platform, key=key, url=url, source=None, chunk_count=0,
                title=args.title or info.get('title') or video_id,
                author=args.author or info.get('uploader') or info.get('channel') or '未知')
    if args.transcript:
        meta['chunk_count'] = write_transcript(args.transcript, meta, work)
        meta['source'] = '用户提供的字幕或逐字稿'
    else:
        language = (info.get('language') or '').lower().split('-')[0]
        manual = dict(info.get('subtitles') or {})
        automatic = dict(info.get('automatic_captions') or {})
        if platform == 'bilibili':
            for track in list(manual):
                if track.startswith(('ai-', 'ai_')):
                    automatic[track] = manual.pop(track)
        for tracks, flag, label in [(manual, '--write-subs', '平台人工字幕'),
                                    (automatic, '--write-auto-subs', '平台自动字幕')]:
            for track in choose_tracks(tracks, language):
                try:
                    inline = next((entry for entry in tracks[track] if entry.get('data')
                                   and entry.get('ext') in {'srt', 'vtt', 'json3', 'json'}), None)
                    if inline:
                        subtitle = work / f'subs.{track}.{inline["ext"]}'
                        subtitle.write_text(inline['data'], encoding='utf-8')
                    else:
                        run([*downloader, '--skip-download', flag, '--sub-langs', re.escape(track),
                             '--sub-format', 'srt/vtt/json3/json', '-o', str(work / 'subs.%(ext)s'), '--', url])
                        subtitle = next(work.glob(f'subs.{track}.*'), None)
                    if subtitle:
                        meta['chunk_count'] = write_transcript(subtitle, meta, work)
                        meta['source'] = f'{label}（{track}）'
                        break
                except (ValueError, KeyError) as error:
                    print(f'{label} {track}: {error}', file=sys.stderr)
            if meta['source']:
                break
    write_meta(work, meta)
    return {'status': 'ready' if meta['source'] else 'needs_transcription', 'metadata': meta,
            'work': str(work)}


def find_existing(args, key):
    for directory in (args.output_dir, args.fallback_dir):
        if directory and (note := existing_note(directory, key)):
            return note
    return None


def inspect_video(downloader, url):
    info = json.loads(run([*downloader, '--dump-single-json', '--simulate',
                           '--write-subs', '--write-auto-subs', '--', url]))
    if 'entries' in info:
        raise ValueError('链接指向多个视频，请提供单个视频或明确的 B 站分 P 链接。')
    return info


def write_meta(work, meta):
    (work / 'metadata.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')


def read_meta(work):
    return json.loads((work / 'metadata.json').read_text(encoding='utf-8'))


def transcribe_video(args):
    meta = read_meta(args.work)
    downloader = YT_DLP + (['--cookies-from-browser', args.browser] if args.browser else [])
    audio = run([*downloader, '-f', 'bestaudio/best', '-o', str(args.work / 'audio.%(ext)s'),
                 '--no-simulate', '--print', 'after_move:filepath', '--', meta['url']]).strip()
    if not audio:
        raise ValueError('yt-dlp 未返回下载的音频文件。')
    meta['chunk_count'] = write_transcript(transcribe(Path(audio), args.work, run), meta, args.work)
    meta['source'] = '本地 Whisper 自动转写'
    write_meta(args.work, meta)
    return {'status': 'ready', 'metadata': meta, 'work': str(args.work)}


def note_result(path, output_dir, status):
    result = {'status': status, 'path': str(path.resolve())}
    if output_dir and path.parent.resolve() == output_dir.resolve():
        result['obsidian_uri'] = 'obsidian://open?' + urlencode({'path': result['path']}, quote_via=quote)
    return result


def clean_title(title):
    title = re.sub(r'[<>:"/\\|?*\[\]\x00-\x1f]', ' ', title)
    title = ' '.join(title.split()).strip(' .')
    return title[:75].rstrip(' .') or '视频总结'


def write_note(directory, meta, content, update):
    directory.mkdir(parents=True, exist_ok=True)
    previous = existing_note(directory, meta['key'])
    target = previous or directory / f'{clean_title(meta["title"])} [{meta["key"]}].md'
    if previous and not update:
        return target, 'existing'
    with NamedTemporaryFile(dir=directory, suffix='.tmp', delete=False) as handle:
        scratch = Path(handle.name)
        handle.write(content.encode('utf-8'))
    try:
        if update:
            os.replace(scratch, target)
        else:
            try:
                if os.name == 'nt':
                    scratch.rename(target)
                else:
                    os.link(scratch, target)
            except FileExistsError:
                return target, 'existing'
    finally:
        scratch.unlink(missing_ok=True)
    if target.read_bytes() != content.encode('utf-8'):
        raise ValueError(f'保存后回读不一致: {target}')
    return target, 'saved'


def save(args):
    meta = read_meta(args.work)
    if not meta['source']:
        raise ValueError('没有可核查的字幕或转写，无法保存总结。')
    body = args.body.read_text(encoding='utf-8-sig').strip()
    if not body:
        raise ValueError('总结正文为空。')
    title = ' '.join(meta['title'].split())
    author = ' '.join(meta['author'].split())
    content = f'# {title}\n\n- 作者：{author}\n- 原视频：<{meta["url"]}>\n- 内容来源：{meta["source"]}\n\n{body}\n'
    if args.fallback_dir:
        previous = existing_note(args.fallback_dir, meta['key'])
        if previous and not args.update and (not args.output_dir or not existing_note(args.output_dir, meta['key'])):
            return note_result(previous, args.output_dir, 'existing')
    if args.output_dir is None:
        error = FileNotFoundError('未配置 Obsidian 笔记目录。请设置 OBSIDIAN_VIDEO_NOTES_DIR 或传入 --output-dir。')
    elif not args.output_dir.is_dir():
        error = FileNotFoundError(f'笔记目标目录不可用: {args.output_dir}')
    else:
        try:
            path, status = write_note(args.output_dir, meta, content, args.update)
            return note_result(path, args.output_dir, status)
        except OSError as exc:
            error = exc
    if not args.fallback_dir:
        raise error
    path, status = write_note(args.fallback_dir, meta, content, args.update)
    return {**note_result(path, args.output_dir, status), 'vault_error': str(error)}


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True)
    fetch = commands.add_parser('fetch')
    fetch.add_argument('url')
    fetch.add_argument('--transcript', type=Path)
    fetch.add_argument('--title')
    fetch.add_argument('--author')
    fetch.set_defaults(action=prepare)
    speech = commands.add_parser('transcribe')
    speech.set_defaults(action=transcribe_video)
    note = commands.add_parser('save')
    note.add_argument('--body', type=Path, required=True)
    note.set_defaults(action=save)
    for command in (fetch, speech, note):
        command.add_argument('--work', type=lambda value: Path(value).resolve(), required=True)
    for command in (fetch, note):
        command.add_argument('--output-dir', type=Path)
        command.add_argument('--fallback-dir', type=Path)
        command.add_argument('--update', action='store_true')
    for command in (fetch, speech):
        command.add_argument('--browser', choices=['edge', 'chrome', 'firefox'])
    args = parser.parse_args()
    try:
        if args.action in (prepare, save) and args.output_dir is None:
            args.output_dir = default_note_dir()
        print(json.dumps(args.action(args), ensure_ascii=False))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    main()

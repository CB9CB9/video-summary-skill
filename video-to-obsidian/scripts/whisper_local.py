import hashlib
import os
import platform
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen
from zipfile import ZipFile


RELEASE = 'https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip'
RELEASE_SHA256 = '6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb'
MODEL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin'
MODEL_SHA256 = '1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b'


def download(url, target, expected_hash):
    request = Request(url, headers={'User-Agent': 'video-to-obsidian'})
    with urlopen(request, timeout=60) as response, target.open('wb') as file:
        shutil.copyfileobj(response, file)
    with target.open('rb') as file:
        if hashlib.file_digest(file, 'sha256').hexdigest() != expected_hash:
            raise ValueError(f'下载文件的 SHA-256 与发布方校验值不一致: {url}')


def setup():
    cache = Path(os.environ.get('VIDEO_TO_OBSIDIAN_CACHE') or
                 Path(os.environ['LOCALAPPDATA']) / 'video-to-obsidian')
    model = cache / 'ggml-small.bin'
    runtime = cache / 'whisper-b5454'
    executable = runtime / 'whisper-cli.exe'
    if executable.is_file() and model.is_file():
        return executable, model
    if os.name != 'nt' or platform.machine().lower() not in {'amd64', 'x86_64'}:
        raise ValueError('自动配置需要 Windows x64。请用 WHISPER_CLI 和 WHISPER_MODEL 指定本机转写工具及模型。')
    cache.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=cache) as staging:
        staging = Path(staging)
        if not executable.is_file():
            archive = staging / 'whisper.zip'
            download(RELEASE, archive, RELEASE_SHA256)
            bundle = staging / 'whisper'
            bundle.mkdir()
            with ZipFile(archive) as files:
                for member in files.namelist():
                    name = Path(member).name
                    if name == 'whisper-cli.exe' or name.lower().endswith('.dll'):
                        with files.open(member) as source, (bundle / name).open('wb') as target:
                            shutil.copyfileobj(source, target)
            if not (bundle / executable.name).is_file():
                raise ValueError('Whisper 发布包缺少 whisper-cli.exe。')
            try:
                bundle.rename(runtime)
            except FileExistsError:
                if not executable.is_file():
                    raise
        if not model.is_file():
            print('正在准备本地 Whisper 多语言模型（约 488 MB）... ', file=sys.stderr, flush=True)
            pending_model = staging / model.name
            download(MODEL, pending_model, MODEL_SHA256)
            try:
                pending_model.replace(model)
            except PermissionError:
                if not model.is_file():
                    raise
    return executable, model


def transcribe(audio, work, run):
    cli = os.environ.get('WHISPER_CLI') or shutil.which('whisper-cli')
    model = os.environ.get('WHISPER_MODEL')
    if not (cli and model):
        bundled_cli, bundled_model = setup()
        cli = cli or bundled_cli
        model = model or bundled_model
    wave = work / 'speech.wav'
    output = work / 'speech'
    run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(audio),
         '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(wave)])
    run([str(cli), '-m', str(model), '-f', str(wave), '-l', 'auto', '-ng', '-osrt', '-of', str(output)])
    return output.with_suffix('.srt')

"""Run Windows' local Simplified Chinese OCR on an explicitly selected image."""
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageOps


SOURCE = 'local_screenshot_ocr'
OCR_LANGUAGE = 'zh-Hans-CN'
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
HAN_SPACING = re.compile(r'(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])')
WINDOWS_OCR = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Length -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq ('IAsyncOperation' + [char]96 + '1')
} | Select-Object -First 1
function Wait-Result($operation, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult()
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('zh-Hans-CN'))
if ($null -eq $engine) { exit 42 }
$file = Wait-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:CTRIP_OCR_INPUT_PATH)) ([Windows.Storage.StorageFile])
$stream = Wait-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
try {
    $decoder = Wait-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $max = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
    if ($decoder.PixelWidth -gt $max -or $decoder.PixelHeight -gt $max) { exit 43 }
    $bitmap = Wait-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    try {
        $result = Wait-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        @{ lines = @($result.Lines | ForEach-Object { $_.Text }) } | ConvertTo-Json -Compress
    } finally { $bitmap.Dispose() }
} finally { $stream.Dispose() }
"""


def read_windows_ocr(path):
    if os.name != 'nt':
        raise RuntimeError('截图文字识别需要 Windows 的简体中文 OCR')
    shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    code = base64.b64encode(WINDOWS_OCR.encode('utf-16-le')).decode('ascii')
    try:
        result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-EncodedCommand', code],
                                env={**os.environ, 'CTRIP_OCR_INPUT_PATH': str(path)},
                                capture_output=True, encoding='utf-8', errors='replace', timeout=35,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Windows 文字识别超时，请试一下较小的区域') from exc
    except OSError as exc:
        raise RuntimeError('无法启动 Windows 文字识别') from exc
    if result.returncode == 42:
        raise RuntimeError('此电脑尚未安装 Windows 简体中文 OCR 语言功能')
    if result.returncode == 43:
        raise RuntimeError('图片边长超过 Windows 文字识别限制，请缩小框选范围')
    if result.returncode:
        raise RuntimeError('Windows 未能完成文字识别')
    try:
        lines = json.loads(result.stdout)['lines']
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('Windows 文字识别未返回预期结果') from exc
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise RuntimeError('Windows 文字识别未返回文字行')
    return lines


def prepare_image(payload, path):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_IMAGE_BYTES:
        raise ValueError('请选择不超过 8 MB 的 PNG 或 JPEG 图片')
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format not in {'PNG', 'JPEG'}:
                raise ValueError('只支持 PNG 或 JPEG 图片')
            if image.width * image.height > MAX_PIXELS:
                raise ValueError('图片像素过多，请缩小框选范围')
            with ImageOps.exif_transpose(image).convert('RGB') as normalized:
                normalized.save(path, format='PNG')
                return list(normalized.size)
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('无法解码这张 PNG 或 JPEG 图片') from exc


def recognize_screenshot(payload, output, stop, emit):
    if stop.is_set():
        return {'reason': 'cancelled', 'complete': False}
    with TemporaryDirectory(prefix='ctrip-ocr-') as work:
        image = Path(work) / 'selected-region.png'
        size = prepare_image(payload, image)
        if stop.is_set():
            return {'reason': 'cancelled', 'complete': False}
        emit('正在识别所选截图区域')
        lines = read_windows_ocr(image)
    if stop.is_set():
        return {'reason': 'cancelled', 'complete': False}
    folder = Path(output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'app-screenshot-ocr-{uuid.uuid4().hex}.json'
    temporary = path.with_suffix('.part')
    result = {'source_method': SOURCE, 'ocr_language': OCR_LANGUAGE, 'image_size': size,
              'image_sha256': hashlib.sha256(payload).hexdigest(), 'report_file': str(path),
              'lines': lines, 'text': '\n'.join(HAN_SPACING.sub('', line) for line in lines),
              'processed_at': datetime.now(timezone.utc).isoformat(),
              'reason': 'read' if lines else 'no_text', 'complete': False}
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    emit(f'识别出 {len(lines)} 行文字')
    return result

"""保存经过页面一致性和主图匹配核验的单张缓存图片。"""
import hashlib
import io
import json
import logging
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from contextlib import nullcontext
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from gallery_match import match_gallery_image
from gallery_cache import changed_candidates

logger = logging.getLogger(__name__)


def gallery_position(xml, confirmed_category):
    # /* ===== 步骤1：核对图集页码；数据源：携程XML；操作：1) 验证包名 2) 验证唯一页码和分类总数 ===== */
    logger.info('开始核对图集页码')
    # 1.1 标签仅验证分类存在，选中状态仍由调用方确认。
    nodes = list(ET.fromstring(xml).iter('node'))
    if not nodes or any(n.get('package') != 'ctrip.android.view' for n in nodes):
        raise ValueError('页面不属于携程图集')
    texts = [n.get('text', '').strip() for n in nodes]
    counters = [tuple(map(int, text.split('/'))) for text in texts if re.fullmatch(r'\d+/\d+', text)]
    totals = [int(match.group(1)) for text in texts
              if (match := re.fullmatch(re.escape(confirmed_category) + r'\s+(\d+)', text))]
    if len(counters) != 1 or len(totals) != 1 or not 1 <= counters[0][0] <= counters[0][1] == totals[0]:
        raise ValueError('图集页码或分类总数不明确')
    logger.info('图集页码核对完成')
    return counters[0]


def capture_current_gallery_frame(adb, baseline, region, output, object_key, confirmed_category, stop, *, candidate_session=None):
    # /* ===== 步骤1：读取当前图集画面；数据源：缓存变化和手机截图；操作：1) 校验前台 2) 夹取截图前后页面 ===== */
    logger.info('开始读取当前图集画面')
    if stop.is_set():
        raise InterruptedError('图集采集已取消')
    # 1.1 跨页复用仅允许同设备、同进入前基线和同对象分类；会话退出才清理候选。
    if candidate_session is not None:
        scope = (object_key, confirmed_category)
        if candidate_session.adb is not adb or candidate_session.baseline != baseline or candidate_session.scope not in (None, scope):
            raise ValueError('候选会话与当前设备、对象或分类不一致')
        candidate_session.scope = scope
        context = nullcontext(candidate_session.refresh(stop))
    else:
        context = changed_candidates(adb, baseline, stop)
    with context as paths:
        adb.ensure_foreground()
        before = adb.snapshot()
        if stop.is_set():
            raise InterruptedError('图集采集已取消')
        command = [adb.executable, '-s', adb.serial, 'exec-out', 'screencap', '-p']
        result = subprocess.run(command, capture_output=True, timeout=35,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('图集截图读取失败')
        adb.ensure_foreground()
        after = adb.snapshot()
        logger.info('当前图集画面读取完成')
        # /* ===== 步骤2：核验并保存当前图片；数据源：主图与候选；操作：1) 再检查取消 2) 保存唯一对应 ===== */
        logger.info('开始核验当前图集图片')
        if stop.is_set():
            raise InterruptedError('图集采集已取消')
        # 2.1 截图仅在内存中参与比较，绝不作为原图片保存。
        with Image.open(io.BytesIO(result.stdout)) as screen:
            if screen.width * screen.height > 20_000_000:
                raise ValueError('截图尺寸超出限制')
            saved = save_gallery_frame(screen, paths, before, after, region, output, object_key, confirmed_category)
        logger.info('当前图集图片核验完成：%s', saved['status'])
        return saved


def save_gallery_frame(screen, candidates, before_xml, after_xml, region, output, object_key, confirmed_category):
    # /* ===== 步骤1：核对页面；数据源：截图前后XML；操作：1) 验证包名和页码 2) 保留人工分类确认边界 ===== */
    logger.info('开始图集页面一致性检查')
    # 1.1 对象只作为调用方声明，分类必须由调用方确认，不能凭标签可见自动推断选中。
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', object_key) or not confirmed_category.strip():
        raise ValueError('需要有效对象标识和已确认的图集分类')
    states = [gallery_position(xml, confirmed_category) for xml in (before_xml, after_xml)]
    if states[0] != states[1]:
        raise ValueError('截图前后图集页码发生变化')
    logger.info('图集页面一致性检查完成')

    # /* ===== 步骤2：比较候选；数据源：有限图片缓存；操作：1) 检查格式与资源上限 2) 按字节去重后匹配 ===== */
    logger.info('开始筛选图集缓存候选')
    # 2.1 仅接收有界候选列表；不遍历设备缓存，也不导出无关图片。
    candidates = list(candidates)
    if len(candidates) > 32:
        raise ValueError('单页候选超过32个，请缩小缓存范围')
    images, payloads = {}, {}
    extensions = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'AVIF': '.avif'}
    total_bytes, total_pixels = 0, 0
    for path in candidates:
        try:
            with Path(path).open('rb') as handle:
                data = handle.read(20_000_001)
            total_bytes += len(data)
            if total_bytes > 50_000_000:
                raise ValueError('候选文件总量超过50MB')
            if len(data) > 20_000_000:
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in images:
                continue
            # 2.2 保留原文件字节，解码只用于比较；动画和过大图像不在本流程中展开。
            with Image.open(io.BytesIO(data)) as photo:
                if photo.format not in extensions or photo.width * photo.height > 20_000_000 or getattr(photo, 'n_frames', 1) != 1:
                    continue
                total_pixels += photo.width * photo.height
                if total_pixels > 50_000_000:
                    raise ValueError('候选图片解码总量超过5000万像素')
                images[digest] = photo.convert('RGB')
                payloads[digest] = (data, extensions[photo.format], list(photo.size))
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            continue
    match = match_gallery_image(screen, images, region)
    logger.info('图集缓存候选匹配完成：%s', match['status'])
    if match['status'] == 'ambiguous':
        # /* ===== 步骤3：保留待核对版本；数据源：歧义候选；操作：1) 仅保留达标候选 2) 不指定唯一来源 ===== */
        logger.info('开始保存图集待核对版本')
        # 3.1 每个版本单独通过颜色与细节门槛；仅与最佳候选接近的版本进入待核对目录。
        qualified = [row for row in match['scores'] if row['error'] <= 0.025 and row['detail_verified']
                     and row['error'] - match['scores'][0]['error'] < 0.01]
        record = {'status': 'ambiguous', 'match': match, 'complete': False, 'original_verified': False,
                  'object_key': object_key, 'identity_verification': 'operator_declared',
                  'category': confirmed_category, 'category_verification': 'operator_confirmed',
                  'gallery_index': states[0][0], 'gallery_total': states[0][1], 'review_candidates': [],
                  'source_method': 'app_external_image_cache', 'saved_at': datetime.now(timezone.utc).isoformat(),
                  'before_xml_sha256': hashlib.sha256(before_xml.encode()).hexdigest(),
                  'after_xml_sha256': hashlib.sha256(after_xml.encode()).hexdigest()}
        if len(qualified) >= 2:
            directory = Path(output) / 'review'
            directory.mkdir(parents=True, exist_ok=True)
            for row in qualified:
                digest = row['candidate']
                data, extension, dimensions = payloads[digest]
                asset = directory / (digest + extension)
                # 3.2 文件按哈希去重，全部写完才保存歧义清单，中断不产生已确认记录。
                if not asset.exists() or hashlib.sha256(asset.read_bytes()).hexdigest() != digest:
                    temporary = asset.with_suffix('.part')
                    temporary.write_bytes(data)
                    temporary.replace(asset)
                record['review_candidates'].append({'file': 'review/' + asset.name, 'sha256': digest,
                                                    'bytes': len(data), 'dimensions': dimensions})
            digests = sorted(row['sha256'] for row in record['review_candidates'])
            scope = hashlib.sha256(json.dumps([object_key, confirmed_category, states[0], digests]).encode()).hexdigest()
            manifest = directory / (scope + '.json')
            temporary = manifest.with_suffix('.part')
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(manifest)
        logger.info('图集待核对版本保存完成：%s个', len(record['review_candidates']))
        return record
    if match['status'] != 'matched':
        return {'status': match['status'], 'match': match}

    # /* ===== 步骤3：保存核验文件；数据源：唯一匹配的缓存字节；操作：1) 保存哈希命名文件 2) 保存关联证据 ===== */
    logger.info('开始保存图集匹配文件')
    # 3.1 字节哈希用于去重；对象、分类和页码均单独记录，不冒充全图集完成证明。
    digest = match['candidate']
    data, extension, dimensions = payloads[digest]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    filename = digest + extension
    record = {'status': 'saved', 'file': filename, 'sha256': digest, 'bytes': len(data),
              'dimensions': dimensions, 'object_key': object_key, 'identity_verification': 'operator_declared',
              'category': confirmed_category, 'category_verification': 'operator_confirmed',
              'gallery_index': states[0][0], 'gallery_total': states[0][1],
              'source_method': 'app_external_image_cache', 'original_verified': False, 'complete': False,
              'saved_at': datetime.now(timezone.utc).isoformat(), 'match': match,
              'before_xml_sha256': hashlib.sha256(before_xml.encode()).hexdigest(),
              'after_xml_sha256': hashlib.sha256(after_xml.encode()).hexdigest()}
    # 3.2 先保存图片，再替换关联清单；中断最多留下未关联文件，不留下指向半张图片的完成记录。
    asset = output / filename
    if not asset.exists() or hashlib.sha256(asset.read_bytes()).hexdigest() != digest:
        temporary = asset.with_suffix('.part')
        temporary.write_bytes(data)
        temporary.replace(asset)
    scope = hashlib.sha256(f'{object_key}|{confirmed_category}|{states[0]}|{digest}'.encode()).hexdigest()
    manifest = output / (scope + '.json')
    temporary = manifest.with_suffix('.part')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(manifest)
    logger.info('图集匹配文件保存完成：第%s张', states[0][0])
    return record

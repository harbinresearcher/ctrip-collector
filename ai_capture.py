import hashlib
import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from core import bounds

logger = logging.getLogger(__name__)
APP_PACKAGE = 'ctrip.android.view'
DISPLAY_COUNT = re.compile(r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)')
RATING_DIMENSIONS = ('卫生', '环境', '服务', '设施')


def rating_dimensions(title_node, parents):
    logger.info('开始配对酒店点评评分维度')
    header = parents.get(title_node)
    if header is None or header.get('package') != APP_PACKAGE:
        return []
    title_top = bounds(title_node)[1]
    labels, scores = {}, []
    for node in header:
        if node.get('package') != APP_PACKAGE or node.get('class') != 'android.widget.TextView':
            continue
        left, top, right, bottom = bounds(node)
        if not (left < right and top < bottom <= title_top):
            continue
        raw = node.get('text', '').strip()
        text = ''.join(raw.split())
        if text in RATING_DIMENSIONS:
            labels.setdefault(text, []).append((left, top, right, bottom))
        elif re.fullmatch(r'(?:[0-4]\.[0-9]|5\.0)', raw):
            scores.append((raw, left, top, right, bottom))
    ratings = []
    for dimension in RATING_DIMENSIONS:
        if len(labels.get(dimension, [])) != 1:
            continue
        left, top, right, bottom = labels[dimension][0]
        # Score and label must share a column and be directly above one another.
        matches = [value for value, x1, y1, x2, y2 in scores
                   if left < (x1 + x2) / 2 < right and x1 < (left + right) / 2 < x2
                   and 0 <= top - y2 <= bottom - top and y2 - y1 <= 2 * (bottom - top)]
        if len(matches) == 1:
            ratings.append({'label': dimension, 'score_text': matches[0]})
    logger.info('酒店点评评分维度配对完成：%s 项', len(ratings))
    return ratings


def save_ai_summary(path, result):
    # /* ===== 步骤1：保存总结；数据源：本次采集结果；操作：1) 写临时文件 2) 完整后替换 ===== */
    logger.info('开始保存 AI 总结')
    # 1.1 临时文件与目标同目录，失败时保留已保存的上一份。
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info('AI 总结保存完成')


def persist_app_ai_summary(xml, result, place, resource_id, output, *, review_run_id=None):
    captured_at = datetime.now(timezone.utc)
    result.update(kind='hotel', name=place, resource_id=int(resource_id),
                  identity_source='operator_declared_current_phone_object',
                  captured_at=captured_at.isoformat())
    if review_run_id is not None:
        result['review_run_id'] = review_run_id
    folder = Path(output) / 'ai'
    folder.mkdir(parents=True, exist_ok=True)
    stamp = captured_at.strftime('%Y%m%dT%H%M%S%fZ')
    stem = folder / f'hotel-{resource_id}-{stamp}'
    payload = xml.encode('utf-8')
    evidence = stem.with_suffix('.xml')
    evidence.write_bytes(payload)
    result.update(evidence=str(evidence), evidence_sha256=hashlib.sha256(payload).hexdigest())
    save_ai_summary(stem.with_suffix('.json'), result)
    return stem


def parse_ai(xml):
    # /* ===== 步骤1：提取 AI 总结；数据源：当前 XML；操作：1) 确认 AI 标识 2) 按总结边界取段落 ===== */
    logger.info('开始解析 AI 总结')
    # 1.1 多窗口 XML 可能包含其他应用，只读取携程节点。
    root = ET.fromstring(xml)
    nodes = [node for node in root.iter('node') if node.get('package') == APP_PACKAGE and node.get('text', '').strip()]
    texts = [node.get('text', '').strip() for node in nodes]
    parents = {child: parent for parent in root.iter() for child in parent}
    # 1.1 没有明确 AI 标识就不把普通点评当总结。
    if '点评总结' not in texts or '由AI生成' not in texts:
        raise ValueError('当前页面没有可识别的 AI 点评总结，请回到酒店全部评论顶部。')
    start = texts.index('由AI生成') + 1
    sections, lead = [], []
    boundary = False
    for node, text in zip(nodes[start:], texts[start:]):
        if text == '收起':
            boundary = True
            break
        if 'AI优选住客晒图' in text or '查看全部总结' in text or text == '全部':
            break
        if '有帮助' in text or '没帮助' in text:
            continue
        # The observed Android layout exposes one paragraph and its count in the same container.
        if DISPLAY_COUNT.fullmatch(text):
            continue
        clean = text.replace('\ufffc', '')
        if match := re.fullmatch(r'([^:：\n]{1,40})[:：]\s*(.+)', clean, re.S):
            group = parents.get(node)
            linked_count = None
            if group is not None and group.get('package') == APP_PACKAGE:
                siblings = [child for child in group if child.get('package') == APP_PACKAGE
                            and child.get('text', '').strip()]
                if (len(siblings) == 2 and siblings[0] is node
                        and DISPLAY_COUNT.fullmatch(count := siblings[1].get('text', '').strip())):
                    linked_count = count
            sections.append({'text': clean, 'topic': match[1].strip(), 'body': match[2].strip(),
                             'linked_count': linked_count})
        else:
            lead.append(text)
    image_count = None
    if 'AI优选住客晒图' in texts:
        following = texts[texts.index('AI优选住客晒图') + 1:]
        if following and re.fullmatch(r'共\d+张', following[0]):
            image_count = int(re.search(r'\d+', following[0]).group())
    result = {'source': 'ctrip_android_ui', 'generated_by_ai': True, 'lead': lead,
              'header_texts': texts[:texts.index('点评总结')],
              'ai_selected_images': {'display_count': image_count, 'count_scope': 'entry_total_unverified',
                                     'selected_count': None, 'items': [], 'status': 'not_captured'},
              'sections': sections, 'expanded_boundary_visible': boundary,
              'rating_dimensions': rating_dimensions(nodes[texts.index('点评总结')], parents),
              'linked_review_scope': 'display_counts_only',
              'complete': False,
              'status': 'expanded_visible' if boundary and sections else 'partial'}
    logger.info('AI 总结解析完成：%s 段', len(sections))
    return result


def parse_gallery(xml):
    # /* ===== 步骤1：解析图集分类；数据源：当前XML；操作：1) 验证图集标识 2) 区分计数与图片 ===== */
    logger.info('开始解析图集分类')
    # 1.1 仅从携程节点提取分类，避免覆盖层伪造图集标签。
    texts = [n.get('text', '').strip() for n in ET.fromstring(xml).iter('node')
             if n.get('package') == 'ctrip.android.view']
    # 1.1 只接收已验证的住客晒图页，数量不是图片文件。
    if '住客晒图' not in texts or '官方相册' not in texts:
        raise ValueError('尚未识别到住客图集')
    categories = {}
    for text in texts:
        match = re.fullmatch(r'(.+?)\s+(\d+)', text)
        if match:
            categories[match[1]] = int(match[2])
    if 'AI优选' not in categories:
        raise ValueError('图集尚未显示AI优选分类')
    result = {'selected_count': categories['AI优选'], 'visible_category_counts': categories,
              'category_scope': 'visible_tabs_only', 'metadata_status': 'captured',
              'items': [], 'status': 'not_captured'}
    logger.info('图集分类解析完成：AI优选%s张', result['selected_count'])
    return result


def capture_gallery_metadata(adb, xml, stem, stop):
    # /* ===== 步骤2：采集图集元数据；数据源：总结入口；操作：1) 有界读取 2) 保存证据并返回 ===== */
    logger.info('开始采集图集元数据')
    result = {'metadata_status': 'entry_not_visible'}
    entry = next((n for n in ET.fromstring(xml).iter('node')
                  if n.get('package') == 'ctrip.android.view'
                  and n.get('text', '').strip() == 'AI优选住客晒图'), None)
    opened = False
    try:
        # 2.1 不猜入口坐标，取消时不进入图集。
        if stop.is_set():
            result['metadata_status'] = 'cancelled'
        elif entry is not None:
            left, top, right, bottom = bounds(entry)
            adb.ensure_foreground()
            if stop.is_set():
                result['metadata_status'] = 'cancelled'
                return result
            adb.run('shell', 'input', 'tap', str((left + right) // 2), str((top + bottom) // 2))
            result['metadata_status'] = 'timeout'
            for _ in range(6):
                if stop.wait(0.5):
                    result['metadata_status'] = 'cancelled'
                    break
                adb.ensure_foreground()
                gallery_xml = adb.snapshot()
                try:
                    parsed = parse_gallery(gallery_xml)
                except ValueError:
                    continue
                # 2.2 只有确认图集后才返回，避免对未跳转页面误按返回。
                opened = True
                evidence = stem.with_suffix('.gallery.xml')
                evidence.write_text(gallery_xml, encoding='utf-8')
                result.update(parsed, evidence=str(evidence))
                break
    except Exception as exc:
        result.update(metadata_status='error', error=str(exc))
        logger.exception('图集元数据采集失败，保留总结')
    finally:
        if opened:
            try:
                adb.ensure_foreground()
                adb.run('shell', 'input', 'keyevent', 'KEYCODE_BACK')
                result['return_action'] = 'back_sent'
            except Exception as exc:
                result['return_error'] = str(exc)
        logger.info('图集元数据采集结束')
    return result


def capture_ai(adb, kind, place, resource_id, output, stop=None, include_gallery=True):
    # /* ===== 步骤2：保存 AI 证据；数据源：前台手机；操作：1) 展开总结 2) 单独保存文本与 XML ===== */
    logger.info('开始保存 AI 证据')
    if kind != 'hotel' or not str(resource_id).isdigit() or not place.strip():
        raise ValueError('AI 自动展开当前仅验证酒店；请填写名称与数字对象 ID。')
    stop = stop or Event()
    if stop.is_set():
        logger.info('AI 采集已取消')
        return {'status': 'cancelled', 'sections': []}
    adb.ensure_foreground()
    xml = adb.snapshot()
    # 2.1 只点击已识别的总结展开按钮，不根据固定坐标猜测。
    parse_ai(xml)
    for node in ET.fromstring(xml).iter('node'):
        if stop.is_set():
            break
        if node.get('package') == 'ctrip.android.view' and node.get('text', '').strip() == '查看全部总结':
            left, top, right, bottom = bounds(node)
            # 2.1.1 读取页面后可能切换应用，点击前再次确认前台与取消状态。
            adb.ensure_foreground()
            if stop.is_set():
                break
            adb.run('shell', 'input', 'tap', str((left + right) // 2), str((top + bottom) // 2))
            # /* ===== 步骤3：等待展开；数据源：点击后页面；操作：1) 最多重读6次 2) 支持取消 ===== */
            logger.info('开始等待 AI 总结展开')
            for _ in range(6):
                # 3.1 快速通道可能读到点击前页面，等待后核对展开边界。
                if stop.wait(0.5):
                    break
                adb.ensure_foreground()
                xml = adb.snapshot()
                if parse_ai(xml)['expanded_boundary_visible']:
                    break
            logger.info('AI 总结展开等待结束')
            break
    result = parse_ai(xml)
    if stop.is_set():
        result['status'] = 'cancelled'
    stem = persist_app_ai_summary(xml, result, place, resource_id, output)
    if include_gallery:
        result['ai_selected_images'].update(capture_gallery_metadata(adb, xml, stem, stop))
        if stop.is_set():
            result['status'] = 'cancelled'
    save_ai_summary(stem.with_suffix('.json'), result)
    logger.info('AI 证据保存完成')
    return result

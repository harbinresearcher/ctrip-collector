import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from core import bounds

logger = logging.getLogger(__name__)


def parse_ai(xml):
    # /* ===== 步骤1：提取 AI 总结；数据源：当前 XML；操作：1) 确认 AI 标识 2) 按总结边界取段落 ===== */
    logger.info('开始解析 AI 总结')
    texts = [n.get('text', '').strip() for n in ET.fromstring(xml).iter('node') if n.get('text', '').strip()]
    # 1.1 没有明确 AI 标识就不把普通点评当总结。
    if '点评总结' not in texts or '由AI生成' not in texts:
        raise ValueError('当前页面没有可识别的 AI 点评总结，请回到酒店全部评论顶部。')
    start = texts.index('由AI生成') + 1
    sections, lead = [], []
    boundary = False
    for text in texts[start:]:
        if text == '收起':
            boundary = True
            break
        if 'AI优选住客晒图' in text or '查看全部总结' in text or text == '全部':
            break
        if '有帮助' in text or '没帮助' in text:
            continue
        # 1.2 关联评论数保留为平台展示值，不宣称已取得这些评论。
        if re.fullmatch(r'\d+', text) and sections:
            sections[-1]['linked_count'] = text
        elif ':' in text or '：' in text:
            sections.append({'text': text.replace('\ufffc', ''), 'linked_count': None})
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
              'status': 'expanded_visible' if boundary and sections else 'partial'}
    logger.info('AI 总结解析完成：%s 段', len(sections))
    return result


def parse_gallery(xml):
    # /* ===== 步骤1：解析图集分类；数据源：当前XML；操作：1) 验证图集标识 2) 区分计数与图片 ===== */
    logger.info('开始解析图集分类')
    texts = [n.get('text', '').strip() for n in ET.fromstring(xml).iter('node')]
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
                  if n.get('text', '').strip() == 'AI优选住客晒图'), None)
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
        if node.get('text', '').strip() == '查看全部总结':
            left, top, right, bottom = bounds(node)
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
    result.update({'kind': kind, 'name': place, 'resource_id': int(resource_id),
                   'identity_source': 'operator_declared_current_phone_object',
                   'captured_at': datetime.now(timezone.utc).isoformat()})
    # 2.2 不自动写入网络对象详情，避免人工名称或 ID 错填污染其他数据。
    folder = Path(output) / 'ai'
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    stem = folder / f'{kind}-{resource_id}-{stamp}'
    stem.with_suffix('.xml').write_text(xml, encoding='utf-8')
    result['evidence'] = str(stem.with_suffix('.xml'))
    # 2.3 先保存总结，补采图集失败时仍保留已有内容。
    stem.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if include_gallery:
        result['ai_selected_images'].update(capture_gallery_metadata(adb, xml, stem, stop))
        if stop.is_set():
            result['status'] = 'cancelled'
    stem.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info('AI 证据保存完成')
    return result

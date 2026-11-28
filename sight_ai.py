"""Read the visible AI review card in Ctrip's Android sight reviews."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from core import bounds
from sight_guide import app_nodes, text
from sight_snapshot import APP, unique_value
from sight_gallery_thumbnails import capture_thumbnails, locate_tiles, write_thumbnails
from sight_review_navigation import MANUAL_PROMPTS, app_nodes as screen_app_nodes
from sight_review_navigation import checked_reviews_title, sight_screen_title


SOURCE = 'android_sight_ai_snapshot'
TABS = ('图片/视频', '景点详情', '必看指南', '用户点评')
SCORE = r'(?:[0-4](?:\.[0-9])?|5(?:\.0)?)'
DIMENSIONS = ('景色', '趣味', '性价比')
QUEUE_MAX_SEARCH_SWIPES = 4
REVIEW_RESTORE_ERROR = '无法确认已回到同一景点的点评页，队列已暂停。'


def read_ratings(group):
    labels = [node for node in group if node.get('package') == APP and text(node)]
    scores = [node for node in labels if re.fullmatch(SCORE, text(node))]
    overall = {}
    if score := unique_value(text(node) for node in labels if re.fullmatch(SCORE + '/5', text(node))):
        overall['score_text'] = score
    if count := unique_value(text(node) for node in labels if re.fullmatch(r'[0-9,]+(?:\.[0-9]+)?[万千wW]?\+?条点评', text(node))):
        overall['count_text'] = count
    dimensions = []
    for name in DIMENSIONS:
        matches = [node for node in labels if text(node) == name]
        if len(matches) != 1:
            continue
        left, top, right, bottom = bounds(matches[0])
        if right <= left or bottom <= top:
            continue
        center = (left + right) / 2
        in_column = []
        for score in scores:
            x1, y1, x2, y2 = bounds(score)
            if x1 <= center <= x2 and y1 < y2 <= top and top - y2 <= bottom - top:
                in_column.append(text(score))
        if len(in_column) == 1:
            dimensions.append({'label': name, 'score_text': in_column[0]})
    return overall, dimensions


def read_gallery(card):
    siblings = list(card)
    galleries = [node for node in siblings
                 if node.get('package') == APP and node.get('resource-id') == 'aipick_usershot_exposure']
    if len(galleries) != 1:
        return {}, None
    anchor = galleries[0]
    left, top, right, bottom = bounds(anchor)
    if right <= left or bottom <= top:
        return {}, None

    # In the observed layout the empty tracking marker precedes the visible gallery branches.
    branches = [anchor]
    for node in siblings[siblings.index(anchor) + 1:]:
        x1, y1, x2, y2 = bounds(node)
        if left <= x1 < x2 <= right and top <= y1 < y2 <= bottom:
            branches.append(node)
    nodes = [node for branch in branches for node in app_nodes(branch)]
    result = {}
    if count := unique_value(match[1] for node in nodes if (match := re.fullmatch(r'共([0-9]{1,9})张', text(node)))):
        result['display_count'] = int(count)
    scrollers = [node for node in nodes if node.get('class') == 'android.widget.HorizontalScrollView']
    scroller = scrollers[0] if len(scrollers) == 1 else None
    if scroller is not None:
        result['visible_labels'] = list(dict.fromkeys(word for node in app_nodes(scroller)
                                                    if (word := text(node)) and any(char.isalnum() for char in word)))
    return result, scroller


def sight_ai_panel(root, expected_name, *, collapse=False):
    nodes = [node for window in root for node in app_nodes(window)]
    parents = {child: parent for parent in root.iter() for child in parent}
    cards = [node for node in nodes if node.get('resource-id') == 'reviewsummary_exposure']
    if len(cards) != 1:
        raise ValueError('未找到景点 AI 总结，请打开「用户点评」并滚到总结卡')
    card = cards[0]
    pages = [node for node in nodes if tuple(child.get('content-desc') for child in node
                                            if child.get('package') == APP and child.get('content-desc') in TABS) == TABS]
    if len(pages) != 1:
        raise ValueError('未识别到景点「用户点评」页标题')
    children = list(pages[0])
    first_tab = next(child for child in children if child.get('package') == APP and child.get('content-desc') == TABS[0])
    last_tab = next(child for child in children if child.get('package') == APP and child.get('content-desc') == TABS[-1])
    toolbar = [word for branch in children[:children.index(first_tab)] for node in app_nodes(branch)
               if (word := text(node)) and any(char.isalnum() for char in word)]
    if toolbar != [expected_name] or not expected_name:
        raise ValueError('景点点评页标题与所填名称不匹配')
    content = next((branch for branch in children[children.index(last_tab) + 1:]
                    if card in app_nodes(branch)), None)
    if content is None:
        raise ValueError('无法确认 AI 总结属于当前景点的点评内容')

    panels = [list(app_nodes(child)) for child in card
              if child.get('resource-id') != 'aipick_usershot_exposure']
    marker = '收起' if collapse else '由AI生成'
    marked = [panel for panel in panels if sum(text(node).replace(' ', '') == marker for node in panel) == 1]
    if len(marked) != 1:
        raise ValueError('总结卡缺少可确认的「由AI生成」标记')
    panel = marked[0]
    window = card
    while parents[window] is not root:
        window = parents[window]
    return card, panel, content, parents, bounds(window)


def ai_control_coordinates(panel, viewport, label):
    controls = [node for node in panel if text(node) == label and node.get('enabled') == 'true'
                and node.get('visible-to-user') != 'false']
    if len(controls) == 1:
        left, top, right, bottom = bounds(controls[0])
        if viewport[0] <= left < right <= viewport[2] and viewport[1] <= top < bottom <= viewport[3]:
            return str((left + right) // 2), str((top + bottom) // 2)
    return None


def read_sight_ai(xml, expected_name):
    card, panel, content, parents, viewport = sight_ai_panel(ET.fromstring(xml), expected_name)
    words = [text(node) for node in panel if text(node)]
    sections = [{'topic': match[1], 'body': match[2].strip()} for word in dict.fromkeys(words)
                if (match := re.fullmatch(r'([\u3400-\u9fff]{2,16})\s*[:：]\s*(\S.*)', word, re.DOTALL))]
    if not sections:
        raise ValueError('未读到景点 AI 总结正文，请将总结卡滚到可见位置')

    group = parents.get(parents[card])
    scores, dimensions = read_ratings(group) if group is not None and any(
        node is group for node in app_nodes(content)) else ({}, [])
    gallery, scroller = read_gallery(card)
    target = ai_control_coordinates(panel, viewport, '查看全部总结') if '收起' not in words else None
    state = 'expanded' if '收起' in words and '查看全部总结' not in words else (
        'preview' if '查看全部总结' in words else 'visible')
    result = {'kind': 'sight', 'name': expected_name, 'source': 'ctrip_android_ui',
            'source_method': SOURCE, 'generated_by_ai': True, 'identity_verification': 'operator_name_exact_match',
            'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(), 'sections': sections,
            'expand_entry_visible': '查看全部总结' in words, 'summary_state': state, 'review_display': scores,
            'rating_dimensions': dimensions, 'ai_selected_images': gallery,
            'status': 'partial', 'complete': False,
            'limitations': ['总结仅含本次读取的可见段落，关联点评需另行读取。',
                            '景点身份按页面标题核对，未核对平台对象编号。']}
    return result, scroller, viewport, target


def parse_sight_ai(xml, expected_name):
    result, _, _, _ = read_sight_ai(xml, expected_name)
    return result


def apply_expanded_summary(result, expanded):
    previous_topics = {section['topic'] for section in result['sections']}
    expanded_topics = {section['topic'] for section in expanded['sections']}
    if expanded['summary_state'] == 'expanded' and previous_topics <= expanded_topics:
        result.update(sections=expanded['sections'], summary_state='expanded',
                      expanded_raw_hash=expanded['raw_hash'])


def capture_expanded_summary(device, name, stop, result, target, *, refresh_target):
    device.ensure_foreground()
    if stop.is_set():
        return
    try:
        if refresh_target:
            _, _, _, target = read_sight_ai(device.snapshot(), name)
            device.ensure_foreground()
        if target is None or stop.is_set():
            return
        device.run('shell', 'input', 'tap', *target)
        if stop.wait(0.8):
            return
        device.ensure_foreground()
        expanded = parse_sight_ai(device.snapshot(), name)
        device.ensure_foreground()
    except (ValueError, ET.ParseError):
        return
    apply_expanded_summary(result, expanded)


def save_summary_report(temporary, path, result, stop):
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    if stop.is_set():
        return False
    temporary.replace(path)
    return True


def capture_sight_ai(device, output, name, stop, *, save_gallery_thumbnails=False, expand_summary=False):
    device.ensure_foreground()
    captured_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
    xml = device.snapshot()
    device.ensure_foreground()
    if stop.is_set():
        return {'reason': 'cancelled', 'complete': False}
    result, scroller, viewport, target = read_sight_ai(xml, name)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    report_id = uuid.uuid4().hex
    path = output / f'sight-ai-{report_id}.json'
    result.update(captured_at=captured_at, file=str(path.resolve()))
    temporary = path.with_suffix('.part')
    archive = output / f'sight-ai-thumbnails-{report_id}.zip'
    staged_archive = archive.with_suffix('.part')
    archive_written = report_written = False
    try:
        if save_gallery_thumbnails:
            tiles = locate_tiles(scroller)
            status, pictures = capture_thumbnails(device, xml, tiles, viewport, stop)
            if pictures and not stop.is_set():
                try:
                    write_thumbnails(staged_archive, result, path.name, pictures)
                    staged_archive.replace(archive)
                    archive_written = True
                except OSError:
                    status = 'archive_unavailable'
            result['limitations'].append('晒图只保存完整可见的屏幕裁图。')
            result['gallery_thumbnails'] = {'status': status, 'visible_tile_candidates': len(tiles),
                                           'saved_count': len(pictures) if archive_written else 0,
                                           'archive': archive.name if archive_written else None,
                                           'labels': [label for _, label, _ in pictures] if archive_written else [],
                                           'report_id': report_id, 'original_files': False, 'complete': False}
        if expand_summary and target is not None and not stop.is_set():
            capture_expanded_summary(device, name, stop, result, target, refresh_target=save_gallery_thumbnails)
        report_written = save_summary_report(temporary, path, result, stop)
        if not report_written:
            return {'reason': 'cancelled', 'complete': False}
    finally:
        temporary.unlink(missing_ok=True)
        staged_archive.unlink(missing_ok=True)
        if archive_written and not report_written:
            archive.unlink(missing_ok=True)
    return result


def check_queue_sight_stop(stop):
    if stop.is_set():
        raise InterruptedError('景点 AI 总结任务已停止。')


def queue_sight_frame(device, name, stop):
    check_queue_sight_stop(stop)
    device.ensure_foreground()
    xml = device.snapshot()
    device.ensure_foreground()
    check_queue_sight_stop(stop)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise RuntimeError(REVIEW_RESTORE_ERROR) from exc
    if any(text(node) in MANUAL_PROMPTS for node in screen_app_nodes(root)):
        raise RuntimeError('景点点评页面需要登录或验证，队列已暂停。')
    if sight_screen_title(root) != name:
        raise RuntimeError(REVIEW_RESTORE_ERROR)
    return xml, root


def queue_ai_gesture(device, stop, *command):
    check_queue_sight_stop(stop)
    device.ensure_foreground()
    check_queue_sight_stop(stop)
    device.run('shell', 'input', *command)
    if stop.wait(0.8):
        check_queue_sight_stop(stop)


def find_queue_sight_ai(device, name, stop):
    previous_xml = None
    for attempt in range(QUEUE_MAX_SEARCH_SWIPES + 1):
        xml, root = queue_sight_frame(device, name, stop)
        try:
            result, _, _, target = read_sight_ai(xml, name)
        except ValueError:
            if checked_reviews_title(root, name) is None:
                raise RuntimeError(REVIEW_RESTORE_ERROR)
            if attempt == QUEUE_MAX_SEARCH_SWIPES or xml == previous_xml:
                return None, None, root
            x1, y1, x2, y2 = bounds(root.find('node'))
            width, height = x2 - x1, y2 - y1
            if width <= 0 or height <= 0:
                raise RuntimeError(REVIEW_RESTORE_ERROR)
            x = str(x1 + width // 2)
            queue_ai_gesture(device, stop, 'swipe', x, str(y1 + height * 5 // 10),
                             x, str(y1 + height * 8 // 10), '450')
            previous_xml = xml
        else:
            return result, target, root


def restore_queue_sight_reviews(device, name, stop, root):
    try:
        _, panel, _, _, viewport = sight_ai_panel(root, name, collapse=True)
    except ValueError:
        collapse_target = None
    else:
        collapse_target = ai_control_coordinates(panel, viewport, '收起')
        if collapse_target is None:
            raise RuntimeError(REVIEW_RESTORE_ERROR)
    if collapse_target is not None:
        queue_ai_gesture(device, stop, 'tap', *collapse_target)
        _, root = queue_sight_frame(device, name, stop)
    if checked_reviews_title(root, name) is None:
        raise RuntimeError(REVIEW_RESTORE_ERROR)


def observe_queue_sight_ai(device, name, stop):
    xml, root = queue_sight_frame(device, name, stop)
    try:
        report, _, _, target = read_sight_ai(xml, name)
    except ValueError:
        return root, None, None
    return root, report, target


def expand_queue_sight_ai(device, name, stop, result, target):
    queue_ai_gesture(device, stop, 'tap', *target)
    root, observed, visible_target = observe_queue_sight_ai(device, name, stop)
    if visible_target == target:
        stop.wait(0.55)
        root, observed, visible_target = observe_queue_sight_ai(device, name, stop)
        if visible_target == target:
            queue_ai_gesture(device, stop, 'tap', *target)
            root, observed, _ = observe_queue_sight_ai(device, name, stop)
    if observed is not None:
        apply_expanded_summary(result, observed)
    return root


def capture_queue_sight_ai(device, output, name, object_id, queue_id, stop, *, report_id):
    """Save a bounded, visible summary while retaining the queue's review page."""
    if (not isinstance(queue_id, str) or not re.fullmatch('[0-9a-f]{32}', queue_id)
            or not isinstance(report_id, str) or not re.fullmatch('[0-9a-f]{32}', report_id)
            or not isinstance(object_id, str) or not re.fullmatch('[1-9][0-9]{0,19}', object_id)):
        raise ValueError('景点 AI 总结的队列身份无效。')
    result, target, root = find_queue_sight_ai(device, name, stop)
    if result is None:
        return {'status': 'not_visible'}

    captured_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
    scene_error = None
    if target is not None:
        try:
            root = expand_queue_sight_ai(device, name, stop, result, target)
        except InterruptedError:
            raise
        except RuntimeError as exc:
            scene_error = exc

    path = Path(output) / f'sight-ai-{report_id}.json'
    result.update(captured_at=captured_at, file=str(path.resolve()), queue_id=queue_id,
                  object_id=object_id, identity_verification='queue_address_and_screen_title',
                  identity_id_verified=False)
    temporary = path.with_suffix(f'.{uuid.uuid4().hex}.part')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        published = save_summary_report(temporary, path, result, stop)
    except OSError:
        status = {'status': 'error', 'error': '无法保存景点 AI 总结。'}
    else:
        if not published:
            check_queue_sight_stop(stop)
        status = {'status': 'saved', 'report_file': path.name, 'section_count': len(result['sections']),
                  'summary_state': result['summary_state']}
    finally:
        temporary.unlink(missing_ok=True)
    if scene_error is not None:
        raise scene_error
    restore_queue_sight_reviews(device, name, stop, root)
    return status

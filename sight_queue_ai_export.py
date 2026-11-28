"""Build a download from sight queue AI reports with verified local provenance."""

import json
from pathlib import Path

from mobile_sight_queue import AI_REPORT_FILE, load_sight_queue
from object_identity import MAX_EVIDENCE_BYTES
from sight_ai import SOURCE
from workbench_result_files import open_output_file


def public_text(value):
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def text_fields(value, *keys):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in keys if public_text(value.get(key))}


def read_sight_queue_ai_report(output, queue_id, item, report_file):
    """Read one report, including a reserved report before its checkpoint was written."""
    match = AI_REPORT_FILE.fullmatch(report_file) if isinstance(report_file, str) else None
    report_id = item.get('ai_report_id')
    if match is None or (report_id is not None and match[1] != report_id):
        raise ValueError('景点 AI 总结只接受队列绑定的报告文件')

    root = Path(output).resolve()
    with open_output_file(root, root / report_file) as stream:
        data = stream.read(MAX_EVIDENCE_BYTES + 1)
    if len(data) > MAX_EVIDENCE_BYTES:
        raise ValueError('景点 AI 总结报告过大')
    try:
        report = json.loads(data.decode('utf-8'))
    except RecursionError as exc:
        raise ValueError('景点 AI 总结报告结构无效') from exc

    identity = {'queue_id': queue_id, 'object_id': item['object_id'], 'name': item['name'],
                'kind': 'sight', 'source_method': SOURCE}
    if (not isinstance(report, dict) or report.get('generated_by_ai') is not True
            or any(report.get(key) != value for key, value in identity.items())):
        raise ValueError('景点 AI 总结报告来源不匹配')
    state = report.get('summary_state')
    sections = report.get('sections')
    if state not in ('expanded', 'preview', 'visible') or not isinstance(sections, list) or not sections:
        raise ValueError('景点 AI 总结报告正文无效')
    public_sections = [text_fields(section, 'topic', 'body') for section in sections]
    count = report.get('section_count', len(sections))
    if type(count) is not int or count != len(sections) or any(len(section) != 2 for section in public_sections):
        raise ValueError('景点 AI 总结报告段落无效')

    result = dict(identity, generated_by_ai=True, summary_state=state, section_count=count, sections=public_sections)
    if review := text_fields(report.get('review_display'), 'score_text', 'count_text'):
        result['review_display'] = review
    dimensions = report.get('rating_dimensions')
    if isinstance(dimensions, list):
        result['rating_dimensions'] = [dimension for raw in dimensions
                                       if len(dimension := text_fields(raw, 'label', 'score_text')) == 2]
    gallery = report.get('ai_selected_images')
    if isinstance(gallery, dict):
        images = {}
        display_count, labels = gallery.get('display_count'), gallery.get('visible_labels')
        if type(display_count) is int and display_count >= 0:
            images['display_count'] = display_count
        if isinstance(labels, list):
            images['visible_labels'] = [label for label in labels if public_text(label)]
        if images:
            result['ai_selected_images'] = images
    return result


def build_sight_queue_ai_export(output, queue_id):
    queue = load_sight_queue(output, queue_id)
    if not queue.get('save_ai_summary'):
        raise ValueError('此队列未启用景点 AI 总结采集')
    items = []
    exported = 0
    for item in queue['items']:
        summary = item.get('ai_summary') or {}
        status = summary.get('status', 'not_attempted')
        entry = {key: item[key] for key in ('object_ref', 'object_id', 'name')}
        entry['status'] = status
        items.append(entry)
        if status != 'saved':
            continue
        try:
            report = read_sight_queue_ai_report(output, queue_id, item, summary['report_file'])
            if any(report[key] != summary[key] for key in ('summary_state', 'section_count')):
                raise ValueError('景点 AI 总结报告与队列记录不符')
        except (ValueError, OSError):
            entry.update(status='unavailable', reason='report_unavailable')
            continue
        entry.update(report, report_file=summary['report_file'])
        exported += 1
    if not exported:
        raise ValueError('此队列暂无可导出的景点 AI 总结')
    return {'queue_id': queue_id, 'scope': 'saved_sight_ai_summaries', 'complete': False,
            'requested_count': len(items), 'exported_count': exported,
            'omitted_count': len(items) - exported, 'items': items}

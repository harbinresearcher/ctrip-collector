"""Run a saved sight review queue on one Android emulator."""
import sqlite3
from pathlib import Path
from subprocess import SubprocessError

from mobile_review_recovery import saved_sight_attempt
from mobile_sight_queue import create_sight_queue, load_sight_queue, lock_sight_queue, save_sight_queue
from object_identity import read_evidence, resolve_object_ref
from runner import Adb, collect, new_review_run_id
from sight_review_navigation import open_sight_reviews, return_from_sight_reviews
from workbench_app_jobs import sight_navigation_address


RECORDED_REASONS = {'target_reached', 'screen_budget_reached', 'stalled_unverified'}
REVIEW_FIELDS = ('run_id', 'count', 'new_count', 'screens', 'sample_csv_count', 'reason',
                 'identity_verification', 'identity_id_verified', 'review_count_check')


def queue_result(queue):
    return dict(queue, items=[{key: item[key] for key in ('object_ref', 'name', 'status', 'result')}
                              for item in queue['items']])


def result_with_error(item, reason, message):
    return dict(item['result'] or {}, reason=reason, error=message)


def review_result(report):
    result = {key: report[key] for key in REVIEW_FIELDS if key in report}
    error = report.get('error') or report.get('report_error') or report.get('export_error')
    if error:
        result['error'] = error
    return result


def recover_attempt(output, item, limit):
    run_id = item.get('attempt_run_id')
    evidence = saved_sight_attempt(output, run_id, item['object_id'], item['name'])
    if not evidence or not evidence[1]:
        return None, None
    reason, sample_count = evidence
    sample = {'sample_run_id': run_id, 'sample_csv_count': sample_count}
    if reason not in RECORDED_REASONS:
        return None, sample
    try:
        report = read_evidence(output / f'report-{run_id}.json')
    except (OSError, ValueError):
        return None, sample
    expected = {'run_id': run_id, 'kind': 'sight', 'place': item['name'], 'object_id': item['object_id'],
                'reason': reason, 'target': limit, 'sample_csv_count': sample_count}
    if any(report.get(field) != value for field, value in expected.items()):
        return None, sample
    result = review_result(report)
    if result.get('error'):
        return None, sample
    result['identity_id_verified'] = False
    return result, sample


def previous_sample(output, item):
    result = item['result'] or {}
    run_id = result.get('sample_run_id') or result.get('run_id')
    evidence = saved_sight_attempt(output, run_id, item['object_id'], item['name'])
    if evidence and evidence[1]:
        return {'sample_run_id': run_id, 'sample_csv_count': evidence[1]}
    return None


def run_sight_queue(output, queue, adb_path, adb_serial, stop, emit):
    queue['reason'] = 'running'
    save_sight_queue(output, queue)
    emit(f"已保存景点点评队列 {queue['queue_id']}")
    previous = None
    with Adb(adb_path, serial=adb_serial, ui_backend='uiautomator2') as device:
        connected = False
        for item in queue['items']:
            if stop.is_set():
                break
            if item['status'] == 'recorded':
                previous = item['name']
                continue
            recovered, sample = recover_attempt(output, item, queue['limit'])
            if recovered is not None:
                item.update(status='recorded', result=recovered)
                save_sight_queue(output, queue)
                previous = item['name']
                emit(f'已恢复 {previous} 上次保存的点评报告')
                continue
            fallback = sample or previous_sample(output, item)
            if fallback:
                item['result'] = dict(item['result'] or {}, **fallback)
            attempt_run_id = new_review_run_id()
            item['attempt_run_id'] = attempt_run_id
            item['status'] = 'running'
            save_sight_queue(output, queue)
            name = item['name']
            emit(f'开始核对景点 {name}')
            try:
                if not connected:
                    device.connect()
                    connected = True
                origin = return_from_sight_reviews(device, previous, stop, next_name=name) if previous else None
                navigation = open_sight_reviews(device, name, item['address'], stop,
                                                allow_search=origin == 'search', require_address_search=True)
                report = collect(device, 'sight', name, output, queue['limit'], queue['max_swipes'], stop, emit,
                                 object_id=item['object_id'], identity_source='evidence_manifest',
                                 identity_evidence=item['identity_evidence'], sight_navigation=navigation,
                                 run_id=attempt_run_id)
            except InterruptedError:
                result = result_with_error(item, 'stopped_by_user', '')
            except (RuntimeError, OSError, ValueError, SubprocessError, sqlite3.Error) as exc:
                result = result_with_error(item, 'navigation_or_collection_error', str(exc))
            else:
                result = review_result(report)
                if not result.get('sample_csv_count') and fallback:
                    result.update(fallback)

            item['result'] = result
            recorded = result['reason'] in RECORDED_REASONS and not result.get('error')
            item['status'] = 'recorded' if recorded else 'retry'
            save_sight_queue(output, queue)
            if recorded:
                previous = name
                emit(f"已保存 {name} 的本轮报告，本地累计 {result.get('count', 0)} 条")
            if not recorded:
                break

    if stop.is_set() or any(item['status'] == 'retry' and item['result']['reason'] == 'stopped_by_user'
                            for item in queue['items']):
        queue['reason'] = 'stopped_by_user'
    elif all(item['status'] == 'recorded' for item in queue['items']):
        queue['reason'] = 'queue_recorded'
    else:
        queue['reason'] = 'needs_attention'
    save_sight_queue(output, queue)
    return queue_result(queue)


def prepare_sight_queue_job(request, output, adb_path, *, adb_serial=None):
    if not adb_path:
        raise ValueError('景点点评队列需要配置 Android 模拟器。')
    output = Path(output).resolve()
    if 'queue_id' in request:
        if any(field in request for field in ('object_refs', 'limit', 'max_swipes')):
            raise ValueError('继续队列时沿用已保存的景点和数量设置。')
        queue_id = request['queue_id']
        try:
            load_sight_queue(output, queue_id)
        except FileNotFoundError as exc:
            raise ValueError(str(exc)) from exc

        def resume(stop, emit):
            with lock_sight_queue(output, queue_id):
                return run_sight_queue(output, load_sight_queue(output, queue_id), adb_path, adb_serial, stop, emit)

        return resume

    refs = request.get('object_refs')
    if (not isinstance(refs, list) or not 1 <= len(refs) <= 20
            or not all(isinstance(ref, str) and ref == ref.strip() for ref in refs)
            or len(set(refs)) != len(refs)):
        raise ValueError('请选择 1 至 20 个不同的本地景点。')
    limit, max_swipes = request.get('limit', 500), request.get('max_swipes', 1500)
    if type(limit) is not int or not 1 <= limit <= 100000 or type(max_swipes) is not int or not 1 <= max_swipes <= 10000:
        raise ValueError('每个景点的点评目标须为 1 至 100000，滑动上限须为 1 至 10000。')
    items = []
    for ref in refs:
        evidence = resolve_object_ref(output, ref)
        if evidence['kind'] != 'sight':
            raise ValueError('请只将景点加入点评队列。')
        items.append(dict(evidence, address=sight_navigation_address(output, evidence)))

    def start(stop, emit):
        queue = create_sight_queue(output, items, limit, max_swipes)
        queue_id = queue['queue_id']
        with lock_sight_queue(output, queue_id):
            return run_sight_queue(output, load_sight_queue(output, queue_id), adb_path, adb_serial, stop, emit)

    return start

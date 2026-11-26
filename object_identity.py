"""手机采集的对象身份来自已落盘证据，不再由操作者手填对象编号。

证据只读，不访问网络、不连接手机：对象快照、网页运行报告、目录报告里本来就有
kind / resource_id / name，手机报告只有在自身已带核验来源时才可用。
解析不到或证据里没有正整数编号时报错退出，不猜测编号。
"""
import json
import logging
import re
from pathlib import Path

from android_coverage import local_file
from mobile_scoped_store import EVIDENCE_MANIFEST, SCREEN_VERIFIED_ID

logger = logging.getLogger(__name__)
# 与 --audit 的 object_key 同一格式；其他写法（mobile:、regions-…）不支持。
OBJECT_KINDS = ('hotel', 'sight', 'restaurant', 'shopping')
OBJECT_REF = re.compile(r'(hotel|sight|restaurant|shopping)-([1-9][0-9]{0,19})')
SNAPSHOT_FILE = re.compile(r'((?:hotel|sight|restaurant|shopping)-[1-9][0-9]{0,19})(?:-public|-app-capture)?-object\.json')
NETWORK_REPORT = re.compile(r'((?:hotel|sight|restaurant|shopping)-[1-9][0-9]{0,19})-report-[0-9A-Za-z]+\.json')
DIRECTORY_KIND = re.compile(r'discovery-(hotel|sight|restaurant|shopping)-')
MOBILE_REPORT = re.compile(r'report-[0-9A-Za-z-]+\.json')
# 手机报告本身默认来自手填编号，只有这两种来源才能反过来当身份证据。
VERIFIED_SOURCES = (EVIDENCE_MANIFEST, SCREEN_VERIFIED_ID)
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024


def positive_identity(value):
    # 只接受可表示为 Object Ref 的编号。
    if type(value) is int and 0 < value < 10**20:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value.strip()):
        return str(int(value.strip()))
    return None


def clean_name(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def read_evidence(path):
    # /* ===== 步骤2：读取一份证据；数据源：输出目录内的报告文件；操作：1) 拒绝链接 2) 限制大小 ===== */
    if not local_file(path) or path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError('不是可读取的普通证据文件')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except RecursionError as exc:
        raise ValueError('证据 JSON 嵌套过深') from exc
    if not isinstance(data, dict):
        raise ValueError('证据文件不是 JSON 对象')
    return data


def directory_objects(path, data):
    rows = data.get('objects')
    if not isinstance(rows, list):
        raise ValueError('目录报告没有对象清单')
    match = DIRECTORY_KIND.match(path.name)
    filename_kind = match[1] if match else None
    report_kind = data.get('kind')
    if report_kind not in OBJECT_KINDS:
        report_kind = None
    if filename_kind and report_kind and filename_kind != report_kind:
        raise ValueError('目录报告类型与文件名不一致')
    directory_kind = report_kind or filename_kind
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_kind = row.get('kind')
        kind = directory_kind if row_kind is None else row_kind
        if kind not in OBJECT_KINDS or (directory_kind and kind != directory_kind):
            continue
        # 餐饮的 POI 编号不能替代点评使用的资源编号。
        identity = positive_identity(row.get('resource_id'))
        name = clean_name(row.get('name'))
        if identity and name:
            yield f'{kind}-{identity}', name


def evidence_candidates(folder, object_ref=None):
    snapshots, network, directories, mobile = [], [], [], []
    for path in folder.iterdir():
        name = path.name
        if match := SNAPSHOT_FILE.fullmatch(name):
            if object_ref is None or match[1] == object_ref:
                snapshots.append((name != f'{match[1]}-object.json', name, path, match[1]))
        elif match := NETWORK_REPORT.fullmatch(name):
            if object_ref is None or match[1] == object_ref:
                network.append((path, match[1]))
        elif name.startswith('discovery-') and name.endswith('.json'):
            directories.append(path)
        elif MOBILE_REPORT.fullmatch(name):
            mobile.append(path)
    # 同一对象优先用标准快照，其次按证据类型和文件名依次读取。
    for _, _, path, key in sorted(snapshots):
        yield path, 'object_snapshot', key
    for path, key in sorted(network):
        yield path, 'network_report', key
    for path in sorted(directories):
        yield path, 'directory_report', None
    for path in sorted(mobile):
        yield path, 'mobile_report', None


def evidence_objects(path, scope, key):
    data = read_evidence(path)
    if scope == 'directory_report':
        yield from directory_objects(path, data)
        return
    if scope == 'object_snapshot':
        kind, identity = key.split('-', 1)
        if data.get('kind') != kind:
            raise ValueError(f'对象类型不是 {kind}')
        found = positive_identity(data.get('resource_id'))
        if found is None:
            raise ValueError('没有正整数对象编号')
        if found != identity:
            raise ValueError(f'对象编号 {found} 与引用 {identity} 不一致')
    elif scope == 'network_report':
        if data.get('object_key') != key:
            raise ValueError('报告对象键与该文件不一致')
    elif scope == 'mobile_report':
        kind = data.get('kind')
        identity = positive_identity(data.get('object_id'))
        if kind not in OBJECT_KINDS or not identity or data.get('identity_source') not in VERIFIED_SOURCES:
            return
        key = f'{kind}-{identity}'
    name = clean_name(data.get('place' if scope == 'mobile_report' else 'name'))
    if name is None:
        raise ValueError('没有对象名称')
    yield key, name


def object_identity(object_ref, name, path):
    kind, identity = object_ref.split('-', 1)
    return {'object_ref': object_ref, 'kind': kind, 'object_id': identity, 'name': name,
            'identity_source': EVIDENCE_MANIFEST, 'identity_evidence': path.name}


def resolve_object_ref(folder, object_ref):
    # /* ===== 步骤4：解析对象身份；数据源：落盘证据；操作：1) 校验引用 2) 逐个读证据 3) 拿不到就报错 ===== */
    logger.info('开始从落盘证据解析对象身份')
    # 先解析成绝对路径：相对目录下的证据文件必须能通过 local_file 的普通文件检查。
    folder = Path(folder).resolve()
    text = object_ref.strip() if isinstance(object_ref, str) else ''
    if not OBJECT_REF.fullmatch(text):
        raise ValueError(f'对象引用无效：{object_ref}；应为 类型-编号，例如 sight-136039')
    if not folder.is_dir():
        raise ValueError(f'输出目录不存在，无法从落盘证据解析 {text}：{folder}')
    problems = []
    for path, scope, key in evidence_candidates(folder, text):
        try:
            for found, name in evidence_objects(path, scope, key):
                if found == text:
                    logger.info('对象身份来自%s：%s', scope, path.name)
                    return object_identity(text, name, path)
        except (OSError, ValueError, TypeError) as exc:
            problems.append(f'{path.name}：{exc}')
    detail = '；'.join(problems) if problems else '输出目录内没有该对象的证据文件'
    raise ValueError(f'无法从落盘证据解析 {text}：{detail}。未采集；不会猜测对象编号。'
                     '请先采集一次网页版对象，或用 --audit 确认证据。')


def list_object_refs(folder, *, kind=None, query='', limit=50):
    """从本地证据列出能解析为对象身份的引用。"""
    if kind is not None and kind not in OBJECT_KINDS:
        raise ValueError('对象类型无效')
    if not isinstance(query, str):
        raise ValueError('搜索内容必须是文本')
    if type(limit) is not int or limit <= 0:
        raise ValueError('结果数量上限必须是正整数')

    folder = Path(folder).resolve()
    if not folder.is_dir():
        return {'objects': [], 'truncated': False}
    objects = {}
    for path, scope, key in evidence_candidates(folder):
        try:
            for ref, name in evidence_objects(path, scope, key):
                if ref not in objects:
                    objects[ref] = object_identity(ref, name, path)
        except (OSError, ValueError, TypeError):
            continue

    term = query.strip().casefold()
    matches = [row for row in objects.values()
               if (kind is None or row['kind'] == kind)
               and (term in row['name'].casefold() or term in row['object_ref'])]
    matches.sort(key=lambda row: (row['name'].casefold(), row['object_ref']))
    return {'objects': matches[:limit], 'truncated': len(matches) > limit}

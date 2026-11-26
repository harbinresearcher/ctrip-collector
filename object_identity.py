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
OBJECT_REF = re.compile(r'(hotel|sight|restaurant|shopping)-([1-9][0-9]{0,19})')
SNAPSHOT_FILE = re.compile(r'((?:hotel|sight|restaurant|shopping)-[1-9][0-9]{0,19})(?:-public|-app-capture)?-object\.json')
NETWORK_REPORT = re.compile(r'((?:hotel|sight|restaurant|shopping)-[1-9][0-9]{0,19})-report-[0-9A-Za-z]+\.json')
MOBILE_REPORT = re.compile(r'report-[0-9A-Za-z-]+\.json')
# 手机报告本身默认来自手填编号，只有这两种来源才能反过来当身份证据。
VERIFIED_SOURCES = (EVIDENCE_MANIFEST, SCREEN_VERIFIED_ID)
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024


def positive_identity(value):
    # /* ===== 步骤1：读取对象编号；数据源：证据字段；操作：1) 只认正整数 2) 字符串补零归一 ===== */
    if type(value) is int and value > 0:
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
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('证据文件不是 JSON 对象')
    return data


def snapshot_reader(kind, identity):
    # 3.1 对象快照带 kind/resource_id/name，是优先级最高的身份证据。
    def read(data):
        if data.get('kind') != kind:
            raise ValueError(f'对象类型不是 {kind}')
        found = positive_identity(data.get('resource_id'))
        if found is None:
            raise ValueError('没有正整数对象编号')
        if found != identity:
            raise ValueError(f'对象编号 {found} 与引用 {identity} 不一致')
        name = clean_name(data.get('name'))
        if name is None:
            raise ValueError('没有对象名称')
        return name
    return read


def network_reader(key):
    # 3.2 网页运行报告的对象键就是 kind-编号，另有接口回显的名称。
    def read(data):
        if data.get('object_key') != key:
            raise ValueError('报告对象键与该文件不一致')
        name = clean_name(data.get('name'))
        if name is None:
            raise ValueError('没有对象名称')
        return name
    return read


def directory_reader(kind, identity):
    # 3.3 目录报告按对象行的 kind + resource_id 定位；餐饮行只有 POI ID 时不匹配。
    def read(data):
        rows = data.get('objects')
        if not isinstance(rows, list):
            raise ValueError('目录报告没有对象清单')
        for row in rows:
            if not isinstance(row, dict) or row.get('kind') not in (None, kind):
                continue
            if positive_identity(row.get('resource_id')) != identity:
                continue
            name = clean_name(row.get('name'))
            if name is None:
                raise ValueError('没有对象名称')
            return name
        return None
    return read


def mobile_reader(kind, identity):
    # 3.4 手机报告只有在自身已带核验来源时才算证据，否则是在用手填编号自证。
    def read(data):
        if data.get('kind') != kind or positive_identity(data.get('object_id')) != identity:
            return None
        if data.get('identity_source') not in VERIFIED_SOURCES:
            return None
        name = clean_name(data.get('place'))
        if name is None:
            raise ValueError('没有对象名称')
        return name
    return read


def evidence_candidates(folder, kind, identity):
    # /* ===== 步骤3：按证据优先级列出候选；数据源：输出目录；操作：1) 快照 2) 运行报告 3) 目录报告 4) 手机报告 ===== */
    key = f'{kind}-{identity}'
    snapshots = [path for path in folder.glob('*-object.json')
                 if (match := SNAPSHOT_FILE.fullmatch(path.name)) and match[1] == key]
    # 同名快照以 <key>-object.json 优先，其余按文件名排序，保证每次解析结果稳定。
    snapshots.sort(key=lambda path: (path.name != f'{key}-object.json', path.name))
    for path in snapshots:
        yield path, 'object_snapshot', snapshot_reader(kind, identity)
    for path in sorted(folder.glob(f'{key}-report-*.json')):
        if NETWORK_REPORT.fullmatch(path.name):
            yield path, 'network_report', network_reader(key)
    for path in sorted(folder.glob('discovery-*.json')):
        yield path, 'directory_report', directory_reader(kind, identity)
    for path in sorted(folder.glob('report-*.json')):
        if MOBILE_REPORT.fullmatch(path.name):
            yield path, 'mobile_report', mobile_reader(kind, identity)


def resolve_object_ref(folder, object_ref):
    # /* ===== 步骤4：解析对象身份；数据源：落盘证据；操作：1) 校验引用 2) 逐个读证据 3) 拿不到就报错 ===== */
    logger.info('开始从落盘证据解析对象身份')
    # 先解析成绝对路径：相对目录下的证据文件必须能通过 local_file 的普通文件检查。
    folder = Path(folder).resolve()
    text = object_ref.strip() if isinstance(object_ref, str) else ''
    match = OBJECT_REF.fullmatch(text)
    if not match:
        raise ValueError(f'对象引用无效：{object_ref}；应为 类型-编号，例如 sight-136039')
    kind, identity = match.groups()
    if not folder.is_dir():
        raise ValueError(f'输出目录不存在，无法从落盘证据解析 {text}：{folder}')
    problems = []
    for path, scope, read in evidence_candidates(folder, kind, identity):
        try:
            name = read(read_evidence(path))
        except (OSError, ValueError, TypeError) as exc:
            problems.append(f'{path.name}：{exc}')
            continue
        if name is None:
            continue
        logger.info('对象身份来自%s：%s', scope, path.name)
        return {'object_ref': text, 'kind': kind, 'object_id': identity, 'name': name,
                'identity_source': EVIDENCE_MANIFEST, 'identity_evidence': path.name}
    detail = '；'.join(problems) if problems else '输出目录内没有该对象的证据文件'
    raise ValueError(f'无法从落盘证据解析 {text}：{detail}。未采集；不会猜测对象编号。'
                     '请先采集一次网页版对象，或用 --audit 确认证据。')
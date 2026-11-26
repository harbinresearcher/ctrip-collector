"""页面结构指纹：只记结构、不记正文，用来发现 App 或网页改版。

两套指纹**不能互换**：

- `structure_fingerprint(xml)`：手机 UI XML。先序遍历每个节点，取
  `(深度, class, resource-id 冒号后的尾段, 是否有非空 text)` 拼成序列，
  再取 sha256 前 16 位。`bounds` 和正文文字不进入指纹；只改文字或边距
  指纹不变，增删节点、改 class、文字从有到无都会变。
- `page_structure_fingerprint(page)`：网页点评页。网页本身是 HTML，不是
  UI XML；但这个采集器不保留原始 HTML，只保留从 HTML 的 `__NEXT_DATA__`
  解析出来的页面状态（JSON）。所以这里按页面状态的层级、键名、列表有无、
  标量类型取指纹，同样不算正文；列表只看去重后的元素结构，点评条数或标签
  数量变化不会改变指纹。有独立点评区（`comment_info`）时只算点评区，没有
  时退回整个页面状态。将来若有通道保留原始 HTML，应另加按标签/class
  序列计算的 HTML 指纹，而不是把 HTML 塞进 XML 函数。

基线在 `tests/layout_baselines.json`，由 `tests/fixtures/` 里的真实样本
重算得到；页面改版时必须同时更新基线文件和本模块的 `FAMILIES`。
"""
import hashlib
import xml.etree.ElementTree as ET

VERSION = 1

# 手机点评首屏按采集类型分别取基线：景点与酒店的界面结构不同，不能共用一个基线。
# 有真实首屏样本的类型：《景点》《酒店》；餐厅、购物还没有样本，只记指纹、不比对。
MOBILE_FAMILIES = {'hotel': 'mobile_reviews_hotel', 'sight': 'mobile_reviews_sight',
                   'restaurant': 'mobile_reviews_restaurant', 'shopping': 'mobile_reviews_shopping'}
# 网页点评页的页面状态按类型分开；目前只有景点页有真实样本。
WEB_FAMILIES = {'sight': 'web_reviews'}

FAMILIES = {
    'mobile_reviews_sight': {'kind': 'xml', 'fixture': 'sight-review-live-yungang.xml',
                             'fingerprint': '7866921e38cff5d7', 'version': 1},
    'mobile_reviews_hotel': {'kind': 'xml', 'fixture': 'hotel-comments.xml',
                             'fingerprint': '764ea7df112ae9b7', 'version': 1},
    'web_reviews': {'kind': 'page', 'fixture': 'web-sight-comment-info.json',
                    'fingerprint': '697dd017b08b88e3', 'version': 1},
}


def _digest(lines):
    return hashlib.sha256('\n'.join(lines).encode('utf-8')).hexdigest()[:16]


def structure_fingerprint(xml):
    """手机 UI XML 的结构指纹；正文和 bounds 不参与计算。"""
    root = ET.fromstring(xml)
    lines = []
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        resource_id = (node.get('resource-id') or '').rsplit(':', 1)[-1]
        has_text = '1' if (node.get('text') or '').strip() else '0'
        lines.append('\t'.join((str(depth), node.get('class') or '', resource_id, has_text)))
        stack.extend((child, depth + 1) for child in reversed(list(node)))
    return _digest(lines)


def page_structure_fingerprint(page):
    """网页页面状态（JSON）的结构指纹；只取键名、层级、列表有无和标量类型。"""
    lines = set()

    def walk(item, depth):
        if isinstance(item, dict):
            lines.add(f'{depth}\tdict')
            for key in sorted(item, key=str):
                lines.add(f'{depth}\tkey\t{key}')
                walk(item[key], depth + 1)
        elif isinstance(item, list):
            lines.add(f'{depth}\tlist\t{1 if item else 0}')
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            lines.add(f'{depth}\tstr')
        elif isinstance(item, bool):
            lines.add(f'{depth}\tbool')
        elif item is None:
            lines.add(f'{depth}\tnone')
        else:
            lines.add(f'{depth}\tnumber')

    walk(page, 0)
    return _digest(sorted(lines))


def mobile_family(kind):
    return MOBILE_FAMILIES.get(kind, f'mobile_reviews_{kind}')


def web_family(kind):
    return WEB_FAMILIES.get(kind, f'web_reviews_{kind}')


def layout_for(family, fingerprint):
    """把实际指纹和基线比成报告字段；没有基线的页面族只记录指纹。"""
    baseline = FAMILIES.get(family) or {}
    expected = baseline.get('fingerprint') or None
    return {'family': family, 'fingerprint': fingerprint, 'expected': expected,
            'version': baseline.get('version', VERSION),
            'changed': bool(fingerprint and expected and fingerprint != expected)}


def attach_layout(report, family, fingerprint):
    """给采集报告加 layout；changed 时写 layout_changed 和限制说明，绝不抛出。"""
    layout = layout_for(family, fingerprint)
    report['layout'] = layout
    if layout['changed']:
        report['layout_changed'] = True
        report['limitations'].append(
            f'页面结构指纹与基线不一致（{family}：期望 {layout["expected"]}，实际 {layout["fingerprint"]}）；'
            '可能是页面改版，也可能是同族页面首屏内容的差异；未中断采集')
    return layout
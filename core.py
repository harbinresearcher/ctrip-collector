"""携程 Android 8.96.4 实测页面解析；保留原始证据，不承诺全量。"""
import csv
import hashlib
import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from mobile_review_fields import SHOPPING_SCORE, extract_card_fields, sight_accessibility_counts

logger = logging.getLogger(__name__)
REVIEW_DATE = r'\d{4}-\d{2}-\d{2}'
DATE_PATTERNS = {'hotel': r'\d{4}年\d{1,2}月入住', 'sight': REVIEW_DATE, 'restaurant': REVIEW_DATE}
APP = 'ctrip.android.view'


def bounds(node):
    values = [int(x) for x in re.findall(r'-?\d+', node.get('bounds', ''))]
    return values if len(values) == 4 else [0, 0, 0, 0]


def visible_hotel_review_tiles(card, body):
    app = 'ctrip.android.view'
    left, _, right, bottom = bounds(body)
    card_left, _, card_right, card_bottom = bounds(card)
    width = right - left
    if width <= 0:
        return None

    def app_branch(node):
        if node.get('package') == app:
            yield node
            for child in node:
                yield from app_branch(child)

    candidates = []
    tolerance = max(2, width // 100)
    for grid in card:
        if grid.get('class') != 'android.view.ViewGroup' or grid.get('package') != app:
            continue
        gx, gy, gr, gb = bounds(grid)
        if abs(gx - left) > tolerance or abs(gr - right) > tolerance or not bottom <= gy < gb <= card_bottom:
            continue
        tiles = set()
        for cell in app_branch(grid):
            box = bounds(cell)
            x, y, r, b = box
            # The hotel grid has two coincident image layers per tile, unlike badges or avatars.
            if not (card_left <= gx <= x < r <= gr <= card_right and gy <= y < b <= gb):
                continue
            tile_width, tile_height = r - x, b - y
            tile_fraction = tile_width / width
            # Historical layouts showed square tiles in three columns, and 4:3 tiles in two.
            expected_ratio = (1 if .29 <= tile_fraction <= .35 else
                              .75 if .45 <= tile_fraction <= .52 else None)
            if expected_ratio is None or abs(tile_height - expected_ratio * tile_width) > max(2, tile_width * .015):
                continue
            if sum(child.get('package') == app and child.get('class') == 'android.widget.ImageView'
                   and bounds(child) == box for child in cell) == 2:
                tiles.add(tuple(box))
        if tiles:
            candidates.append(sorted(tiles, key=lambda box: (box[1], box[0])))
    return candidates[0] if len(candidates) == 1 else None


def shopping_review_rows(root):
    rows = []
    for scroll in root.iter('node'):
        if (scroll.get('package') != APP or scroll.get('class') != 'android.widget.ScrollView'
                or len(scroll) != 1 or scroll[0].get('package') != APP):
            continue
        left, _, right, _ = bounds(scroll)
        nodes = [node for node in scroll[0] if node.get('package') == APP]
        # The app exposes each card's background and its content as siblings.
        markers = [index for index, node in enumerate(nodes) if node.get('class') == 'android.view.ViewGroup'
                   and len(node) == 0 and not (node.get('text') or node.get('content-desc'))
                   and bounds(node)[::2] == [left, right]]
        for start, end in zip(markers, markers[1:] + [len(nodes)]):
            if row := shopping_review_row(nodes[start:end], right - left):
                rows.append(row)
    return rows


def shopping_review_row(nodes, width):
    bounds_region = bounds(nodes[0])
    _, top, _, bottom = bounds_region
    if width <= 0 or top >= bottom:
        return None
    entries = [(index, node, node.get('text', '').strip()) for index, node in enumerate(nodes)
               if node.get('class') == 'android.widget.TextView'
               and top <= bounds(node)[1] < bounds(node)[3] <= bottom]
    scores = [(index, score) for index, _, label in entries if (score := SHOPPING_SCORE.fullmatch(label))]
    dates = [(index, label) for index, _, label in entries if re.fullmatch(REVIEW_DATE, label)]
    if len(scores) != 1 or len(dates) != 1:
        return None
    (rating_index, score), (date_index, date) = scores[0], dates[0]
    avatars = [i for i in range(1, rating_index - 1)
               if nodes[i].get('class') == 'android.view.ViewGroup'
               and len(nodes[i]) == 1 and nodes[i][0].get('package') == APP
               and nodes[i][0].get('class') == 'android.widget.ImageView'
               and nodes[i + 1].get('class') == 'android.widget.TextView'
               and bounds(nodes[i])[2] <= bounds(nodes[i + 1])[0]]
    if len(avatars) != 1:
        return None
    body_nodes = [node for group in nodes[rating_index + 1:date_index]
                  if group.get('class') == 'android.view.ViewGroup'
                  for node in group if node.get('package') == APP and node.get('class') == 'android.widget.TextView'
                  and node.get('text', '').strip()
                  and (box := bounds(node))[2] - box[0] >= width * .7
                  and top <= box[1] < box[3] <= bottom]
    if len(body_nodes) != 1:
        return None
    content = body_nodes[0].get('text')
    rating = f'{float(score[1]):.1f}'
    texts = [branch.get('text') for node in nodes[1:] for branch in node.iter('node')
             if branch.get('package') == APP and branch.get('text', '').strip()]
    return {'author': nodes[avatars[0] + 1].get('text', '').strip(), 'date': date, 'date_kind': 'review_date',
            'rating': rating, 'content': content, 'has_expand': any(word.strip() in ('展开', '展开全文') for word in texts),
            'raw_texts': texts, 'card_fields': extract_card_fields('shopping', texts, content, rating)}


def parse_reviews(xml, kind, *, locate_expansion=False, locate_media=False):
    # /* ================================================================
    # 步骤1：定位完整评论卡片
    # 数据源：本次 XML；操作：1) 验证所属应用 2) 按日期和评分定位最近祖先
    # ================================================================ */
    logger.info('开始解析 %s 页面', kind)
    # 1.1 拒绝错误输入，不将错误页当作空评论页。
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError('页面 XML 损坏') from exc
    if not any(n.get('package') == 'ctrip.android.view' for n in root.iter('node')):
        raise ValueError('当前界面不是携程')
    if kind == 'shopping':
        rows = shopping_review_rows(root)
        logger.info('购物页面解析完成，识别 %s 张评论卡片', len(rows))
        return rows
    parents = {child: parent for parent in root.iter() for child in parent}
    pattern = re.compile(DATE_PATTERNS[kind])
    rows = []
    # 1.2 从日期向上找包含单个日期、评分及正文的最小容器。
    for date_node in root.iter('node'):
        if (date_node.get('package') != 'ctrip.android.view' or date_node not in parents
                or not pattern.fullmatch(date_node.get('text', ''))):
            continue
        header = list(parents[date_node])
        # 1.2.1 多窗口 XML 中的其他应用节点不参与作者或正文匹配。
        author_nodes = [n for n in header[:header.index(date_node)]
                        if n.get('package') == 'ctrip.android.view' and n.get('text', '').strip()]
        if not author_nodes:
            continue
        author = author_nodes[0].get('text')
        card = date_node
        while card in parents:
            card = parents[card]
            if card.get('package') != 'ctrip.android.view':
                break
            leaves = [n for n in card.iter('node') if n.get('package') == 'ctrip.android.view'
                      and n.get('text', '').strip()]
            if sum(bool(pattern.fullmatch(n.get('text', ''))) for n in leaves) != 1:
                break
            rating_node = next((n for n in leaves if re.fullmatch(r'[0-5]\.\d', n.get('text', ''))), None)
            if rating_node is None:
                continue
            width = bounds(card)[2] - bounds(card)[0]
            body_nodes = [n for n in leaves[leaves.index(rating_node) + 1:]
                          if n.get('text', '').strip()
                          and bounds(n)[2] - bounds(n)[0] >= width * .65
                          and not n.get('text', '').startswith(('酒店回复', '商家回复'))]
            if not body_nodes:
                continue
            # 1.3 保留完整节点文本，不按字数过滤；日期类型单独标记。
            texts = [n.get('text', '') for n in leaves]
            # 1.4 实测卡片正文位于首个宽文本节点，其后可能是平台宣传文案。
            body = body_nodes[0].get('text', '')
            rating = rating_node.get('text')
            fields = extract_card_fields(kind, texts, body, rating)
            if kind == 'sight':
                heading_and_body = texts[leaves.index(rating_node):leaves.index(body_nodes[0]) + 1]
                for field, value in sight_accessibility_counts(card.get('content-desc'), heading_and_body).items():
                    # A numbered control in the visible card takes precedence over the accessibility description.
                    fields.setdefault(field, value)
            tiles = visible_hotel_review_tiles(card, body_nodes[0]) if kind == 'hotel' else None
            if tiles:
                fields['media_tiles_visible'] = len(tiles)
            rows.append({'author': author, 'date': date_node.get('text'),
                         'date_kind': 'stay_month' if kind == 'hotel' else 'review_date',
                         'rating': rating, 'content': body,
                         'has_expand': '展开' in texts, 'raw_texts': texts,
                         'card_fields': fields})
            if locate_media and tiles:
                rows[-1]['media_tile_bounds'] = tiles
            if locate_expansion and kind == 'hotel':
                # The verified layout keeps the body and its control under the same local container.
                node = body_nodes[0]
                container = parents[node]
                if container in tuple(card.iter()) and container is not card and container.get('package') == 'ctrip.android.view':
                    local_texts = [child for child in container.iter('node')
                                   if child.get('package') == 'ctrip.android.view' and child.get('text', '').strip()]
                    controls = [child for child in local_texts if child.get('text') in ('展开', '收起')]
                    extras = [child for child in local_texts if child is not node and child not in controls
                              and not re.fullmatch(r'[\ue000-\uf8ff]', child.get('text', ''))]
                    if len(controls) == 1 and not extras:
                        rows[-1]['body_control'] = (controls[0].get('text'), bounds(controls[0]))
            break
    logger.info('页面解析完成，识别 %s 张评论卡片', len(rows))
    return rows


class Store:
    def __init__(self, folder):
        # /* ============================================================
        # 步骤2：准备持久存储
        # 目标：本地 SQLite；操作：1) 创建目录 2) 建立唯一记录键
        # ============================================================ */
        logger.info('开始打开数据库')
        # 2.1 所有写入限制在选定输出目录。
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.folder / 'reviews.sqlite3')
        self.db.execute('''CREATE TABLE IF NOT EXISTS reviews (
            key TEXT PRIMARY KEY, kind TEXT, place TEXT, author TEXT, date TEXT,
            date_kind TEXT, rating TEXT, content TEXT, has_expand INTEGER,
            raw_texts TEXT, source TEXT, captured_at TEXT)''')
        self.db.commit()
        logger.info('数据库打开完成')

    def save(self, kind, place, rows, source):
        # /* ============================================================
        # 步骤3：保存去重后的评论
        # 数据源：解析结果；操作：1) 计算内容指纹 2) 事务写入证据路径
        # ============================================================ */
        logger.info('开始保存评论')
        before = self.db.total_changes
        # 3.1 同一正文不同作者或对象保留；指纹不是服务端评论 ID。
        for row in rows:
            fingerprint = json.dumps([kind, place, row['author'], row['date'], row['content']], ensure_ascii=False)
            key = hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()
            self.db.execute('INSERT OR IGNORE INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            (key, kind, place, row['author'], row['date'], row['date_kind'],
                             row['rating'], row['content'], int(row['has_expand']),
                             json.dumps(row['raw_texts'], ensure_ascii=False), source,
                             datetime.now(timezone.utc).isoformat()))
        self.db.commit()
        added = self.db.total_changes - before
        logger.info('保存完成，新增 %s 条', added)
        return added

    def count(self, kind, place):
        return self.db.execute('SELECT count(*) FROM reviews WHERE kind=? AND place=?', (kind, place)).fetchone()[0]

    def export(self, kind, place):
        # /* ============================================================
        # 步骤4：导出可核对的 CSV
        # 数据源：数据库；操作：1) 精确筛选对象 2) 转义公式并原子替换
        # ============================================================ */
        logger.info('开始导出 CSV')
        # 4.1 文件名使用摘要，不将用户输入拼入文件路径。
        tag = hashlib.sha256((kind + ':' + place).encode()).hexdigest()[:12]
        target = self.folder / f'{kind}-{tag}.csv'
        temporary = target.with_suffix('.csv.tmp')
        cursor = self.db.execute('SELECT * FROM reviews WHERE kind=? AND place=? ORDER BY rowid', (kind, place))
        with temporary.open('w', encoding='utf-8-sig', newline='') as output:
            writer = csv.writer(output)
            writer.writerow([item[0] for item in cursor.description])
            for row in cursor:
                writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value for value in row])
        temporary.replace(target)
        logger.info('CSV 导出完成：%s', target)
        return target

    def close(self):
        self.db.close()

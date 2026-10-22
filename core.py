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

logger = logging.getLogger(__name__)
DATE_PATTERNS = {'hotel': r'\d{4}年\d{1,2}月入住', 'sight': r'\d{4}-\d{2}-\d{2}'}


def bounds(node):
    values = [int(x) for x in re.findall(r'-?\d+', node.get('bounds', ''))]
    return values if len(values) == 4 else [0, 0, 0, 0]


def parse_reviews(xml, kind):
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
    parents = {child: parent for parent in root.iter() for child in parent}
    pattern = re.compile(DATE_PATTERNS[kind])
    rows = []
    # 1.2 从日期向上找包含单个日期、评分及正文的最小容器。
    for date_node in root.iter('node'):
        if not pattern.fullmatch(date_node.get('text', '')):
            continue
        header = list(parents[date_node])
        author_nodes = [n for n in header[:header.index(date_node)] if n.get('text', '').strip()]
        if not author_nodes:
            continue
        author = author_nodes[0].get('text')
        card = date_node
        while card in parents:
            card = parents[card]
            leaves = [n for n in card.iter('node') if n.get('text', '').strip()]
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
            rows.append({'author': author, 'date': date_node.get('text'),
                         'date_kind': 'stay_month' if kind == 'hotel' else 'review_date',
                         'rating': rating_node.get('text'), 'content': body,
                         'has_expand': '展开' in texts, 'raw_texts': texts})
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

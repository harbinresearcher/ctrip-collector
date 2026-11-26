"""导出不依赖系统临时目录：受限环境下 SQLite 建不了临时文件也要能导出。"""
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
SCRIPT = '''
import sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import network
folder = Path(sys.argv[1])
with sqlite3.connect(folder / 'network.sqlite3') as db:
    jsonl, csv_path = network.export_records(db, 'sight-9', folder)
lines = Path(jsonl).read_text(encoding='utf-8').splitlines()
print(len(lines))
print(Path(csv_path).read_text(encoding='utf-8-sig').splitlines()[0][:40])
'''


class ExportWithoutTempDirectoryTests(unittest.TestCase):
    def test_export_works_when_system_temp_is_unusable(self):
        # /* ===== 步骤1：临时目录不可写；数据源：真实 SQLite 库；操作：1) 断点 TEMP 2) 仍能导出 ===== */
        logger.info('开始导出临时目录受限测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            # 1.0 300 行 × 约 8KB 正文：实测这一步在旧写法（ORDER BY rowid）下会触发 SQLite
            #     建临时文件，TEMP 不可写时就报 unable to open database file。
            rows = [('sight-9', str(index), json.dumps({'content': 'x' * 8000}), 't')
                    for index in range(300)]
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, '
                           'data TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(object_key,comment_id))')
                db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)', rows)
                db.commit()
            # 1.1 把三个临时目录环境变量都指向不存在的路径，等价于 TEMP 不可写。
            missing = str(root / 'no-such-temp')
            env = {**os.environ, 'TEMP': missing, 'TMP': missing, 'SQLITE_TMPDIR': missing}
            result = subprocess.run([sys.executable, '-c', SCRIPT, str(root), str(ROOT)],
                                    env=env, capture_output=True, encoding='utf-8', errors='replace',
                                    timeout=120)
            # 1.2 导出行数与库中一致，且退出码为 0（不再出现 unable to open database file）。
            self.assertEqual(result.returncode, 0, (result.stderr or '')[-400:])
            self.assertEqual(result.stdout.splitlines()[0], '300')
            self.assertIn('object_key', result.stdout)
        logger.info('导出临时目录受限测试完成')


if __name__ == '__main__':
    unittest.main()
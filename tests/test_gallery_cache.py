import logging
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gallery_cache import cache_snapshot, changed_candidates, GalleryCandidateSession

logger = logging.getLogger(__name__)
REMOTE = '/sdcard/Android/data/ctrip.android.view/cache/CTPictureCache/v2.ols100.1/14/abc_123.cnt'


class GalleryCacheTests(unittest.TestCase):
    def test_session_reuses_prefetch_across_pages_and_cleans_on_exit(self):
        # /* ===== 步骤1：验证预加载复用；数据源：两页相同缓存元数据；操作：1) 首次读取 2) 第二页复用 ===== */
        logger.info('开始跨页缓存复用验证')
        adb = Mock(executable='adb', serial='device')
        adb.run.return_value = REMOTE + '|3|101'
        # 1.1 第二页没有新增缓存，仍需保留第一页预加载的候选，且不重复读取手机文件。
        with patch('gallery_cache.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=b'abc', stderr=b'')) as run:
            with GalleryCandidateSession(adb, {}) as session:
                first = session.refresh(Event())
                second = session.refresh(Event())
                self.assertEqual(first, second)
                self.assertEqual(second[0].read_bytes(), b'abc')
                self.assertEqual(run.call_count, 1)
                local = first[0]
            self.assertFalse(local.exists())
        logger.info('跨页缓存复用验证完成')

    def test_session_refreshes_changed_bytes_and_excludes_old_unrelated_cache(self):
        # /* ===== 步骤2：验证范围隔离；数据源：旧缓存和更新候选；操作：1) 不读取无关旧文件 2) 更新同路径候选 ===== */
        logger.info('开始会话候选范围验证')
        adb = Mock(executable='adb', serial='device')
        old = REMOTE.replace('abc_123', 'old')
        adb.run.side_effect = [REMOTE + '|3|101\n' + old + '|5|10', REMOTE + '|3|102\n' + old + '|5|10']
        # 2.1 同路径元数据变化必须重新读取，不能一直保留旧内容。
        with patch('gallery_cache.subprocess.run', side_effect=[SimpleNamespace(returncode=0, stdout=b'abc'), SimpleNamespace(returncode=0, stdout=b'def')]) as run:
            with GalleryCandidateSession(adb, {old: (5, 10)}) as session:
                self.assertEqual(session.refresh(Event())[0].read_bytes(), b'abc')
                paths = session.refresh(Event())
                self.assertEqual(len(paths), 1)
                self.assertEqual(paths[0].read_bytes(), b'def')
                self.assertEqual(run.call_count, 2)
        logger.info('会话候选范围验证完成')

    def test_excessive_delta_is_rejected_before_reading_files(self):
        # /* ===== 步骤1：验证数量限制；数据源：33个变化项；操作：1) 超限拒绝 2) 不读取正文 ===== */
        logger.info('开始缓存数量边界验证')
        adb = Mock(executable='adb', serial='device')
        adb.run.return_value = '\n'.join(REMOTE.replace('.cnt', f'{i}.cnt') + '|3|101' for i in range(33))
        # 1.1 缓存批量变化时停止，不任意截取32个后冒充完整候选。
        with patch('gallery_cache.subprocess.run') as run:
            with self.assertRaises(ValueError):
                with changed_candidates(adb, {}, Event()):
                    self.fail('too many candidates')
            run.assert_not_called()
        logger.info('缓存数量边界验证完成')

    def test_caller_failure_also_removes_temporary_files(self):
        # /* ===== 步骤2：验证调用失败清理；数据源：匹配流程异常；操作：1) 抛出异常 2) 清理候选 ===== */
        logger.info('开始候选异常清理验证')
        adb = Mock(executable='adb', serial='device')
        adb.run.return_value = REMOTE + '|3|101'
        # 2.1 匹配器失败后也不能遗留整批缓存文件。
        with patch('gallery_cache.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=b'abc', stderr=b'')):
            with self.assertRaises(RuntimeError):
                with changed_candidates(adb, {}, Event()) as paths:
                    local = paths[0]
                    raise RuntimeError('matcher failed')
            self.assertFalse(local.exists())
        logger.info('候选异常清理验证完成')

    def test_metadata_rejects_outside_paths_and_duplicates(self):
        # /* ===== 步骤1：验证目录边界；数据源：缓存元数据；操作：1) 解析合法项 2) 拒绝越界和重复 ===== */
        logger.info('开始缓存目录验证')
        adb = Mock()
        adb.run.return_value = REMOTE + '|3|100\n'
        self.assertEqual(cache_snapshot(adb), {REMOTE: (3, 100)})
        # 1.1 不允许其他目录、命令字符或重复记录混入候选。
        for text in ['/sdcard/private|3|100', REMOTE + ';x|3|100', (REMOTE + '|3|100\n') * 2]:
            adb.run.return_value = text
            with self.assertRaises(ValueError):
                cache_snapshot(adb)
        logger.info('缓存目录验证完成')

    def test_only_changed_file_is_read_and_temporary_files_are_removed(self):
        # /* ===== 步骤2：验证候选暂存；数据源：前后快照；操作：1) 只读取变化项 2) 退出后清理临时文件 ===== */
        logger.info('开始缓存暂存验证')
        adb = Mock(executable='adb', serial='device')
        adb.run.return_value = REMOTE + '|3|101\n'
        # 2.1 相同文件无变化时不读取正文，mtime变化仅生成候选，不分配页码。
        with patch('gallery_cache.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=b'abc', stderr=b'')) as run:
            with changed_candidates(adb, {REMOTE: (3, 100)}, Event()) as paths:
                self.assertEqual(len(paths), 1)
                path = paths[0]
                self.assertEqual(path.read_bytes(), b'abc')
                self.assertIn('head -c 4', run.call_args.args[0][-1])
            self.assertFalse(path.exists())
            run.reset_mock()
            with changed_candidates(adb, {REMOTE: (3, 101)}, Event()) as paths:
                self.assertEqual(paths, [])
            run.assert_not_called()
        logger.info('缓存暂存验证完成')

    def test_cancel_and_size_race_do_not_yield_candidates(self):
        # /* ===== 步骤3：验证中止边界；数据源：取消标记和变更文件；操作：1) 取消不读 2) 长度变化拒绝 ===== */
        logger.info('开始缓存中止验证')
        adb = Mock(executable='adb', serial='device')
        adb.run.return_value = REMOTE + '|3|101\n'
        stop = Event()
        stop.set()
        # 3.1 已取消时不触碰设备；读取期间尺寸改变时不能使用旧元数据。
        with self.assertRaises(InterruptedError):
            with changed_candidates(adb, {}, stop):
                self.fail('cancelled')
        adb.run.assert_not_called()
        with patch('gallery_cache.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=b'abcd', stderr=b'')):
            with self.assertRaises(ValueError):
                with changed_candidates(adb, {}, Event()):
                    self.fail('size changed')
        logger.info('缓存中止验证完成')

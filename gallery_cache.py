"""只读取携程图片缓存变化项，候选临时文件退出后清理。"""
import logging
import hashlib
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)
CACHE_ROOT = '/sdcard/Android/data/ctrip.android.view/cache/CTPictureCache'
PATH_PATTERN = re.compile(re.escape(CACHE_ROOT) + r'/v[A-Za-z0-9_.-]+/\d+/[A-Za-z0-9_-]+\.cnt')


def cache_snapshot(adb):
    # /* ===== 步骤1：读取图片索引；数据源：固定缓存目录；操作：1) 仅取元数据 2) 检查路径和数值 ===== */
    logger.info('开始读取图片缓存索引')
    # 1.1 命令路径固定，不将用户输入拼入远端命令。
    adb.ensure_foreground()
    raw = adb.run('shell', f"find {CACHE_ROOT} -type f -print0 | xargs -0 stat -c '%n|%s|%Y'")
    result = {}
    for line in raw.splitlines():
        fields = line.rsplit('|', 2)
        if len(fields) != 3 or not PATH_PATTERN.fullmatch(fields[0]) or not all(re.fullmatch(r'\d+', value) for value in fields[1:]):
            raise ValueError('图片缓存索引包含未知格式或目录')
        path, size, modified = fields
        if path in result:
            raise ValueError('图片缓存索引包含重复路径')
        result[path] = (int(size), int(modified))
    logger.info('图片缓存索引读取完成：%s项', len(result))
    return result


@contextmanager
def changed_candidates(adb, before, stop):
    # /* ===== 步骤2：暂存单页候选；数据源：当前缓存；操作：1) 使用独立会话 2) 退出清理 ===== */
    logger.info('开始读取单页图片候选')
    # 2.1 保留单页调用方式；多页采集由调用方持有同一会话。
    if stop.is_set():
        raise InterruptedError('图集采集已取消')
    try:
        with GalleryCandidateSession(adb, before) as session:
            yield session.refresh(stop)
    finally:
        logger.info('单页图片候选读取结束')


class GalleryCandidateSession:
    def __init__(self, adb, baseline):
        self.adb = adb
        self.baseline = dict(baseline)
        self.observed = dict(baseline)
        self.files = {}
        self.directory = None
        self.scope = None
        self.recent_paths = set()

    def __enter__(self):
        # /* ===== 步骤1：建立会话；数据源：进入图集前快照；操作：1) 隔离临时目录 2) 保留固定基线 ===== */
        logger.info('开始建立图集候选会话')
        # 1.1 每个对象和分类单独创建会话，禁止跨对象复用。
        self.directory = tempfile.TemporaryDirectory(prefix='ctrip-gallery-')
        logger.info('图集候选会话建立完成')
        return self

    def __exit__(self, *args):
        # /* ===== 步骤3：清理会话；数据源：本次临时文件；操作：1) 清理图片 2) 清空关联 ===== */
        logger.info('开始清理图集候选会话')
        # 3.1 临时候选不进入交付目录，成功与异常退出均清理。
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None
        self.files.clear()
        self.recent_paths.clear()
        logger.info('图集候选会话清理完成')

    def refresh(self, stop):
        # /* ===== 步骤2：更新会话候选；数据源：缓存元数据；操作：1) 保留预加载 2) 只读取新变化 ===== */
        logger.info('开始更新图集候选')
        if stop.is_set():
            raise InterruptedError('图集采集已取消')
        if self.directory is None:
            raise RuntimeError('图集候选会话未打开')
        after = cache_snapshot(self.adb)
        # 2.1 只处理上次读取后的变化；固定基线保留作会话身份，淘汰候选不触发无意义重读。
        selected = {path: meta for path, meta in after.items()
                    if (self.observed.get(path) != meta or path in self.recent_paths) and 0 < meta[0] <= 20_000_000}
        if len(selected) > 32 or sum(meta[0] for meta in selected.values()) > 50_000_000:
            raise ValueError('图片缓存变化过多，请缩小采集范围')
        combined = {path: entry[0] for path, entry in self.files.items()}
        combined.update(selected)
        # 2.2 当前新变化优先；仅删除本会话自己创建的旧临时候选，不删除已导出的图片。
        for remote in list(self.files):
            if len(combined) <= 32 and sum(meta[0] for meta in combined.values()) <= 50_000_000:
                break
            if remote not in selected:
                _, local = self.files.pop(remote)
                local.unlink()
                del combined[remote]
        for remote, meta in selected.items():
            if stop.is_set():
                raise InterruptedError('图集采集已取消')
            if remote in self.files and self.files[remote][0] == meta:
                continue
            # 2.3 有界读取并核对长度；元数据变化时覆盖本会话候选，不沿用旧字节。
            self.adb.ensure_foreground()
            command = [self.adb.executable, '-s', self.adb.serial, 'exec-out', f'head -c {meta[0] + 1} {remote}']
            result = subprocess.run(command, capture_output=True, timeout=35,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError('图片缓存读取失败')
            if len(result.stdout) != meta[0]:
                raise ValueError('图片缓存读取期间大小发生变化')
            local = Path(self.directory.name) / (hashlib.sha256(remote.encode()).hexdigest() + '.bin')
            local.write_bytes(result.stdout)
            self.files.pop(remote, None)
            self.files[remote] = (meta, local)
        if stop.is_set():
            raise InterruptedError('图集采集已取消')
        self.observed = after
        self.recent_paths.clear()
        logger.info('图集候选更新完成：%s个', len(self.files))
        return [entry[1] for entry in self.files.values()]

    def include_recent(self, stop):
        # /* ===== 步骤4：补查近期旧缓存；数据源：未变化图片索引；操作：1) 限定16个 2) 按现有预算读取 ===== */
        logger.info('开始补查近期图片候选')
        if stop.is_set():
            raise InterruptedError('图集采集已取消')
        if self.directory is None:
            raise RuntimeError('图集候选会话未打开')
        after = cache_snapshot(self.adb)
        remaining = 16
        remaining_bytes = 50_000_000
        # 4.1 时间仅用于候选排序，不能证明图片属于当前酒店或图集。
        for path, meta in sorted(after.items(), key=lambda item: (-item[1][1], item[0])):
            if remaining <= 0:
                break
            if path in self.files or not 0 < meta[0] <= min(20_000_000, remaining_bytes):
                continue
            self.recent_paths.add(path)
            remaining -= 1
            remaining_bytes -= meta[0]
        result = self.refresh(stop)
        logger.info('近期图片候选补查完成')
        return result

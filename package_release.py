"""生成可核验的交付压缩包，不修改采集数据。"""
import hashlib
import json
import logging
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

logger = logging.getLogger(__name__)
# 1.0 交付目录按「携程采集测试版-主.次」编号；比较时按数字而不是文件名字典序。
RELEASE_PATTERN = re.compile(r'携程采集测试版-(\d+(?:\.\d+)*)')


def latest_release(output):
    # /* ===== 步骤0：定位本次交付目录；数据源：outputs；操作：1) 只认编号目录 2) 取最大编号 ===== */
    logger.info('开始定位交付目录')
    output = Path(output)
    candidates = []
    # 0.1 没有 outputs 时明确失败，不在空目录上生成只含清单的压缩包。
    for path in output.iterdir() if output.is_dir() else ():
        match = RELEASE_PATTERN.fullmatch(path.name)
        if path.is_dir() and match is not None:
            candidates.append((tuple(int(part) for part in match[1].split('.')), path.name))
    if not candidates:
        raise ValueError(f'{output} 下没有「携程采集测试版-编号」目录，请先完成打包')
    name = max(candidates)[1]
    logger.info('交付目录定位完成：%s', name)
    return name


def main():
    # /* ================================================================
    # 步骤1：记录文件校验值
    # 数据源：交付目录；操作：1) 排除缓存 2) 记录所有交付文件摘要
    # ================================================================ */
    logger.info('开始整理交付文件')
    # 1.1 目录固定在本任务 outputs，不遍历其他用户目录。
    output = Path(__file__).resolve().parents[2] / 'outputs'
    release = output / latest_release(output)
    files = sorted(p for p in release.rglob('*') if p.is_file()
                   and '__pycache__' not in p.parts and p.name != 'SHA256SUMS.json')
    manifest = {p.relative_to(release).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    manifest_file = release / 'SHA256SUMS.json'
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info('文件校验值保存完成，共 %s 项', len(manifest))
    # /* ================================================================
    # 步骤2：打包并检查完整性
    # 目标：交付 ZIP；操作：1) 压缩文件 2) 验证 CRC 和必需文件
    # ================================================================ */
    logger.info('开始生成 ZIP')
    # 2.1 ZIP 包含整个目录，保留 EXE 与运行环境相对位置。
    target = output / f'{release.name}.zip'
    with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
        for path in files + [manifest_file]:
            archive.write(path, path.relative_to(output).as_posix())
    with ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('ZIP 完整性检查失败')
        if f'{release.name}/CtripCollector/CtripCollector.exe' not in archive.namelist():
            raise RuntimeError('ZIP 缺少主程序')
    logger.info('ZIP 检查完成：%s，%s 字节', target, target.stat().st_size)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except ValueError as exc:
        raise SystemExit(f'打包未执行：{exc}')
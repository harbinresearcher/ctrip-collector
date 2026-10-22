"""生成可核验的交付压缩包，不修改采集数据。"""
import hashlib
import json
import logging
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

logger = logging.getLogger(__name__)


def main():
    # /* ================================================================
    # 步骤1：记录文件校验值
    # 数据源：交付目录；操作：1) 排除缓存 2) 记录所有交付文件摘要
    # ================================================================ */
    logger.info('开始整理交付文件')
    # 1.1 目录固定在本任务 outputs，不遍历其他用户目录。
    output = Path(__file__).resolve().parents[2] / 'outputs'
    release = output / '携程采集测试版-0.9'
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
    target = output / '携程采集测试版-0.9.zip'
    with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
        for path in files + [manifest_file]:
            archive.write(path, path.relative_to(output).as_posix())
    with ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('ZIP 完整性检查失败')
        if '携程采集测试版-0.9/CtripCollector/CtripCollector.exe' not in archive.namelist():
            raise RuntimeError('ZIP 缺少主程序')
    logger.info('ZIP 检查完成：%s，%s 字节', target, target.stat().st_size)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()





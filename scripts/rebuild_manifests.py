# -*- coding: utf-8 -*-
"""
按 R2 里现有的图片重建壁纸清单 (迁移 / 恢复用; 日常由 CI 增量更新)

用法:
    python scripts/rebuild_manifests.py --dry-run    # 只看计划, 不写入
    python scripts/rebuild_manifests.py              # 写入 {R2_PREFIX}/manifests/

说明:
    - 配置与 main.py 相同 (R2_* / R2_PREFIX / PORTRAIT_DIR / LANDSCAPE_DIR, 本地从 .env 读), 不需要 TARGET_USER 与 cookie
    - 只写 manifests/: 不删除、不改名任何图片, 不碰抓取状态 (state/)
    - 不下载图片; 条目信息全部来自对象键 (推文 ID -> 发帖时间)
    - 与 CI 同时运行可能互相覆盖清单, 请在 workflow 空闲时执行
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main         # noqa: E402  (需先把仓库根目录加入 sys.path)
import manifest     # noqa: E402


def run(dry_run):
    s3 = main.get_r2_client()
    dirs = {'portrait': main.PORTRAIT_DIR, 'landscape': main.LANDSCAPE_DIR}
    grouped, skipped = manifest.plan_rebuild(s3, main.R2_BUCKET, main.R2_PREFIX, dirs)
    users = sorted({username for username, _ in grouped}, key=str.lower)

    print(f'桶: {main.R2_BUCKET}  前缀: {main.R2_PREFIX or "(根目录)"}')
    print(f'检测到 {len(users)} 个用户')
    for username in users:
        portrait, landscape = grouped[(username, 'portrait')], grouped[(username, 'landscape')]
        dates = sorted(e['created_at'] for e in portrait + landscape)
        print(f'  {username}: 竖屏 {len(portrait)} 张, 横屏方图 {len(landscape)} 张'
              f'  ({dates[0][:10]} ~ {dates[-1][:10]})')
    if skipped:
        print(f'忽略 {len(skipped)} 个非图片 / 命名不符的对象, 例如: {", ".join(skipped[:3])}')

    print('\n清单路径:')
    for username in users:
        for orientation in manifest.ORIENTATIONS:
            print(f'  {manifest.manifest_key(main.R2_PREFIX, username, orientation)}')
    print(f'  {manifest.index_key(main.R2_PREFIX)}')

    if dry_run:
        print('\n[dry-run] 未写入任何内容; 去掉 --dry-run 即写入')
        return
    manifest.apply_rebuild(s3, main.R2_BUCKET, main.R2_PREFIX, grouped)
    print(f'\n已写入 {len(users) * len(manifest.ORIENTATIONS) + 1} 个清单文件')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='按 R2 里现有的图片重建壁纸清单')
    parser.add_argument('--dry-run', action='store_true', help='只打印检测结果与将写入的清单路径, 不写入')
    run(parser.parse_args().dry_run)

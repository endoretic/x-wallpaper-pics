# -*- coding: utf-8 -*-
"""
壁纸清单 (manifest): 给 Cloudflare Worker / Muzei 用的图片索引, 与图片同桶存放

    {R2_PREFIX}/manifests/index.json                所有用户及各自张数
    {R2_PREFIX}/manifests/{用户名}/portrait.json     竖屏, 新 -> 旧
    {R2_PREFIX}/manifests/{用户名}/landscape.json    横屏方图, 新 -> 旧

    - 清单里只存对象键, 不存 URL; 条目 ID 由文件名推出 (推文ID 或 推文ID-序号), 重复生成也不变
    - 两种写法: 增量 (CI 每轮只并入新图) / 重建 (scripts/rebuild_manifests.py, 以桶内实际对象为准)
    - 读清单时除"对象不存在"外的任何错误都直接抛出, 绝不拿空清单覆盖线上清单
"""

import calendar
import json
import re
import time

VERSION = 1
ORIENTATIONS = ('portrait', 'landscape')
RESERVED_DIRS = ('state', 'manifests')      # 前缀下与用户目录平级的系统目录, 不是用户
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{1,15}$')
IMAGE_NAME_RE = re.compile(r'^(?P<post_id>\d+)-(?P<date>\d{4}-\d{2}-\d{2})-.+-img(?:-(?P<index>\d+))?'
                           r'\.(?:jpg|jpeg|png|webp)$', re.IGNORECASE)
TWITTER_EPOCH_MS = 1288834974657            # snowflake 纪元 (2010-11-04)


def join_key(*parts):
    return '/'.join(p.strip('/') for p in parts if p and p.strip('/'))


def manifest_key(prefix, username, orientation):
    return join_key(prefix, 'manifests', username, f'{orientation}.json')


def index_key(prefix):
    return join_key(prefix, 'manifests', 'index.json')


def utc_iso(msecs=None):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() if msecs is None else msecs / 1000))


def post_time_msecs(post_id, date_str):
    # 推文 ID 是 snowflake, 高位就是发帖时间 (精确到毫秒); 2010 年以前的老 ID 不是, 退回文件名里的日期
    if len(post_id) >= 15:
        return (int(post_id) >> 22) + TWITTER_EPOCH_MS
    return calendar.timegm(time.strptime(date_str, '%Y-%m-%d')) * 1000


def parse_image_key(key, prefix, dirs):
    # 对象键 -> (用户名, 横竖, 清单条目); 不是图片 (状态/清单/视频/命名不符) 返回 None
    # dirs: {'portrait': 竖屏目录名, 'landscape': 横屏方图目录名}
    base = join_key(prefix)
    base = base + '/' if base else ''
    if not key.startswith(base):
        return None
    parts = key[len(base):].split('/')
    if len(parts) != 3:
        return None
    username, folder, name = parts
    orientation = next((o for o, d in dirs.items() if d == folder), None)
    match = IMAGE_NAME_RE.match(name)
    if not orientation or not match or username in RESERVED_DIRS or not USERNAME_RE.match(username):
        return None
    post_id, index = match['post_id'], match['index']
    entry = {'id': f'{post_id}-{index}' if index else post_id, 'key': key, 'post_id': post_id,
             'created_at': utc_iso(post_time_msecs(post_id, match['date']))}
    if index:
        entry['media_index'] = int(index)
    return username, orientation, entry


def sort_entries(entries):
    # 新 -> 旧; 同一条推文的多张图按序号正序
    return sorted(entries, key=lambda e: (e['created_at'], int(e['post_id']), -e.get('media_index', 0)),
                  reverse=True)


def merge_entries(existing, new):
    # 按条目 ID 去重; 同 ID 以新条目为准, 但保留旧条目里新条目没有的字段 (如宽高)
    merged = {e['id']: e for e in existing}
    added = 0
    for entry in new:
        if entry['id'] not in merged:
            added += 1
        merged[entry['id']] = {**merged.get(entry['id'], {}), **entry}
    return sort_entries(merged.values()), added


def manifest_doc(username, orientation, entries, now_iso):
    return {'version': VERSION, 'username': username, 'orientation': orientation,
            'updated_at': now_iso, 'count': len(entries), 'images': entries}


def index_doc(rows, now_iso):
    return {'version': VERSION, 'updated_at': now_iso,
            'users': sorted(rows, key=lambda row: row['username'].lower())}


########## R2 读写 ##########


def load_json(s3, bucket, key):
    # 对象不存在返回 None; 其他错误 (网络/权限/内容损坏) 直接抛出, 防止用空清单覆盖线上清单
    try:
        body = s3.get_object(Bucket=bucket, Key=key)['Body'].read()
    except Exception as e:
        if 'NoSuchKey' in str(e):
            return None
        raise
    return json.loads(body.decode('utf-8'))


def save_json(s3, bucket, key, doc):
    s3.put_object(Bucket=bucket, Key=key, ContentType='application/json; charset=utf-8',
                  Body=json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def list_keys(s3, bucket, prefix):
    keys, token = [], None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': prefix, 'MaxKeys': 1000}
        if token:
            kwargs['ContinuationToken'] = token
        resp = s3.list_objects_v2(**kwargs)
        keys += [item['Key'] for item in resp.get('Contents', [])]
        if not resp.get('IsTruncated'):
            return keys
        token = resp['NextContinuationToken']


def update_index(s3, bucket, prefix, counts, now_iso):
    # counts: {用户名: {横竖: 张数}}; 没给的横竖从该清单本身读, 保证 index 与清单一致
    doc = load_json(s3, bucket, index_key(prefix)) or {}
    rows = {row['username']: row for row in doc.get('users', [])}
    for username, by_orientation in counts.items():
        row = {'username': username}
        for orientation in ORIENTATIONS:
            if orientation not in by_orientation:
                existing = load_json(s3, bucket, manifest_key(prefix, username, orientation)) or {}
                by_orientation[orientation] = len(existing.get('images', []))
            row[f'{orientation}_count'] = by_orientation[orientation]
        row['updated_at'] = now_iso
        rows[username] = row
    save_json(s3, bucket, index_key(prefix), index_doc(rows.values(), now_iso))


def add_images(s3, bucket, prefix, dirs, keys, extra=None, now_iso=None):
    # 增量: 把本轮新上传的图片键并入对应清单, 再刷新 index.json; 返回 {(用户名, 横竖): 新增条数}
    # extra: {对象键: {'width': ..., 'height': ...}} 下载时已知的额外字段
    now_iso = now_iso or utc_iso()
    grouped = {}
    for key in keys:
        parsed = parse_image_key(key, prefix, dirs)
        if parsed:
            username, orientation, entry = parsed
            entry.update((extra or {}).get(key, {}))
            grouped.setdefault((username, orientation), []).append(entry)

    added, counts = {}, {}
    for (username, orientation), entries in grouped.items():
        key = manifest_key(prefix, username, orientation)
        doc = load_json(s3, bucket, key) or {}
        merged, added[(username, orientation)] = merge_entries(doc.get('images', []), entries)
        save_json(s3, bucket, key, manifest_doc(username, orientation, merged, now_iso))
        counts.setdefault(username, {})[orientation] = len(merged)
    if counts:
        update_index(s3, bucket, prefix, counts, now_iso)
    return added


def plan_rebuild(s3, bucket, prefix, dirs):
    # 只读: 以桶内实际对象为准算出全部清单; 返回 ({(用户名, 横竖): 条目列表}, 被忽略的键)
    # 每个出现过的用户两种横竖都给出 (可能为空), 保证 Worker 总能读到完整的一对清单
    base = join_key(prefix)
    system = tuple(join_key(base, d) + '/' for d in RESERVED_DIRS)
    grouped, skipped = {}, []
    for key in list_keys(s3, bucket, base + '/' if base else ''):
        parsed = parse_image_key(key, prefix, dirs)
        if parsed:
            username, orientation, entry = parsed
            grouped.setdefault((username, orientation), []).append(entry)
        elif not key.startswith(system):
            skipped.append(key)
    for username in {u for u, _ in grouped}:
        for orientation in ORIENTATIONS:
            grouped[(username, orientation)] = sort_entries(grouped.get((username, orientation), []))
    return grouped, skipped


def apply_rebuild(s3, bucket, prefix, grouped, now_iso=None):
    # 写入重建结果: 保留旧清单里同 ID 条目的额外字段 (如宽高); 桶里已不存在的图片从清单里去掉
    # 只写 manifests/, 不碰图片与 state/
    now_iso = now_iso or utc_iso()
    counts = {}
    for (username, orientation), entries in grouped.items():
        key = manifest_key(prefix, username, orientation)
        old = {e['id']: e for e in (load_json(s3, bucket, key) or {}).get('images', [])}
        merged = sort_entries([{**old.get(e['id'], {}), **e} for e in entries])
        save_json(s3, bucket, key, manifest_doc(username, orientation, merged, now_iso))
        counts.setdefault(username, {})[orientation] = len(merged)
    rows = [{'username': u, **{f'{o}_count': c[o] for o in ORIENTATIONS}, 'updated_at': now_iso}
            for u, c in counts.items()]
    save_json(s3, bucket, index_key(prefix), index_doc(rows, now_iso))

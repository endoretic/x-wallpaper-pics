# -*- coding: utf-8 -*-
"""离线自检

覆盖: cookie 读取 / 横竖判定 / JPEG 尺寸解析 / 媒体提取 /
      本地分文件夹落盘 / R2 增量同步(用假 S3 + 假 X 接口, 完全不联网)
"""

import asyncio
import io
import json
import os
import shutil
import sys
import types
import urllib.parse

TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_selftest_out')
T0 = 1700000000     # 基准时间戳(秒)


# ---------------------------------------------------------------- 假 httpx
def make_jpeg(width, height, orientation=1):
    e = 'little'
    entry = (0x0112).to_bytes(2, e) + (3).to_bytes(2, e) + (1).to_bytes(4, e) + orientation.to_bytes(2, e) + b'\x00\x00'
    ifd = (1).to_bytes(2, e) + entry + (0).to_bytes(4, e)
    body = b'Exif\x00\x00II' + (42).to_bytes(2, e) + (8).to_bytes(4, e) + ifd
    app1 = b'\xff\xe1' + (len(body) + 2).to_bytes(2, 'big') + body
    sof = b'\xff\xc0\x00\x11\x08' + height.to_bytes(2, 'big') + width.to_bytes(2, 'big') + b'\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xd9'
    return b'\xff\xd8' + app1 + sof


def media_item(width, height, token):
    return {'media_url_https': f'https://pbs.twimg.com/media/{token}.jpg',
            'original_info': {'width': width, 'height': height}}


def tweet_entry(tweet_id, seconds, medias):
    return {'entryId': f'tweet-{tweet_id}', 'item': {'itemContent': {'tweet_results': {'result': {
        'rest_id': tweet_id,
        'legacy': {'full_text': f'text {tweet_id}', 'extended_entities': {'media': medias}},
        'edit_control': {'editable_until_msecs': str(seconds * 1000 + 3600000)},
    }}}}}


# 三张图: 两张竖屏 + 一张横屏, 时间各不相同
FAKE_TIMELINE = [
    tweet_entry('1001', T0 + 300, [media_item(1080, 1920, 'a')]),
    tweet_entry('1002', T0 + 200, [media_item(1920, 1080, 'b')]),
    tweet_entry('1003', T0 + 100, [media_item(800, 1200, 'c')]),
]
FAKE_IMAGES = {'a': make_jpeg(1080, 1920), 'b': make_jpeg(1920, 1080), 'c': make_jpeg(800, 1200)}
_api_calls = []


class _Response:
    def __init__(self, content=b'', status_code=200, text=None):
        self.content = content
        self.status_code = status_code
        self.text = text if text is not None else content.decode('utf-8', errors='replace')

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


def _route(url, is_sync_client=False):
    _api_calls.append(url)
    if 'UserByScreenName' in url:
        body = {'data': {'user': {'result': {'rest_id': '42',
                'legacy': {'name': '测试昵称', 'media_count': 3}}}}}
        return _Response(json.dumps(body).encode())
    if 'UserMedia' in url:
        if '"cursor":"' in url:      # 第二页起为 moduleItems; 自检只用一页
            instructions = [{'moduleItems': []}]
        else:
            # 与线上结构一致: instructions[-1].entries[0].content.items
            instructions = [
                {'entries': [{'entryId': 'cursor-bottom-0', 'content': {'value': 'CURSOR-NEXT'}}]},
                {'entries': [{'content': {'items': FAKE_TIMELINE}}]},
            ]
        body = {'data': {'user': {'result': {'timeline_v2': {'timeline': {'instructions': instructions}}}}}}
        return _Response(json.dumps(body).encode())
    if 'pbs.twimg.com' in url:
        token = urllib.parse.urlparse(url).path.rsplit('/', 1)[-1].split('.')[0]
        if 'name=orig' in url:
            return _Response(b'', 404)          # 原图 404 -> 应退回 4096x4096
        return _Response(FAKE_IMAGES.get(token, make_jpeg(100, 100)))
    if url.endswith('.mp4'):
        return _Response(b'\x00\x00\x00\x18ftypmp42fake-video')
    return _Response(b'', 404)


class _FakeSyncClient:
    # 同步客户端 (main.download_one 用)
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, *args, **kwargs):
        return _route(url)


class _FakeClient:
    # 异步客户端 (main.download_all_local 用)
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, *args, **kwargs):
        return _route(url)


_fake_httpx = types.ModuleType('httpx')
_fake_httpx.get = lambda url, *a, **k: _route(url)
_fake_httpx.Client = _FakeSyncClient
_fake_httpx.AsyncClient = _FakeClient
sys.modules['httpx'] = _fake_httpx

# ---------------------------------------------------------------- 假 boto3
R2_STORE = {}
UPLOADS = []


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _FakeS3:
    def get_object(self, Bucket=None, Key=None, **kwargs):
        if Key not in R2_STORE:
            raise Exception('An error occurred (NoSuchKey) when calling the GetObject operation')
        return {'Body': _Body(R2_STORE[Key])}

    def put_object(self, Bucket=None, Key=None, Body=b'', **kwargs):
        R2_STORE[Key] = Body
        return {}

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        with open(Filename, 'rb') as f:
            R2_STORE[Key] = f.read()
        UPLOADS.append((Key, (ExtraArgs or {}).get('ContentType')))


_fake_boto3 = types.ModuleType('boto3')
_fake_boto3.client = lambda *a, **k: _FakeS3()
sys.modules['boto3'] = _fake_boto3
_fake_botocore = types.ModuleType('botocore')
_fake_config = types.ModuleType('botocore.config')
_fake_config.Config = lambda **kwargs: kwargs
sys.modules['botocore'] = _fake_botocore
sys.modules['botocore.config'] = _fake_config

# ---------------------------------------------------------------- 环境与导入
os.environ.update({
    'X_COOKIE': 'auth_token=AAA111; ct0=BBB222;',
    'TARGET_USER': 'testuser',
    'R2_ENDPOINT': 'https://fake.r2.cloudflarestorage.com',
    'R2_BUCKET': 'fake-bucket',
    'R2_ACCESS_KEY_ID': 'AKIAFAKE',
    'R2_SECRET_ACCESS_KEY': 'FAKESECRET',
    'R2_PREFIX': 'pic',
    'HAS_VIDEO': '0',
    'MAX_KNOWN_IDS': '5',
    'FULL_SYNC_PAGES': '1',
    'FORCE_FULL': '0',
    'SAVE_PATH': TEST_DIR,
})

import main
from main import (build_media, collect_media, content_type_for, find_new_tweet_ids, guess_remote_ext,
                  is_portrait, load_cookie, local_target, object_key, parse_jpeg_size, parse_page,
                  r2_sync, LANDSCAPE_DIR, PORTRAIT_DIR)

# ---------------------------------------------------------------- 0. cookie
print('=== 0. cookie 读取 ===')
print('  环境变量 ->', load_cookie())
assert load_cookie() == 'auth_token=AAA111; ct0=BBB222;'
os.environ['X_COOKIE'] = 'guest_id=v1%3A123; auth_token=AAA111; ct0=BBB222; twid=u%3D999'
assert load_cookie() == 'auth_token=AAA111; ct0=BBB222;', '整行 cookie 粘贴时应只取所需两项'
print('  整行 cookie 粘贴 ->', load_cookie())

cookie_path = os.path.join(TEST_DIR, 'cookie.txt')
shutil.rmtree(TEST_DIR, ignore_errors=True)
os.makedirs(TEST_DIR, exist_ok=True)
try:
    del os.environ['X_COOKIE']
    main.COOKIE_FILE = cookie_path
    with open(cookie_path, 'w', encoding='utf-8') as f:
        f.write('\n  auth_token=FROM_FILE; ct0=CT0_FILE;  \n')
    assert load_cookie() == 'auth_token=FROM_FILE; ct0=CT0_FILE;'
    print('  cookie.txt ->', load_cookie())
    for bad, label in (('auth_token=xxxxxxxxxxx; ct0=xxxxxxxxxxx;', '占位符'),
                       ('auth_token=ONLY_ONE;', '缺 ct0')):
        with open(cookie_path, 'w', encoding='utf-8') as f:
            f.write(bad)
        try:
            load_cookie()
            raise AssertionError(f'{label} 应该报错')
        except SystemExit as e:
            print(f'  {label} -> 正确报错: {e}')
finally:
    os.environ['X_COOKIE'] = 'auth_token=AAA111; ct0=BBB222;'
    main.COOKIE_FILE = 'cookie.txt'

# ---------------------------------------------------------------- 1. 横竖判定
print('\n=== 1. 横竖判定 ===')
for label, media, expect in (
        ('竖图 1080x1920', media_item(1080, 1920, 'x'), True),
        ('横图 1920x1080', media_item(1920, 1080, 'x'), False),
        ('正方形 1000x1000', media_item(1000, 1000, 'x'), False)):
    got = is_portrait(media)
    print(f'  {label:16} -> {got}  期望 {expect}')
    assert got is expect
main.unknown_size_count = 0
assert is_portrait({'media_url_https': 'https://pbs.twimg.com/media/nosize.jpg'}) is False
print('  尺寸取不到 (图片头也失败) -> False (按横图处理), unknown_size_count =', main.unknown_size_count)

# ---------------------------------------------------------------- 2. JPEG 解析
print('\n=== 2. JPEG 尺寸解析 ===')
assert parse_jpeg_size(make_jpeg(1080, 1440)) == (1080, 1440)
print('  无 exif 竖图 ->', parse_jpeg_size(make_jpeg(1080, 1440)))
assert parse_jpeg_size(make_jpeg(1080, 1440, orientation=6)) == (1440, 1080)
print('  exif 方向 6 ->', parse_jpeg_size(make_jpeg(1080, 1440, orientation=6)), '(宽高已互换)')
assert parse_jpeg_size(make_jpeg(1080, 1440, orientation=0)) == (1080, 1440), '非法方向值应回退为 1'
assert parse_jpeg_size(b'not an image') is None and parse_jpeg_size(b'') is None
print('  非 jpeg / 空数据 -> None / None')

# ---------------------------------------------------------------- 3. 媒体提取与命名
print('\n=== 3. 媒体提取与 R2 键名 ===')
tweets, page_max = parse_page(FAKE_TIMELINE)
print(f'  解析出 {len(tweets)} 条推文, 该页最新时间戳 -> {main.stamp2date(page_max)}')
assert len(tweets) == 3 and page_max == (T0 + 300) * 1000
assert len(find_new_tweet_ids(tweets, 0, set())) == 3
assert find_new_tweet_ids(tweets, (T0 + 300) * 1000, {'1001'}) == []          # 水位线已覆盖
assert find_new_tweet_ids(tweets, (T0 + 300) * 1000, set()) == ['1001']       # 同秒未记录 -> 仍要处理
print('  水位线/同秒补漏 判定正确')

media_list = collect_media(tweets, '测试昵称')
for m in media_list:
    print(f'  {"竖屏" if m["is_portrait"] else "横图"}  {object_key(m)}')
assert len(media_list) == 3
assert [m['is_portrait'] for m in media_list] == [True, False, True]
assert media_list[0]['saved_name'].startswith('1001-')          # 文件名以推文 ID 开头
assert object_key(media_list[0]) == f'pic/testuser/{PORTRAIT_DIR}/1001-{main.stamp2date((T0 + 300) * 1000)}-测试昵称-img.jpg'
assert object_key(media_list[1]).startswith(f'pic/testuser/{LANDSCAPE_DIR}/')
assert object_key(media_list[0], ) != object_key(media_list[2]), '不同推文不能重名'
print('  键名: {前缀}/{用户名}/{横竖目录}/{推文ID}-{日期}-{昵称}-img.jpg')

video = {'tweet_id': '9', 'date': '2024-01-01', 'url': 'https://video/x.mp4',
         'saved_name': '9-2024-01-01-测试昵称-vid.mp4', 'is_image': False, 'is_portrait': False}
assert object_key(video) == 'pic/testuser/9-2024-01-01-测试昵称-vid.mp4', '视频不参与横竖分目录'
print('  视频 ->', object_key(video))
assert guess_remote_ext(make_jpeg(1, 1), media_list[0]) == '.jpg'
assert guess_remote_ext(b'\x89PNG\r\n\x1a\n' + b'\x00' * 20, media_list[0]) == '.png'
assert content_type_for('.png') == 'image/png' and content_type_for('.jpg') == 'image/jpeg'
print('  后缀纠正与 ContentType -> png/jpeg 均正确')

# ---------------------------------------------------------------- 4. 本地落盘
print('\n=== 4. 本地模式分文件夹 ===')
local_dir = os.path.join(TEST_DIR, 'local')
shutil.rmtree(local_dir, ignore_errors=True)
main.download_all_local(media_list + [video], local_dir, 1, 4)
listing = sorted(os.path.relpath(os.path.join(dp, f), local_dir).replace('\\', '/')
                 for dp, dn, fn in os.walk(local_dir) for f in fn)
for path in listing:
    print('  ', path)
assert any(p.startswith(PORTRAIT_DIR + '/') for p in listing)
assert any(p.startswith(LANDSCAPE_DIR + '/') for p in listing)
assert any(p.endswith('.mp4') for p in listing) and not any(p.startswith(PORTRAIT_DIR) and p.endswith('.mp4') for p in listing)
assert len(listing) == 4
assert any('name=orig' in u for u in _api_calls), '图片应先请求原图'
assert any('name=4096x4096' in u for u in _api_calls), '原图 404 后应退回 4096x4096'
assert local_target(media_list[0], local_dir, 1).endswith('.jpg')

# ---------------------------------------------------------------- 5. R2 增量同步
print('\n=== 5. R2 增量同步 (假 S3 + 假 X 接口) ===')
main.down_count = main.portrait_count = main.landscape_count = 0
print('--- 第 1 次运行: 首次全量 ---')
r2_sync()
state_key = 'pic/state/testuser.json'
assert state_key in R2_STORE, '应该写回状态'
state = json.loads(R2_STORE[state_key].decode())
uploaded_keys = sorted(k for k in R2_STORE if k != state_key)
for k in uploaded_keys:
    print('  桶内对象:', k)
assert len(uploaded_keys) == 3, uploaded_keys
assert len([k for k in uploaded_keys if f'/{PORTRAIT_DIR}/' in k]) == 2
assert len([k for k in uploaded_keys if f'/{LANDSCAPE_DIR}/' in k]) == 1
assert state['watermark_msecs'] == (T0 + 300) * 1000
assert sorted(state['known_tweet_ids']) == ['1001', '1002', '1003']
assert state['uploaded'] == 3
expect_key = f'pic/testuser/{PORTRAIT_DIR}/1001-{main.stamp2date((T0 + 300) * 1000)}-测试昵称-img.jpg'
assert dict(UPLOADS)[expect_key] == 'image/jpeg', dict(UPLOADS)
assert main.down_count == 3 and main.portrait_count == 2 and main.landscape_count == 1
print('  状态:', json.dumps({k: v for k, v in state.items() if k != 'known_tweet_ids'}, ensure_ascii=False),
      'known_ids =', state['known_tweet_ids'])

print('--- 第 2 次运行: 无新图片, 应零下载零上传 ---')
uploads_before = list(UPLOADS)
main.down_count = main.portrait_count = main.landscape_count = 0
r2_sync()
assert UPLOADS == uploads_before, f'不应有新的上传: {UPLOADS[len(uploads_before):]}'
assert main.down_count == 0, '无新图时不应下载'
print('  未产生任何上传/下载, 状态水位线保持 ->', json.loads(R2_STORE[state_key].decode())['watermark_msecs'])

print('--- 第 3 次运行: 出现新推文, 只抓新的那张 ---')
FAKE_TIMELINE.insert(0, tweet_entry('1004', T0 + 400, [media_item(1080, 1350, 'd')]))
FAKE_IMAGES['d'] = make_jpeg(1080, 1350)
main.down_count = main.portrait_count = main.landscape_count = 0
r2_sync()
new_keys = [k for k, _ in UPLOADS if k not in dict(uploads_before)]
assert len(new_keys) == 1 and '1004-' in new_keys[0], new_keys
assert new_keys[0].startswith(f'pic/testuser/{PORTRAIT_DIR}/')
assert main.down_count == 1 and main.portrait_count == 1
assert json.loads(R2_STORE[state_key].decode())['watermark_msecs'] == (T0 + 400) * 1000
print('  只新增:', new_keys[0])

shutil.rmtree(TEST_DIR, ignore_errors=True)
print('\n全部自检通过 (测试用户: testuser, 全程未联网)')

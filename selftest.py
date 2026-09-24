# -*- coding: utf-8 -*-
"""离线自检

覆盖: .env 加载 / cookie 读取 / 横竖判定 / JPEG 尺寸解析 / 媒体提取 /
      本地分文件夹落盘 / R2 增量同步 / 壁纸清单 (用假 S3 + 假 X 接口, 完全不联网)
"""

import asyncio
import calendar
import io
import json
import os
import shutil
import sys
import time
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

    def list_objects_v2(self, Bucket=None, Prefix='', MaxKeys=1000, ContinuationToken=None, **kwargs):
        # 故意每页只给 2 个, 顺带测分页
        keys = sorted(k for k in R2_STORE if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + min(MaxKeys, 2)]
        resp = {'Contents': [{'Key': k, 'Size': len(R2_STORE[k])} for k in page],
                'IsTruncated': start + len(page) < len(keys)}
        if resp['IsTruncated']:
            resp['NextContinuationToken'] = str(start + len(page))
        return resp


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

# ---------------------------------------------------------------- 0. .env 自动加载
print('=== 0. .env 自动加载 ===')
assert main.load_env_file(os.path.join(TEST_DIR, '不存在的.env')) is None
print('  文件不存在 ->', None, '(静默跳过)')

tmp_env = os.path.join(TEST_DIR, 'dotenv_test.env')
os.makedirs(TEST_DIR, exist_ok=True)
with open(tmp_env, 'w', encoding='utf-8') as f:
    f.write('\ufeff# 注释行会被跳过\n'
            '\n'
            'ENV_TEST_PLAIN=plain-value\n'
            'ENV_TEST_QUOTED="带 空格 与;分号"\n'
            "ENV_TEST_SINGLE='单引号'\n"
            'export ENV_TEST_EXPORT=exported\n'
            'ENV_TEST_COOKIE=auth_token=AAA; ct0=BBB;\n'
            'ENV_TEST_EMPTY=\n')
for key in ('ENV_TEST_PLAIN', 'ENV_TEST_QUOTED', 'ENV_TEST_SINGLE', 'ENV_TEST_EXPORT',
            'ENV_TEST_COOKIE', 'ENV_TEST_EMPTY'):
    os.environ.pop(key, None)
main.load_env_file(tmp_env)
for key in ('ENV_TEST_PLAIN', 'ENV_TEST_QUOTED', 'ENV_TEST_SINGLE', 'ENV_TEST_EXPORT'):
    print(f'  {key} = {os.environ.get(key)!r}')
assert os.environ['ENV_TEST_PLAIN'] == 'plain-value'
assert os.environ['ENV_TEST_QUOTED'] == '带 空格 与;分号', '引号应被去掉, 值原样保留'
assert os.environ['ENV_TEST_SINGLE'] == '单引号'
assert os.environ['ENV_TEST_EXPORT'] == 'exported', 'export 前缀应被去掉'
assert os.environ.get('ENV_TEST_COOKIE') == 'auth_token=AAA; ct0=BBB;', '值里的 = 与 ; 要保留'
assert 'ENV_TEST_EMPTY' not in os.environ, '空值 (KEY=) 应被跳过'
print('  空值 (KEY=) -> 跳过不写入, 配置项退回默认值')

os.environ['ENV_TEST_EXISTING'] = '来自环境'
with open(tmp_env, 'w', encoding='utf-8') as f:
    f.write('ENV_TEST_EXISTING=来自文件\n')
main.load_env_file(tmp_env)
print(f'  已有环境变量 -> 保持 {os.environ["ENV_TEST_EXISTING"]!r} (不被 .env 覆盖)')
assert os.environ['ENV_TEST_EXISTING'] == '来自环境'
os.environ.pop('ENV_TEST_EXISTING', None)

# import main 时会尝试加载 .env: 本地有该文件, CI 里没有 (直接跳过, 不能因此报错)
loaded = main.load_env_file()
assert loaded is None or isinstance(loaded, int)
print(f'  import main 时加载 .env -> {"跳过 (CI 无此文件)" if loaded is None else f"载入 {loaded} 项 (本地通道)"}')

# ---------------------------------------------------------------- 1. 两条配置通道
print('\n=== 1. 本地 .env / CI 环境变量 (cookie 只认 X_COOKIE) ===')
print('  X_COOKIE ->', load_cookie())
assert load_cookie() == 'auth_token=AAA111; ct0=BBB222;'
os.environ['X_COOKIE'] = 'guest_id=v1%3A123; auth_token=AAA111; ct0=BBB222; twid=u%3D999'
assert load_cookie() == 'auth_token=AAA111; ct0=BBB222;', '整行 cookie 粘贴时应只取所需两项'
print('  整行 cookie 粘贴 ->', load_cookie())

# 回归: 任何调 X 接口的入口都必须自己把 cookie 填进请求头 (这里用假 httpx 抓实际发出的头)
main._headers.pop('cookie', None)
main._headers.pop('x-csrf-token', None)
CAPTURED = {}
_saved_route = _route


def _capture(url, *args, **kwargs):
    CAPTURED['url'] = url
    CAPTURED['headers'] = kwargs.get('headers') or {}
    return _saved_route(url)

import httpx as _fake
_saved_get = _fake.get          # 存原始假 get (它接受 **kwargs), 之后要还原
_fake.get = _capture
main.get_media_page('42', None)          # 直接调底层函数, 不经过 main()
sent = CAPTURED.get('headers', {})
print(f'  直接调 get_media_page -> 请求头 cookie = {sent.get("cookie")!r}, x-csrf-token = {sent.get("x-csrf-token")!r}')
assert sent.get('cookie'), '请求必须带 cookie (否则服务端只会回 403)'
assert sent.get('x-csrf-token'), '请求必须带 x-csrf-token'
_fake.get = _saved_get

# 不再有 cookie.txt 兜底: 只认环境变量 (本地来自 .env, CI 来自 Secret)
with open(os.path.join(TEST_DIR, 'cookie.txt'), 'w', encoding='utf-8') as f:
    f.write('auth_token=FROM_FILE; ct0=CT0_FILE;')
for bad, label in ((None, '未配置'), ('auth_token=xxxxxxxxxxx; ct0=xxxxxxxxxxx;', '占位符'),
                   ('auth_token=ONLY_ONE;', '缺 ct0')):
    os.environ['X_COOKIE'] = bad if bad else ''
    try:
        load_cookie()
        raise AssertionError(f'{label} 应该报错')
    except SystemExit as e:
        print(f'  {label} -> 正确报错: {str(e).splitlines()[0]}')
os.environ['X_COOKIE'] = 'auth_token=AAA111; ct0=BBB222;'

# TARGET_USER 同样只认环境变量, 缺失时启动即报错
import subprocess


def run_main(env_overrides):
    env = {k: v for k, v in os.environ.items() if k not in env_overrides}
    env.update(env_overrides)
    env['X_COOKIE'] = 'auth_token=AAA111; ct0=BBB222;'
    # 显式指向不存在的 .env: 本机开发时根目录有真实 .env, 不能让子进程读到它,
    # 否则测不出"未配置就报错"; CI 里本来就没有 .env, 行为一致
    env['ENV_FILE'] = '.env.selftest-not-exist'
    return subprocess.run([sys.executable, 'main.py'], cwd=os.path.dirname(os.path.abspath(__file__)),
                          env=env, capture_output=True, encoding='utf-8', errors='replace', timeout=60)


p = run_main({'TARGET_USER': ''})
first_line = (p.stderr or p.stdout or '').strip().splitlines()
print(f'  TARGET_USER 未配置 -> 退出码 {p.returncode}: {first_line[0] if first_line else "(无输出)"}')
assert p.returncode != 0 and 'TARGET_USER' in (p.stderr or ''), p.stderr
shutil.rmtree(TEST_DIR, ignore_errors=True)

# ---------------------------------------------------------------- 2. 横竖判定
print('\n=== 2. 横竖判定 ===')
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
print('\n=== 3. JPEG 尺寸解析 ===')
assert parse_jpeg_size(make_jpeg(1080, 1440)) == (1080, 1440)
print('  无 exif 竖图 ->', parse_jpeg_size(make_jpeg(1080, 1440)))
assert parse_jpeg_size(make_jpeg(1080, 1440, orientation=6)) == (1440, 1080)
print('  exif 方向 6 ->', parse_jpeg_size(make_jpeg(1080, 1440, orientation=6)), '(宽高已互换)')
assert parse_jpeg_size(make_jpeg(1080, 1440, orientation=0)) == (1080, 1440), '非法方向值应回退为 1'
assert parse_jpeg_size(b'not an image') is None and parse_jpeg_size(b'') is None
print('  非 jpeg / 空数据 -> None / None')

# ---------------------------------------------------------------- 3. 媒体提取与命名
print('\n=== 4. 媒体提取与 R2 键名 ===')
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
print('\n=== 5. 本地模式分文件夹 ===')
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
print('\n=== 6. R2 增量同步 (假 S3 + 假 X 接口) ===')
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

# ---------------------------------------------------------------- 6. 壁纸清单
print('\n=== 7. 壁纸清单 (manifest) ===')
import manifest

DIRS = {'portrait': PORTRAIT_DIR, 'landscape': LANDSCAPE_DIR}
S3 = _FakeS3()
R2_STORE.clear()


def snowflake(iso, seq=0):
    # 按指定发帖时间构造推文 ID (snowflake 高位 = 毫秒时间戳 - 纪元)
    msecs = calendar.timegm(time.strptime(iso, '%Y-%m-%dT%H:%M:%SZ')) * 1000
    return str(((msecs - manifest.TWITTER_EPOCH_MS) << 22) | seq)


def load(key):
    return json.loads(R2_STORE[key].decode('utf-8'))


def img(user, folder, name):
    return f'pic/{user}/{folder}/{name}'


OLD, MID, NEW, NEWEST = (snowflake(f'2025-09-{day}T08:00:00Z') for day in (21, 22, 23, 24))
P_KEY, L_KEY, INDEX_KEY = ('pic/manifests/testuser/portrait.json', 'pic/manifests/testuser/landscape.json',
                           'pic/manifests/index.json')
batch = [
    img('testuser', PORTRAIT_DIR, f'{OLD}-2025-09-21-测试昵称-img.jpg'),
    img('testuser', LANDSCAPE_DIR, f'{MID}-2025-09-22-测试昵称-img.png'),
    img('testuser', PORTRAIT_DIR, f'{NEW}-2025-09-23-测试昵称-img-1.jpg'),
    img('testuser', PORTRAIT_DIR, f'{NEW}-2025-09-23-测试昵称-img-2.webp'),
    img('other_user', PORTRAIT_DIR, f'{MID}-2025-09-22-别的昵称-img.jpg'),
    'pic/state/testuser.json',                              # 状态文件
    'pic/testuser/9-2024-01-01-测试昵称-vid.mp4',            # 视频
    img('bad-name!', PORTRAIT_DIR, '1-2024-01-01-x-img.jpg'),   # 非法用户名
]

# 键名解析
user, orientation, entry = manifest.parse_image_key(batch[0], 'pic', DIRS)
assert (user, orientation) == ('testuser', 'portrait')
assert entry == {'id': OLD, 'key': batch[0], 'post_id': OLD, 'created_at': '2025-09-21T08:00:00Z'}, entry
assert manifest.parse_image_key(batch[1], 'pic', DIRS)[1] == 'landscape'
multi = manifest.parse_image_key(batch[3], 'pic', DIRS)[2]
assert multi['id'] == f'{NEW}-2' and multi['media_index'] == 2
assert [manifest.parse_image_key(k, 'pic', DIRS) for k in batch[5:]] == [None, None, None]
short = manifest.parse_image_key(img('testuser', PORTRAIT_DIR, '1001-2023-11-14-测试昵称-img.jpg'), 'pic', DIRS)[2]
assert short['created_at'] == '2023-11-14T00:00:00Z', '非 snowflake ID 退回文件名日期'
assert manifest.parse_image_key(f'testuser/{PORTRAIT_DIR}/{OLD}-2025-09-21-x-img.jpg', '', DIRS)[0] == 'testuser'
assert manifest.manifest_key('', 'u', 'portrait') == 'manifests/u/portrait.json'
print('  键名解析: 竖屏 / 横屏方图 / 多图序号 / 中文键名正确; 状态、视频、非法用户名被忽略')

# 增量并入
added = manifest.add_images(S3, 'fake-bucket', 'pic', DIRS, batch,
                            extra={batch[0]: {'width': 1080, 'height': 1920}}, now_iso='2025-09-23T09:00:00Z')
assert added == {('testuser', 'portrait'): 3, ('testuser', 'landscape'): 1, ('other_user', 'portrait'): 1}, added
portrait = load(P_KEY)
assert [e['id'] for e in portrait['images']] == [f'{NEW}-1', f'{NEW}-2', OLD], '应为新 -> 旧, 同一推文按序号'
assert (portrait['username'], portrait['orientation'], portrait['count']) == ('testuser', 'portrait', 3)
assert portrait['images'][2]['width'] == 1080 and portrait['images'][0]['key'] == batch[2]
assert [e['id'] for e in load(L_KEY)['images']] == [MID]
assert [e['key'] for e in load('pic/manifests/other_user/portrait.json')['images']] == [batch[4]], '用户之间不能混'
assert [(u['username'], u['portrait_count'], u['landscape_count']) for u in load(INDEX_KEY)['users']] == \
    [('other_user', 1, 0), ('testuser', 3, 1)]
assert not any('http' in R2_STORE[k].decode('utf-8') for k in R2_STORE), '清单里不能出现 URL'
print(f'  首次并入: 竖屏 {portrait["count"]} / 横屏方图 1 / 另一用户 1, 新 -> 旧排序正确')

added = manifest.add_images(S3, 'fake-bucket', 'pic', DIRS, [batch[0], batch[2]])
assert added == {('testuser', 'portrait'): 0}, added
assert [e['id'] for e in load(P_KEY)['images']] == [f'{NEW}-1', f'{NEW}-2', OLD]
assert load(P_KEY)['images'][2]['width'] == 1080, '重复并入不能丢掉已有字段'
print('  重复并入 -> 新增 0, 条目与已有字段不变')

newest_key = img('testuser', PORTRAIT_DIR, f'{NEWEST}-2025-09-24-测试昵称-img.jpg')
manifest.add_images(S3, 'fake-bucket', 'pic', DIRS, [newest_key])
assert load(P_KEY)['images'][0]['id'] == NEWEST and load(P_KEY)['count'] == 4
assert [(u['username'], u['portrait_count'], u['landscape_count']) for u in load(INDEX_KEY)['users']] == \
    [('other_user', 1, 0), ('testuser', 4, 1)], '只动了竖屏时, 横屏张数应从其清单读回'
print('  新图并入 -> 排在最前, index 计数同步')


class _BrokenS3(_FakeS3):
    def get_object(self, Bucket=None, Key=None, **kwargs):
        raise Exception('An error occurred (InternalError) when calling the GetObject operation')


snapshot = dict(R2_STORE)
failed = False
try:
    manifest.add_images(_BrokenS3(), 'fake-bucket', 'pic', DIRS,
                        [img('testuser', PORTRAIT_DIR, f'{NEWEST}-2025-09-24-测试昵称-img-9.jpg')])
except Exception as e:
    failed = 'InternalError' in str(e)
assert failed and R2_STORE == snapshot, '读清单出错时必须报错, 且不能写任何清单'
print('  读清单出错 -> 直接报错, 线上清单保持不动')

# 重建: 以桶内对象为准; MID 横图已从桶里消失 -> 应被移出清单
for key in batch[:1] + batch[2:5] + batch[6:] + [newest_key]:
    R2_STORE[key] = b'img'
R2_STORE['pic/state/testuser.json'] = b'{"watermark_msecs": 1}'
not_manifests = {k: v for k, v in R2_STORE.items() if '/manifests/' not in k}
before_plan = dict(R2_STORE)
grouped, skipped = manifest.plan_rebuild(S3, 'fake-bucket', 'pic', DIRS)
assert R2_STORE == before_plan, 'dry-run (plan) 不能写任何东西'
assert sorted(grouped) == [('other_user', 'landscape'), ('other_user', 'portrait'),
                           ('testuser', 'landscape'), ('testuser', 'portrait')]
assert len(grouped[('testuser', 'portrait')]) == 4 and grouped[('testuser', 'landscape')] == []
assert skipped == [batch[7], batch[6]], skipped
manifest.apply_rebuild(S3, 'fake-bucket', 'pic', grouped, now_iso='2025-09-25T00:00:00Z')
portrait = load(P_KEY)
assert [e['id'] for e in portrait['images']] == [NEWEST, f'{NEW}-1', f'{NEW}-2', OLD]
assert portrait['images'][3]['width'] == 1080, '重建应保留旧清单里的宽高'
assert load(L_KEY)['images'] == [] and load(L_KEY)['count'] == 0, '桶里已删除的图应移出清单'
assert [(u['username'], u['portrait_count'], u['landscape_count']) for u in load(INDEX_KEY)['users']] == \
    [('other_user', 1, 0), ('testuser', 4, 0)]
assert {k: v for k, v in R2_STORE.items() if '/manifests/' not in k} == not_manifests, '重建只能写 manifests/'
print(f'  重建: 分页列举 {len(not_manifests)} 个对象, 忽略 {len(skipped)} 个; 状态与图片未被改动')

shutil.rmtree(TEST_DIR, ignore_errors=True)
print('\n全部自检通过 (测试用户: testuser, 全程未联网)')

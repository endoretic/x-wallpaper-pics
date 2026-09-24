# -*- coding: utf-8 -*-
"""
指定用户的图片下载器 —— 本地运行 / GitHub Actions 定时增量同步

功能:
    下载目标用户自己发布的全部图片 (横竖都下, 不含转推)
    竖构图图片 (height > width) 单独放进 "竖屏", 其余进 "横屏方图"
    走 UserMedia 接口 (该接口本身不含转推), 图片一律取原图

两种运行形态:
    1. 本地: python main.py            结果存到本地目录 (SAVE_PATH)
    2. CI  : python main.py --sync-r2  增量检查新图片 -> 上传 Cloudflare R2 (私有桶)
                                       状态存在 R2 上, 无新图片时零下载

全部配置来自环境变量, 仓库里不保存任何真实密钥 (见 README / .env.example)
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import time

import httpx

########## 配置 (全部来自环境变量) ##########

TARGET_USER = os.environ.get('TARGET_USER', 'lilmonix3')
# 目标用户名 (@ 后面的字符), 只支持一个用户

COOKIE_FILE = os.environ.get('COOKIE_FILE', 'cookie.txt')
# 本地 cookie 文件名; 环境变量 X_COOKIE 优先于该文件

SAVE_PATH = os.environ.get('SAVE_PATH', '')
# 本地保存目录, 留空 = 脚本所在目录 (--sync-r2 模式下用临时目录, 该值忽略)

HAS_VIDEO = os.environ.get('HAS_VIDEO', '0') == '1'
# 是否同时下载视频 (视频不进横竖文件夹)

MAX_MEDIA = int(os.environ.get('MAX_MEDIA', '0'))
# 本地模式单次最多下载多少份, 0 = 不限

MAX_CONCURRENT_REQUESTS = int(os.environ.get('MAX_CONCURRENT_REQUESTS', '8'))
# 最大并发下载数, 网络差可调低

PROXY = os.environ.get('PROXY', '')
# 代理, 留空不使用, 格式: http://127.0.0.1:7890

PORTRAIT_DIR = os.environ.get('PORTRAIT_DIR', '竖屏')
LANDSCAPE_DIR = os.environ.get('LANDSCAPE_DIR', '横屏方图')
# 竖构图图片 / 其余图片 的子文件夹名, 同时作为 R2 里的目录前缀

# --- R2 (Cloudflare) ---
R2_ENDPOINT = os.environ.get('R2_ENDPOINT', '')            # https://<ACCOUNT_ID>.r2.cloudflarestorage.com
R2_BUCKET = os.environ.get('R2_BUCKET', '')                # 桶名
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID', '')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY', '')
R2_PREFIX = os.environ.get('R2_PREFIX', '').strip('/')
# 桶内根前缀, 留空则直接放在桶根目录下

MAX_KNOWN_IDS = int(os.environ.get('MAX_KNOWN_IDS', '1000'))
# 状态里保留多少条"已处理推文 ID", 用于避免同一时间戳的推文被漏掉
# 每个 ID 占 20 字节左右, 1000 条约 20KB, 完全无压力

FULL_SYNC_PAGES = int(os.environ.get('FULL_SYNC_PAGES', '1'))
# (--sync-r2 首次运行 / FORCE_FULL=1 时) 最多翻多少页历史内容做一次性回填
# UserMedia 每页最多 500 条推文, 默认 1 页; 想一次性全量回填就把这个值调大

FORCE_FULL = os.environ.get('FORCE_FULL', '0') == '1'
# 置 1 时忽略已有状态, 重新全量扫描 (注意: 已上传的对象会因重名被跳过, 不会重复占用空间)

########## 配置结束 ##########

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_headers = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    'authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
    'referer': f'https://twitter.com/{TARGET_USER}',
}
proxies = PROXY or None

request_count = 0       # API 调用次数
down_count = 0          # 已下载文件数
portrait_count = 0      # 其中竖屏图片数
landscape_count = 0     # 其中横图/方图数
unknown_size_count = 0  # 尺寸读不到(按横图处理)的图片数


def quote_url(url):
    return url.replace('{', '%7B').replace('}', '%7D')


def stamp2date(msecs_stamp: int) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(msecs_stamp / 1000))


def get_heighest_video_quality(variants) -> str:
    # 找到最高清晰度的视频地址
    if len(variants) == 1:      # gif 适配
        return variants[0]['url']
    max_bitrate = 0
    heighest_url = None
    for i in variants:
        if 'bitrate' in i and int(i['bitrate']) > max_bitrate:
            max_bitrate = int(i['bitrate'])
            heighest_url = i['url']
    return heighest_url


def parse_exif_orientation(exif: bytes) -> int:
    # 在 exif 段中查找方向标记 (tag 0x0112), 取不到返回 1 (正常方向)
    pos = exif.find(b'Exif\x00\x00')
    if pos < 0:
        return 1
    tiff = exif[pos + 6:]
    if len(tiff) < 8:
        return 1
    if tiff[:2] == b'II':
        endian = 'little'
    elif tiff[:2] == b'MM':
        endian = 'big'
    else:
        return 1
    ifd_offset = int.from_bytes(tiff[4:8], endian)
    if ifd_offset + 2 > len(tiff):
        return 1
    entry_count = int.from_bytes(tiff[ifd_offset:ifd_offset + 2], endian)
    for i in range(min(entry_count, 64)):    # 遍历 ifd0 的条目, 找方向标记
        entry = tiff[ifd_offset + 2 + i * 12: ifd_offset + 14 + i * 12]
        if len(entry) < 12:
            break
        if int.from_bytes(entry[:2], endian) == 0x0112:
            value = int.from_bytes(entry[8:10], endian)     # 该值只有 2 字节, 不存在偏移问题
            return value if 1 <= value <= 8 else 1
    return 1


def parse_jpeg_size(data: bytes):
    # 从 jpeg 头部解析 (width, height), 兼容 exif 旋转标记
    if len(data) < 4 or data[:2] != b'\xff\xd8':
        return None
    i = 2
    orientation = 1
    for _ in range(64):     # 最多解析 64 个标记段, 防止异常数据死循环
        if i + 4 > len(data):
            return None
        if data[i] != 0xFF:     # 跳过填充字节
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xE1:      # exif 段: 记录方向标记
            seg_len = int.from_bytes(data[i + 2:i + 4], 'big')
            orientation = parse_exif_orientation(data[i + 4:i + 2 + seg_len])
            i += 2 + seg_len
        elif marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > len(data):
                return None
            h = int.from_bytes(data[i + 5:i + 7], 'big')
            w = int.from_bytes(data[i + 7:i + 9], 'big')     # 旋转 90/270 度时宽高互换
            return (h, w) if orientation in (5, 6, 7, 8) else (w, h)
        elif marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        else:
            seg_len = int.from_bytes(data[i + 2:i + 4], 'big')
            if seg_len < 2:
                return None
            i += 2 + seg_len
    return None


def parse_png_size(data: bytes):
    if len(data) >= 24 and data[:8] == b'\x89PNG\r\n\x1a\n':
        return int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
    return None


def get_image_size(_media):
    # 获取图片原始宽高: 优先用接口返回的 original_info, 缺失时读取图片头部 (只取前 2KB)
    info = _media.get('original_info')
    if info and info.get('width') and info.get('height'):
        return int(info['width']), int(info['height'])
    url = _media.get('media_url_https')
    if not url:
        return None
    try:
        response = httpx.get(quote_url(url + '?name=small'), headers=_headers, proxy=proxies, timeout=(3.05, 16))
        data = response.content[:2048]
        return parse_jpeg_size(data) or parse_png_size(data)
    except Exception:
        return None


def is_portrait(_media) -> bool:
    # 判断图片是否竖构图 (height > width); 尺寸读不到时按非竖屏处理, 只影响它放哪个文件夹
    info = _media.get('original_info')
    if info and info.get('width') and info.get('height'):
        width, height = int(info['width']), int(info['height'])
    else:
        size = get_image_size(_media)
        if not size:
            global unknown_size_count
            unknown_size_count += 1
            print(f'[尺寸未知] 将放入横图文件夹: {_media.get("media_url_https")}')
            return False
        width, height = size
    return height > width


########## cookie ##########


def load_cookie() -> str:
    # 读取 cookie: 环境变量 X_COOKIE 优先, 其次读 cookie 文件
    # 无论哪种来源, 都只提取 auth_token 与 ct0 两项, 多粘贴的内容会被忽略
    raw = os.environ.get('X_COOKIE', '').strip()
    source = '环境变量 X_COOKIE'
    if not raw:
        path = COOKIE_FILE if os.path.isabs(COOKIE_FILE) else os.path.join(_SCRIPT_DIR, COOKIE_FILE)
        if not os.path.exists(path):
            raise SystemExit(f'找不到 cookie: 请把 auth_token 与 ct0 填入 {path}, 或设置环境变量 X_COOKIE')
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read().strip()
        source = path

    found = {}
    for key in ('auth_token', 'ct0'):
        match = re.search(rf'(?:^|[;\s]){key}=([^;\s]+)', raw)      # 支持整行 cookie 直接粘贴
        if match:
            found[key] = match.group(1).strip().strip('"')

    missing = [key for key in ('auth_token', 'ct0') if key not in found or found[key] in ('', 'xxxxxxxxxxx')]
    if missing:
        raise SystemExit(f'{source} 中缺少有效的 {", ".join(missing)} (格式: auth_token=值; ct0=值;)')
    return f'auth_token={found["auth_token"]}; ct0={found["ct0"]};'


########## X 接口 ##########


def get_user_info():
    # 获取用户数字 ID / 昵称 / 媒体推文数
    url = ('https://twitter.com/i/api/graphql/xc8f1g7BYqr6VTzTbvNlGw/UserByScreenName?variables={"screen_name":"' + TARGET_USER +
           '","withSafetyModeUserFields":false}&features={"hidden_profile_likes_enabled":false,"hidden_profile_subscriptions_enabled":false,"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"subscriptions_verification_info_verified_since_enabled":true,"highlights_tweets_tab_ui_enabled":true,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"responsive_web_graphql_timeline_navigation_enabled":true}&fieldToggles={"withAuxiliaryUserLabels":false}')
    response = httpx.get(quote_url(url), headers=_headers, proxy=proxies, timeout=(3.05, 16)).text
    global request_count
    request_count += 1
    try:
        raw_data = json.loads(response)
        result = raw_data['data']['user']['result']
        return result['rest_id'], result['legacy']['name'], result['legacy']['media_count']
    except Exception:
        print('获取用户信息失败, 请检查用户名与 cookie 是否正确')
        print(response[:500])
        return None


def get_media_page(rest_id, cursor):
    # 请求一页 [媒体] 标签页内容 (UserMedia 接口, 内容不含转推)
    url = ('https://twitter.com/i/api/graphql/Le6KlbilFmSu-5VltFND-Q/UserMedia?variables={"userId":"' + rest_id + '","count":500,'
           + ('"cursor":"' + cursor + '",' if cursor else '') +
           '"includePromotedContent":false,"withClientEventToken":false,"withBirdwatchNotes":false,"withVoice":true,"withV2Timeline":true}&features={"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"tweetypie_unmention_optimization_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":false,"tweet_awards_web_tipping_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"rweb_video_timestamps_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"responsive_web_media_download_video_enabled":false,"responsive_web_enhance_cards_enabled":false}')
    response = httpx.get(quote_url(url), headers=_headers, proxy=proxies, timeout=(3.05, 16)).text
    global request_count
    request_count += 1
    try:
        raw_data = json.loads(response)
    except Exception:
        print('API次数已超限' if 'Rate limit exceeded' in response else '获取数据失败')
        print(response[:500])
        return None, None

    try:
        instructions = raw_data['data']['user']['result']['timeline_v2']['timeline']['instructions']
    except Exception:
        print('返回数据结构异常, 可能 cookie 已失效或被风控')
        print(response[:500])
        return None, None
    next_cursor = None
    for i in instructions[-1]['entries']:
        if 'bottom' in i.get('entryId', ''):
            next_cursor = i.get('content', {}).get('value')

    if not cursor:      # 第一页: instructions[-1] -> 末尾 entry 的 content.items
        entries = instructions[-1]['entries']
        items = entries[0]['content'].get('items', []) if entries else []
    elif 'moduleItems' in instructions[0]:   # 后续页: instructions[0].moduleItems
        items = instructions[0]['moduleItems']
    else:
        items = []      # 没有更多内容
    return items, next_cursor


def extract_tweet(item):
    # 从一条时间线条目中取出 (推文ID, 时间戳, 媒体列表); 不是推文条目则返回 None
    if 'tweet' not in item.get('entryId', ''):
        return None
    result = item['item']['itemContent']['tweet_results']['result']
    tweet = result['tweet']['legacy'] if 'tweet' in result else result['legacy']    # 适配限制回复账号
    medias = tweet['extended_entities']['media']
    msecs = int(result['edit_control']['editable_until_msecs']) - 3600000
    return result.get('rest_id', ''), msecs, medias


def build_media(_media, tweet_id, date_str, user_name):
    # 把一条媒体信息整理成统一结构
    if 'video_info' in _media:
        if not HAS_VIDEO:
            return None
        return {'tweet_id': tweet_id, 'date': date_str,
                'url': get_heighest_video_quality(_media['video_info']['variants']),
                'saved_name': f'{tweet_id}-{date_str}-{user_name}-vid.mp4',
                'is_image': False, 'is_portrait': False}
    return {'tweet_id': tweet_id, 'date': date_str, 'url': _media['media_url_https'],
            'saved_name': f'{tweet_id}-{date_str}-{user_name}-img.jpg',
            'is_image': True, 'is_portrait': is_portrait(_media)}


def parse_page(items):
    # 把一页时间线条目解析成 [(推文ID, 时间戳, 媒体列表)], 顺便统计该页最新时间戳
    tweets = []
    max_msecs = 0
    for item in items:
        try:
            extracted = extract_tweet(item)
        except Exception:
            continue
        if not extracted:
            continue
        tweets.append(extracted)
        max_msecs = max(max_msecs, extracted[1])
    return tweets, max_msecs


def find_new_tweet_ids(tweets, watermark_msecs, known_ids):
    # 找出"还没处理过"的推文 ID: 时间晚于水位线的, 或时间相同但 ID 不在已处理集合里的
    return [tweet_id for tweet_id, msecs, _ in tweets
            if msecs > watermark_msecs or (msecs == watermark_msecs and tweet_id not in known_ids)]


def collect_media(tweets, user_name, watermark_msecs=None, known_ids=None):
    # 从一页已解析的推文中挑出媒体; 传入水位线时只保留"还没处理过"的推文
    # 采用 >= 水位线 + 已处理 ID 集合: 同一秒内发的多条推文不会被漏掉
    watermark_msecs = watermark_msecs or 0
    known_ids = known_ids or set()
    new_ids = set(find_new_tweet_ids(tweets, watermark_msecs, known_ids))
    media_list = []
    for tweet_id, msecs, medias in tweets:
        if tweet_id not in new_ids:
            continue
        date_str = stamp2date(msecs)
        for _media in medias:
            built = build_media(_media, tweet_id, date_str, user_name)
            if built:
                media_list.append(built)
    return media_list


########## 下载 ##########


def local_target(_media, folder, index):
    # 本地模式的目标路径: 图片按横竖分文件夹, 视频放用户目录
    sub = (PORTRAIT_DIR if _media['is_portrait'] else LANDSCAPE_DIR) if _media['is_image'] else ''
    return f'{folder}/{sub}/{index:04d}-{_media["saved_name"]}'.replace('//', '/')


def object_key(_media):
    # R2 里的对象键: {R2_PREFIX}/{用户名}/{竖屏|横屏方图}/{推文ID}-{日期}-{昵称}-img.jpg
    # 用推文 ID 而不是序号, 保证可重复执行且不会重名
    parts = [R2_PREFIX, TARGET_USER,
             (PORTRAIT_DIR if _media['is_portrait'] else LANDSCAPE_DIR) if _media['is_image'] else '',
             _media['saved_name']]
    return '/'.join(p for p in parts if p)


def content_type_for(remote_ext: str) -> str:
    return {'.jpg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
            '.mp4': 'video/mp4'}.get(remote_ext, 'application/octet-stream')


def guess_remote_ext(data: bytes, _media) -> str:
    # 追查真实图片格式: 原图是 png/webp 时保存名虽写成 .jpg, 这里把后缀纠正过来
    if not _media['is_image']:
        return '.mp4'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    return '.jpg'


def download_one(url, is_image, attempt):
    # 同步下载单个文件, attempt 用于图片原图 404 时逐级退回
    target = url
    if is_image:
        target = url + ('&' if '?' in url else '?') + ('name=orig', 'name=4096x4096', 'name=large')[attempt]
    with httpx.Client(proxy=proxies) as client:
        response = client.get(quote_url(target), timeout=(3.05, 30))
    response.raise_for_status()
    return response.content


def fetch_media_bytes(_media, retries=3):
    # 下载一份媒体, 返回 bytes; 全部失败返回 None
    for attempt in range(retries):
        try:
            return download_one(_media['url'], _media['is_image'], attempt)
        except Exception as e:
            print(f'  第 {attempt + 1} 次下载失败: {e}')
            time.sleep(2)
    return None


def download_all_local(media_list, folder, start_index, max_concurrent):
    # 本地模式: 并发下载到本地目录
    async def _main():
        semaphore = asyncio.Semaphore(max_concurrent)
        os.makedirs(f'{folder}/{PORTRAIT_DIR}', exist_ok=True)
        os.makedirs(f'{folder}/{LANDSCAPE_DIR}', exist_ok=True)

        async def down_save(_media, index):
            global down_count, portrait_count, landscape_count
            save_file = local_target(_media, folder, index)
            async with semaphore:
                for attempt in range(3):
                    try:
                        target = _media['url']
                        if _media['is_image']:
                            target = target + ('&' if '?' in target else '?') + ('name=orig', 'name=4096x4096', 'name=large')[attempt]
                        async with httpx.AsyncClient(proxy=proxies) as client:
                            response = await client.get(quote_url(target), timeout=(3.05, 30))
                        response.raise_for_status()
                        with open(save_file, 'wb') as f:
                            f.write(response.content)
                        down_count += 1
                        if _media['is_image']:
                            if _media['is_portrait']:
                                portrait_count += 1
                            else:
                                landscape_count += 1
                        print(f'[{index}] {os.path.relpath(save_file, folder)}')
                        return
                    except Exception as e:
                        print(f'{os.path.basename(save_file)} 第 {attempt + 1} 次下载失败: {e}')
                        await asyncio.sleep(2)
                print(f'[跳过] {os.path.basename(save_file)} 连续 3 次下载失败: {_media["url"]}')

        await asyncio.gather(*[asyncio.create_task(down_save(media, start_index + order))
                               for order, media in enumerate(media_list)])

    asyncio.run(_main())


########## R2 ##########


def get_r2_client():
    import boto3
    from botocore.config import Config

    for name, value in (('R2_ENDPOINT', R2_ENDPOINT), ('R2_BUCKET', R2_BUCKET),
                        ('R2_ACCESS_KEY_ID', R2_ACCESS_KEY_ID), ('R2_SECRET_ACCESS_KEY', R2_SECRET_ACCESS_KEY)):
        if not value:
            raise SystemExit(f'缺少环境变量 {name}, --sync-r2 模式需要完整的 R2 配置')
    return boto3.client('s3', endpoint_url=R2_ENDPOINT, aws_access_key_id=R2_ACCESS_KEY_ID,
                        aws_secret_access_key=R2_SECRET_ACCESS_KEY, region_name='auto',
                        config=Config(retries={'max_attempts': 5, 'mode': 'standard'}))


def load_state(s3, key):
    # 从 R2 读取同步状态; 不存在或损坏时返回空状态
    try:
        body = s3.get_object(Bucket=R2_BUCKET, Key=key)['Body'].read()
        state = json.loads(body.decode('utf-8'))
        if not isinstance(state, dict):
            raise ValueError('状态格式异常')
        state.setdefault('watermark_msecs', 0)
        state.setdefault('known_tweet_ids', [])
        state.setdefault('uploaded', 0)
        return state
    except Exception as e:
        if 'NoSuchKey' not in str(e) and '404' not in str(e):
            print(f'读取状态失败({e}), 按首次运行处理')
        return {'watermark_msecs': 0, 'known_tweet_ids': [], 'uploaded': 0, 'updated': ''}


def save_state(s3, key, state):
    state['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    s3.put_object(Bucket=R2_BUCKET, Key=key, Body=json.dumps(state, ensure_ascii=False).encode('utf-8'),
                  ContentType='application/json')


def upload_to_r2(s3, _media, data, remote_ext):
    # 上传一份媒体到 R2; 图片保存名固定 .jpg, 按真实格式替换后缀
    key = object_key(_media)
    if _media['is_image']:
        key = os.path.splitext(key)[0] + remote_ext
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=remote_ext) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        s3.upload_file(tmp_path, R2_BUCKET, key, ExtraArgs={'ContentType': content_type_for(remote_ext)})
        return key
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def r2_sync(force_full=False):
    # 增量同步: 检查是否有新图片, 有则下载并上传到 R2
    global down_count, portrait_count, landscape_count
    s3 = get_r2_client()
    state_key = '/'.join(p for p in (R2_PREFIX, 'state', f'{TARGET_USER}.json') if p)
    state = {} if force_full else load_state(s3, state_key)
    watermark = state.get('watermark_msecs', 0)
    known_ids = set(state.get('known_tweet_ids', []))
    if force_full:
        print('FORCE_FULL=1: 忽略已有状态, 重新全量扫描')

    info = get_user_info()
    if not info:
        raise SystemExit(1)
    rest_id, user_name, media_count = info
    mode = '全量回填' if not watermark else f'增量检查 (水位线 {stamp2date(watermark)})'
    print(f'昵称: {user_name}\n用户名: {TARGET_USER}\n含媒体的推文数(不含转推): {media_count}\n'
          f'桶: {R2_BUCKET}  前缀: {R2_PREFIX or "(根目录)"}\n模式: {mode}\n')

    max_pages = 0 if not watermark else max(1, FULL_SYNC_PAGES)
    cursor = None
    page = 0
    new_media = []
    new_ids = []
    max_msecs = watermark
    while True:
        items, next_cursor = get_media_page(rest_id, cursor)
        if items is None:
            break
        page += 1
        tweets, page_max = parse_page(items)
        max_msecs = max(max_msecs, page_max)
        new_ids += find_new_tweet_ids(tweets, watermark, known_ids)
        new_media += collect_media(tweets, user_name, watermark, known_ids)
        print(f'第 {page} 页: 解析 {len(tweets)} 条推文, 累计发现 {len(new_media)} 份新媒体')
        if not next_cursor or not items or (max_pages and page >= max_pages):
            break
        cursor = next_cursor

    uploaded = 0
    if not new_media:
        print('\n没有新图片, 结束')
    else:
        print(f'\n发现 {len(new_media)} 份新媒体, 开始下载并上传...')
        for order, _media in enumerate(new_media, 1):
            data = fetch_media_bytes(_media)
            if not data:
                print(f'[{order}/{len(new_media)}] 跳过 (下载失败): {_media["url"]}')
                continue
            remote_ext = guess_remote_ext(data, _media) if _media['is_image'] else '.mp4'
            key = upload_to_r2(s3, _media, data, remote_ext)
            uploaded += 1
            down_count += 1
            if _media['is_image']:
                if _media['is_portrait']:
                    portrait_count += 1
                else:
                    landscape_count += 1
            print(f'[{order}/{len(new_media)}] 已上传 {key}  ({len(data) // 1024} KB)')
        print(f'\n上传完成: {uploaded} / {len(new_media)}')

    # 更新状态: 水位线取"本页看到的全部推文时间"与旧值的较大者, ID 只保留本轮之前处理过的
    state['watermark_msecs'] = max(max_msecs, watermark)
    state['known_tweet_ids'] = (list(known_ids) + new_ids)[-MAX_KNOWN_IDS:]
    state['uploaded'] = state.get('uploaded', 0) + uploaded
    save_state(s3, state_key, state)
    print(f'状态已更新: {state_key} (水位线 {stamp2date(state["watermark_msecs"])}, 已记录 {len(state["known_tweet_ids"])} 个推文 ID)')
    print(f'\n完成: 上传 {down_count} 份 (竖屏 {portrait_count} / 横图方图 {landscape_count})')
    print(f'共调用 {request_count} 次 API')


########## 本地模式 ##########


def local_run():
    global down_count
    start = time.time()
    save_path = SAVE_PATH or os.getcwd()
    save_path = save_path.replace('\\', '/').rstrip('/')

    info = get_user_info()
    if not info:
        raise SystemExit(1)
    rest_id, user_name, media_count = info
    folder = f'{save_path}/{user_name}_{TARGET_USER}'
    os.makedirs(folder, exist_ok=True)
    print(f'昵称: {user_name}\n用户名: {TARGET_USER}\n含媒体的推文数(不含转推): {media_count}\n保存目录: {folder}\n开始下载...\n')

    cursor = None
    index = 1
    while True:
        items, next_cursor = get_media_page(rest_id, cursor)
        if items is None:
            break
        media_list, _ = collect_media(items, user_name)
        if media_list:
            if MAX_MEDIA:
                media_list = media_list[:max(0, MAX_MEDIA - index + 1)]
            download_all_local(media_list, folder, index, MAX_CONCURRENT_REQUESTS)
            index += len(media_list)
        if not next_cursor or not items or (MAX_MEDIA and index > MAX_MEDIA):
            break
        cursor = next_cursor

    print(f'\n完成: 共下载 {down_count} 份文件')
    print(f'  竖屏图片 {portrait_count} 张 -> {folder}/{PORTRAIT_DIR}/')
    print(f'  横图方图 {landscape_count} 张 -> {folder}/{LANDSCAPE_DIR}/')
    if unknown_size_count:
        print(f'  其中 {unknown_size_count} 张尺寸读取失败, 已按横图处理')
    print(f'共调用 {request_count} 次 API, 耗时 {time.time() - start:.1f} 秒')


def main():
    _headers['cookie'] = load_cookie()
    _headers['x-csrf-token'] = re.findall(r'ct0=([^;]+)', _headers['cookie'])[0]

    if '--sync-r2' in sys.argv:
        r2_sync(force_full=FORCE_FULL)
    else:
        local_run()


if __name__ == '__main__':
    main()

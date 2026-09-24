/**
 * 壁纸 API: 经 R2 binding 读取私有桶里的清单与图片, 对外只暴露带 token 的只读接口
 *
 *   GET /api/v1/users
 *   GET /api/v1/wallpapers/{username}?orientation=portrait&limit=50&offset=0&recent=0
 *   GET /api/v1/latest/{username}?orientation=portrait
 *   GET /api/v1/random/{username}?orientation=portrait&recent=0[&redirect=1]
 *   GET /api/v1/muzei/{username}?orientation=portrait&recent=30&limit=1000
 *   GET /api/v1/image/{username}/{orientation}/{id}
 *
 * 定时任务 (可选): 按 wrangler.jsonc 的 crons 调 GitHub 接口触发同步 workflow, 代替不可靠的 GitHub 定时触发;
 *       没配置 GITHUB_DISPATCH_TOKEN / GITHUB_DISPATCH_REPO 时什么都不做
 *
 * 鉴权: Authorization: Bearer <token>, 或 ?token=<token> (仅供手动测试); 不通过一律 403, 先于任何查找,
 *       因此不会透露用户或图片是否存在
 * 图片只能经由清单解析得到, 且对象键必须形如 {前缀}/{用户名}/{横竖目录}/{文件}.jpg|png|webp,
 *       所以 state/ 与 manifests/ 永远不会被当作图片返回
 */

export interface Env {
  WALLPAPER_BUCKET: R2Bucket;
  WALLPAPER_ACCESS_TOKEN?: string;
  R2_PREFIX?: string;
  PORTRAIT_DIR?: string;
  LANDSCAPE_DIR?: string;
  /** 细粒度 GitHub token, 只需对目标仓库的 Actions 读写权限 (secret) */
  GITHUB_DISPATCH_TOKEN?: string;
  /** owner/repo (secret, 让配置文件保持通用) */
  GITHUB_DISPATCH_REPO?: string;
  SYNC_WORKFLOW?: string;
  SYNC_REF?: string;
}

const API = '/api/v1/';
const ORIENTATIONS = ['portrait', 'landscape'] as const;
type Orientation = (typeof ORIENTATIONS)[number];

const USERNAME_RE = /^[A-Za-z0-9_]{1,15}$/;
const RESERVED_DIRS = new Set(['state', 'manifests']);     // 前缀下与用户目录平级的系统目录
const ID_RE = /^\d{1,20}(-\d{1,3})?$/;
const IMAGE_FILE_RE = /^[^/\\]+\.(jpe?g|png|webp)$/i;
const MAX_LIMIT = 1000;

const CACHE_JSON = 'private, max-age=60';
const CACHE_IMAGE = 'private, max-age=86400';               // 图片内容不变, 但只允许客户端自己缓存
const CACHE_NONE = 'no-store';

interface ManifestImage {
  id: string;
  key: string;
  post_id: string;
  created_at: string;
  media_index?: number;
  width?: number;
  height?: number;
}

interface Manifest {
  version: number;
  username: string;
  orientation: Orientation;
  updated_at: string;
  count: number;
  images: ManifestImage[];
}

class HttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      return await handle(request, env);
    } catch (e) {
      if (e instanceof HttpError) {
        return json({ error: e.message }, e.status, CACHE_NONE);
      }
      // 只记错误信息, 不记 URL (可能带 ?token=)
      console.error('unhandled error:', e instanceof Error ? e.message : String(e));
      return json({ error: 'internal error' }, 500, CACHE_NONE);
    }
  },

  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    // 失败时抛出, 会记在 Worker 的 Cron 事件日志里
    await dispatchSync(env);
  },
} satisfies ExportedHandler<Env>;

/** 触发同步 workflow (等同于在 Actions 页面点 Run workflow); 返回是否真的发出了请求 */
export async function dispatchSync(env: Env, fetcher: typeof fetch = fetch): Promise<boolean> {
  const token = env.GITHUB_DISPATCH_TOKEN;
  const repo = env.GITHUB_DISPATCH_REPO;
  if (!token || !repo) return false;
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) throw new Error('GITHUB_DISPATCH_REPO 格式不对, 应为 owner/repo');
  const workflow = encodeURIComponent(env.SYNC_WORKFLOW || 'sync-images.yml');
  const response = await fetcher(`https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'Content-Type': 'application/json',
      'User-Agent': 'wallpaper-worker',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    body: JSON.stringify({ ref: env.SYNC_REF || 'main' }),
  });
  if (response.status !== 204) throw new Error(`触发同步失败: HTTP ${response.status}`);    // 不记响应内容和 token
  return true;
}

async function handle(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const auth = await authenticate(request, url, env);
  if (!auth) throw new HttpError(403, 'forbidden');
  if (request.method !== 'GET') throw new HttpError(405, 'method not allowed');
  if (!url.pathname.startsWith(API)) throw new HttpError(404, 'not found');

  const [route, ...args] = url.pathname.slice(API.length).split('/');
  const params = url.searchParams;
  const arity = (n: number) => {
    if (args.length !== n) throw new HttpError(404, 'not found');
  };

  switch (route) {
    case 'users': {
      arity(0);
      const index = await readJson<{ updated_at?: string; users?: unknown[] }>(env, joinKey(prefix(env), 'manifests', 'index.json'));
      return json({ version: 1, updated_at: index?.updated_at ?? null, users: index?.users ?? [] });
    }

    case 'wallpapers': {
      arity(1);
      const { username, orientation, manifest, images } = await loadFor(env, args[0], params.get('orientation'));
      const pool = recentPool(images, intParam(params, 'recent', 0));
      const offset = intParam(params, 'offset', 0);
      const limit = Math.min(Math.max(intParam(params, 'limit', 50), 1), MAX_LIMIT);
      const page = pool.slice(offset, offset + limit);
      return json({
        username, orientation, updated_at: manifest.updated_at,
        total: pool.length, offset, count: page.length,
        images: page.map((img) => describe(img, username, orientation)),
      });
    }

    case 'latest': {
      arity(1);
      const { username, orientation, images } = await loadFor(env, args[0], params.get('orientation'));
      const [newest] = images;
      if (!newest) throw new HttpError(404, 'no images');
      return json(describe(newest, username, orientation));
    }

    case 'random': {
      arity(1);
      const { username, orientation, images } = await loadFor(env, args[0], params.get('orientation'));
      const pool = recentPool(images, intParam(params, 'recent', 0));
      if (!pool.length) throw new HttpError(404, 'no images');
      const pick = pool[crypto.getRandomValues(new Uint32Array(1))[0] % pool.length];
      const item = describe(pick, username, orientation);
      if (params.get('redirect') === '1') {
        // 用 ?token= 访问时把 token 带到跳转地址, 否则浏览器手动测试会在跳转后 403
        const location = new URL(item.url, url.origin);
        if (auth.via === 'query') location.searchParams.set('token', auth.token);
        return new Response(null, { status: 302, headers: { Location: location.toString(), 'Cache-Control': CACHE_NONE } });
      }
      return json(item, 200, CACHE_NONE);
    }

    case 'muzei': {
      arity(1);
      const { username, orientation, manifest, images, cache } = await loadFor(env, args[0], params.get('orientation'));
      const recent = intParam(params, 'recent', 30);
      const limit = Math.min(Math.max(intParam(params, 'limit', MAX_LIMIT), 1), MAX_LIMIT);
      // 全量列表有 700 多条, 生成一次要好几毫秒: 按参数记住生成好的结果, 清单变了会随缓存一起作废
      const memoKey = `${url.origin}|${recent}|${limit}`;
      let body = cache.feeds.get(memoKey);
      if (body === undefined) {
        body = JSON.stringify({
          version: 1, updated_at: manifest.updated_at, username, orientation,
          images: recentPool(images, recent).slice(0, limit).map((img) => {
            const item = describe(img, username, orientation);
            // 图片地址不带 token: 客户端下载时自己加 Authorization 头
            return { ...item, image_url: url.origin + item.url,
                     title: img.created_at.slice(0, 10), byline: `@${username}` };
          }),
        });
        cache.feeds.set(memoKey, body);
        while (cache.feeds.size > MAX_CACHED_FEEDS) cache.feeds.delete(cache.feeds.keys().next().value!);
      }
      return jsonText(body);
    }

    case 'image': {
      arity(3);
      const [user, orientationArg, id] = args;
      if (!ID_RE.test(id)) throw new HttpError(400, 'invalid image id');
      const { images } = await loadFor(env, user, orientationArg);
      const image = images.find((img) => img.id === id);
      if (!image) throw new HttpError(404, 'not found');
      return serveImage(request, env, image);
    }

    default:
      throw new HttpError(404, 'not found');
  }
}

// ---------------------------------------------------------------- 鉴权

async function authenticate(request: Request, url: URL, env: Env): Promise<{ token: string; via: 'header' | 'query' } | null> {
  const expected = env.WALLPAPER_ACCESS_TOKEN;
  if (!expected) {
    console.error('WALLPAPER_ACCESS_TOKEN 未配置, 所有请求都会被拒绝');
    return null;
  }
  const header = request.headers.get('Authorization');
  const [token, via] = header !== null
    ? [header.startsWith('Bearer ') ? header.slice(7).trim() : '', 'header' as const]
    : [url.searchParams.get('token') ?? '', 'query' as const];
  if (!token || !(await timingSafeEqual(token, expected))) return null;
  return { token, via };
}

async function timingSafeEqual(a: string, b: string): Promise<boolean> {
  // 先各自做哈希, 长度一致后再做恒定时间比较, 避免通过响应时间逐字猜 token
  const encoder = new TextEncoder();
  const [ha, hb] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(a)),
    crypto.subtle.digest('SHA-256', encoder.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(ha, hb);
}

// ---------------------------------------------------------------- 清单

function prefix(env: Env): string {
  return (env.R2_PREFIX ?? '').replace(/^\/+|\/+$/g, '');
}

function folderOf(env: Env, orientation: Orientation): string {
  return orientation === 'portrait' ? env.PORTRAIT_DIR || '竖屏' : env.LANDSCAPE_DIR || '横屏方图';
}

function joinKey(...parts: string[]): string {
  return parts.filter(Boolean).join('/');
}

async function readJson<T>(env: Env, key: string): Promise<T | null> {
  const object = await env.WALLPAPER_BUCKET.get(key);
  return object ? ((await object.json()) as T) : null;
}

async function loadFor(env: Env, usernameArg: string, orientationArg: string | null) {
  if (!USERNAME_RE.test(usernameArg) || RESERVED_DIRS.has(usernameArg)) throw new HttpError(400, 'invalid username');
  const orientation = (orientationArg ?? 'portrait') as Orientation;
  if (!ORIENTATIONS.includes(orientation)) throw new HttpError(400, 'invalid orientation');
  const key = joinKey(prefix(env), 'manifests', usernameArg, `${orientation}.json`);
  const cached = await loadManifest(env, key, usernameArg, orientation);
  if (!cached) throw new HttpError(404, 'unknown user');
  return { username: usernameArg, orientation, manifest: cached.manifest, images: cached.images, cache: cached };
}

// 解析并校验过的清单留在本 isolate 的内存里: 每次请求只用 ETag 向 R2 确认清单变没变,
// 没变时 R2 不传正文、这里也不重新解析 (完整清单 700 多条, 每次都解析会吃掉免费版大部分 CPU 额度)
interface CachedManifest {
  etag: string;
  manifest: Manifest;
  images: ManifestImage[];
  /** 生成好的 /muzei 响应, 键为 来源|recent|limit */
  feeds: Map<string, string>;
}
const manifestCache = new Map<string, CachedManifest>();
const MAX_CACHED_MANIFESTS = 32;
const MAX_CACHED_FEEDS = 8;

async function loadManifest(env: Env, key: string, username: string, orientation: Orientation): Promise<CachedManifest | null> {
  const cached = manifestCache.get(key);
  const object = await env.WALLPAPER_BUCKET.get(key, cached ? { onlyIf: { etagDoesNotMatch: cached.etag } } : undefined);
  if (!object) {
    manifestCache.delete(key);
    return null;
  }
  if (cached && !('body' in object)) return cached;         // ETag 没变
  if (!('body' in object)) return null;
  const manifest = await object.json<Manifest>();
  const entry = { etag: object.etag, manifest, images: validImages(manifest, env, username, orientation), feeds: new Map<string, string>() };
  manifestCache.delete(key);
  manifestCache.set(key, entry);
  while (manifestCache.size > MAX_CACHED_MANIFESTS) manifestCache.delete(manifestCache.keys().next().value!);
  return entry;
}

function validImages(manifest: Manifest, env: Env, username: string, orientation: Orientation): ManifestImage[] {
  // 只保留 ID 合法、且对象键确实指向"请求里的"用户与横竖目录下图片文件的条目 (不信任清单自己写的用户名)
  const base = joinKey(prefix(env), username, folderOf(env, orientation)) + '/';
  return (Array.isArray(manifest.images) ? manifest.images : []).filter((img) =>
    typeof img?.key === 'string' && ID_RE.test(img.id ?? '')
    && img.key.startsWith(base) && IMAGE_FILE_RE.test(img.key.slice(base.length)));
}

function recentPool(images: ManifestImage[], recent: number): ManifestImage[] {
  return recent > 0 ? images.slice(0, recent) : images;
}

function describe(img: ManifestImage, username: string, orientation: Orientation) {
  return {
    id: img.id,
    created_at: img.created_at,
    ...(img.width && img.height ? { width: img.width, height: img.height } : {}),
    url: `${API}image/${username}/${orientation}/${img.id}`,
    source_url: `https://x.com/${username}/status/${img.post_id}`,
  };
}

// ---------------------------------------------------------------- 响应

async function serveImage(request: Request, env: Env, image: ManifestImage): Promise<Response> {
  const object = await env.WALLPAPER_BUCKET.get(image.key, { onlyIf: request.headers });
  if (!object) throw new HttpError(404, 'not found');
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set('ETag', object.httpEtag);
  headers.set('Cache-Control', CACHE_IMAGE);
  headers.set('X-Content-Type-Options', 'nosniff');
  if (!headers.get('Content-Type')) headers.set('Content-Type', contentTypeOf(image.key));
  if (!('body' in object)) return new Response(null, { status: 304, headers });   // If-None-Match 命中
  return new Response(object.body, { headers });
}

function contentTypeOf(key: string): string {
  const ext = key.slice(key.lastIndexOf('.') + 1).toLowerCase();
  return { png: 'image/png', webp: 'image/webp' }[ext] ?? 'image/jpeg';
}

function intParam(params: URLSearchParams, name: string, fallback: number): number {
  const raw = params.get(name);
  if (raw === null || raw === '') return fallback;
  if (!/^\d{1,6}$/.test(raw)) throw new HttpError(400, `invalid ${name}`);
  return Number(raw);
}

function json(body: unknown, status = 200, cache = CACHE_JSON): Response {
  return jsonText(JSON.stringify(body), status, cache);
}

function jsonText(text: string, status = 200, cache = CACHE_JSON): Response {
  return new Response(text, {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': cache, 'X-Content-Type-Options': 'nosniff' },
  });
}

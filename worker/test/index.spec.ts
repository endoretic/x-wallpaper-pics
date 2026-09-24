import { env } from 'cloudflare:test';
import { beforeEach, describe, expect, it } from 'vitest';
import worker, { dispatchSync, type Env } from '../src/index';

const ENV = env as unknown as Env;
const TOKEN = 'test-token';
const P = 'pics';
const USER = 'testuser';
const PORTRAIT = `${P}/${USER}/竖屏`;
const LANDSCAPE = `${P}/${USER}/横屏方图`;
const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 0xff, 0xd9]);
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 9]);

const portraitImages = [
  { id: '300-1', key: `${PORTRAIT}/300-2025-09-23-昵称-img-1.jpg`, post_id: '300', created_at: '2025-09-23T08:00:00Z', media_index: 1, width: 1080, height: 1920 },
  { id: '300-2', key: `${PORTRAIT}/300-2025-09-23-昵称-img-2.png`, post_id: '300', created_at: '2025-09-23T08:00:00Z', media_index: 2 },
  { id: '200', key: `${PORTRAIT}/200-2025-09-22-昵称-img.jpg`, post_id: '200', created_at: '2025-09-22T08:00:00Z' },
  { id: '100', key: `${PORTRAIT}/100-2025-09-21-昵称-img.jpg`, post_id: '100', created_at: '2025-09-21T08:00:00Z' },
];
const landscapeImages = [
  { id: '250', key: `${LANDSCAPE}/250-2025-09-22-昵称-img.jpg`, post_id: '250', created_at: '2025-09-22T12:00:00Z' },
];

function manifest(orientation: string, images: object[], username = USER) {
  return JSON.stringify({ version: 1, username, orientation, updated_at: '2025-09-23T09:00:00Z', count: images.length, images });
}

async function call(path: string, init: RequestInit & { token?: string | null } = {}, env: Env = ENV) {
  const headers = new Headers(init.headers);
  const token = init.token === undefined ? TOKEN : init.token;
  if (token !== null) headers.set('Authorization', `Bearer ${token}`);
  return worker.fetch(new Request(`https://wallpaper.test${path}`, { ...init, headers }), env);
}

beforeEach(async () => {
  const bucket = ENV.WALLPAPER_BUCKET;
  for (const img of [...portraitImages, ...landscapeImages]) {
    const png = img.key.endsWith('.png');
    await bucket.put(img.key, png ? PNG : JPEG, { httpMetadata: { contentType: png ? 'image/png' : 'image/jpeg' } });
  }
  await bucket.put(`${P}/state/${USER}.json`, '{"watermark_msecs":1,"secret":"state"}');
  await bucket.put(`${P}/manifests/${USER}/portrait.json`, manifest('portrait', portraitImages));
  await bucket.put(`${P}/manifests/${USER}/landscape.json`, manifest('landscape', landscapeImages));
  await bucket.put(`${P}/manifests/index.json`, JSON.stringify({
    version: 1, updated_at: '2025-09-23T09:00:00Z',
    users: [{ username: USER, portrait_count: 4, landscape_count: 1, updated_at: '2025-09-23T09:00:00Z' }],
  }));
});

describe('鉴权', () => {
  it('没有 token -> 403', async () => {
    expect((await call('/api/v1/users', { token: null })).status).toBe(403);
  });

  it('错误 token -> 403, 且与路径是否存在无关', async () => {
    for (const path of ['/api/v1/users', `/api/v1/image/${USER}/portrait/100`, '/api/v1/image/nobody/portrait/1', '/whatever']) {
      const res = await call(path, { token: 'wrong' });
      expect(res.status, path).toBe(403);
      expect(await res.json()).toEqual({ error: 'forbidden' });
    }
  });

  it('非 Bearer 格式 -> 403', async () => {
    expect((await call('/api/v1/users', { token: null, headers: { Authorization: TOKEN } })).status).toBe(403);
  });

  it('正确 token (请求头 / ?token=) -> 200', async () => {
    expect((await call('/api/v1/users')).status).toBe(200);
    expect((await call(`/api/v1/users?token=${TOKEN}`, { token: null })).status).toBe(200);
  });

  it('Worker 未配置 token 时一律 403', async () => {
    const res = await call('/api/v1/users', {}, { ...ENV, WALLPAPER_ACCESS_TOKEN: undefined });
    expect(res.status).toBe(403);
  });

  it('非 GET -> 405', async () => {
    expect((await call('/api/v1/users', { method: 'POST' })).status).toBe(405);
  });
});

describe('列表接口', () => {
  it('/users 返回 index.json 里的用户', async () => {
    const body = await (await call('/api/v1/users')).json<any>();
    expect(body.users).toEqual([{ username: USER, portrait_count: 4, landscape_count: 1, updated_at: '2025-09-23T09:00:00Z' }]);
  });

  it('/wallpapers 默认竖屏, 新 -> 旧, 分页, 不暴露对象键', async () => {
    const res = await call(`/api/v1/wallpapers/${USER}?limit=2&offset=1`);
    expect(res.status).toBe(200);
    expect(res.headers.get('Cache-Control')).toBe('private, max-age=60');
    const body = await res.json<any>();
    expect(body).toMatchObject({ username: USER, orientation: 'portrait', total: 4, offset: 1, count: 2 });
    expect(body.images.map((i: any) => i.id)).toEqual(['300-2', '200']);
    expect(body.images[1]).toEqual({
      id: '200', created_at: '2025-09-22T08:00:00Z',
      url: `/api/v1/image/${USER}/portrait/200`, source_url: `https://x.com/${USER}/status/200`,
    });
    expect(JSON.stringify(body)).not.toContain('竖屏');
  });

  it('recent 只取最新 N 张; 宽高有才给', async () => {
    const body = await (await call(`/api/v1/wallpapers/${USER}?recent=2`)).json<any>();
    expect(body.total).toBe(2);
    expect(body.images[0]).toMatchObject({ id: '300-1', width: 1080, height: 1920 });
    expect(body.images[1].width).toBeUndefined();
  });

  it('/latest 返回指定横竖的最新一张', async () => {
    expect((await (await call(`/api/v1/latest/${USER}`)).json<any>()).id).toBe('300-1');
    expect((await (await call(`/api/v1/latest/${USER}?orientation=landscape`)).json<any>()).id).toBe('250');
  });

  it('/random 只从指定横竖和 recent 范围里挑', async () => {
    const portraitIds = new Set(portraitImages.map((i) => i.id));
    for (let n = 0; n < 20; n++) {
      const pick = await (await call(`/api/v1/random/${USER}?orientation=portrait&recent=2`)).json<any>();
      expect(['300-1', '300-2']).toContain(pick.id);
      expect(portraitIds.has(pick.id)).toBe(true);
    }
    const landscape = await (await call(`/api/v1/random/${USER}?orientation=landscape`)).json<any>();
    expect(landscape.id).toBe('250');
  });

  it('/random?redirect=1 跳到图片接口; ?token= 访问时跳转地址保留 token', async () => {
    const viaHeader = await call(`/api/v1/random/${USER}?recent=1&redirect=1`);
    expect(viaHeader.status).toBe(302);
    expect(viaHeader.headers.get('Location')).toBe(`https://wallpaper.test/api/v1/image/${USER}/portrait/300-1`);
    const viaQuery = await call(`/api/v1/random/${USER}?recent=1&redirect=1&token=${TOKEN}`, { token: null });
    expect(new URL(viaQuery.headers.get('Location')!).searchParams.get('token')).toBe(TOKEN);
  });

  it('/muzei 返回绝对地址 (不带 token), 默认最新 30 张', async () => {
    const body = await (await call(`/api/v1/muzei/${USER}`)).json<any>();
    expect(body).toMatchObject({ version: 1, username: USER, orientation: 'portrait' });
    expect(body.images).toHaveLength(4);
    expect(body.images[0]).toMatchObject({
      id: '300-1', title: '2025-09-23', byline: `@${USER}`,
      image_url: `https://wallpaper.test/api/v1/image/${USER}/portrait/300-1`,
      source_url: `https://x.com/${USER}/status/300`,
    });
    expect(JSON.stringify(body)).not.toContain(TOKEN);
    expect((await (await call(`/api/v1/muzei/${USER}?recent=0&limit=3`)).json<any>()).images).toHaveLength(3);
  });

  it('参数校验: 非法用户名 / 横竖 / 数字 -> 400; 未知用户 -> 404', async () => {
    expect((await call('/api/v1/wallpapers/bad-name!')).status).toBe(400);
    expect((await call('/api/v1/wallpapers/state')).status).toBe(400);
    expect((await call(`/api/v1/wallpapers/${USER}?orientation=square`)).status).toBe(400);
    expect((await call(`/api/v1/wallpapers/${USER}?limit=-1`)).status).toBe(400);
    expect((await call('/api/v1/wallpapers/nobody')).status).toBe(404);
    expect((await call('/api/v1/nothing')).status).toBe(404);
  });
});

describe('图片接口', () => {
  it('返回图片字节, 保留 MIME, 只允许私有缓存', async () => {
    const res = await call(`/api/v1/image/${USER}/portrait/300-2`);
    expect(res.status).toBe(200);
    expect(res.headers.get('Content-Type')).toBe('image/png');
    expect(res.headers.get('Cache-Control')).toBe('private, max-age=86400');
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(PNG);
  });

  it('带 If-None-Match 命中 -> 304', async () => {
    const first = await call(`/api/v1/image/${USER}/portrait/200`);
    const etag = first.headers.get('ETag')!;
    await first.arrayBuffer();
    const again = await call(`/api/v1/image/${USER}/portrait/200`, { headers: { 'If-None-Match': etag } });
    expect(again.status).toBe(304);
  });

  it('未知 ID -> 404; 横竖不对 -> 404; 非法 ID -> 400', async () => {
    expect((await call(`/api/v1/image/${USER}/portrait/999`)).status).toBe(404);
    expect((await call(`/api/v1/image/${USER}/landscape/100`)).status).toBe(404);
    expect((await call(`/api/v1/image/${USER}/portrait/abc`)).status).toBe(400);
  });

  it('不能取到 state/ 或 manifests/, 也不能直接传对象键', async () => {
    const attempts = [
      `/api/v1/image/${USER}/portrait/..%2F..%2Fstate%2F${USER}.json`,
      `/api/v1/image/state/portrait/${USER}`,
      `/api/v1/image/manifests/portrait/index`,
      `/api/v1/image/${encodeURIComponent(`${P}/state/${USER}.json`)}`,
      `/api/v1/image/${USER}/portrait/100/../../state`,
      `/${P}/state/${USER}.json`,
    ];
    for (const path of attempts) {
      const res = await call(path);
      expect([400, 404], path).toContain(res.status);
      expect(await res.text(), path).not.toContain('secret');
    }
  });

  it('被篡改的清单也不能把请求引到 state/ 或别的用户目录', async () => {
    await ENV.WALLPAPER_BUCKET.put(`${P}/manifests/${USER}/portrait.json`, manifest('portrait', [
      { id: '1', key: `${P}/state/${USER}.json`, post_id: '1', created_at: '2025-09-24T00:00:00Z' },
      { id: '2', key: `${P}/other/竖屏/2-2025-09-24-x-img.jpg`, post_id: '2', created_at: '2025-09-24T00:00:00Z' },
      { id: '3', key: `${PORTRAIT}/nested/3-2025-09-24-x-img.jpg`, post_id: '3', created_at: '2025-09-24T00:00:00Z' },
      { id: '4', key: `${PORTRAIT}/4-2025-09-24-x-vid.mp4`, post_id: '4', created_at: '2025-09-24T00:00:00Z' },
      ...portraitImages,
    ], 'other'));
    for (const id of ['1', '2', '3', '4']) {
      const res = await call(`/api/v1/image/${USER}/portrait/${id}`);
      expect(res.status, id).toBe(404);
      expect(await res.text()).not.toContain('secret');
    }
    const feed = await (await call(`/api/v1/muzei/${USER}?recent=0`)).json<any>();
    expect(feed.images.map((i: any) => i.id)).toEqual(['300-1', '300-2', '200', '100']);
  });
});

describe('清单缓存', () => {
  it('清单更新后, 下一次请求立即看到新内容', async () => {
    expect((await (await call(`/api/v1/latest/${USER}`)).json<any>()).id).toBe('300-1');
    await ENV.WALLPAPER_BUCKET.put(`${P}/manifests/${USER}/portrait.json`, manifest('portrait', [
      { id: '400', key: `${PORTRAIT}/400-2025-09-24-昵称-img.jpg`, post_id: '400', created_at: '2025-09-24T08:00:00Z' },
      ...portraitImages,
    ]));
    expect((await (await call(`/api/v1/latest/${USER}`)).json<any>()).id).toBe('400');
    expect((await (await call(`/api/v1/wallpapers/${USER}`)).json<any>()).total).toBe(5);
  });

  it('/muzei 的结果缓存也随清单更新作废', async () => {
    const first = await (await call(`/api/v1/muzei/${USER}?recent=0`)).json<any>();
    expect(first.images.map((i: any) => i.id)).toEqual(['300-1', '300-2', '200', '100']);
    await ENV.WALLPAPER_BUCKET.put(`${P}/manifests/${USER}/portrait.json`, manifest('portrait', portraitImages.slice(2)));
    const second = await (await call(`/api/v1/muzei/${USER}?recent=0`)).json<any>();
    expect(second.images.map((i: any) => i.id)).toEqual(['200', '100']);
  });

  it('清单被删除后返回 404, 不会继续用旧缓存', async () => {
    expect((await call(`/api/v1/wallpapers/${USER}`)).status).toBe(200);
    await ENV.WALLPAPER_BUCKET.delete(`${P}/manifests/${USER}/portrait.json`);
    expect((await call(`/api/v1/wallpapers/${USER}`)).status).toBe(404);
    expect((await call(`/api/v1/image/${USER}/portrait/100`)).status).toBe(404);
  });
});

describe('定时触发同步', () => {
  const calls: { url: string; init: RequestInit }[] = [];
  const fakeFetch = (status: number) => (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return new Response(null, { status });
  }) as unknown as typeof fetch;

  beforeEach(() => { calls.length = 0; });

  it('没配置 token 或仓库时什么都不做', async () => {
    expect(await dispatchSync({ ...ENV, GITHUB_DISPATCH_TOKEN: undefined, GITHUB_DISPATCH_REPO: 'o/r' }, fakeFetch(204))).toBe(false);
    expect(await dispatchSync({ ...ENV, GITHUB_DISPATCH_TOKEN: 't', GITHUB_DISPATCH_REPO: undefined }, fakeFetch(204))).toBe(false);
    expect(calls).toHaveLength(0);
  });

  it('按 GitHub workflow_dispatch 接口发请求', async () => {
    expect(await dispatchSync({ ...ENV, GITHUB_DISPATCH_TOKEN: 'gh-token', GITHUB_DISPATCH_REPO: 'owner/repo' }, fakeFetch(204))).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('https://api.github.com/repos/owner/repo/actions/workflows/sync-images.yml/dispatches');
    expect(calls[0].init.method).toBe('POST');
    expect((calls[0].init.headers as Record<string, string>).Authorization).toBe('Bearer gh-token');
    expect(JSON.parse(calls[0].init.body as string)).toEqual({ ref: 'main' });
  });

  it('GitHub 返回非 204 时报错, 且错误信息里没有 token', async () => {
    await expect(dispatchSync({ ...ENV, GITHUB_DISPATCH_TOKEN: 'gh-token', GITHUB_DISPATCH_REPO: 'owner/repo' }, fakeFetch(401)))
      .rejects.toThrow(/HTTP 401/);
    await expect(dispatchSync({ ...ENV, GITHUB_DISPATCH_TOKEN: 'gh-token', GITHUB_DISPATCH_REPO: 'owner/repo' }, fakeFetch(401)))
      .rejects.not.toThrow(/gh-token/);
  });

  it('仓库名格式不对时拒绝发请求', async () => {
    await expect(dispatchSync({ ...ENV, GITHUB_DISPATCH_TOKEN: 't', GITHUB_DISPATCH_REPO: 'owner/repo/../x' }, fakeFetch(204)))
      .rejects.toThrow(/owner\/repo/);
    expect(calls).toHaveLength(0);
  });
});

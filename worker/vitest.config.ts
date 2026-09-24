import { cloudflareTest } from '@cloudflare/vitest-pool-workers';
import { defineConfig } from 'vitest/config';

// 测试跑在本地 workerd 里, R2 用 Miniflare 模拟, 不连线上桶
export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: './wrangler.jsonc' },
      miniflare: { bindings: { WALLPAPER_ACCESS_TOKEN: 'test-token' } },
    }),
  ],
});

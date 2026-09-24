# 壁纸 API（Cloudflare Worker）

通过 R2 binding 读取私有桶里的壁纸清单和图片，对外只提供带 token 的只读接口。手机端（Muzei 插件等）只和这个 Worker 打交道，拿不到任何 R2、GitHub 或 Cloudflare 凭据。

清单由仓库根目录的抓取任务生成（见主 README 的「壁纸清单」一节），Worker 只读，不写桶。

## 部署

```powershell
cd worker
npm ci
npx wrangler login                                   # 浏览器里授权一次
# 按需修改 wrangler.jsonc: bucket_name 换成你的桶, vars 与抓取端的 R2_PREFIX / 目录名保持一致
npx wrangler deploy
npx wrangler secret put WALLPAPER_ACCESS_TOKEN       # 粘贴一个足够长的随机字符串
```

- 没配置 `WALLPAPER_ACCESS_TOKEN` 时 Worker 拒绝所有请求，所以先部署再设 token 也不会有暴露窗口
- token 只存在 Cloudflare 的 secret 里，不要写进仓库或 `wrangler.jsonc`
- 本地开发（`npx wrangler dev`）用的 token 写在 `worker/.dev.vars`（已被 gitignore）：`WALLPAPER_ACCESS_TOKEN=...`

## 可选：用 Worker 定时触发同步

GitHub Actions 的定时触发是尽力而为，可能大幅延迟甚至一直不触发。Worker 的 Cron Trigger（`wrangler.jsonc` 里的 `triggers.crons`，默认每小时第 23 分钟）可以代为调用 GitHub 接口触发 `sync-images` workflow，效果等同于在 Actions 页面手动点 Run workflow。

1. GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → Generate new token
   - Repository access：**Only select repositories**，只选这个仓库
   - Permissions → Repository permissions → **Actions: Read and write**（其余保持 No access）
2. 把 token 和仓库名存成 Worker 的 secret：

```powershell
npx wrangler secret put GITHUB_DISPATCH_TOKEN      # 粘贴上一步的 token
npx wrangler secret put GITHUB_DISPATCH_REPO       # 例如 owner/repo
```

- 没配置这两个 secret 时，定时任务什么都不做
- token 只能触发 / 取消这个仓库的 workflow，读不到仓库的 Secrets
- 触发失败（例如 token 过期）会记在 Cloudflare 控制台该 Worker 的 Cron 事件日志里
- 可选变量：`SYNC_WORKFLOW`（默认 `sync-images.yml`）、`SYNC_REF`（默认 `main`）

## 接口

所有请求都要带 `Authorization: Bearer <token>`（手动测试时也可以用 `?token=<token>`，但它会出现在浏览器历史和各种日志里，日常不要用）。鉴权失败一律 `403`，不透露用户或图片是否存在。

| 接口 | 说明 |
| --- | --- |
| `GET /api/v1/users` | 所有用户及竖屏 / 横屏方图张数 |
| `GET /api/v1/wallpapers/{用户名}` | 图片列表，新 → 旧；参数 `orientation`、`limit`（默认 50，最大 1000）、`offset`、`recent` |
| `GET /api/v1/latest/{用户名}` | 最新一张；参数 `orientation` |
| `GET /api/v1/random/{用户名}` | 随机一张；参数 `orientation`、`recent`，加 `redirect=1` 直接跳转到图片 |
| `GET /api/v1/muzei/{用户名}` | 给壁纸客户端的列表（绝对地址、标题、署名）；参数 `orientation`、`recent`（默认 30，`0` = 全部）、`limit` |
| `GET /api/v1/image/{用户名}/{orientation}/{id}` | 图片本体，保留原 MIME，支持 `If-None-Match` |

- `orientation`：`portrait`（默认）或 `landscape`
- `recent`：只在最新 N 张里取，`0` = 全部
- 图片地址由列表接口给出，客户端不需要自己拼，也不需要知道桶内目录结构
- 图片只能经由清单找到，且对象键必须位于 `{前缀}/{用户名}/{竖屏|横屏方图}/` 下，`state/`、`manifests/` 不会被当作图片返回

示例：

```bash
curl -H "Authorization: Bearer $TOKEN" https://<你的 worker 域名>/api/v1/users
curl -H "Authorization: Bearer $TOKEN" "https://<你的 worker 域名>/api/v1/muzei/<用户名>?recent=30"
curl -H "Authorization: Bearer $TOKEN" -o wall.jpg "https://<你的 worker 域名>/api/v1/random/<用户名>?redirect=1" -L
```

## 测试

```powershell
npm test          # 本地 workerd + 模拟 R2，不连线上桶
npm run typecheck
```

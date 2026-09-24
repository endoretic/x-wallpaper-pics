# X图片定向下载

- 走 `UserMedia` 接口，该接口本身不含转推，也无需 `X-Client-Transaction-ID`
- 竖屏判定：`height > width`（正方形算横图方图），优先用接口返回的 `original_info`，缺失时读图片头兜底
- 图片按原图下载（`name=orig`，404 时自动退回 `4096x4096`、`large`），并按真实格式纠正 `.jpg/.png/.webp` 后缀
- 增量靠 R2 上的水位线状态文件，**没有新图片时零下载、零上传**
- 所有配置来自环境变量，仓库里不保存任何真实密钥

目录 / 对象键结构：

```
{R2_PREFIX}/{用户名}/竖屏/{推文ID}-{日期}-{昵称}-img.jpg
{R2_PREFIX}/{用户名}/横屏方图/{推文ID}-{日期}-{昵称}-img.jpg
{R2_PREFIX}/state/{用户名}.json          # 增量状态
{R2_PREFIX}/manifests/index.json                 # 壁纸清单: 所有用户及张数
{R2_PREFIX}/manifests/{用户名}/portrait.json      # 竖屏清单, 新 -> 旧
{R2_PREFIX}/manifests/{用户名}/landscape.json     # 横屏方图清单, 新 -> 旧
```

用**推文 ID** 命名，保证脚本可重复执行、不会重名或错位。一条推文有多张图时追加 `-1` / `-2`，否则它们会算出同一个键、上传时互相覆盖：

```
单图推文: 1111111111111111111-2026-09-23-昵称-img.jpg
多图推文: 2222222222222222222-2026-04-26-昵称-img-1.jpg
          2222222222222222222-2026-04-26-昵称-img-2.jpg
```

本仓库基于 [caolvchong-top/twitter_download](https://github.com/caolvchong-top/twitter_download)（MIT）精简改写。

---

## 快速开始（本地）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 填配置: 编辑仓库根目录的 .env (空模板, 已被 .gitignore 忽略)
#   TARGET_USER=目标用户名
#   X_COOKIE=auth_token=...; ct0=...;

.\.venv\Scripts\python.exe main.py     # 结果落在 ./{昵称}_{用户名}/
```

配置有两条通道，最终都落到环境变量：

| | 本地 | CI（GitHub Actions） |
| --- | --- | --- |
| 来源 | 仓库根目录的 `.env`，启动时自动加载 | repository **secrets** / **variables** |
| 由谁注入 | `python-dotenv`（`load_env_file()`） | workflow 里的 `env:` 段 |
| `.env` 是否存在 | 有（git 忽略，不进仓库） | **没有** —— CI checkout 后不存在该文件，加载逻辑静默跳过 |

必填项缺失时**启动即报错**。

自检（完全离线，用假 httpx + 假 S3，不碰网络和密钥）：

```powershell
.\.venv\Scripts\python.exe selftest.py
```

## 配置项

全部通过环境变量传入；本地写进 `.env`，CI 写进 Secrets / Variables；完整模板见 `.env.example`。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `TARGET_USER` | 是 | 目标用户名（`@` 后面那串），一次只支持一个 |
| `X_COOKIE` | 是 | `auth_token=...; ct0=...;`，整行 cookie 直接粘也行，只会取这两项。cookie 的唯一来源（本地 = `.env`，CI = Secret） |
| `ENV_FILE` | 否 | `.env` 文件名，默认 `.env`（仅影响本地） |
| `SAVE_PATH` | 否 | 本地模式保存目录，留空 = 脚本所在目录 |
| `HAS_VIDEO` | 否 | `1` = 连视频一起下（视频不进横竖目录） |
| `MAX_MEDIA` | 否 | 本地模式单次上限，`0` = 不限 |
| `MAX_CONCURRENT_REQUESTS` | 否 | 并发下载数，默认 `8` |
| `PROXY` | 否 | 如 `http://127.0.0.1:7890` |
| `R2_ENDPOINT` | CI 必填 | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | CI 必填 | 桶名 |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | CI 必填 | R2 API 令牌 |
| `R2_PREFIX` | 否 | 桶内根前缀，留空 = 放桶根目录 |
| `MAX_KNOWN_IDS` | 否 | 状态里记录多少条已处理推文 ID，默认 `1000` |
| `FULL_SYNC_PAGES` | 否 | 首次 / `FORCE_FULL=1` 时最多翻几页历史（每页最多 500 条推文），默认 `1` |
| `FORCE_FULL` | 否 | `1` = 忽略已有状态重新全量扫描 |
| `PORTRAIT_DIR` / `LANDSCAPE_DIR` | 否 | 两个子目录名，默认 `竖屏` / `横屏方图` |

---

## 部署到 GitHub Actions + Cloudflare R2

### 1. 建 R2 桶和 API 令牌

1. Cloudflare 控制台 → **R2** → **Create bucket**，例如 `wallpaper-pics`（保持私有，不要开 Public access）
2. 记下 **Account ID**，endpoint 形如 `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
3. R2 → **Manage R2 API Tokens** → **Create API token** → 权限选 **Object Read & Write**，建议限定到这一个桶
4. 保存返回的 **Access Key ID** 与 **Secret Access Key**（Secret 只显示一次）

> 也支持手动 `aws s3api` / rclone 验证，但本工具只用 boto3 的 `upload_file` / `put_object` / `get_object`。

### 2. 建 GitHub private repo 并推送

```bash
git init && git add -A && git commit -m "init"
gh repo create <你的仓库名> --private --source=. --push
```

推之前先确认没有密钥被带上去：

```bash
git status --short          # .env 不应出现在列表里
git ls-files | findstr /i "cookie env"    # 只应看到 *.example
```

### 3. 配置 Actions 变量与密钥

**Settings → Secrets and variables → Actions**

Secrets：

| 名称 | 值 |
| --- | --- |
| `X_COOKIE` | `auth_token=...; ct0=...;` |
| `R2_ENDPOINT` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | `wallpaper-pics` |
| `R2_ACCESS_KEY_ID` | 上一步的 Access Key ID |
| `R2_SECRET_ACCESS_KEY` | 上一步的 Secret Access Key |

Variables：

| 名称 | 值 |
| --- | --- |
| `TARGET_USER` | 目标用户名 |
| `R2_PREFIX` | 例如 `pic`，留空则放桶根 |

### 4. 跑起来

- 工作流 `.github/workflows/sync-images.yml` 默认每小时第 7 分钟跑一次（cron 是 UTC）
- **首次运行**：手动触发一次 `sync-images`（Actions → sync-images → Run workflow），这样会立刻回填最近一页历史图片
- 想一次性回填更多：把 `FULL_SYNC_PAGES` 调大（例如 `20` ≈ 最多 1 万条推文），或在手动触发时勾选 **force_full**
- 只有仓库有活动时定时任务才会被调度；长期无提交的仓库 GitHub 可能暂停定时任务，届时手动触发一次即可恢复

### 5. 增量是怎么判断的

状态存在 `{R2_PREFIX}/state/{用户名}.json`：

```json
{
  "watermark_msecs": 1700000300000,
  "known_tweet_ids": ["1001", "1002", "1003"],
  "uploaded": 3,
  "updated": "2026-09-24 10:24:44"
}
```

- 每轮只请求**第一页**最新的 [媒体] 内容，凡是推文时间早于水位线的直接跳过 → **没有新图片时不下载任何文件**
- 同一秒可能发多条推文，所以判定是「时间 > 水位线」**或**「时间 == 水位线且推文 ID 未记录过」，`known_tweet_ids` 就是补这个漏的
- 对象键里带推文 ID，重复执行只会覆盖同名对象，不会产生重复图片

### 6. 壁纸清单（manifest）

清单是给壁纸客户端用的图片索引，只存对象键、推文 ID 和发帖时间（由推文 ID 推出），不存任何 URL，也不需要重新下载图片。条目 ID 由文件名推出，重复生成也不会变。

按桶里现有的图片重建全部清单（迁移或恢复时用；只写 `manifests/`，不动图片和 `state/`）：

```powershell
.\.venv\Scripts\python.exe scripts\rebuild_manifests.py --dry-run   # 先看检测结果
.\.venv\Scripts\python.exe scripts\rebuild_manifests.py             # 写入
```

请在 workflow 空闲时执行，避免与 CI 同时写清单。

### 7. 注意

- 定时任务在整点高峰期可能延迟几分钟到几十分钟，属正常
- Cookie 会过期（改密码、登出全部设备、长期不用）。失效后 workflow 会在「获取用户信息失败」处报错，重新导出 cookie 更新 Secret 即可
- 私有桶取图需要走 S3 API 或 Cloudflare 的签名 URL；如需公开访问再单独配 R2 自定义域名，本工具不依赖这一点
- 建议用小号 cookie 跑，避免主号被风控

## 可选：壁纸 API（Cloudflare Worker）

[`worker/`](worker/) 是一个只读的 Cloudflare Worker：通过 R2 binding 读清单和图片，对外提供带 token 的接口，供手机壁纸客户端（如 Muzei 插件）使用。R2 桶保持私有，手机端拿不到任何存储凭据。部署步骤与接口说明见 [worker/README.md](worker/README.md)。

## 可选：手动用 AWS CLI / rclone 核对桶内容

```bash
aws s3 ls s3://wallpaper-pics/pic/ --recursive --endpoint-url https://<ACCOUNT_ID>.r2.cloudflarestorage.com
# 或
rclone lsf r2:wallpaper-pics/pic/
```

## 免责声明

1. 本项目仅供编程学习交流、学术研究及个人练习使用。
2. 使用本工具下载的所有媒体内容（图片、视频等）的知识产权均归原作者及所属平台所有，请尊重相关版权。
3. 请勿将本工具及所获取的数据用于恶意抓取、侵权传播或其他违法用途。
4. 开发者不对任何因不当使用本工具而导致的违规行为、法律纠纷或直接/间接损失承担责任。

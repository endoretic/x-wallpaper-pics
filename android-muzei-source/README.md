# X 壁纸源（Muzei 插件）

一个很小的安卓应用，作为 [Muzei](https://github.com/muzei/muzei) 的图片来源（Muzei Source）：从你自己部署的壁纸 Worker（见 [worker/README.md](../worker/README.md)）拉取图片列表，交给 Muzei 轮换。

- 只和 Worker 的 `/api/v1` 接口打交道，不需要知道桶内目录结构
- 手机上只保存 Worker 地址和访问 token，没有任何 R2、GitHub 或 Cloudflare 凭据
- Worker 地址和 token 装好后在手机上填写，APK 里不写死任何地址，所以任何人用这个仓库部署的 Worker 都能用同一个 APK
- 轮换节奏、「下一张」等都交给 Muzei 自己处理；插件只负责提供图片池

## 安装与配置

1. 装好 Muzei（Google Play 或 F-Droid）
2. 安装本插件的 APK（见下文「获取 APK」）
3. 在 Muzei 里选择图片来源「X 壁纸源」，首次启用会打开设置页：
   - **Worker 地址**：例如 `https://wallpaper.example.com`，只接受 https
   - **访问 token**：Worker 的 `WALLPAPER_ACCESS_TOKEN`
   - **X 用户名**：点「测试连接」会列出 Worker 上已有的用户；只有一个时自动填入
   - **方向**：竖屏，或横屏 / 方图
   - **轮换范围**：最新 10 / 30 / 100 张，或全部
4. 点「保存」。插件会在后台拉取列表，之后每 12 小时刷新一次；Muzei 把现有图片轮完时也会触发刷新

设置页也可以从桌面图标直接打开。改了方向或轮换范围后，旧图会被移出轮换池。

### 显示方式

Muzei 自己的铺法是按高度铺满屏幕，比屏幕宽的部分跟着桌面滑动平移（很多桌面已不再滑动壁纸，于是露出哪一段不受控制）。设置页的「显示方式」可以改为由插件按手机屏幕分辨率先排好，再交给 Muzei：

| 方式 | 效果 | 位置滑条 |
| --- | --- | --- |
| 不处理（默认） | Muzei 原本的效果，左右随桌面滑动 | 不起作用 |
| 覆盖 | 等比铺满屏幕，多出的部分按位置裁掉 | 决定保留哪一段 |
| 填充 | 整张图完整显示，空白处留黑边 | 决定图片摆在哪 |
| 居中 | 原始像素大小，不缩放；比屏幕大时按位置裁，比屏幕小时按位置摆放 | 两者都起作用 |
| 拉伸 | 拉成屏幕大小，会变形 | 不起作用 |

选了「不处理」以外的方式后，桌面翻页时壁纸不再平移。改了方式或位置并保存后，Muzei 会换成按新设置排好的图。

### 本地原图

下载过的原图会在插件自己的私有目录里存一份（上限 256 MB，超出时先删最久没用的）。Muzei 自带的缓存放在系统缓存目录，可能被系统或清理软件清掉，也最多只保留最近显示过的 100 张；有了这份原图，重新显示或改显示方式时都不必再从 Worker 下载。

### 网络不稳时

- 某张图从 Worker 下载失败后，接下来 30 秒内本地没有原图的图片都直接失败，不再逐张等待连接超时（网络不通时 Muzei 会把图片池里的图挨个试一遍）
- 拉取列表失败时，从 30 秒起线性递增重试，最多 5 次；之后交给 Muzei 下一次请求加载或 12 小时的定时刷新
- Muzei 请求加载时，如果插件里没有图或列表超过 1 小时没刷新，会立即重新拉取，不会被还在退避等待的旧任务卡住；刚刷新过且有图时什么都不做，不会每换一张壁纸都请求一次 Worker

## 获取 APK

APK 由 GitHub Actions 打包（[android-build.yml](../.github/workflows/android-build.yml)），本地不需要安装 Android Studio：

- `android-muzei-source/` 有改动时自动运行，也可以在 Actions 页面手动触发
- **main 分支**上签名核对通过的包会发布到仓库的 **Releases**（`muzei-source-v0.1.N`），永久保存，不登录也能下载，手机浏览器可以直接打开
- 其他分支和 PR 只打包不发布，APK 在运行记录页面底部的 **Artifacts** 里，受仓库的产物保留天数限制
- 用 `adb install -r <apk>` 安装，或在手机上下载后点开安装
- 每个 Release 的说明里都附有签名证书的 SHA-256，可用 `apksigner verify --print-certs` 核对

### 签名

安卓要求新版 APK 与已安装的旧版使用同一个签名密钥才能覆盖安装。在仓库 **Settings → Secrets and variables → Actions** 里配置：

| Secret | 内容 |
| --- | --- |
| `ANDROID_KEYSTORE_BASE64` | keystore 文件的 base64 |
| `ANDROID_KEYSTORE_PASSWORD` | keystore 密码（keystore 与密钥用同一个密码） |

密钥别名默认为 `release`。生成方法（JDK 自带 `keytool`）：

```bash
keytool -genkeypair -keystore release.jks -storetype PKCS12 -alias release \
  -keyalg RSA -keysize 4096 -validity 36500 -dname "CN=wallpaper source"
base64 -w0 release.jks     # 结果填进 ANDROID_KEYSTORE_BASE64
```

没有配置这两个 Secret 时，CI 只打 debug 包（每次签名都不同，更新时需要先卸载旧版）。keystore 请自己备份好：丢了就只能卸载重装。

## 本地打包（可选）

需要 JDK 17、Android SDK（compileSdk 36）和 Gradle 9.7：

```bash
gradle -p android-muzei-source testDebugUnitTest assembleDebug
```

## 测试

`app/src/test/` 下是纯 JVM 单元测试，覆盖接口响应解析、坏条目跳过、token 稳定性、横竖与轮换范围参数、错误提示，以及 token 只发给配置的 Worker 地址。CI 每次打包前都会运行。

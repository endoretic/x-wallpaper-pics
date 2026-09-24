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

## 获取 APK

APK 由 GitHub Actions 打包（[android-build.yml](../.github/workflows/android-build.yml)），本地不需要安装 Android Studio：

- `android-muzei-source/` 有改动时自动运行，也可以在 Actions 页面手动触发
- 打好的 APK 在运行记录页面底部的 **Artifacts**（`wallpaper-source-apk`）里下载
- 用 `adb install -r <apk>` 安装，或把 APK 传到手机上点开安装

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

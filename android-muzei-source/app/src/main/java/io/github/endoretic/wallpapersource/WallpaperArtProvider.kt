package io.github.endoretic.wallpapersource

import android.provider.BaseColumns
import com.google.android.apps.muzei.api.provider.Artwork
import com.google.android.apps.muzei.api.provider.MuzeiArtProvider
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.io.InputStream

/**
 * Muzei 的图片来源: 列表由 RefreshWorker 在后台拉取, 轮换节奏交给 Muzei 自己控制
 */
class WallpaperArtProvider : MuzeiArtProvider() {

    override fun onLoadRequested(initial: Boolean) {
        // Muzei 首次启用、图片池轮完或找不到可用的图时调用; 可能在离线时被调用, 所以交给 WorkManager 在联网后执行
        // 没有图 / 列表过期时替换掉可能正在退避等待的旧任务, 立即拉取 (否则会一直等到退避结束, 表现为"点下一张没反应");
        // 刚刷新过且有图时什么都不做, 省得每换一张壁纸都请求一次 Worker
        val context = context ?: return
        val hasArtwork = query(contentUri, arrayOf(BaseColumns._ID), null, null, null).use { it.count > 0 }
        val lastRefreshAt = Settings(context).lastRefreshAt
        if (RefreshPolicy.shouldRefreshNow(initial, hasArtwork, lastRefreshAt, System.currentTimeMillis())) {
            RefreshWorker.enqueue(context, replace = true)
        }
        RefreshWorker.schedulePeriodic(context)
    }

    override fun getDescription(): String {
        val context = context ?: return ""
        val config = Settings(context).config() ?: return context.getString(R.string.not_configured)
        val orientation = context.getString(
            if (config.orientation == WallpaperApi.LANDSCAPE) R.string.orientation_landscape else R.string.orientation_portrait)
        val pool = if (config.recent > 0) context.getString(R.string.pool_recent, config.recent)
        else context.getString(R.string.pool_all)
        val display = Settings(context).displaySpec()?.let { " · ${context.getString(SettingsActivity.modeLabel(it.mode))}" } ?: ""
        return "@${config.username} · $orientation · $pool$display"
    }

    /**
     * Muzei 缓存里没有这张图时调用
     * 先查本地原图 (ImageStore), 没有才带上 token 去请求 Worker (默认实现不带认证头), 下载后存一份;
     * 设置了显示方式时, 再排进与屏幕同尺寸的画布交给 Muzei (方式记在这张图的 metadata 里)
     */
    @Throws(IOException::class)
    override fun openFile(artwork: Artwork): InputStream {
        val context = context ?: throw IOException("provider 尚未初始化")
        val config = Settings(context).config() ?: throw IOException("尚未配置 Worker 地址与 token")
        val imageUrl = artwork.persistentUri?.toString() ?: throw IllegalStateException("图片没有地址")
        val store = ImageStore(File(context.noBackupFilesDir, "originals"))
        val key = ImageStore.keyFor(artwork.token, imageUrl)
        val bytes = store.get(key) ?: download(config, imageUrl).also { store.put(key, it) }
        val spec = DisplaySpec.parse(artwork.metadata) ?: return ByteArrayInputStream(bytes)
        val (width, height) = ImageFitter.portraitScreenSize(context)
        return ByteArrayInputStream(ImageFitter.render(bytes, spec, width, height))
    }

    /** 从 Worker 下载原图; 刚失败过时先暂停一会儿, 其余图片直接失败, 不再逐张等连接超时 */
    private fun download(config: SourceConfig, imageUrl: String): ByteArray {
        if (breaker.isPaused(System.currentTimeMillis())) throw IOException("刚才连不上 Worker, 暂停下载片刻")
        return try {
            WallpaperApi.openImage(config, imageUrl).use { it.readBytes() }.also { breaker.onSuccess() }
        } catch (e: IOException) {
            breaker.onFailure(System.currentTimeMillis())
            throw e
        }
    }

    companion object {
        private val breaker = NetworkBreaker()
    }
}

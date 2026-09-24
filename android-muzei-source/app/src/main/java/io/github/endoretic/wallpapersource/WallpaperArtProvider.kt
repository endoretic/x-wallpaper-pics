package io.github.endoretic.wallpapersource

import com.google.android.apps.muzei.api.provider.Artwork
import com.google.android.apps.muzei.api.provider.MuzeiArtProvider
import java.io.IOException
import java.io.InputStream

/**
 * Muzei 的图片来源: 列表由 RefreshWorker 在后台拉取, 轮换节奏交给 Muzei 自己控制
 */
class WallpaperArtProvider : MuzeiArtProvider() {

    override fun onLoadRequested(initial: Boolean) {
        // Muzei 把现有图片轮完 (或首次启用) 时调用; 可能在离线时被调用, 所以交给 WorkManager 在联网后执行
        val context = context ?: return
        RefreshWorker.enqueue(context, replace = false)
        RefreshWorker.schedulePeriodic(context)
    }

    override fun getDescription(): String {
        val context = context ?: return ""
        val config = Settings(context).config() ?: return context.getString(R.string.not_configured)
        val orientation = context.getString(
            if (config.orientation == WallpaperApi.LANDSCAPE) R.string.orientation_landscape else R.string.orientation_portrait)
        val pool = if (config.recent > 0) context.getString(R.string.pool_recent, config.recent)
        else context.getString(R.string.pool_all)
        return "@${config.username} · $orientation · $pool"
    }

    /** 默认实现下载图片时不带认证头; 这里改成带上 token 去请求 Worker */
    @Throws(IOException::class)
    override fun openFile(artwork: Artwork): InputStream {
        val context = context ?: throw IOException("provider 尚未初始化")
        val config = Settings(context).config() ?: throw IOException("尚未配置 Worker 地址与 token")
        val imageUrl = artwork.persistentUri?.toString() ?: throw IllegalStateException("图片没有地址")
        return WallpaperApi.openImage(config, imageUrl)
    }
}

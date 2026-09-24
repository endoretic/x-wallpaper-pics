package io.github.endoretic.wallpapersource

import android.content.Context

/**
 * 设置存在应用私有的 SharedPreferences 里 (其他应用读不到, 且已排除在备份/换机迁移之外)
 * 手机上只有权限很低的 Worker 访问 token, 没有任何 R2 / GitHub / Cloudflare 凭据
 */
class Settings(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("source", Context.MODE_PRIVATE)

    val baseUrl: String get() = prefs.getString(KEY_BASE_URL, "") ?: ""
    val accessToken: String get() = prefs.getString(KEY_TOKEN, "") ?: ""
    val username: String get() = prefs.getString(KEY_USERNAME, "") ?: ""
    val orientation: String get() = prefs.getString(KEY_ORIENTATION, WallpaperApi.PORTRAIT) ?: WallpaperApi.PORTRAIT
    val recent: Int get() = prefs.getInt(KEY_RECENT, DEFAULT_RECENT)

    /** 配置完整时返回可用的配置, 否则 null */
    fun config(): SourceConfig? {
        val base = WallpaperApi.normalizeBaseUrl(baseUrl) ?: return null
        if (accessToken.isEmpty() || !WallpaperApi.isValidUsername(username)) return null
        return SourceConfig(base, accessToken, username, orientation, recent)
    }

    fun save(config: SourceConfig) {
        prefs.edit()
            .putString(KEY_BASE_URL, config.baseUrl)
            .putString(KEY_TOKEN, config.accessToken)
            .putString(KEY_USERNAME, config.username)
            .putString(KEY_ORIENTATION, config.orientation)
            .putInt(KEY_RECENT, config.recent)
            .apply()
    }

    companion object {
        const val DEFAULT_RECENT = 30
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_TOKEN = "access_token"
        private const val KEY_USERNAME = "username"
        private const val KEY_ORIENTATION = "orientation"
        private const val KEY_RECENT = "recent"
    }
}

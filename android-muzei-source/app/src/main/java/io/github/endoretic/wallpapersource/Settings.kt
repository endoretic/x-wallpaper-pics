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

    /** 显示方式: null = 不处理, 交给 Muzei 自己铺 */
    val displayMode: FitMode? get() = FitMode.fromKey(prefs.getString(KEY_DISPLAY_MODE, null))
    val positionX: Int get() = prefs.getInt(KEY_POSITION_X, DisplaySpec.CENTER).coerceIn(0, 100)
    val positionY: Int get() = prefs.getInt(KEY_POSITION_Y, DisplaySpec.CENTER).coerceIn(0, 100)

    fun displaySpec(): DisplaySpec? = displayMode?.let { DisplaySpec(it, positionX, positionY) }

    fun save(config: SourceConfig, displayMode: FitMode?, positionX: Int, positionY: Int) {
        prefs.edit()
            .putString(KEY_BASE_URL, config.baseUrl)
            .putString(KEY_TOKEN, config.accessToken)
            .putString(KEY_USERNAME, config.username)
            .putString(KEY_ORIENTATION, config.orientation)
            .putInt(KEY_RECENT, config.recent)
            .putString(KEY_DISPLAY_MODE, displayMode?.key)
            .putInt(KEY_POSITION_X, positionX.coerceIn(0, 100))
            .putInt(KEY_POSITION_Y, positionY.coerceIn(0, 100))
            .apply()
    }

    companion object {
        const val DEFAULT_RECENT = 30
        private const val KEY_DISPLAY_MODE = "display_mode"
        private const val KEY_POSITION_X = "position_x"
        private const val KEY_POSITION_Y = "position_y"
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_TOKEN = "access_token"
        private const val KEY_USERNAME = "username"
        private const val KEY_ORIENTATION = "orientation"
        private const val KEY_RECENT = "recent"
    }
}

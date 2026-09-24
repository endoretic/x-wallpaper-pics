package io.github.endoretic.wallpapersource

import org.json.JSONObject
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URI
import java.net.URISyntaxException
import java.net.URL
import java.net.UnknownHostException
import javax.net.ssl.SSLException

/**
 * 与壁纸 Worker 通信: 只认 Worker 的 /api/v1 接口, 不需要知道桶内目录结构
 *
 * 纯 Kotlin + org.json, 不依赖 Android 组件, 方便在 JVM 上做单元测试
 * token 只会发给用户配置的 Worker 地址 (同源校验), 不会被带到别的主机
 */
object WallpaperApi {
    const val PORTRAIT = "portrait"
    const val LANDSCAPE = "landscape"

    private val USERNAME_RE = Regex("^[A-Za-z0-9_]{1,15}$")
    private val IMAGE_ID_RE = Regex("^\\d{1,20}(-\\d{1,3})?$")
    private const val CONNECT_TIMEOUT_MS = 15_000
    private const val READ_TIMEOUT_MS = 30_000

    /** Worker 返回了非 2xx 状态码; 继承 IOException, Muzei 会把它当作可重试的错误 */
    class ApiException(val status: Int, message: String) : IOException(message)

    /** 规范化用户填的 Worker 地址: 补 https://, 去掉末尾斜杠; 不是合法的 https 地址返回 null */
    fun normalizeBaseUrl(raw: String): String? {
        var text = raw.trim().trimEnd('/')
        if (text.isEmpty()) return null
        if (!text.contains("://")) text = "https://$text"
        val uri = try {
            URI(text)
        } catch (e: URISyntaxException) {
            return null
        }
        if (!uri.scheme.equals("https", ignoreCase = true) || uri.host.isNullOrEmpty()
            || uri.rawQuery != null || uri.rawFragment != null || uri.rawUserInfo != null) return null
        val path = (uri.rawPath ?: "").trimEnd('/')
        return "https://${uri.rawAuthority}$path"
    }

    fun isValidUsername(username: String): Boolean = USERNAME_RE.matches(username)

    fun usersUrl(baseUrl: String): String = "$baseUrl/api/v1/users"

    fun feedUrl(config: SourceConfig): String =
        "${config.baseUrl}/api/v1/muzei/${config.username}?orientation=${config.orientation}&recent=${config.recent}"

    /** url 是否与 Worker 地址同源 (https + 同主机 + 同端口); 只有同源的地址才会带上 token */
    fun sameOrigin(baseUrl: String, url: String): Boolean {
        return try {
            val base = URI(baseUrl)
            val target = URI(url)
            target.scheme.equals("https", ignoreCase = true)
                && target.host != null && target.host.equals(base.host, ignoreCase = true)
                && target.port == base.port
        } catch (e: URISyntaxException) {
            false
        }
    }

    /** 解析 /api/v1/muzei 的响应; 单个条目不合法时跳过它, 不影响其余条目 */
    fun parseFeed(json: String, config: SourceConfig): List<FeedImage> {
        val images = JSONObject(json).optJSONArray("images") ?: return emptyList()
        val result = LinkedHashMap<String, FeedImage>()
        for (i in 0 until images.length()) {
            val item = images.optJSONObject(i) ?: continue
            val id = item.string("id") ?: continue
            val imageUrl = item.string("image_url") ?: continue
            if (!IMAGE_ID_RE.matches(id) || !sameOrigin(config.baseUrl, imageUrl)) continue
            // token 只由 用户名/横竖/图片 ID 决定: 同一张图每次刷新都是同一个 token, Muzei 不会当成新图
            val token = "${config.username}/${config.orientation}/$id"
            result.getOrPut(token) {
                FeedImage(
                    token = token,
                    title = item.string("title") ?: item.string("created_at")?.take(10),
                    byline = item.string("byline") ?: "@${config.username}",
                    imageUrl = imageUrl,
                    sourceUrl = item.string("source_url")?.takeIf { it.startsWith("https://") },
                )
            }
        }
        return result.values.toList()
    }

    /** 解析 /api/v1/users 的响应 */
    fun parseUsers(json: String): List<UserSummary> {
        val users = JSONObject(json).optJSONArray("users") ?: return emptyList()
        return (0 until users.length()).mapNotNull { i ->
            val item = users.optJSONObject(i) ?: return@mapNotNull null
            val username = item.string("username") ?: return@mapNotNull null
            UserSummary(username, item.optInt("portrait_count"), item.optInt("landscape_count"))
        }
    }

    /** 把异常翻译成给用户看的一句话 (不会包含 token) */
    fun describe(error: Throwable): String = when (error) {
        is ApiException -> when (error.status) {
            403 -> "访问 token 不对 (403)"
            404 -> "找不到: Worker 地址或用户名不对 (404)"
            400 -> "请求参数不对 (400)"
            in 500..599 -> "Worker 出错了 (${error.status}), 稍后再试"
            else -> "请求失败 (HTTP ${error.status})"
        }
        is UnknownHostException -> "找不到这个地址, 检查 Worker 地址或网络"
        is SocketTimeoutException -> "连接超时, 检查网络"
        is SSLException -> "HTTPS 连接失败 (证书问题)"
        is IOException -> "网络错误: ${error.message ?: error.javaClass.simpleName}"
        else -> "出错了: ${error.javaClass.simpleName}"
    }

    // ------------------------------------------------------------------ 网络

    fun fetchFeed(config: SourceConfig): List<FeedImage> = parseFeed(getText(feedUrl(config), config.accessToken), config)

    fun fetchUsers(baseUrl: String, accessToken: String): List<UserSummary> =
        parseUsers(getText(usersUrl(baseUrl), accessToken))

    /**
     * 下载一张图片, 供 Muzei 缓存
     * 图片已不存在 (404/410) 时抛非 IOException, Muzei 会把这张图从列表里删掉; 其余错误都可重试
     */
    fun openImage(config: SourceConfig, imageUrl: String): InputStream {
        check(sameOrigin(config.baseUrl, imageUrl)) { "图片地址不是配置的 Worker, 拒绝发送 token" }
        val connection = open(imageUrl, config.accessToken)
        val status = connection.responseCode
        if (status == 404 || status == 410) {
            connection.disconnect()
            throw IllegalStateException("图片已不存在 ($status)")
        }
        if (status !in 200..299) {
            connection.disconnect()
            throw ApiException(status, "HTTP $status")
        }
        return connection.inputStream
    }

    private fun getText(url: String, accessToken: String): String {
        val connection = open(url, accessToken)
        try {
            connection.setRequestProperty("Accept", "application/json")
            val status = connection.responseCode
            if (status !in 200..299) throw ApiException(status, "HTTP $status")
            return connection.inputStream.use { it.readBytes().toString(Charsets.UTF_8) }
        } finally {
            connection.disconnect()
        }
    }

    private fun open(url: String, accessToken: String): HttpURLConnection {
        val connection = URL(url).openConnection() as HttpURLConnection
        connection.connectTimeout = CONNECT_TIMEOUT_MS
        connection.readTimeout = READ_TIMEOUT_MS
        connection.instanceFollowRedirects = false      // 不跟随跳转, 避免 token 被带到别的地址
        connection.setRequestProperty("Authorization", "Bearer $accessToken")
        return connection
    }

    private fun JSONObject.string(key: String): String? =
        if (isNull(key)) null else optString(key).takeIf { it.isNotEmpty() }
}

/** 用户在设置页填的配置 */
data class SourceConfig(
    val baseUrl: String,
    val accessToken: String,
    val username: String,
    val orientation: String,
    /** 只在最新 N 张里轮换, 0 = 全部 */
    val recent: Int,
) {
    // 防止 token 被意外打进日志
    override fun toString(): String =
        "SourceConfig(baseUrl=$baseUrl, username=$username, orientation=$orientation, recent=$recent)"
}

data class FeedImage(
    val token: String,
    val title: String?,
    val byline: String?,
    val imageUrl: String,
    val sourceUrl: String?,
)

data class UserSummary(val username: String, val portraitCount: Int, val landscapeCount: Int)

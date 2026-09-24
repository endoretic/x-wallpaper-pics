package io.github.endoretic.wallpapersource

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.net.UnknownHostException

class WallpaperApiTest {
    private val base = "https://wallpaper.example.com"
    private val portrait = SourceConfig(base, "secret-token", "demouser", WallpaperApi.PORTRAIT, 30)

    /** 与 Worker 的 /api/v1/muzei 响应同构 */
    private fun feed(vararg images: String) =
        """{"version":1,"updated_at":"2025-09-23T09:00:00Z","username":"demouser","orientation":"portrait","images":[${images.joinToString(",")}]}"""

    private fun image(id: String, url: String = "$base/api/v1/image/demouser/portrait/$id") =
        """{"id":"$id","created_at":"2025-09-23T08:00:00Z","url":"/api/v1/image/demouser/portrait/$id",""" +
            """"image_url":"$url","source_url":"https://x.com/demouser/status/${id.substringBefore('-')}",""" +
            """"title":"2025-09-23","byline":"@demouser"}"""

    @Test
    fun `合法响应变成多张图片`() {
        val images = WallpaperApi.parseFeed(feed(image("300-1"), image("300-2"), image("200")), portrait)
        assertEquals(listOf("300-1", "300-2", "200"), images.map { it.token.substringAfterLast('/') })
        with(images[0]) {
            assertEquals("demouser/portrait/300-1", token)
            assertEquals("2025-09-23", title)
            assertEquals("@demouser", byline)
            assertEquals("$base/api/v1/image/demouser/portrait/300-1", imageUrl)
            assertEquals("https://x.com/demouser/status/300", sourceUrl)
        }
    }

    @Test
    fun `同一张图每次刷新 token 不变`() {
        val first = WallpaperApi.parseFeed(feed(image("300-1"), image("200")), portrait)
        val second = WallpaperApi.parseFeed(feed(image("999"), image("200"), image("300-1")), portrait)
        assertEquals(first.map { it.token }.toSet(), second.map { it.token }.toSet() - "demouser/portrait/999")
    }

    @Test
    fun `个别坏条目被跳过, 不影响其余图片`() {
        val images = WallpaperApi.parseFeed(feed(
            image("100"),
            """{"image_url":"$base/api/v1/image/demouser/portrait/1"}""",       // 缺 id
            image("../../state"),                                               // 非法 id
            image("101", url = "http://wallpaper.example.com/x"),               // 不是 https
            image("102", url = "https://evil.example.net/api/v1/image/x"),      // 别的主机: 不能把 token 发过去
            """{"id":"103","image_url":null}""",                                // 地址为 null
            "\"not an object\"",
            image("100"),                                                       // 重复
            image("104"),
        ), portrait)
        assertEquals(listOf("100", "104"), images.map { it.token.substringAfterLast('/') })
    }

    @Test
    fun `没有 images 字段时返回空列表`() {
        assertTrue(WallpaperApi.parseFeed("""{"version":1}""", portrait).isEmpty())
    }

    @Test
    fun `横竖与轮换范围写进请求地址, 且区分 token`() {
        assertEquals("$base/api/v1/muzei/demouser?orientation=portrait&recent=30", WallpaperApi.feedUrl(portrait))
        val allLandscape = portrait.copy(orientation = WallpaperApi.LANDSCAPE, recent = 0)
        assertEquals("$base/api/v1/muzei/demouser?orientation=landscape&recent=0", WallpaperApi.feedUrl(allLandscape))
        val landscapeImage = WallpaperApi.parseFeed(feed(image("300-1")), allLandscape).single()
        assertEquals("demouser/landscape/300-1", landscapeImage.token)
    }

    @Test
    fun `token 错误时给出有用的提示`() {
        assertTrue(WallpaperApi.describe(WallpaperApi.ApiException(403, "HTTP 403")).contains("token"))
        assertTrue(WallpaperApi.describe(WallpaperApi.ApiException(404, "HTTP 404")).contains("用户名"))
        assertTrue(WallpaperApi.describe(WallpaperApi.ApiException(503, "HTTP 503")).contains("稍后"))
        assertTrue(WallpaperApi.describe(UnknownHostException("x")).contains("地址"))
        assertTrue(WallpaperApi.describe(IOException("boom")).contains("boom"))
    }

    @Test
    fun `Worker 地址规范化`() {
        assertEquals(base, WallpaperApi.normalizeBaseUrl(" wallpaper.example.com/ "))
        assertEquals(base, WallpaperApi.normalizeBaseUrl("https://wallpaper.example.com"))
        assertEquals("https://example.com/sub", WallpaperApi.normalizeBaseUrl("https://example.com/sub/"))
        assertNull(WallpaperApi.normalizeBaseUrl("http://wallpaper.example.com"))
        assertNull(WallpaperApi.normalizeBaseUrl(""))
        assertNull(WallpaperApi.normalizeBaseUrl("https://example.com/?token=x"))
        assertNull(WallpaperApi.normalizeBaseUrl("https://user:pass@example.com"))
        assertNull(WallpaperApi.normalizeBaseUrl("not a url at all"))
    }

    @Test
    fun `只有同源地址才会带 token`() {
        assertTrue(WallpaperApi.sameOrigin(base, "$base/api/v1/image/demouser/portrait/1"))
        assertTrue(WallpaperApi.sameOrigin(base, "https://WALLPAPER.example.com/x"))
        assertFalse(WallpaperApi.sameOrigin(base, "https://wallpaper.example.com.evil.net/x"))
        assertFalse(WallpaperApi.sameOrigin(base, "https://wallpaper.example.com:8443/x"))
        assertFalse(WallpaperApi.sameOrigin(base, "http://wallpaper.example.com/x"))
        assertFalse(WallpaperApi.sameOrigin(base, "::bad::"))
    }

    @Test
    fun `用户名校验`() {
        assertTrue(WallpaperApi.isValidUsername("demo_user1"))
        assertFalse(WallpaperApi.isValidUsername(""))
        assertFalse(WallpaperApi.isValidUsername("bad-name"))
        assertFalse(WallpaperApi.isValidUsername("a".repeat(16)))
    }

    @Test
    fun `解析用户列表`() {
        val users = WallpaperApi.parseUsers(
            """{"version":1,"users":[{"username":"demouser","portrait_count":743,"landscape_count":159},{"portrait_count":1}]}""")
        assertEquals(listOf(UserSummary("demouser", 743, 159)), users)
    }

    @Test
    fun `配置打印时不包含 token`() {
        assertFalse(portrait.toString().contains("secret-token"))
    }
}

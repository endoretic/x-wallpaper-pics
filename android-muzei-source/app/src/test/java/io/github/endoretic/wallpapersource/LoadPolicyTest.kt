package io.github.endoretic.wallpapersource

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LoadPolicyTest {
    private val hour = RefreshPolicy.STALE_AFTER_MS
    private val now = 10 * hour

    @Test
    fun `没有图时立即刷新 (不等正在退避的旧任务)`() {
        assertTrue(RefreshPolicy.shouldRefreshNow(initial = false, hasArtwork = false, lastRefreshAt = now - 1, now = now))
    }

    @Test
    fun `首次启用立即刷新`() {
        assertTrue(RefreshPolicy.shouldRefreshNow(initial = true, hasArtwork = true, lastRefreshAt = now - 1, now = now))
    }

    @Test
    fun `刚刷新过且有图时不刷新, 省请求`() {
        assertFalse(RefreshPolicy.shouldRefreshNow(initial = false, hasArtwork = true, lastRefreshAt = now - hour + 1, now = now))
    }

    @Test
    fun `列表过期后刷新; 从没刷新过也算过期`() {
        assertTrue(RefreshPolicy.shouldRefreshNow(initial = false, hasArtwork = true, lastRefreshAt = now - hour, now = now))
        assertTrue(RefreshPolicy.shouldRefreshNow(initial = false, hasArtwork = true, lastRefreshAt = 0, now = now))
    }

    @Test
    fun `系统时间被往回调过时也刷新`() {
        assertTrue(RefreshPolicy.shouldRefreshNow(initial = false, hasArtwork = true, lastRefreshAt = now + hour, now = now))
    }

    @Test
    fun `下载失败后暂停一段时间, 到期或成功后恢复`() {
        val breaker = NetworkBreaker(pauseMs = 30_000)
        assertFalse(breaker.isPaused(now))
        breaker.onFailure(now)
        assertTrue(breaker.isPaused(now + 29_999))
        assertFalse(breaker.isPaused(now + 30_000))
        breaker.onFailure(now)
        breaker.onSuccess()
        assertFalse(breaker.isPaused(now + 1))
    }
}

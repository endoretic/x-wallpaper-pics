package io.github.endoretic.wallpapersource

/**
 * 何时刷新列表、何时暂停联网下载: 纯逻辑, 时间由调用方传入, 方便在 JVM 上做单元测试
 */
object RefreshPolicy {
    /** 列表超过这么久没刷新过, Muzei 请求加载时就重新拉取 */
    const val STALE_AFTER_MS = 60 * 60 * 1000L

    /**
     * Muzei 请求加载时要不要立即拉取 (替换掉正在退避等待的旧任务)
     * 没有图 / 首次 / 列表过期时要; 刚刷新过且有图时不要, 省得每换一张壁纸都请求一次 Worker
     */
    fun shouldRefreshNow(initial: Boolean, hasArtwork: Boolean, lastRefreshAt: Long, now: Long): Boolean =
        initial || !hasArtwork || now - lastRefreshAt >= STALE_AFTER_MS || now < lastRefreshAt
}

/**
 * 联网下载失败后暂停一小段时间: 期间本地没有原图的请求直接失败, 不再连接 Worker
 *
 * 网络不通时 Muzei 会把图片池里的图挨个试一遍, 每张都要等连接超时;
 * 有了这个开关, 第一张失败后其余的立即失败, Muzei 很快进入重试, 也不会对 Worker 连发一串注定失败的请求
 */
class NetworkBreaker(private val pauseMs: Long = DEFAULT_PAUSE_MS) {
    @Volatile
    private var pausedUntil = 0L

    fun isPaused(now: Long): Boolean = now < pausedUntil

    fun onFailure(now: Long) {
        pausedUntil = now + pauseMs
    }

    fun onSuccess() {
        pausedUntil = 0L
    }

    companion object {
        const val DEFAULT_PAUSE_MS = 30_000L
    }
}

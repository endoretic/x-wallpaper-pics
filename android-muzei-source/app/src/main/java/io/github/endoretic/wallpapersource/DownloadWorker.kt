package io.github.endoretic.wallpapersource

import android.content.Context
import android.os.SystemClock
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequest
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import com.google.android.apps.muzei.api.provider.ProviderContract
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * 把图片池里本地还没有的原图下载到 ImageStore, 之后 Muzei 换图只读本地, 不在换图的那一刻临时联网
 *
 * 待下载的列表取自插件自己的图片池 (RefreshWorker 刚写入的); 系统限制后台任务单次最多 10 分钟,
 * 所以每轮最多跑 8 分钟, 没下完就排队接着下一轮
 */
class DownloadWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val config = Settings(applicationContext).config() ?: return Result.failure()
        val store = ImageStore.forContext(applicationContext)
        val pending = pendingDownloads(applicationContext, store)
        val deadline = SystemClock.elapsedRealtime() + TIME_BUDGET_MS
        var failuresInRow = 0
        for ((index, item) in pending.withIndex()) {
            if (isStopped) return Result.success()                  // 被系统叫停: 下次刷新时会重新排队
            if (SystemClock.elapsedRealtime() > deadline) {
                enqueue(applicationContext, remaining = pending.size - index)
                return Result.success()
            }
            val (key, url) = item
            try {
                store.put(key, WallpaperApi.openImage(config, url).use { it.readBytes() })
                failuresInRow = 0
            } catch (e: IOException) {
                failuresInRow++
                if (failuresInRow >= MAX_FAILURES_IN_ROW) {
                    Log.w(TAG, "预下载暂停: ${WallpaperApi.describe(e)}")
                    return if (runAttemptCount + 1 < MAX_ATTEMPTS) Result.retry() else Result.failure()
                }
            } catch (e: IllegalStateException) {
                // 图片已不存在或地址不对: 跳过这张, 不影响其余的
            }
        }
        return Result.success()
    }

    companion object {
        private const val TAG = "WallpaperSource"
        private const val WORK_NAME = "download"
        private const val TIME_BUDGET_MS = 8 * 60 * 1000L
        private const val MAX_FAILURES_IN_ROW = 3
        private const val MAX_ATTEMPTS = 5
        /** 缺的图超过这么多张 (例如第一次下载"全部") 时只在 Wi-Fi 下下载, 免得耗掉大量手机流量 */
        const val WIFI_ONLY_ABOVE = 50

        /**
         * 排一轮预下载; 已有一轮在跑时排在它后面 (那一轮算出的待下载列表可能不含刚加入的新图)
         * remaining: 预计要下载的张数, 决定是否只在 Wi-Fi 下进行
         */
        fun enqueue(context: Context, remaining: Int) {
            if (remaining <= 0) return
            val network = if (remaining > WIFI_ONLY_ABOVE) NetworkType.UNMETERED else NetworkType.CONNECTED
            val request = OneTimeWorkRequest.Builder(DownloadWorker::class.java)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(network).build())
                .setBackoffCriteria(BackoffPolicy.LINEAR, 60, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(WORK_NAME, ExistingWorkPolicy.APPEND_OR_REPLACE, request)
        }

        /** 图片池里本地还没有原图的 (键, 地址); 同一张图只算一次 */
        fun pendingDownloads(context: Context, store: ImageStore): List<Pair<String, String>> {
            val contentUri = ProviderContract.getProviderClient(context, WallpaperArtProvider::class.java).contentUri
            val columns = arrayOf(ProviderContract.Artwork.TOKEN, ProviderContract.Artwork.PERSISTENT_URI)
            val result = LinkedHashMap<String, String>()
            context.contentResolver.query(contentUri, columns, null, null, null)?.use { cursor ->
                while (cursor.moveToNext()) {
                    val url = cursor.getString(1) ?: continue
                    val key = ImageStore.keyFor(cursor.getString(0), url)
                    if (!store.contains(key)) result.putIfAbsent(key, url)
                }
            }
            return result.toList()
        }
    }
}

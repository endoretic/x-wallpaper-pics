package io.github.endoretic.wallpapersource

import android.content.Context
import android.net.Uri
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequest
import androidx.work.PeriodicWorkRequest
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import com.google.android.apps.muzei.api.provider.Artwork
import com.google.android.apps.muzei.api.provider.ProviderContract
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * 从 Worker 拉取壁纸列表, 整体替换 Muzei 里的图片池
 *
 * 同一张图的 token 不变, Muzei 会原地更新而不是当成新图, 已缓存的图片也不会重新下载;
 * 不在新列表里的图 (例如改了横竖或轮换范围) 会被移除
 */
class RefreshWorker(context: Context, params: WorkerParameters) : Worker(context, params) {

    override fun doWork(): Result {
        val config = Settings(applicationContext).config() ?: return Result.failure()
        val images = try {
            WallpaperApi.fetchFeed(config)
        } catch (e: WallpaperApi.ApiException) {
            Log.w(TAG, "刷新失败: ${WallpaperApi.describe(e)}")
            // 5xx 多半是暂时性的, 值得重试; 403/404 是配置问题, 重试也没用, 等用户改设置
            return if (e.status >= 500 && runAttemptCount + 1 < MAX_ATTEMPTS) Result.retry() else Result.failure()
        } catch (e: IOException) {
            Log.w(TAG, "刷新失败: ${WallpaperApi.describe(e)}")
            // 重试几次就停, 剩下的交给 Muzei 下次请求加载或 12 小时的定时刷新, 不在后台无限退避
            return if (runAttemptCount + 1 < MAX_ATTEMPTS) Result.retry() else Result.failure()
        }
        Settings(applicationContext).markRefreshed(System.currentTimeMillis())
        if (images.isEmpty()) return Result.success()      // 列表为空时保留现有壁纸, 不清空

        // 显示方式记进每张图: 改了方式或位置时 token 跟着变, Muzei 会整体换成新排版的图
        val display = Settings(applicationContext).displaySpec()
        val artworks = images.map { image ->
            Artwork(
                title = image.title,
                byline = image.byline,
                token = DisplaySpec.tokenFor(image.token, display),
                persistentUri = Uri.parse(image.imageUrl),
                webUri = image.sourceUrl?.let(Uri::parse),
                metadata = display?.encode(),
            )
        }
        ProviderContract.getProviderClient(applicationContext, WallpaperArtProvider::class.java).setArtwork(artworks)
        return Result.success()
    }

    companion object {
        private const val TAG = "WallpaperSource"
        private const val WORK_NOW = "refresh"
        private const val WORK_PERIODIC = "refresh-periodic"
        private const val MAX_ATTEMPTS = 5
        private val NETWORK = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        /** 立即刷新一次 (联网后执行); replace = true 用于改了设置之后 */
        fun enqueue(context: Context, replace: Boolean) {
            // 失败后 30 秒起线性递增重试 (默认是指数退避, 几次之后要等几个小时)
            val request = OneTimeWorkRequest.Builder(RefreshWorker::class.java).setConstraints(NETWORK)
                .setBackoffCriteria(BackoffPolicy.LINEAR, 30, TimeUnit.SECONDS).build()
            WorkManager.getInstance(context).enqueueUniqueWork(
                WORK_NOW, if (replace) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP, request)
        }

        /** 每 12 小时刷新一次, 让新发的图能进到轮换池 (即使 Muzei 还没轮完现有的图) */
        fun schedulePeriodic(context: Context) {
            val request = PeriodicWorkRequest.Builder(RefreshWorker::class.java, 12, TimeUnit.HOURS)
                .setConstraints(NETWORK).build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                WORK_PERIODIC, ExistingPeriodicWorkPolicy.KEEP, request)
        }
    }
}

package io.github.endoretic.wallpapersource

import android.content.Context
import java.io.File
import java.security.MessageDigest

/**
 * 原图的本地副本: 刷新列表后由 DownloadWorker 预先下载, Muzei 换图时只从这里读, 不再临时联网
 *
 * 放在 noBackupFilesDir: 不会被系统或清理软件当作缓存清掉, 也不参与备份; 永久保存, 不自动清理
 * (Muzei 自带的缓存在 cacheDir, 会被清理, 且最多只留最近显示过的 100 张)。
 * 改显示方式或位置时, 也直接用这里的原图重新排版
 *
 * 只依赖一个目录, 方便在 JVM 上做单元测试
 */
class ImageStore(private val directory: File) {

    fun contains(key: String): Boolean = synchronized(LOCK) {
        val file = fileFor(key)
        file.isFile && file.length() > 0
    }

    /** 取出原图; 没有时返回 null */
    fun get(key: String): ByteArray? = synchronized(LOCK) {
        val file = fileFor(key)
        if (!file.isFile || file.length() == 0L) return null
        return try {
            file.readBytes()
        } catch (e: Exception) {
            null
        }
    }

    /** 存入原图: 先写临时文件再改名, 下载中断也不会留下半张图 */
    fun put(key: String, bytes: ByteArray) = synchronized(LOCK) {
        if (bytes.isEmpty()) return
        directory.mkdirs()
        val target = fileFor(key)
        val temp = File(directory, "${target.name}.tmp")
        try {
            temp.writeBytes(bytes)
            if (!temp.renameTo(target)) {
                target.delete()
                temp.renameTo(target)
            }
        } finally {
            temp.delete()
        }
    }

    fun totalBytes(): Long = synchronized(LOCK) {
        directory.listFiles { file -> file.isFile && !file.name.endsWith(".tmp") }?.sumOf { it.length() } ?: 0L
    }

    private fun fileFor(key: String): File = File(directory, sha256(key).take(40))

    private fun sha256(text: String): String =
        MessageDigest.getInstance("SHA-256").digest(text.toByteArray()).joinToString("") { "%02x".format(it) }

    companion object {
        private val LOCK = Any()

        fun forContext(context: Context): ImageStore = ImageStore(File(context.noBackupFilesDir, "originals"))

        /** 同一张图的键与显示方式、Worker 地址都无关: 用去掉排版后缀的 token (用户名/横竖/图片 ID) */
        fun keyFor(artworkToken: String?, imageUrl: String): String = artworkToken?.substringBefore('@') ?: imageUrl
    }
}

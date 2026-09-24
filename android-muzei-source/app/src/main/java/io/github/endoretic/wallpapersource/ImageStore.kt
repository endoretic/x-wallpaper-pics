package io.github.endoretic.wallpapersource

import java.io.File
import java.security.MessageDigest

/**
 * 原图的本地副本: 下载过的图不再找 Worker 要
 *
 * Muzei 自己的缓存放在 cacheDir, 会被系统在空间紧张时、或被清理软件清掉, 而且最多只留最近显示过的 100 张;
 * 这里放在 noBackupFilesDir (不会被当作缓存清理, 也不参与备份), 总大小超过上限时按最久未用淘汰。
 * 改显示方式或位置时, 也直接用这里的原图重新排版, 不用重新下载
 *
 * 只依赖一个目录, 方便在 JVM 上做单元测试
 */
class ImageStore(private val directory: File, private val maxBytes: Long = DEFAULT_MAX_BYTES) {

    /** 取出原图 (并记为刚用过); 没有时返回 null */
    fun get(key: String): ByteArray? = synchronized(LOCK) {
        val file = fileFor(key)
        if (!file.isFile || file.length() == 0L) return null
        file.setLastModified(System.currentTimeMillis())
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
        trim()
    }

    fun totalBytes(): Long = synchronized(LOCK) { files().sumOf { it.length() } }

    /** 超过上限时, 从最久没用的开始删 */
    private fun trim() {
        val files = files().sortedBy { it.lastModified() }
        var total = files.sumOf { it.length() }
        for (file in files) {
            if (total <= maxBytes) break
            total -= file.length()
            file.delete()
        }
    }

    private fun files(): List<File> = directory.listFiles { file -> file.isFile && !file.name.endsWith(".tmp") }?.toList() ?: emptyList()

    private fun fileFor(key: String): File = File(directory, sha256(key).take(40))

    private fun sha256(text: String): String =
        MessageDigest.getInstance("SHA-256").digest(text.toByteArray()).joinToString("") { "%02x".format(it) }

    companion object {
        /** 默认上限 256 MB: 全部 700 多张竖屏图大约 200 MB */
        const val DEFAULT_MAX_BYTES = 256L * 1024 * 1024
        private val LOCK = Any()

        /** 同一张图的键与显示方式、Worker 地址都无关: 用去掉排版后缀的 token (用户名/横竖/图片 ID) */
        fun keyFor(artworkToken: String?, imageUrl: String): String = artworkToken?.substringBefore('@') ?: imageUrl
    }
}

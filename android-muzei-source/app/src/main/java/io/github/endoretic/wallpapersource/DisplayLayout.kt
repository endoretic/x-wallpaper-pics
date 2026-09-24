package io.github.endoretic.wallpapersource

import kotlin.math.min
import kotlin.math.roundToInt

/**
 * 壁纸显示方式: 插件先按手机屏幕分辨率生成一张与屏幕同尺寸的图, 再交给 Muzei (Muzei 只会原样铺满)
 *
 * 不设置 (null) 时保持 Muzei 自己的铺法: 按高度铺满, 比屏幕宽的部分随桌面滑动平移
 * 纯 Kotlin, 不依赖 Android 组件, 方便在 JVM 上做单元测试
 */
enum class FitMode(val key: String) {
    /** 覆盖: 等比放大铺满屏幕, 多出的部分按位置裁掉 */
    COVER("cover"),
    /** 填充: 等比缩小让整张图完整显示, 空白处留黑边, 图片按位置摆放 */
    FIT("fit"),
    /** 居中: 原始像素大小, 不缩放; 比屏幕大时按位置裁, 比屏幕小时按位置摆放 */
    CENTER("center"),
    /** 拉伸: 直接拉成屏幕尺寸, 会变形; 位置不起作用 */
    STRETCH("stretch");

    companion object {
        fun fromKey(key: String?): FitMode? = entries.firstOrNull { it.key == key }
    }
}

/** 显示方式 + 位置 (x / y 为 0–100 的百分比: 0 = 最左 / 最上, 100 = 最右 / 最下) */
data class DisplaySpec(val mode: FitMode, val x: Int, val y: Int) {
    init {
        require(x in 0..100 && y in 0..100) { "位置必须在 0–100 之间" }
    }

    /** 存进 Artwork.metadata, 下载图片时据此排版; 拉伸与位置无关, 统一记成居中, 免得挪滑条也重建图片池 */
    fun encode(): String = if (mode == FitMode.STRETCH) "fit:${mode.key},50,50" else "fit:${mode.key},$x,$y"

    companion object {
        const val CENTER = 50
        private val ENCODED = Regex("^fit:([a-z]+),(\\d{1,3}),(\\d{1,3})$")

        fun parse(metadata: String?): DisplaySpec? {
            val match = ENCODED.matchEntire(metadata ?: return null) ?: return null
            val (mode, x, y) = match.destructured
            return DisplaySpec(FitMode.fromKey(mode) ?: return null, x.toInt().coerceIn(0, 100), y.toInt().coerceIn(0, 100))
        }

        /**
         * 显示方式也是图片身份的一部分: 改了方式或位置, token 随之变化, Muzei 会把旧图 (连同缓存) 换成新排版的图;
         * 不处理时保持原 token, 与未开启该功能时一致
         */
        fun tokenFor(baseToken: String, spec: DisplaySpec?): String =
            if (spec == null) baseToken else "$baseToken@${spec.encode()}"
    }
}

/** src: 从原图里取的区域; dst: 画到屏幕画布上的位置; 都是 [left, top, right, bottom] 像素, right / bottom 不含 */
class Placement(val src: IntArray, val dst: IntArray)

object DisplayLayout {
    fun compute(spec: DisplaySpec, imageWidth: Int, imageHeight: Int, screenWidth: Int, screenHeight: Int): Placement {
        require(imageWidth > 0 && imageHeight > 0 && screenWidth > 0 && screenHeight > 0)
        val full = intArrayOf(0, 0, imageWidth, imageHeight)
        val screen = intArrayOf(0, 0, screenWidth, screenHeight)
        return when (spec.mode) {
            FitMode.STRETCH -> Placement(full, screen)
            FitMode.COVER -> Placement(coverRect(imageWidth, imageHeight, screenWidth, screenHeight, spec), screen)
            FitMode.FIT -> {
                val scale = min(screenWidth.toDouble() / imageWidth, screenHeight.toDouble() / imageHeight)
                val width = (imageWidth * scale).roundToInt().coerceIn(1, screenWidth)
                val height = (imageHeight * scale).roundToInt().coerceIn(1, screenHeight)
                val left = offset(screenWidth - width, spec.x)
                val top = offset(screenHeight - height, spec.y)
                Placement(full, intArrayOf(left, top, left + width, top + height))
            }
            FitMode.CENTER -> {
                // 每个方向单独处理: 图比屏幕大就在图里按位置取一段, 比屏幕小就在屏幕上按位置摆放
                val (srcLeft, dstLeft, width) = axis(imageWidth, screenWidth, spec.x)
                val (srcTop, dstTop, height) = axis(imageHeight, screenHeight, spec.y)
                Placement(intArrayOf(srcLeft, srcTop, srcLeft + width, srcTop + height),
                          intArrayOf(dstLeft, dstTop, dstLeft + width, dstTop + height))
            }
        }
    }

    /** 解码时的降采样倍数 (2 的幂): 只在取出的区域比要画的尺寸大一倍以上时才缩小, 保证清晰度不低于屏幕 */
    fun sampleSize(srcWidth: Int, srcHeight: Int, dstWidth: Int, dstHeight: Int): Int {
        var sample = 1
        while (srcWidth / (sample * 2) >= dstWidth && srcHeight / (sample * 2) >= dstHeight) sample *= 2
        return sample
    }

    /** 覆盖: 在图里取与屏幕同比例的最大区域, 按位置定位 */
    private fun coverRect(imageWidth: Int, imageHeight: Int, screenWidth: Int, screenHeight: Int, spec: DisplaySpec): IntArray {
        val screenRatio = screenWidth.toDouble() / screenHeight
        val imageRatio = imageWidth.toDouble() / imageHeight
        var width = imageWidth
        var height = imageHeight
        if (imageRatio > screenRatio) {
            width = (imageHeight * screenRatio).roundToInt().coerceIn(1, imageWidth)     // 图比屏幕宽: 裁左右
        } else if (imageRatio < screenRatio) {
            height = (imageWidth / screenRatio).roundToInt().coerceIn(1, imageHeight)   // 图比屏幕瘦长: 裁上下
        }
        val left = offset(imageWidth - width, spec.x)
        val top = offset(imageHeight - height, spec.y)
        return intArrayOf(left, top, left + width, top + height)
    }

    /** 居中模式的单个方向: 返回 (原图起点, 屏幕起点, 长度) */
    private fun axis(image: Int, screen: Int, percent: Int): Triple<Int, Int, Int> =
        if (image >= screen) Triple(offset(image - screen, percent), 0, screen)
        else Triple(0, offset(screen - image, percent), image)

    private fun offset(room: Int, percent: Int): Int = (room * percent / 100.0).roundToInt()
}

package io.github.endoretic.wallpapersource

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.BitmapRegionDecoder
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect
import android.hardware.display.DisplayManager
import android.os.Build
import android.util.Log
import android.view.Display
import java.io.ByteArrayOutputStream
import kotlin.math.max
import kotlin.math.min

/**
 * 按显示方式把原图排进一张与屏幕同尺寸的画布, 输出 JPEG 交给 Muzei
 * 只解码需要的区域 (BitmapRegionDecoder), 必要时降采样, 大图也不会占太多内存
 */
object ImageFitter {
    private const val TAG = "WallpaperSource"
    private const val JPEG_QUALITY = 95

    /** 竖持手机时的屏幕像素尺寸 (宽 < 高), 取面板的物理分辨率, 包含状态栏和导航栏 */
    fun portraitScreenSize(context: Context): Pair<Int, Int> {
        val display = context.getSystemService(DisplayManager::class.java)?.getDisplay(Display.DEFAULT_DISPLAY)
        val (a, b) = display?.mode?.let { it.physicalWidth to it.physicalHeight }
            ?: context.resources.displayMetrics.let { it.widthPixels to it.heightPixels }
        return min(a, b) to max(a, b)
    }

    /** 排版失败 (解不出尺寸、内存不足等) 时原样返回, 让 Muzei 按默认方式显示, 不至于没有壁纸 */
    fun render(bytes: ByteArray, spec: DisplaySpec, screenWidth: Int, screenHeight: Int): ByteArray = try {
        renderOrThrow(bytes, spec, screenWidth, screenHeight) ?: bytes
    } catch (e: Exception) {
        Log.w(TAG, "排版失败, 使用原图: ${e.javaClass.simpleName}")
        bytes
    } catch (e: OutOfMemoryError) {
        Log.w(TAG, "排版时内存不足, 使用原图")
        bytes
    }

    private fun renderOrThrow(bytes: ByteArray, spec: DisplaySpec, screenWidth: Int, screenHeight: Int): ByteArray? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null

        val placement = DisplayLayout.compute(spec, bounds.outWidth, bounds.outHeight, screenWidth, screenHeight)
        val (srcLeft, srcTop, srcRight, srcBottom) = placement.src
        val (dstLeft, dstTop, dstRight, dstBottom) = placement.dst
        val options = BitmapFactory.Options().apply {
            inSampleSize = DisplayLayout.sampleSize(srcRight - srcLeft, srcBottom - srcTop, dstRight - dstLeft, dstBottom - dstTop)
        }
        val region = decodeRegion(bytes, Rect(srcLeft, srcTop, srcRight, srcBottom), options) ?: return null

        val output = Bitmap.createBitmap(screenWidth, screenHeight, Bitmap.Config.ARGB_8888)
        try {
            Canvas(output).apply {
                drawColor(Color.BLACK)
                drawBitmap(region, null, Rect(dstLeft, dstTop, dstRight, dstBottom),
                    Paint(Paint.FILTER_BITMAP_FLAG or Paint.ANTI_ALIAS_FLAG))
            }
            return ByteArrayOutputStream().use { out ->
                output.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
                out.toByteArray()
            }
        } finally {
            region.recycle()
            output.recycle()
        }
    }

    private fun decodeRegion(bytes: ByteArray, rect: Rect, options: BitmapFactory.Options): Bitmap? {
        val decoder: BitmapRegionDecoder? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            BitmapRegionDecoder.newInstance(bytes, 0, bytes.size)
        } else {
            @Suppress("DEPRECATION")
            BitmapRegionDecoder.newInstance(bytes, 0, bytes.size, false)
        }
        decoder ?: return null
        return try {
            decoder.decodeRegion(rect, options)
        } finally {
            decoder.recycle()
        }
    }
}

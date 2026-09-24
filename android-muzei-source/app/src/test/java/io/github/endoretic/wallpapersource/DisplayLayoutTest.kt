package io.github.endoretic.wallpapersource

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DisplayLayoutTest {
    // 常见的 9:20 手机屏幕
    private val sw = 1080
    private val sh = 2400
    private val screen = intArrayOf(0, 0, sw, sh)

    private fun layout(mode: FitMode, w: Int, h: Int, x: Int = 50, y: Int = 50) =
        DisplayLayout.compute(DisplaySpec(mode, x, y), w, h, sw, sh)

    @Test
    fun `覆盖 - 比屏幕宽的图只裁左右, 按水平位置定位`() {
        // 1620 × 2400 的竖图, 同比例区域宽 1080, 可移动范围 540
        assertArrayEquals(intArrayOf(0, 0, 1080, 2400), layout(FitMode.COVER, 1620, 2400, x = 0).src)
        assertArrayEquals(intArrayOf(270, 0, 1350, 2400), layout(FitMode.COVER, 1620, 2400).src)
        assertArrayEquals(intArrayOf(540, 0, 1620, 2400), layout(FitMode.COVER, 1620, 2400, x = 100, y = 0).src)
        assertArrayEquals(screen, layout(FitMode.COVER, 1620, 2400).dst)
    }

    @Test
    fun `覆盖 - 比屏幕瘦长的图只裁上下, 按垂直位置定位`() {
        // 1000 × 3000, 同比例区域高 1000 / 0.45 ≈ 2222, 可移动范围 778
        assertArrayEquals(intArrayOf(0, 0, 1000, 2222), layout(FitMode.COVER, 1000, 3000, y = 0).src)
        assertArrayEquals(intArrayOf(0, 389, 1000, 2611), layout(FitMode.COVER, 1000, 3000).src)
        assertArrayEquals(intArrayOf(0, 778, 1000, 3000), layout(FitMode.COVER, 1000, 3000, x = 0, y = 100).src)
    }

    @Test
    fun `覆盖 - 取出的区域始终与屏幕同比例且不越界`() {
        for ((w, h) in listOf(4096 to 2731, 1080 to 1350, 900 to 3000, 540 to 1200, 1 to 1, 3000 to 1)) {
            for (pos in listOf(0, 37, 50, 100)) {
                val (left, top, right, bottom) = layout(FitMode.COVER, w, h, pos, pos).src.toList()
                assertTrue("$w×$h @$pos 越界", left >= 0 && top >= 0 && right <= w && bottom <= h)
                assertTrue("$w×$h 应至少保留一整条边", right - left == w || bottom - top == h)
                if (right - left > 2 && bottom - top > 2) assertEquals(0.45, (right - left).toDouble() / (bottom - top), 0.01)
            }
        }
    }

    @Test
    fun `填充 - 整张图完整显示, 按位置摆放在空白里`() {
        // 1620 × 2400 缩到宽 1080 → 高 1600, 上下共空 800
        assertArrayEquals(intArrayOf(0, 0, 1620, 2400), layout(FitMode.FIT, 1620, 2400).src)
        assertArrayEquals(intArrayOf(0, 400, 1080, 2000), layout(FitMode.FIT, 1620, 2400).dst)
        assertArrayEquals(intArrayOf(0, 0, 1080, 1600), layout(FitMode.FIT, 1620, 2400, y = 0).dst)
        assertArrayEquals(intArrayOf(0, 800, 1080, 2400), layout(FitMode.FIT, 1620, 2400, y = 100).dst)
        // 比屏幕瘦长: 高铺满, 左右留边
        assertArrayEquals(intArrayOf(140, 0, 940, 2400), layout(FitMode.FIT, 1000, 3000).dst)
        // 小图也会放大到贴边
        assertArrayEquals(intArrayOf(0, 900, 1080, 1500), layout(FitMode.FIT, 360, 200).dst)
    }

    @Test
    fun `居中 - 原始像素大小, 大图按位置裁, 小图按位置摆放`() {
        // 2000 × 3000 比屏幕大: 两个方向都从图里取屏幕大小的一段
        with(layout(FitMode.CENTER, 2000, 3000)) {
            assertArrayEquals(intArrayOf(460, 300, 1540, 2700), src)
            assertArrayEquals(screen, dst)
        }
        // 800 × 600 比屏幕小: 原尺寸放在屏幕上
        with(layout(FitMode.CENTER, 800, 600, x = 0, y = 100)) {
            assertArrayEquals(intArrayOf(0, 0, 800, 600), src)
            assertArrayEquals(intArrayOf(0, 1800, 800, 2400), dst)
        }
        // 宽度比屏幕大、高度比屏幕小: 分别处理
        with(layout(FitMode.CENTER, 1620, 1000)) {
            assertArrayEquals(intArrayOf(270, 0, 1350, 1000), src)
            assertArrayEquals(intArrayOf(0, 700, 1080, 1700), dst)
        }
    }

    @Test
    fun `拉伸 - 整张图拉满屏幕, 与位置无关`() {
        for (pos in listOf(0, 50, 100)) {
            with(layout(FitMode.STRETCH, 1620, 2400, pos, pos)) {
                assertArrayEquals(intArrayOf(0, 0, 1620, 2400), src)
                assertArrayEquals(screen, dst)
            }
        }
    }

    @Test
    fun `降采样不会让清晰度低于要画的尺寸`() {
        assertEquals(1, DisplayLayout.sampleSize(1080, 2400, 1080, 2400))
        assertEquals(1, DisplayLayout.sampleSize(2159, 4799, 1080, 2400))
        assertEquals(2, DisplayLayout.sampleSize(2160, 4800, 1080, 2400))
        assertEquals(2, DisplayLayout.sampleSize(4096, 4096, 1080, 1080))       // 再缩到 1024 就不够 1080 了
        assertEquals(4, DisplayLayout.sampleSize(8192, 8192, 1080, 1080))
        assertEquals(1, DisplayLayout.sampleSize(4096, 1000, 1080, 1080))       // 高度已经不够, 不缩
    }

    @Test
    fun `显示方式编码进 metadata 与 token`() {
        val spec = DisplaySpec(FitMode.COVER, 30, 70)
        assertEquals(spec, DisplaySpec.parse(spec.encode()))
        assertEquals(DisplaySpec(FitMode.FIT, 0, 100), DisplaySpec.parse("fit:fit,0,100"))
        assertNull(DisplaySpec.parse(null))
        assertNull(DisplaySpec.parse("fit:unknown,50,50"))
        assertNull(DisplaySpec.parse("crop:50,50"))
        assertEquals("u/portrait/300-1", DisplaySpec.tokenFor("u/portrait/300-1", null))
        assertEquals("u/portrait/300-1@fit:cover,30,70", DisplaySpec.tokenFor("u/portrait/300-1", spec))
        // 拉伸与位置无关: 挪滑条不应改变 token
        assertEquals(DisplaySpec(FitMode.STRETCH, 0, 0).encode(), DisplaySpec(FitMode.STRETCH, 100, 100).encode())
    }
}

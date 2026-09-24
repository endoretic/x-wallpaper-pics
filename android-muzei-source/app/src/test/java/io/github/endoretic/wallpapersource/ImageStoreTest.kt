package io.github.endoretic.wallpapersource

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ImageStoreTest {
    @get:Rule
    val temp = TemporaryFolder()

    @Test
    fun `存入后能原样取出, 没存过的返回 null`() {
        val store = ImageStore(temp.newFolder("originals"))
        store.put("demouser/portrait/300-1", byteArrayOf(1, 2, 3))
        assertArrayEquals(byteArrayOf(1, 2, 3), store.get("demouser/portrait/300-1"))
        assertNull(store.get("demouser/portrait/999"))
    }

    @Test
    fun `同一个键覆盖写入, 空数据不写`() {
        val store = ImageStore(temp.newFolder("originals"))
        store.put("k", byteArrayOf(1))
        store.put("k", byteArrayOf(2, 2))
        store.put("k", byteArrayOf())
        assertArrayEquals(byteArrayOf(2, 2), store.get("k"))
        assertEquals(2L, store.totalBytes())
    }

    @Test
    fun `永久保存, 不因数量或大小自动清理`() {
        val store = ImageStore(temp.newFolder("originals"))
        repeat(50) { store.put("k$it", ByteArray(1024)) }
        assertTrue((0 until 50).all { store.contains("k$it") })
        assertEquals(50L * 1024, store.totalBytes())
    }

    @Test
    fun `contains 只认完整写入的图`() {
        val dir = temp.newFolder("originals")
        val store = ImageStore(dir)
        store.put("done", byteArrayOf(1))
        assertTrue(store.contains("done"))
        assertFalse(store.contains("missing"))
        assertTrue("不应残留临时文件", dir.listFiles()!!.none { it.name.endsWith(".tmp") })
    }

    @Test
    fun `键与显示方式和 Worker 地址无关`() {
        assertEquals("u/portrait/300-1", ImageStore.keyFor("u/portrait/300-1@fit:cover,50,50", "https://a.example/x"))
        assertEquals("u/portrait/300-1", ImageStore.keyFor("u/portrait/300-1", "https://b.example/x"))
        assertEquals("https://a.example/x", ImageStore.keyFor(null, "https://a.example/x"))
    }
}

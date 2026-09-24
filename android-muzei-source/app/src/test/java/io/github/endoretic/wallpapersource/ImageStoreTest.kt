package io.github.endoretic.wallpapersource

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
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
    fun `超过上限时先删最久没用的`() {
        val dir = temp.newFolder("originals")
        val store = ImageStore(dir, maxBytes = 25)
        store.put("a", ByteArray(10))
        store.put("b", ByteArray(10))
        // 把 a 和 b 的最近使用时间拉开, 再读一次 a, 让 b 成为最久没用的
        dir.listFiles()!!.forEach { it.setLastModified(1_000_000) }
        store.get("a")
        store.put("c", ByteArray(10))                   // 30 > 25, 应删掉 b
        assertTrue(store.get("a") != null)
        assertNull(store.get("b"))
        assertTrue(store.get("c") != null)
        assertTrue(store.totalBytes() <= 25)
    }

    @Test
    fun `键与显示方式和 Worker 地址无关`() {
        assertEquals("u/portrait/300-1", ImageStore.keyFor("u/portrait/300-1@fit:cover,50,50", "https://a.example/x"))
        assertEquals("u/portrait/300-1", ImageStore.keyFor("u/portrait/300-1", "https://b.example/x"))
        assertEquals("https://a.example/x", ImageStore.keyFor(null, "https://a.example/x"))
    }
}

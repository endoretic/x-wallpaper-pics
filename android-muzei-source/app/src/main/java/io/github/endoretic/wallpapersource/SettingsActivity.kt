package io.github.endoretic.wallpapersource

import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.EditText
import android.widget.RadioGroup
import android.widget.TextView

/**
 * 设置页: 同时作为 Muzei 的 settingsActivity 与 setupActivity (首次启用前必须保存一次, 返回 RESULT_OK)
 * 也可以从桌面图标直接打开
 */
class SettingsActivity : Activity() {
    private lateinit var baseUrl: EditText
    private lateinit var token: EditText
    private lateinit var username: EditText
    private lateinit var orientation: RadioGroup
    private lateinit var pool: RadioGroup
    private lateinit var status: TextView
    private lateinit var testButton: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        applySystemBarInsets(findViewById(R.id.root))

        baseUrl = findViewById(R.id.base_url)
        token = findViewById(R.id.access_token)
        username = findViewById(R.id.username)
        orientation = findViewById(R.id.orientation)
        pool = findViewById(R.id.pool)
        status = findViewById(R.id.status)
        testButton = findViewById(R.id.test)

        if (savedInstanceState == null) {
            val settings = Settings(this)
            baseUrl.setText(settings.baseUrl)
            token.setText(settings.accessToken)
            username.setText(settings.username)
            orientation.check(
                if (settings.orientation == WallpaperApi.LANDSCAPE) R.id.orientation_landscape else R.id.orientation_portrait)
            pool.check(POOL_BUTTONS.entries.firstOrNull { it.value == settings.recent }?.key ?: R.id.pool_30)
        }

        testButton.setOnClickListener { testConnection() }
        findViewById<Button>(R.id.save).setOnClickListener { save() }
    }

    /** 读表单; 不合法时在状态栏提示并返回 null */
    private fun readForm(requireUsername: Boolean): SourceConfig? {
        val base = WallpaperApi.normalizeBaseUrl(baseUrl.text.toString())
            ?: return showError(R.string.error_base_url)
        val accessToken = token.text.toString().trim()
        if (accessToken.isEmpty()) return showError(R.string.error_token)
        val user = username.text.toString().trim().removePrefix("@")
        if (requireUsername && !WallpaperApi.isValidUsername(user)) return showError(R.string.error_username)
        val orientationValue =
            if (orientation.checkedRadioButtonId == R.id.orientation_landscape) WallpaperApi.LANDSCAPE else WallpaperApi.PORTRAIT
        val recent = POOL_BUTTONS[pool.checkedRadioButtonId] ?: Settings.DEFAULT_RECENT
        return SourceConfig(base, accessToken, user, orientationValue, recent)
    }

    private fun showError(message: Int): SourceConfig? {
        status.setText(message)
        return null
    }

    private fun testConnection() {
        val form = readForm(requireUsername = false) ?: return
        testButton.isEnabled = false
        status.setText(R.string.testing)
        Thread {
            val message = try {
                val users = WallpaperApi.fetchUsers(form.baseUrl, form.accessToken)
                if (users.size == 1) runOnUiThread { if (username.text.isBlank()) username.setText(users[0].username) }
                if (users.isEmpty()) getString(R.string.test_ok_empty)
                else getString(R.string.test_ok, users.joinToString("\n") {
                    getString(R.string.test_user_line, it.username, it.portraitCount, it.landscapeCount)
                })
            } catch (e: Exception) {
                getString(R.string.test_failed, WallpaperApi.describe(e))
            }
            runOnUiThread {
                if (!isFinishing && !isDestroyed) {
                    status.text = message
                    testButton.isEnabled = true
                }
            }
        }.start()
    }

    private fun save() {
        val form = readForm(requireUsername = true) ?: return
        Settings(this).save(form)
        RefreshWorker.enqueue(this, replace = true)
        RefreshWorker.schedulePeriodic(this)
        setResult(RESULT_OK)
        finish()
    }

    /** targetSdk 35+ 强制全面屏, 内容会画到状态栏/导航栏下面, 这里按系统栏留出内边距 */
    private fun applySystemBarInsets(root: View) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return
        root.setOnApplyWindowInsetsListener { view, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
    }

    companion object {
        private val POOL_BUTTONS = mapOf(R.id.pool_10 to 10, R.id.pool_30 to 30, R.id.pool_100 to 100, R.id.pool_all to 0)
    }
}

package io.github.endoretic.wallpapersource

import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.EditText
import android.widget.RadioGroup
import android.widget.SeekBar
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
    private lateinit var displayMode: RadioGroup
    private lateinit var positionX: SeekBar
    private lateinit var positionY: SeekBar
    private lateinit var positionXLabel: TextView
    private lateinit var positionYLabel: TextView

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
        displayMode = findViewById(R.id.display_mode)
        positionX = findViewById(R.id.position_x)
        positionY = findViewById(R.id.position_y)
        positionXLabel = findViewById(R.id.position_x_label)
        positionYLabel = findViewById(R.id.position_y_label)

        if (savedInstanceState == null) {
            val settings = Settings(this)
            baseUrl.setText(settings.baseUrl)
            token.setText(settings.accessToken)
            username.setText(settings.username)
            orientation.check(
                if (settings.orientation == WallpaperApi.LANDSCAPE) R.id.orientation_landscape else R.id.orientation_portrait)
            pool.check(POOL_BUTTONS.entries.firstOrNull { it.value == settings.recent }?.key ?: R.id.pool_30)
            displayMode.check(MODE_BUTTONS.entries.firstOrNull { it.value == settings.displayMode }?.key ?: R.id.display_none)
            positionX.progress = settings.positionX
            positionY.progress = settings.positionY
        }
        val onPositionChanged = object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar, progress: Int, fromUser: Boolean) = refreshDisplayControls()
            override fun onStartTrackingTouch(seekBar: SeekBar) = Unit
            override fun onStopTrackingTouch(seekBar: SeekBar) = Unit
        }
        positionX.setOnSeekBarChangeListener(onPositionChanged)
        positionY.setOnSeekBarChangeListener(onPositionChanged)
        displayMode.setOnCheckedChangeListener { _, _ -> refreshDisplayControls() }
        refreshDisplayControls()

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
        Settings(this).save(form, MODE_BUTTONS[displayMode.checkedRadioButtonId], positionX.progress, positionY.progress)
        RefreshWorker.enqueue(this, replace = true)
        RefreshWorker.schedulePeriodic(this)
        setResult(RESULT_OK)
        finish()
    }

    /** 位置滑条只在用得上的显示方式下可调 (不处理 / 拉伸时位置不起作用) */
    private fun refreshDisplayControls() {
        val mode = MODE_BUTTONS[displayMode.checkedRadioButtonId]
        val usesPosition = mode != null && mode != FitMode.STRETCH
        positionX.isEnabled = usesPosition
        positionY.isEnabled = usesPosition
        positionXLabel.text = getString(R.string.position_x, positionX.progress)
        positionYLabel.text = getString(R.string.position_y, positionY.progress)
        positionXLabel.isEnabled = usesPosition
        positionYLabel.isEnabled = usesPosition
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
        private val MODE_BUTTONS: Map<Int, FitMode?> = mapOf(
            R.id.display_none to null, R.id.display_cover to FitMode.COVER, R.id.display_fit to FitMode.FIT,
            R.id.display_center to FitMode.CENTER, R.id.display_stretch to FitMode.STRETCH)

        /** 显示方式的简短名称 (用于 Muzei 里的来源描述) */
        fun modeLabel(mode: FitMode): Int = when (mode) {
            FitMode.COVER -> R.string.mode_cover
            FitMode.FIT -> R.string.mode_fit
            FitMode.CENTER -> R.string.mode_center
            FitMode.STRETCH -> R.string.mode_stretch
        }
    }
}

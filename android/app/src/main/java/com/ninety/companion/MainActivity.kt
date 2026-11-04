package com.ninety.companion

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.view.KeyEvent
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.PyException
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.net.InetSocketAddress
import java.net.Socket
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "99Peiban"
        private const val PORT = 8787
    }

    private var webView: WebView? = null
    private var pyServer: PyObject? = null

    /** WebView 的文件选择器回调。没有它，页面里的 <input type="file"> 点了没反应。 */
    private var filePathCallback: ValueCallback<Array<Uri>>? = null

    private val fileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val cb = filePathCallback
        filePathCallback = null
        if (cb == null) return@registerForActivityResult

        if (result.resultCode != Activity.RESULT_OK) {
            cb.onReceiveValue(null)
            return@registerForActivityResult
        }
        val data = result.data
        val uris: Array<Uri>? = when {
            data == null -> null
            data.clipData != null -> {
                val clip = data.clipData!!
                Array(clip.itemCount) { clip.getItemAt(it).uri }
            }
            data.data != null -> arrayOf(data.data!!)
            else -> null
        }
        cb.onReceiveValue(uris)
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        installCrashHandler()

        val base = filesDir

        // 覆盖安装时 filesDir 还在，若不主动刷新，角色数据永远停在第一次安装的版本。
        // 这里只覆盖 card.json / lore.json / persona.md，不动用户换过的 avatar.png。
        val marker = File(base, ".data_version")
        val curVer = try {
            packageManager.getPackageInfo(packageName, 0).versionCode
        } catch (e: Exception) {
            0
        }
        val oldVer = try {
            marker.readText().trim().toInt()
        } catch (e: Exception) {
            -1
        }
        if (oldVer != curVer && File(base, "characters").exists()) {
            refreshCharacterData()
            pruneRemovedCharacters()
            Log.i(TAG, "角色数据已刷新 $oldVer -> $curVer")
        }

        // 前端必须每次覆盖：否则升级后界面永远是第一次安装的版本
        syncWebAssets(base)
        copyAssetDir("characters", File(base, "characters"))
        copyAssetDir("stickers", File(base, "stickers"))
        copyAssetDir("data", File(base, "data"))
        try {
            marker.writeText(curVer.toString())
        } catch (e: Exception) {
            Log.w(TAG, "写版本标记失败", e)
        }
        // 这两个 JSON 很小，每次启动覆盖一次，以后加词条 / 加章节不用清数据
        for (name in listOf("world.json", "story.json")) {
            try {
                val dst = File(base, "data/$name")
                dst.parentFile?.mkdirs()
                assets.open("data/$name").use { input ->
                    dst.outputStream().use { output -> input.copyTo(output) }
                }
            } catch (e: Exception) {
                Log.w(TAG, "$name 更新跳过", e)
            }
        }

        // 先给一个白屏，避免黑屏等太久
        val wv = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = true
            settings.textZoom = 100
            webViewClient = WebViewClient()
            // 头像、表情包都靠 <input type="file"> 选图，必须实现 onShowFileChooser
            webChromeClient = object : WebChromeClient() {
                override fun onShowFileChooser(
                    view: WebView?,
                    callback: ValueCallback<Array<Uri>>?,
                    params: FileChooserParams?
                ): Boolean {
                    filePathCallback?.onReceiveValue(null)
                    filePathCallback = callback
                    return try {
                        val intent = params?.createIntent() ?: Intent(Intent.ACTION_GET_CONTENT).apply {
                            addCategory(Intent.CATEGORY_OPENABLE)
                            type = "image/*"
                        }
                        fileChooserLauncher.launch(intent)
                        true
                    } catch (e: Exception) {
                        Log.e(TAG, "打开文件选择器失败", e)
                        filePathCallback = null
                        false
                    }
                }
            }
            setBackgroundColor(0xFFFFFFFF.toInt())
        }
        webView = wv
        setContentView(wv)

        bootPython(base)

        // 带上版本号，绕开 WebView 对 HTML 的缓存（否则文件换了还显示旧页面）
        val appVer = try {
            packageManager.getPackageInfo(packageName, 0).versionCode
        } catch (e: Exception) {
            0
        }

        // 端口通了再加载页面
        Thread {
            val ok = waitPort(PORT, 120)
            runOnUiThread {
                if (ok) {
                    wv.loadUrl("http://127.0.0.1:$PORT/mobile?v=$appVer")
                } else {
                    wv.loadDataWithBaseURL(
                        null,
                        "<html><body style='font-family:sans-serif;padding:32px;line-height:1.9'>" +
                            "<h3>服务没能启动</h3>" +
                            "<p>日志已写到应用目录的 crash.log，可以发给我看。</p>" +
                            "<p style='color:#888;font-size:13px'>路径：Android/data/com.ninety.companion/files/crash.log</p>" +
                            "</body></html>",
                        "text/html", "utf-8", null
                    )
                }
            }
        }.start()
    }

    private fun bootPython(base: File) {
        Thread {
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(applicationContext))
                }
                val py = Python.getInstance()
                val entry = py.getModule("android_entry")
                pyServer = entry
                // 时区必须显式传：Chaquopy 里的 Python 未必拿得到手机时区，
                // 猜错会把中午当凌晨，角色就会答「现在是凌晨一点」。
                val tzMin = java.util.TimeZone.getDefault()
                    .getOffset(System.currentTimeMillis()) / 60000
                val verName = try {
                    packageManager.getPackageInfo(packageName, 0).versionName ?: ""
                } catch (e: Exception) {
                    ""
                }
                Log.i(TAG, "设备时区偏移 ${tzMin} 分钟，版本 ${verName}")
                entry.callAttr("start", base.absolutePath, PORT, tzMin, verName)
            } catch (e: PyException) {
                Log.e(TAG, "Python 启动失败", e)
                writeCrash("Python 启动失败", e)
            } catch (e: Throwable) {
                Log.e(TAG, "启动异常", e)
                writeCrash("启动异常", e)
            }
        }.start()
    }

    private fun waitPort(port: Int, tries: Int): Boolean {
        repeat(tries) {
            try {
                Socket().use { s ->
                    s.connect(InetSocketAddress("127.0.0.1", port), 400)
                }
                Log.i(TAG, "服务已就绪 :$port")
                return true
            } catch (e: Exception) {
                Thread.sleep(300)
            }
        }
        return false
    }

    /** 删掉新版本里已经不存在的角色目录。不这么做，覆盖安装后删过的角色还会留在手机上。 */
    private fun pruneRemovedCharacters() {
        val alive = try {
            assets.list("characters")?.toSet() ?: emptySet<String>()
        } catch (e: Exception) {
            emptySet<String>()
        }
        if (alive.isEmpty()) return
        val dir = File(filesDir, "characters")
        var n = 0
        dir.listFiles()?.forEach { f ->
            if (f.isDirectory && f.name !in alive) {
                if (f.deleteRecursively()) n++
            }
        }
        if (n > 0) Log.i(TAG, "移除已下线角色 $n 个")
    }

    /** 只覆盖文本类数据，保留用户自己换的头像。 */
    private fun refreshCharacterData() {
        val names = setOf("card.json", "lore.json", "persona.md")
        fun walk(assetPath: String, destDir: File) {
            val children = assets.list(assetPath) ?: return
            if (children.isEmpty()) return
            destDir.mkdirs()
            for (child in children) {
                val a = "$assetPath/$child"
                val d = File(destDir, child)
                val sub = assets.list(a)
                if (sub.isNullOrEmpty()) {
                    // 文本类数据无条件覆盖；头像只在本地还没有时才补，
                    // 免得把用户自己换过的图又盖回去
                    val shouldWrite = child in names ||
                        (child == "avatar.png" && !d.exists())
                    if (shouldWrite) {
                        try {
                            assets.open(a).use { i ->
                                d.outputStream().use { o -> i.copyTo(o) }
                            }
                        } catch (e: Exception) {
                            Log.w(TAG, "刷新失败: $a", e)
                        }
                    }
                } else {
                    walk(a, d)
                }
            }
        }
        walk("characters", File(filesDir, "characters"))
    }

    /** 把 assets 下的目录整个复制到内部存储（可写）默认「已存在则跳过」。 */
    private fun copyAssetDir(assetPath: String, dest: File, overwrite: Boolean = false) {
        if (dest.exists() && !overwrite) return
        try {
            val children = assets.list(assetPath)
            if (children.isNullOrEmpty()) {
                // 是文件
                dest.parentFile?.mkdirs()
                assets.open(assetPath).use { input ->
                    dest.outputStream().use { output -> input.copyTo(output) }
                }
                return
            }
            dest.mkdirs()
            for (child in children) {
                copyAssetDir("$assetPath/$child", File(dest, child), overwrite)
            }
        } catch (e: Exception) {
            Log.e(TAG, "复制资源失败: $assetPath", e)
        }
    }

    /**
     * 前端资源每次启动都覆盖。
     *
     * copyAssetDir 的默认行为是「目录已存在就跳过」，结果 filesDir/web 永远停在
     * 第一次安装的版本 —— 后来加的表情面板、剧情回顾、聊天菜单在升级后的手机上
     * 全部看不到。前端是纯静态文件，覆盖没有任何副作用，所以这里必须强制刷。
     */
    private fun syncWebAssets(base: File) {
        val stamp = File(base, ".web_version")
        val cur = try {
            packageManager.getPackageInfo(packageName, 0).versionCode.toString()
        } catch (e: Exception) {
            "0"
        }
        val old = try {
            stamp.readText().trim()
        } catch (e: Exception) {
            ""
        }
        // 即便版本号没变也刷一次（重装同版本号的情况），成本只有几个小文件
        if (old == cur && File(base, "web/mobile.html").exists()) {
            // 同版本仍校验一遍大小，防上次复制中断留下空文件
            val f = File(base, "web/mobile.html")
            if (f.length() > 10000) return
        }
        copyAssetDir("web", File(base, "web"), overwrite = true)
        try {
            stamp.writeText(cur)
        } catch (e: Exception) {
            Log.w(TAG, "写前端版本标记失败", e)
        }
        Log.i(TAG, "前端资源已刷新 -> $cur")
    }

    /** 崩溃写盘：手机上没 logcat，出了问题只能靠这个文件。 */
    private fun installCrashHandler() {
        val prev = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            writeCrash("未捕获异常 @ ${thread.name}", throwable)
            prev?.uncaughtException(thread, throwable)
        }
    }

    private fun writeCrash(title: String, e: Throwable) {
        try {
            val sw = StringWriter()
            e.printStackTrace(PrintWriter(sw))
            val ts = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())
            val f = File(filesDir, "crash.log")
            f.appendText("\n===== $ts  $title =====\n${sw}\n")
        } catch (ignored: Throwable) {
        }
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView?.canGoBack() == true) {
            webView?.goBack()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onDestroy() {
        filePathCallback?.onReceiveValue(null)
        filePathCallback = null
        webView?.destroy()
        super.onDestroy()
    }
}

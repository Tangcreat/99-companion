# 99陪伴 · Android 版

把 PC 端的 Python 后端整个塞进 APK，**手机独立运行**，不需要连电脑。
做法和星伴一样用 Chaquopy（Python 跑在 Android 里），WebView 加载 `127.0.0.1`。

## 一次性准备

| 组件 | 位置 | 说明 |
|---|---|---|
| JDK 17 | `C:/Program Files\Eclipse Adoptium\jdk-17.0.21.9-hotspot` | 已装 |
| Android SDK | `E:/android-sdk` | 已装（platform 34 / build-tools 34.0.0）|
| Gradle 8.6 | `E:/android-sdk/gradle-8.6` | 已下载 |

## 编译

```bat
set JAVA_HOME=C:/Program Files\Eclipse Adoptium\jdk-17.0.21.9-hotspot
set ANDROID_HOME=E:/android-sdk
set PATH=%JAVA_HOME%\bin;E:/android-sdk/gradle-8.6/bin;%PATH%

cd E:/Users/2026-11-03-21-15-33/starlit-clone/android
gradle assembleDebug
```

产物：`app\build\outputs\apk\debug\app-debug.apk`

装到手机：

```bat
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

## 改了后端代码之后

```bat
cd E:/Users/2026-11-03-21-15-33/starlit-clone
python tools\sync_android.py
cd android && gradle assembleDebug
```

`sync_android.py` 会把 `server.py`、`core/`、`web/`、`characters/`、`stickers/`
重新拷进 Android 工程。

## 结构

```
android/
├── app/src/main/
│   ├── python/            Chaquopy 打包的 Python 源码
│   │   ├── android_entry.py   入口：设 STARLIT_DATA_ROOT 再 import server
│   │   ├── server.py          与 PC 版同一份
│   │   └── core/              与 PC 版同一份
│   ├── assets/
│   │   ├── web/               前端页面
│   │   ├── characters/        角色卡（首次启动复制到内部存储）
│   │   └── stickers/
│   ├── java/.../MainActivity.kt
│   └── AndroidManifest.xml
```

## 关键点

- **数据目录**：APK 里的 assets 是只读的，所以首次启动把 `web/` `characters/` `stickers/`
  复制到 `filesDir`，Python 在那边读写。第二次启动跳过复制。
- **启动顺序**：Python 在子线程里跑（`serve_forever` 会阻塞），主线程轮询 8797 端口，
  通了才 `loadUrl`，所以不会 ANR。
- **ABI 只打 arm64-v8a**：包体积砍一半，现代手机都支持。

## 已知风险

- **Pillow** 在 Chaquopy 上需要 Android wheel。如果编译时 pip 阶段报错，
  把 `app/build.gradle.kts` 里的 `install("pillow")` 删掉——只有头像上传和
  表情包索引会受影响，聊天本身不用它。
- Chaquopy 16.x 支持到 Python 3.12，这里用 3.11。
- 首次编译要下 Chaquopy 的 Python 运行时（约 40MB），会比较久。

@echo off
setlocal

set "JAVA_HOME=C:/Program Files\Eclipse Adoptium\jdk-17.0.21.9-hotspot"
set "ANDROID_HOME=E:/android-sdk"
set "ANDROID_SDK_ROOT=E:/android-sdk"
set "NODE_OPTIONS="
set "PROJ=%~dp0.."
set "PYEXE=%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe"
set "GRADLE=E:/android-sdk/gradle-8.6/bin/gradle.bat"

echo ================================================
echo   99 Peiban - build Android APK
echo ================================================
echo.

if not exist "%JAVA_HOME%\bin\java.exe" (
  echo [ERROR] JDK not found at %JAVA_HOME%
  pause
  exit /b 1
)
if not exist "%GRADLE%" (
  echo [ERROR] Gradle not found at %GRADLE%
  pause
  exit /b 1
)

echo [1/3] Sync backend and assets...
if exist "%PYEXE%" (
  "%PYEXE%" "%PROJ%\tools\sync_android.py"
) else (
  echo   [WARN] python.exe not found, skip sync
)

echo.
echo [2/3] Clean previous python env...
if exist "app\build\python\env" rmdir /s /q "app\build\python\env"

echo.
echo [3/3] Building (first run downloads ~40MB runtime, 3-10 min)...
call "%GRADLE%" assembleDebug --no-daemon --console=plain
set BUILD_RC=%ERRORLEVEL%

echo.
if exist "app\build\outputs\apk\debug\app-debug.apk" (
  echo ================================================
  echo   BUILD OK
  echo ================================================
  for %%F in ("app\build\outputs\apk\debug\app-debug.apk") do echo   APK: %%~fF
  echo.
  echo   Copy app-debug.apk to your phone and install it.
  echo   First launch copies character data (1-2 sec).
  echo   Then set your own API Key in Settings.
) else (
  echo ================================================
  echo   BUILD FAILED  (exit code %BUILD_RC%)
  echo ================================================
  echo   Copy the error lines above and send them back.
)
echo.
pause

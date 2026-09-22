@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
echo ============================================
echo   AI 自动剪辑 - 一次性安装
echo ============================================
echo.

echo [1/4] 检查 FFmpeg ...
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo     没找到 ffmpeg。请先安装 FFmpeg 并加入 PATH，然后重新运行本文件。
    echo     Windows 下载： https://www.gyan.dev/ffmpeg/builds/
    pause
    exit /b 1
)
echo     OK

echo [2/4] 创建虚拟环境 .venv ...
if not exist ".venv\Scripts\python.exe" py -3.12 -m venv .venv
if not exist ".venv\Scripts\python.exe" py -3.11 -m venv .venv
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo     创建失败。请先安装 Python 3.11 或 3.12： https://www.python.org/downloads/
    pause
    exit /b 1
)
echo     OK

echo [3/4] 安装依赖（第一次会下载几分钟）...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo     依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)
echo     OK

echo [4/4] 生成配置文件 ...
if not exist ".env" copy ".env.example" ".env" >nul
if not exist "auto-cut.settings.json" copy "auto-cut.settings.example.json" "auto-cut.settings.json" >nul
echo     OK

echo.
echo 安装完成。还差最后一步：
echo   1. 用记事本打开本目录下的 .env
echo   2. 把 MINIMAX_API_KEY 换成你自己的 Key，保存
echo   3. 把一个产品的素材文件夹拖到「拖入素材文件夹.cmd」上
echo.
pause

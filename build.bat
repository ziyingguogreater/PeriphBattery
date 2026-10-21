@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
    echo [1/3] 创建虚拟环境...
    python -m venv .venv || goto :err
)

echo [2/3] 安装依赖...
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt pyinstaller || goto :err

echo [3/3] 打包...
pyinstaller --noconfirm PeriphBattery.spec || goto :err

echo.
echo 完成：dist\PeriphBattery.exe
pause
exit /b 0

:err
echo.
echo 构建失败，请检查上面的报错。
pause
exit /b 1

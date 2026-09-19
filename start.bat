@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动待办事项提醒系统...
python app.py
if errorlevel 1 (
  echo.
  echo 启动失败，请确认已安装 Python 3.9+ 并添加到了 PATH。
  pause
)

@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  QQ Agent
echo ============================================
echo.
if not exist config.json (
    copy config.example.json config.json >nul
    echo [!] 已生成 config.json，请先填入 llm.api_key 和 behaviour.owner_qq
    notepad config.json
    exit /b 1
)
echo [1/2] 配置与连通性自检 ...
python run.py --check
if errorlevel 1 (
    echo.
    echo [x] 自检未通过，请按上面的提示修好再启动。
    pause
    exit /b 1
)
echo.
echo [2/2] 启动智能体（Ctrl+C 退出）...
python run.py
pause

@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  QQ Agent
echo ============================================
echo.
rem 配置只写到用户数据目录。不要在仓库目录里生成含 api_key / token 的 config.json。
python run.py --init > "%TEMP%\napqq-init-out.txt"
if errorlevel 1 (
    echo [x] 初始化配置失败。
    type "%TEMP%\napqq-init-out.txt"
    del "%TEMP%\napqq-init-out.txt" >nul 2>&1
    pause
    exit /b 1
)
set "LINE="
set /p LINE=<"%TEMP%\napqq-init-out.txt"
del "%TEMP%\napqq-init-out.txt" >nul 2>&1
for /f "tokens=1,* delims=|" %%a in ("%LINE%") do (
    set "STATE=%%a"
    set "CFG=%%b"
)
if "%STATE%"=="created" (
    echo [!] 已在用户数据目录生成配置，不在本仓库内。
    echo     请填写 llm.api_key 和 behaviour.owner_qq。
    echo     OneBot access token 已写入该文件，请同步到 NapCat。
    echo     不要把 token 或 api_key 发出来，也不要提交到 git。
    notepad "%CFG%"
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

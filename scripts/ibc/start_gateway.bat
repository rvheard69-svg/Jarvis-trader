@echo off
REM Auto-login IB Gateway on native Windows via IBC (https://github.com/IbcAlpha/IBC),
REM so it comes up logged into your PAPER account without you clicking
REM through the login screen (and its daily 24h auto-restart) by hand.
REM
REM One-time setup:
REM   1. Download IBC from https://github.com/IbcAlpha/IBC/releases and
REM      unzip it somewhere OUTSIDE this repo (e.g. C:\ibc) - the rendered
REM      config below will hold your real IB password and must never sit
REM      in a git working tree.
REM   2. Install IB Gateway itself (see spike/ib_connect.py's CHECKLIST).
REM   3. In this project's .env, set:
REM        IB_USERNAME, IB_PASSWORD   - your PAPER account login
REM        IBC_PATH                   - path to the IBC folder from step 1
REM        IBC_GATEWAY_MAJOR_VERSION  - the Gateway version IBC should
REM                                     launch (the folder name under
REM                                     C:\Jts\ibgateway\<version>\ is this
REM                                     number)
REM
REM Usage:
REM   scripts\ibc\start_gateway.bat
REM
REM Then point this project at it as usual (.env: IB_HOST=127.0.0.1,
REM IB_PORT=4002) and run python main.py / scripts\run_forever.bat.
REM
REM This only starts Gateway - it does not start main.py. Chain the two in
REM Task Scheduler (see README.md) if you want both unattended.

setlocal enabledelayedexpansion
cd /d "%~dp0..\.."

if not exist .env (
    echo No .env found - copy .env.example to .env and fill in IB_USERNAME/IB_PASSWORD/IBC_PATH first.
    exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "line=%%A"
    if not "!line:~0,1!"=="#" if not "!line!"=="" set "%%A=%%B"
)

if "%IB_USERNAME%"=="" (echo Set IB_USERNAME in .env & exit /b 1)
if "%IB_PASSWORD%"=="" (echo Set IB_PASSWORD in .env & exit /b 1)
if "%IBC_PATH%"=="" (echo Set IBC_PATH in .env to where you unzipped IBC & exit /b 1)
if "%IBC_GATEWAY_MAJOR_VERSION%"=="" (echo Set IBC_GATEWAY_MAJOR_VERSION in .env & exit /b 1)
if "%IB_PORT%"=="" set "IB_PORT=4002"

set "RENDERED_CONFIG=%IBC_PATH%\config.ini"
echo Rendering scripts\ibc\config.ini.template -^> %RENDERED_CONFIG% (real credentials, outside this repo)

REM No envsubst on native Windows - a small PowerShell helper does the same
REM ${VAR} substitution against this process's environment.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\ibc\render_config.ps1" ^
    -TemplatePath "scripts\ibc\config.ini.template" ^
    -OutputPath "%RENDERED_CONFIG%"
if errorlevel 1 (
    echo Failed to render config.ini via PowerShell.
    exit /b 1
)

set "GATEWAY_SCRIPT=%IBC_PATH%\StartGateway.bat"
if not exist "%GATEWAY_SCRIPT%" (
    echo Expected IBC's launcher at %GATEWAY_SCRIPT%
    exit /b 1
)

echo Starting IB Gateway %IBC_GATEWAY_MAJOR_VERSION% via IBC (paper trading mode)...
call "%GATEWAY_SCRIPT%" "%IBC_GATEWAY_MAJOR_VERSION%" "%RENDERED_CONFIG%"

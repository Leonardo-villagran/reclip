@echo off
setlocal

cd /d "%~dp0"

set "VENV_DIR="
if exist "env\Scripts\python.exe" set "VENV_DIR=env"
if not defined VENV_DIR if exist "venv\Scripts\python.exe" set "VENV_DIR=venv"

if not defined VENV_DIR (
    echo No virtual environment found ^(env or venv^).
    echo Creating env with Python...
    where py >nul 2>&1
    if %errorlevel%==0 (
        py -3 -m venv env
    ) else (
        python -m venv env
    )
    if errorlevel 1 (
        echo Failed to create virtual environment.
        exit /b 1
    )
    set "VENV_DIR=env"
)

set "VENV_PY=%CD%\%VENV_DIR%\Scripts\python.exe"
set "VENV_SCRIPTS=%CD%\%VENV_DIR%\Scripts"

if not exist "%VENV_PY%" (
    echo Python not found in %VENV_DIR%\Scripts\python.exe
    exit /b 1
)

set "PATH=%VENV_SCRIPTS%;%PATH%"

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo Missing required tool: ffmpeg
    echo Install ffmpeg and make sure it is available in PATH.
    exit /b 1
)

where aria2c >nul 2>&1
if errorlevel 1 (
    echo Warning: aria2c not found. Torrent downloads will be disabled.
)

if exist "requirements.txt" (
    "%VENV_PY%" -m pip install -q -r requirements.txt
) else (
    "%VENV_PY%" -m pip install -q flask yt-dlp
)
if errorlevel 1 (
    echo Failed to install Python dependencies.
    exit /b 1
)

if "%PORT%"=="" set "PORT=8899"
if "%HOST%"=="" set "HOST=127.0.0.1"

set "OPEN_HOST=%HOST%"
if /I "%OPEN_HOST%"=="0.0.0.0" set "OPEN_HOST=localhost"
if /I "%OPEN_HOST%"=="127.0.0.1" set "OPEN_HOST=localhost"
set "APP_URL=http://%OPEN_HOST%:%PORT%"
set "COMET_EXE="

for %%F in (
    "%LOCALAPPDATA%\Perplexity\Comet\Application\comet*.exe"
    "%LOCALAPPDATA%\Programs\*\comet*.exe"
    "%ProgramFiles%\*\comet*.exe"
    "%ProgramFiles(x86)%\*\comet*.exe"
) do (
    if not defined COMET_EXE if exist "%%~fF" set "COMET_EXE=%%~fF"
)

if not defined COMET_EXE (
    for /f "delims=" %%P in ('where comet*.exe 2^>nul') do (
        if not defined COMET_EXE set "COMET_EXE=%%P"
    )
)

echo.
echo   ReClip is running at %APP_URL%
echo.

if exist "%COMET_EXE%" (
    start "" "%COMET_EXE%" "%APP_URL%"
) else (
    start "" "%APP_URL%"
)

"%VENV_PY%" app.py
exit /b %errorlevel%

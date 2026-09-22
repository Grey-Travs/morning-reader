@echo off
REM First-time setup: installs the Python and web dependencies.
cd /d "%~dp0"

echo ============================================
echo   Morning Reader - first-time setup
echo ============================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

echo [1/3] Creating Python environment...
REM Its OWN environment, deliberately. Night Reader sits beside this one and a
REM shell that already has its .venv active would otherwise satisfy the imports,
REM hiding a dependency this app never declared.
%PY% -m venv .venv || goto :err

echo [2/3] Installing Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err

echo [3/3] Installing web interface packages...
pushd web
call npm install || (popd & goto :err)
popd

echo.
echo Setup complete!
echo Next: make sure you're logged into Claude Code, then double-click start.bat.
echo Morning Reader opens on port 8100, so it can run beside Night Reader.
echo.
pause
exit /b 0

:err
echo.
echo Setup hit an error. See the messages above.
pause
exit /b 1

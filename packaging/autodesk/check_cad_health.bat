@echo off
REM =======================================================
REM TopoSpatial-CAD Pre-flight Check for Autodesk AutoCAD
REM =======================================================
echo.
echo =======================================================
echo   Checking AutoCAD & TopoSpatial-CAD Environment...
echo =======================================================
echo.

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [FAIL] Python was not found in PATH.
    echo Please install Python 3.10+ from python.org and check 'Add to PATH'.
    pause
    exit /b 1
)

python -m server.main --doctor
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [NOTICE] If Python modules are missing, run:
    echo   pip install -r requirements.txt
    echo.
)

echo.
pause

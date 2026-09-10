@echo off
setlocal EnableExtensions
title Setup

set "LHM_VERSION=0.9.6"
set "DOWNLOAD_URL=https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v%LHM_VERSION%/LibreHardwareMonitor.zip"
set "ZIP_FILE=%CD%\LibreHardwareMonitor.zip"
set "TEMP_DIR=%CD%\LHM_temp"
set "DLL_DIR=%CD%\DLLs"

set "DLL_LIST=BlackSharp.Core.dll DiskInfoToolkit.dll HidSharp.dll LibreHardwareMonitorLib.dll RAMSPDToolkit-NDD.dll System.Memory.dll System.Runtime.CompilerServices.Unsafe.dll"

echo.
echo [1/5] Downloading LibreHardwareMonitor v%LHM_VERSION%...

curl -L --fail --progress-bar -o "%ZIP_FILE%" "%DOWNLOAD_URL%"

if errorlevel 1 (
echo ERROR: Download failed.
goto END
)

echo Done.
echo.

echo [2/5] Extracting ZIP file...

if exist "%TEMP_DIR%" rmdir /S /Q "%TEMP_DIR%" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP_FILE%' -DestinationPath '%TEMP_DIR%' -Force"

if errorlevel 1 (
echo ERROR: Extraction failed.
goto END
)

echo Done.
echo.

echo [3/5] Copying DLLs...

if not exist "%DLL_DIR%" mkdir "%DLL_DIR%"

for %%D in (%DLL_LIST%) do (
echo %%D
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f=Get-ChildItem -LiteralPath '%TEMP_DIR%' -Filter '%%D' -File -Recurse | Select-Object -First 1; if($f){Copy-Item $f.FullName '%DLL_DIR%' -Force}else{exit 1}" >nul 2>&1

if errorlevel 1 (
    echo ERROR: %%D not found.
    goto END
)


)

echo Done.
echo.

echo [4/5] Unblocking DLLs...

powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%DLL_DIR%' -Filter '*.dll' -File | Unblock-File" >nul 2>&1

echo Done.
echo.

echo [5/5] Installing Python packages...

python -m pip install pythonnet pystray pillow

if errorlevel 1 (
echo.
echo ERROR: pip installation failed.
goto END
)

echo.
echo Done.
echo.
echo Cleaning up...
del /Q "%ZIP_FILE%" >nul 2>&1
rmdir /S /Q "%TEMP_DIR%" >nul 2>&1
echo Setup complete.
echo.

:END
cmd /k
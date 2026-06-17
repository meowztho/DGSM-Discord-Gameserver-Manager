@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "ROOT=%cd%"
set "SRC=%ROOT%\src"
set "APP_NAME=DGSM"
set "APP_MAIN=%SRC%\Main.py"
set "ICON_ICO=%SRC%\Logo.ico"
set "ICON_PNG=%SRC%\Logo.png"
set "PY_CMD=python"

echo [BUILD] Root: %ROOT%

where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys" >nul 2>nul
  if not errorlevel 1 (
    set "PY_CMD=py -3.12"
  )
)

%PY_CMD% -c "import sys;print(sys.version)" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python interpreter not usable: %PY_CMD%
  exit /b 1
)
echo [BUILD] Interpreter: %PY_CMD%

if not exist "%APP_MAIN%" (
  echo [ERROR] Missing entry file: %APP_MAIN%
  exit /b 1
)

if not exist "%SRC%\steam" mkdir "%SRC%\steam"
if not exist "%SRC%\steam\GSM\servers" mkdir "%SRC%\steam\GSM\servers"
if not exist "%SRC%\plugin_templates" mkdir "%SRC%\plugin_templates"

%PY_CMD% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo [BUILD] Installing PyInstaller...
  %PY_CMD% -m pip install pyinstaller
  if errorlevel 1 (
    echo [ERROR] Could not install PyInstaller.
    exit /b 1
  )
)

if not exist "%ICON_ICO%" if exist "%ICON_PNG%" (
  echo [BUILD] Creating Logo.ico from Logo.png...
  %PY_CMD% -c "from PIL import Image; Image.open(r'%ICON_PNG%').convert('RGBA').save(r'%ICON_ICO%', format='ICO', sizes=[(16,16),(24,24),(32,32),(40,40),(48,48),(64,64),(96,96),(128,128),(256,256)])" >nul 2>nul
)

echo [BUILD] Running PyInstaller...
if exist "%ICON_ICO%" (
  %PY_CMD% -m PyInstaller --noconfirm --clean --onedir --name "%APP_NAME%" --icon "%ICON_ICO%" --paths "%SRC%" --add-data "%ICON_PNG%;." "%APP_MAIN%"
) else (
  %PY_CMD% -m PyInstaller --noconfirm --clean --onedir --name "%APP_NAME%" --paths "%SRC%" --add-data "%ICON_PNG%;." "%APP_MAIN%"
)
if errorlevel 1 (
  echo [ERROR] Build failed.
  exit /b 1
)

if exist "%ROOT%\dist\%APP_NAME%\_internal\Logo.png" (
  copy /y "%ROOT%\dist\%APP_NAME%\_internal\Logo.png" "%ROOT%\dist\%APP_NAME%\Logo.png" >nul
)

echo [BUILD] Done: "%ROOT%\dist\%APP_NAME%\%APP_NAME%.exe"
echo [BUILD] Runtime paths remain under "%SRC%".
exit /b 0

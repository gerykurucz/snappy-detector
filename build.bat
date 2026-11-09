@echo off
REM =====================================================================
REM SnappyAI - Nuitka Build Script (single builder)
REM Produces the whole application into dist\SnappyAI\
REM =====================================================================

setlocal enabledelayedexpansion

REM ---------------------------------------------------------------------
REM Configuration
REM ---------------------------------------------------------------------
set PROJECT_NAME=SnappyAI
set MAIN_SCRIPT=main.py
set BUILD_DIR=dist
set OUT_DIR=%BUILD_DIR%\%PROJECT_NAME%
set PYTHON_EXE=python

REM Nuitka names the build/dist folders after the main script ("main"),
REM not after --output-filename. Keep these in sync with MAIN_SCRIPT.
for %%F in ("%MAIN_SCRIPT%") do set APP_BASENAME=%%~nF
set DIST_DIR=%BUILD_DIR%\%APP_BASENAME%.dist
set TEMP_BUILD_DIR=%BUILD_DIR%\%APP_BASENAME%.build

REM ---------------------------------------------------------------------
REM Clean previous build
REM ---------------------------------------------------------------------
echo [1/9] Cleaning previous build...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
mkdir "%OUT_DIR%"
mkdir "%OUT_DIR%\configs"
mkdir "%OUT_DIR%\recordings"
mkdir "%OUT_DIR%\models"
mkdir "%OUT_DIR%\capture"
mkdir "%OUT_DIR%\driver"
mkdir "%OUT_DIR%\docs"
mkdir "%OUT_DIR%\logs"
mkdir "%OUT_DIR%\reports"
mkdir "%OUT_DIR%\scripts\exec"
mkdir "%OUT_DIR%\scripts\undo"
mkdir "%OUT_DIR%\cache"

REM ---------------------------------------------------------------------
REM Copy model files
REM ---------------------------------------------------------------------
echo [2/9] Copying model assets...
if exist models xcopy /y models\*.onnx "%OUT_DIR%\models\" >nul 2>&1
if exist models xcopy /y models\*.npz "%OUT_DIR%\models\" >nul 2>&1
if exist models xcopy /y models\*.pt "%OUT_DIR%\models\" >nul 2>&1

REM ---------------------------------------------------------------------
REM Stage runtime DLLs next to the exe
REM ---------------------------------------------------------------------
echo [3/9] Staging runtime DLLs...
copy interception.dll "%OUT_DIR%\" >nul 2>&1
copy ViGEmClient.dll "%OUT_DIR%\" >nul 2>&1
copy interception.dll "%OUT_DIR%\driver\" >nul 2>&1
copy ViGEmClient.dll "%OUT_DIR%\driver\" >nul 2>&1
copy interception.dll "%OUT_DIR%\capture\" >nul 2>&1

REM ---------------------------------------------------------------------
REM Build with Nuitka
REM ---------------------------------------------------------------------
REM ultralytics/onnxslim/torchvision/polars are optional: detection
REM falls back to onnxruntime when ultralytics is missing. Following them
REM pulls in sympy, whose generated C files exhaust the MSVC heap (C1060).
echo [4/9] Compiling with Nuitka...
%PYTHON_EXE% -m nuitka ^
    --standalone ^
    --output-dir="%BUILD_DIR%" ^
    --output-filename="%PROJECT_NAME%.exe" ^
    --windows-console-mode=disable ^
    --enable-plugin=tk-inter ^
    --enable-plugin=multiprocessing ^
    --include-module=input ^
    --include-module=detection ^
    --include-package=assist ^
    --include-package=gui ^
    --include-package=reticle ^
    --include-package=config ^
    --include-package=system ^
    --include-package=online ^
    --include-package=maintenance ^
    --include-package=measurement ^
    --include-package=console ^
    --include-package=scanner ^
    --include-package=engine ^
    --include-package=utils ^
    --include-module=spoofer ^
    --nofollow-import-to=test_* ^
    --nofollow-import-to=torch ^
    --nofollow-import-to=scipy ^
    --nofollow-import-to=torch.* ^
    --nofollow-import-to=scipy.* ^
    --nofollow-import-to=sympy ^
    --nofollow-import-to=sympy.* ^
    --nofollow-import-to=ultralytics ^
    --nofollow-import-to=ultralytics.* ^
    --nofollow-import-to=ultralytics_platform ^
    --nofollow-import-to=ultralytics_platform.* ^
    --nofollow-import-to=onnxslim ^
    --nofollow-import-to=onnxslim.* ^
    --nofollow-import-to=torchvision ^
    --nofollow-import-to=torchvision.* ^
    --nofollow-import-to=polars ^
    --nofollow-import-to=polars.* ^
    --nofollow-import-to=thop ^
    --nofollow-import-to=thop.* ^
    --nofollow-import-to=xml ^
    --nofollow-import-to=xml.* ^
    --nofollow-import-to=unittest ^
    --nofollow-import-to=unittest.* ^
    --nofollow-import-to=tkinter.test ^
    --nofollow-import-to=lib2to3 ^
    --nofollow-import-to=distutils ^
    --nofollow-import-to=setuptools ^
    --nofollow-import-to=pip ^
    --nofollow-import-to=pkg_resources ^
    --nofollow-import-to=wheel ^
    --nofollow-import-to=matplotlib ^
    --nofollow-import-to=matplotlib.* ^
    --nofollow-import-to=mpl_toolkits ^
    --nofollow-import-to=mpl_toolkits.* ^
    --nofollow-import-to=mpmath ^
    --nofollow-import-to=mpmath.* ^
    --nofollow-import-to=onnxruntime.tools ^
    --nofollow-import-to=onnxruntime.tools.* ^
    --nofollow-import-to=onnxruntime.quantization ^
    --nofollow-import-to=onnxruntime.quantization.* ^
    --nofollow-import-to=onnxruntime.transformers ^
    --nofollow-import-to=onnxruntime.transformers.* ^
    --nofollow-import-to=transformers ^
    --nofollow-import-to=transformers.* ^
    --nofollow-import-to=pylab ^
    --windows-icon-from-ico=assets/icon.ico ^
    --company-name="SnappyAI" ^
    --product-name="SnappyAI" ^
    --file-version="1.0.0" ^
    --product-version="1.0.0" ^
    --copyright="2026 SnappyAI" ^
    --low-memory ^
    --remove-output ^
    --show-progress ^
    %MAIN_SCRIPT%

if errorlevel 1 (
    echo Nuitka compilation failed!
    exit /b 1
)

REM ---------------------------------------------------------------------
REM Post-process: organize output
REM ---------------------------------------------------------------------
echo [5/9] Organizing output...

REM The .dist folder is named after MAIN_SCRIPT (main.dist), not PROJECT_NAME.
if not exist "%DIST_DIR%\%PROJECT_NAME%.exe" (
    echo ERROR: expected executable not found: "%DIST_DIR%\%PROJECT_NAME%.exe"
    exit /b 1
)

REM Move the exe to the clean root
move /y "%DIST_DIR%\%PROJECT_NAME%.exe" "%OUT_DIR%\" >nul

REM Copy every remaining top-level file (DLLs, .pyd modules, data) next to the
REM exe. Nuitka's bootloader loads python3xx.dll and extension modules relative
REM to the exe, so they must stay in the application root.
for /f "delims=" %%F in ('dir /a-d /b "%DIST_DIR%" 2^>nul') do (
    if /i not "%%~nxF"=="%PROJECT_NAME%.exe" (
        copy /y "%DIST_DIR%\%%F" "%OUT_DIR%\" >nul
    )
)

REM Copy remaining subdirectories (package dirs like cv2, numpy, etc.) to app root
for /d %%D in ("%DIST_DIR%\*") do (
    if /i not "%%~nxD"=="__pycache__" (
        xcopy /s /i /y "%%D" "%OUT_DIR%\%%~nxD\" >nul 2>&1
    )
)

REM Python bytecode cache (if produced) goes to cache folder
if exist "%DIST_DIR%\__pycache__" (
    xcopy /s /i /y "%DIST_DIR%\__pycache__" "%OUT_DIR%\cache\" >nul 2>&1
)

REM ---------------------------------------------------------------------
REM Create default configs if missing
REM ---------------------------------------------------------------------
echo [6/9] Creating default configuration...
if not exist "%OUT_DIR%\configs\config.json" (
    if exist configs\config.json (
        copy configs\config.json "%OUT_DIR%\configs\" >nul 2>&1
    ) else (
        echo {} > "%OUT_DIR%\configs\config.json"
    )
)
if not exist "%OUT_DIR%\configs\config.ini" (
    if exist configs\config.ini (
        copy configs\config.ini "%OUT_DIR%\configs\" >nul 2>&1
    ) else (
        type nul > "%OUT_DIR%\configs\config.ini"
    )
)
if not exist "%OUT_DIR%\configs\reticle_presets.json" (
    type nul > "%OUT_DIR%\configs\reticle_presets.json" 2>nul
)

REM ---------------------------------------------------------------------
REM Copy recordings library template if present
REM ---------------------------------------------------------------------
echo [7/9] Staging recordings...
if exist recordings\macros.json (
    copy recordings\macros.json "%OUT_DIR%\recordings\" >nul 2>&1
)

REM ---------------------------------------------------------------------
REM Create convenience launcher batch
REM ---------------------------------------------------------------------
echo [8/9] Creating launcher...
(
    echo @echo off
    echo REM SnappyAI launcher - sets up paths and runs the application
    echo cd /d "%%~dp0"
    echo set SNAPPYAI_HOME=%%~dp0
    echo set PATH=%%~dp0;%%PATH%%
    echo %PROJECT_NAME%.exe %%*
) > "%OUT_DIR%\run.bat"

REM ---------------------------------------------------------------------
REM Cleanup build artifacts
REM ---------------------------------------------------------------------
echo [9/9] Cleaning build artifacts...
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%" >nul 2>&1
if exist "%TEMP_BUILD_DIR%" rmdir /s /q "%TEMP_BUILD_DIR%" >nul 2>&1

REM ---------------------------------------------------------------------
REM Summary
REM ---------------------------------------------------------------------
echo.
echo =====================================================================
echo BUILD COMPLETE
echo =====================================================================
echo Output: %OUT_DIR%
echo.
echo Layout:
echo   %PROJECT_NAME%\
echo   ^|-- %PROJECT_NAME%.exe          (main executable + runtime DLLs)
echo   ^|-- run.bat                     (convenience launcher batch)
echo   ^|-- configs\                    (config.json, config.ini, reticle_presets.json)
echo   ^|-- recordings\                 (macros.json)
echo   ^|-- models\                     (ONNX models + hpose_bundle.npz)
echo   ^|-- capture\                    (interception.dll)
echo   ^|-- driver\                     (ViGEmClient.dll, interception.dll)
echo   ^|-- docs\                       (documentation)
echo   ^|-- logs\                       (snappyai.log + rotated)
echo   ^|-- reports\                    (latency report.html / report.md)
echo   ^|-- scripts\exec\               (apply.ps1, maintenance.ps1, ...)
echo   ^|-- scripts\undo\               (undo.ps1)
echo   ^|-- cache\                      (temp files)
echo   ^|-- *.dll, *.pyd, cv2/, numpy/, onnxruntime/, etc. (runtime files)
echo.
echo To distribute: zip the entire "%PROJECT_NAME%" folder.
echo =====================================================================

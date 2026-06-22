@echo off
chcp 65001 >nul
set VENV=build_env
set NAME=ModelDownloader

echo === Step 1: Check aria2c ===
if exist "aria2c.exe" (echo   aria2c.exe found) else (echo   [WARNING] aria2c.exe missing!)

echo === Step 2: Clean old build ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo === Step 3: Build exe ===
"%VENV%\Scripts\pyinstaller.exe" --noconsole --onefile --name "%NAME%" ^
    --add-data "static;static" ^
    --add-data "download_models.ps1;." ^
    --add-data "get-model-lists.py;." ^
    --add-data "aria2c.exe;." ^
    --hidden-import=huggingface_hub ^
    --hidden-import=requests ^
    launcher.py

echo === Step 5: Copy external files ===
copy favorite_list.json dist\ /y >nul 2>&1
echo === Done ===
echo Output:
echo   dist\%NAME%.exe
echo   dist\favorite_list.json
pause

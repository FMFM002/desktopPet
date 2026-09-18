@echo off
chcp 65001 >nul
title 清理临时文件（PTRTS1.9.0）
cd /d "%~dp0"

echo.
echo  即将清理两处「AI 的沙箱动不了」的东西：
echo    1) tools\dev_tests\_llm_tmp  和  tools\dev_tests\_scratch_a  两个空目录
echo    2) D:\AppData\Ollama\models\blobs 里的半截下载残片（约 4.36GB）
echo.
echo  正式模型文件不会动；脚本会再问你一次。
echo.
echo  （如果这里一闪就没了，改用 PowerShell 直接跑下面这行：）
echo    ^& "%~dp0_cleanup_190.ps1"
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_cleanup_190.ps1"

echo.
echo  ---- 脚本已结束（退出码 %errorlevel%）----
pause

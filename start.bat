@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 약품관리 서버 (이 창을 닫으면 꺼져요)
if not exist ".venv\Scripts\python.exe" (
  echo 먼저 install.bat 을 실행해 주세요.
  pause
  exit /b 1
)
set FIRST=1
:loop
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q --disable-pip-version-check
if "%FIRST%"=="1" start "" http://localhost:8000
set FIRST=0
".venv\Scripts\python.exe" server.py
rem 업데이트 후에는 3번 코드로 끝나서 자동으로 다시 시작
if %errorlevel%==3 (
  echo 업데이트를 적용해 다시 시작합니다...
  goto loop
)
echo 서버가 멈췄어요. 이 창의 메시지를 확인하고 start.bat 을 다시 실행해 주세요.
pause

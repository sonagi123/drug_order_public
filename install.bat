@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================
echo   약품관리 프로그램 설치
echo ============================================
echo.

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY (
  echo [1/3] 파이썬을 설치합니다. 잠시 기다려 주세요...
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  ) else (
    echo.
    echo 파이썬 설치를 확인하지 못했어요. https://www.python.org 에서 Python 3.12를 설치한 뒤
    echo 이 파일을 다시 실행해 주세요. 설치할 때 "Add python.exe to PATH"를 체크하세요.
    pause
    exit /b 1
  )
) else (
  echo [1/3] 파이썬 확인 완료
)

git --version >nul 2>&1
if errorlevel 1 (
  echo [2/3] Git을 설치합니다. ^(업데이트에 필요^)
  winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
) else (
  echo [2/3] Git 확인 완료
)

echo [3/3] 프로그램에 필요한 구성요소를 설치합니다...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 (
  echo 구성요소 설치에 실패했어요. 인터넷 연결을 확인하고 다시 실행해 주세요.
  pause
  exit /b 1
)

echo.
echo 설치가 끝났어요.
echo  - 지금 실행: start.bat
echo  - 윈도우 켤 때 자동 실행: autostart.bat
echo  - 태블릿/다른 PC에서 접속 허용: firewall.bat ^(마우스 오른쪽 - 관리자 권한으로 실행^)
echo.
pause

@echo off
chcp 65001 >nul
rem 서버로 쓸 PC(항상 켜 두는 PC)에서 처음 한 번만: Git 설치 → 프로그램 내려받기 → install.bat 실행
echo ============================================
echo   약품관리 처음 설치 (C:\drug_order)
echo ============================================
git --version >nul 2>&1
if errorlevel 1 (
  echo Git을 설치합니다...
  winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
  set "PATH=%PATH%;C:\Program Files\Git\cmd"
)
if exist "C:\drug_order\server.py" (
  echo 이미 C:\drug_order 에 설치돼 있어요. install.bat 을 실행합니다.
) else (
  echo 프로그램을 내려받습니다... (로그인 필요 없음)
  git clone https://github.com/sonagi123/drug_order_public C:\drug_order
  if errorlevel 1 (
    echo 내려받기에 실패했어요. 인터넷 연결을 확인하고 다시 실행해 주세요.
    pause
    exit /b 1
  )
)
cd /d C:\drug_order
call install.bat

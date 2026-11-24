@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   리포트 비밀번호 초기화
echo ============================================
echo 서버 PC에서만 실행할 수 있어요. 리포트를 다음에 열 때 새 비밀번호를 정하게 돼요.
echo.
set /p OK=초기화할까요? (Y/N): 
if /i not "%OK%"=="Y" exit /b 0
".venv\Scripts\python.exe" -c "import sqlite3; c=sqlite3.connect(r'data\pharmacy.db'); c.execute(\"UPDATE settings SET value='' WHERE key='report_pw'\"); c.commit(); print('초기화했어요. 리포트를 열면 새 비밀번호를 정할 수 있어요.')"
pause

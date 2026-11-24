@echo off
chcp 65001 >nul
net session >nul 2>&1
if errorlevel 1 (
  echo 이 파일은 마우스 오른쪽 버튼 → "관리자 권한으로 실행" 으로 열어 주세요.
  pause
  exit /b 1
)
netsh advfirewall firewall delete rule name="DrugOrder 8000" >nul 2>&1
netsh advfirewall firewall add rule name="DrugOrder 8000" dir=in action=allow protocol=TCP localport=8000 profile=private,domain
echo 병원 내부망에서 8000번 포트로 접속할 수 있게 허용했어요.
pause

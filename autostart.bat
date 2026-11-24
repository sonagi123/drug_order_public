@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\DrugOrder.lnk'); $s.TargetPath='%~dp0start.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Set-Content -Path ([Environment]::GetFolderPath('Desktop')+'\DrugOrder.url') -Value \"[InternetShortcut]`r`nURL=http://localhost:8000\""
if errorlevel 1 (
  echo 자동 실행 등록에 실패했어요.
) else (
  echo 윈도우를 켤 때 약품관리가 자동으로 실행되도록 등록했어요.
  echo 바탕화면에 DrugOrder 바로가기도 만들었어요. 더블클릭하면 약품관리가 열려요.
  echo 해제하려면 시작프로그램 폴더^(Win+R → shell:startup^)에서 DrugOrder 를 지우세요.
)
pause

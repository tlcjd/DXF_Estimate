@echo off
setlocal EnableExtensions
chcp 65001 >nul
rem git 저장소(이 폴더)의 최신 소스를 실제 실행·테스트 폴더(..\@@테스트 웹)로 복사합니다.
rem 테스트 폴더의 캐시(web_trial\data), 설정, zip, 기존 데스크톱 파일은 지우거나 덮어쓰지 않습니다.
cd /d "%~dp0"
set "TARGET="
for /D %%D in ("%~dp0..\@@*") do if not defined TARGET set "TARGET=%%~fD"
if not defined TARGET (
  echo ERROR: 테스트 폴더를 찾지 못했습니다.
  pause
  exit /b 1
)
where git >nul 2>nul
if not errorlevel 1 (
  echo [1/2] GitHub 최신본 받기
  git pull --ff-only
  if errorlevel 1 (
    echo ERROR: git pull 실패. 저장소 폴더에 커밋 안 된 변경이 있는지 확인하세요.
    pause
    exit /b 1
  )
)
echo [2/2] 테스트 폴더로 복사: %TARGET%
robocopy "%~dp0app" "%TARGET%\app" /E /XD __pycache__ /NFL /NDL /NJH /NP >nul
if errorlevel 8 goto fail
robocopy "%~dp0web_trial" "%TARGET%\web_trial" /E /XD __pycache__ data /XF root_path.txt /NFL /NDL /NJH /NP >nul
if errorlevel 8 goto fail
robocopy "%~dp0." "%TARGET%" README.md 웹_시험판_실행.bat /NFL /NDL /NJH /NP >nul
if errorlevel 8 goto fail
for /f %%H in ('git rev-parse --short HEAD 2^>nul') do echo 반영 완료 - 커밋 %%H
echo 이제 테스트 폴더의 웹_시험판_실행.bat 로 실행하세요.
pause
exit /b 0
:fail
echo ERROR: 복사 중 오류가 발생했습니다.
pause
exit /b 1

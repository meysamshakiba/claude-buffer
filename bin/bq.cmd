@echo off
REM bq - queue a task without needing a Claude session.
REM Works when you are locked out, which is the entire point.
REM
REM   bq "fix the off-by-one in axi_fifo.sv line 214"
REM   bq            show queue + daemon status
REM   bq log [N]    tail the daemon log
REM   bq stop
REM   bq autostart  keep a daemon running from now on
setlocal
if "%BUFFER_SKILL_DIR%"=="" set "BUFFER_SKILL_DIR=%USERPROFILE%\.claude\skills\buffer"
if "%PYTHON%"=="" set "PYTHON=python"
set "Q=%BUFFER_SKILL_DIR%\scripts\buffer_queue.py"
set "D=%BUFFER_SKILL_DIR%\scripts\drain.py"
set "A=%BUFFER_SKILL_DIR%\scripts\autostart.py"

if "%~1"=="" (
  "%PYTHON%" "%Q%" list
  "%PYTHON%" "%D%" --status
  exit /b %ERRORLEVEL%
)
if "%~1"=="log" (
  set "N=%~2"
  if "%~2"=="" set "N=30"
  call "%PYTHON%" "%D%" --tail %%N%%
  exit /b %ERRORLEVEL%
)
if "%~1"=="stop" (
  "%PYTHON%" "%D%" --stop
  exit /b %ERRORLEVEL%
)
if "%~1"=="status" (
  "%PYTHON%" "%Q%" status
  "%PYTHON%" "%D%" --status
  exit /b %ERRORLEVEL%
)
if "%~1"=="report" (
  set "H=%~2"
  if "%~2"=="" set "H=12"
  call "%PYTHON%" "%D%" --report %%H%%
  exit /b %ERRORLEVEL%
)
if "%~1"=="autostart" (
  set "SUB=%~2"
  if "%~2"=="" set "SUB=install"
  call "%PYTHON%" "%A%" --%%SUB%%
  exit /b %ERRORLEVEL%
)

"%PYTHON%" "%Q%" add %*
if errorlevel 1 exit /b %ERRORLEVEL%
REM Make sure something will actually run it.
"%PYTHON%" "%D%" --status >nul 2>&1 || "%PYTHON%" "%D%" --daemon --watch
exit /b %ERRORLEVEL%

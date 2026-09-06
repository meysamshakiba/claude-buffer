@echo off
REM bq - queue a task without needing a Claude session.
REM Works when you are locked out, which is the entire point.
REM
REM   bq "fix the off-by-one in axi_fifo.sv line 214"
REM   bq            show queue + daemon status
REM   bq log [N]    tail the daemon log
REM   bq report [H] what ran, what changed, what needs you
REM   bq sessions   every attempt and how it ended
REM   bq summary ID the handover written when a limit interrupted a task
REM   bq stop
REM   bq autostart  keep a daemon running from now on
REM
REM Dispatch is by goto, not by parenthesised blocks: inside a block %ERRORLEVEL%
REM is substituted when the block is parsed -- before the command in it runs --
REM so every exit code came back as whatever preceded it, and a failed command
REM looked like success.
setlocal
if "%BUFFER_SKILL_DIR%"=="" set "BUFFER_SKILL_DIR=%USERPROFILE%\.claude\skills\buffer"
if "%PYTHON%"=="" set "PYTHON=python"
set "Q=%BUFFER_SKILL_DIR%\scripts\buffer_queue.py"
set "D=%BUFFER_SKILL_DIR%\scripts\drain.py"
set "A=%BUFFER_SKILL_DIR%\scripts\autostart.py"
set "S=%BUFFER_SKILL_DIR%\scripts\sessions.py"

if "%~1"==""          goto :overview
if /i "%~1"=="log"       goto :log
if /i "%~1"=="stop"      goto :stop
if /i "%~1"=="status"    goto :status
if /i "%~1"=="sessions"  goto :sessions
if /i "%~1"=="summary"   goto :summary
if /i "%~1"=="report"    goto :report
if /i "%~1"=="autostart" goto :autostart

REM Queue subcommands go to the queue. Without this `bq list` enqueued a task
REM called "list", which the daemon then dutifully ran as a prompt.
set "QCMDS= add list peek claim heartbeat done fail requeue reset remove clear "
echo %QCMDS% | findstr /i /c:" %~1 " >nul
if not errorlevel 1 goto :queue
goto :addtask

:overview
"%PYTHON%" "%Q%" list
"%PYTHON%" "%D%" --status
exit /b %ERRORLEVEL%

:log
set "N=%~2"
if "%N%"=="" set "N=30"
"%PYTHON%" "%D%" --tail %N%
exit /b %ERRORLEVEL%

:stop
"%PYTHON%" "%D%" --stop
exit /b %ERRORLEVEL%

:status
"%PYTHON%" "%Q%" status
"%PYTHON%" "%D%" --status
exit /b %ERRORLEVEL%

:sessions
"%PYTHON%" "%S%"
exit /b %ERRORLEVEL%

:summary
"%PYTHON%" "%S%" --summary %2
exit /b %ERRORLEVEL%

:report
set "H=%~2"
if "%H%"=="" set "H=12"
"%PYTHON%" "%D%" --report %H%
exit /b %ERRORLEVEL%

:autostart
set "SUB=%~2"
if "%SUB%"=="" set "SUB=install"
"%PYTHON%" "%A%" --%SUB%
exit /b %ERRORLEVEL%

:queue
"%PYTHON%" "%Q%" %*
exit /b %ERRORLEVEL%

:addtask
"%PYTHON%" "%Q%" add %*
if errorlevel 1 exit /b %ERRORLEVEL%
REM Make sure something will actually run it.
"%PYTHON%" "%D%" --status >nul 2>&1 || "%PYTHON%" "%D%" --daemon --watch
exit /b %ERRORLEVEL%

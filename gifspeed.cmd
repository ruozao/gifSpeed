@echo off
rem gifspeed launcher -- picks a Python interpreter that actually has Pillow.
rem
rem Usage is identical to `python -m gifspeed`:
rem     gifspeed.cmd --info demo.gif
rem     gifspeed.cmd demo.gif --speeds 0.5,2,5,10 -o out
rem     gifspeed.cmd --gui
setlocal enabledelayedexpansion
set "HERE=%~dp0"
set "PY="

call :probe python
if defined PY goto :run
call :probe py
if defined PY goto :run
for %%V in (314 313 312 311 310) do (
    call :probe "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    if defined PY goto :run
)

echo [gifspeed] No Python interpreter with Pillow was found.
echo            Install it:              python -m pip install Pillow
echo            Or point at one directly: "C:\path\to\python.exe" -m gifspeed --help
exit /b 2

:probe
"%~1" -c "import PIL" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0

:run
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
"%PY%" -m gifspeed %*
exit /b %errorlevel%
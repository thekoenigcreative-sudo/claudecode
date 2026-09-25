@echo off
rem One-time: store Rick's IBKR login in Windows Credential Manager for IB Gateway (IBC).
rem Double-click this file. It asks for the IBKR username and password; the password is not
rem shown as you type. Nothing is written to a file. See scripts\ibkr_login_setup.py.
title IB Gateway - store the IBKR login (one time)
"C:\venvs\asx-bot\Scripts\python.exe" "%~dp0scripts\ibkr_login_setup.py" %*
echo.
pause

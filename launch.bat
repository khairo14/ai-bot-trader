@echo off
:: AI Bot Trader v2.5.0 - Windows launcher
:: Delegates to launch.ps1 so PowerShell handles all logic.
:: -ExecutionPolicy Bypass means users never need to change their system policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1"
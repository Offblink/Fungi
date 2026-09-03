@echo off
cd /d "%~dp0"
if "%~1"=="" (python -m fungi --server) else (python -m fungi %*)

@echo off
cd /d "%~dp0"
echo [Fungi] NOTE: clients must join from hosts on the SAME LAN (same router/subnet).
echo [Fungi] Copy the join command printed below onto client machines as-is (port changes on restart).
if "%~1"=="" (python -m fungi --server) else (python -m fungi %*)

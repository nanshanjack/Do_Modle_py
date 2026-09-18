@echo off
rem Python launcher for this project (isolated venv).
rem Usage:
rem   .\py.cmd scripts\verify.py
rem   .\py.cmd -m pytest
rem   .\py.cmd scripts\verify.py --skip-network
"C:\Users\Administrator\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" %*

@echo off
title Shinsei Pricing
cd /d C:\Users\fabio\Downloads\shinsei_pricing\shinsei_pricing
echo.
echo ================================
echo   SHINSEI PRICING - INICIANDO
echo ================================
echo.
echo [1/2] Iniciando servidor...
start "Shinsei Server" cmd /k "python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload"
timeout /t 3 /nobreak > nul
echo [2/2] Iniciando ngrok...
start "Shinsei Ngrok" cmd /k "ngrok http 8000"
echo.
echo Abrindo simulador...
timeout /t 5 /nobreak > nul
start http://127.0.0.1:8000/simulador

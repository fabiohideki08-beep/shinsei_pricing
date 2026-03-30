@echo off
title SHINSEI MARKET
cd /d "%~dp0"

echo Iniciando Shinsei Pricing...
echo Instalando dependencias...
python -m pip install -r requirements.txt

echo Iniciando sistema...
python -m uvicorn app:app --host 127.0.0.1 --port 8000

pause
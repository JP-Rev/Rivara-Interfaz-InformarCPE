@echo off
rem Genera el CSV de CTG de soja para informar a Visec.
rem Doble clic. Trabaja siempre en la carpeta donde esta este archivo.

chcp 65001 >nul
cd /d "%~dp0"
title Generar CTG de soja para Visec

rem Busca Python: primero el lanzador "py", despues "python".
set PYTHON=
where py >nul 2>nul && set PYTHON=py
if not defined PYTHON where python >nul 2>nul && set PYTHON=python
if not defined PYTHON (
    echo No se encontro Python en este equipo.
    echo Instalalo desde https://www.python.org/downloads/windows/
    echo y marca la opcion "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

echo ================================================
echo  CTG de soja para informar a Visec
echo ================================================
echo.
echo Formato de fecha: AAAA-MM-DD   (ejemplo: 2026-10-01)
echo Enter para usar la fecha de hoy.
echo.

set DESDE=
set HASTA=
set /p DESDE=Desde:
set /p HASTA=Hasta (Enter = igual que desde):

set ARGS=
if not "%DESDE%"=="" set ARGS=--desde %DESDE%
if not "%HASTA%"=="" set ARGS=%ARGS% --hasta %HASTA%

echo.
%PYTHON% ctg_soja.py %ARGS%

echo.
pause

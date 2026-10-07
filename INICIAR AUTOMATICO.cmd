@echo off
cd /d "%~dp0"
python automatizar_duas.py --verificar-online %*
set "eset_task_result=%errorlevel%"
echo.
if "%eset_task_result%"=="0" (
    echo As duas licencas foram salvas na pasta resultados.
) else (
    echo A execucao encontrou um erro. Consulte automatizacao.log.
)
pause
exit /b %eset_task_result%

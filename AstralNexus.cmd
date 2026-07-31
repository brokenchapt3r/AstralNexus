@echo off
setlocal
python -m AstralNexus.cli.main %*
exit /b %ERRORLEVEL%

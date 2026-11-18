@echo off
rem Runs the SEO report runner without typing the Python path or setting PYTHONPATH:
rem     seo clients check
rem     seo report open --client <key>
rem The runner settings are found in %LOCALAPPDATA%\JasperSEOReporting\config\runner.yaml unless --config is given.
setlocal
set "PYTHONPATH=%~dp0src"
"%~dp0.venv\Scripts\python.exe" -m seo_report %*
exit /b %ERRORLEVEL%

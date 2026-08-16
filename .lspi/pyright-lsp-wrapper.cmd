@echo off
setlocal EnableDelayedExpansion

if "%~1"=="--version" (
  pyright --version
  exit /b !errorlevel!
)

if "%~1"=="--help" (
  pyright-langserver --help
  exit /b !errorlevel!
)

if "%~1"=="-h" (
  pyright-langserver --help
  exit /b !errorlevel!
)

pyright-langserver %*

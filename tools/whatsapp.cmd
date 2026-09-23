@echo off
setlocal
rem ---------------------------------------------------------------------------
rem  Command line front end for the installed WhatsApp connector.
rem
rem  The connector's engine is usable from a terminal, which is the quickest way
rem  to link an account, check an installation, or test against a real account
rem  without building a workflow. This wrapper finds Designer's interpreter and
rem  the installed tool's bundled libraries so you do not have to.
rem
rem  Usage:
rem      whatsapp doctor
rem      whatsapp link --phone +15550100
rem      whatsapp status
rem      whatsapp sync
rem      whatsapp chats
rem      whatsapp read --limit 20
rem      whatsapp send --to "+15550100" --message "hello"
rem      whatsapp prune --days 90
rem
rem  Every command accepts --profile NAME (default: default).
rem
rem  NOTE: plain setlocal, deliberately. With "enabledelayedexpansion" the shell
rem  expands !...! in the command line, which silently strips every exclamation
rem  mark from the arguments - so `send --message "Done!"` sent "Done". The
rem  directory search below is written as a subroutine so it needs no delayed
rem  expansion at all.
rem ---------------------------------------------------------------------------

rem -- Designer's embedded Python: the same interpreter the tools run on -------
set "PYEXE="
for /d %%D in ("%ProgramFiles%\Alteryx\bin\Python\python-3.13*-embed-amd64") do set "PYEXE=%%D\python.exe"
if not defined PYEXE (
    echo ERROR: Could not find Alteryx Designer's Python 3.13 interpreter under
    echo        "%ProgramFiles%\Alteryx\bin\Python".
    echo        Is Designer 2026.1 installed?
    exit /b 1
)

rem -- The installed tool's bundled libraries ---------------------------------
rem  Prefer a per-user install, fall back to all-users, then to a local build.
set "PKG="
for /d %%D in ("%APPDATA%\Alteryx\Tools\WhatsAppInput_*") do call :use "%%D\site-packages"
for /d %%D in ("%ProgramData%\Alteryx\Tools\WhatsAppInput_*") do call :use "%%D\site-packages"
call :use "%LOCALAPPDATA%\Alteryx\WhatsAppConnector-build\_stage"

if not defined PKG (
    echo ERROR: Could not find the installed WhatsApp connector.
    echo        Looked in:
    echo          %APPDATA%\Alteryx\Tools\WhatsAppInput_*
    echo          %ProgramData%\Alteryx\Tools\WhatsAppInput_*
    echo        Install the .yxi first, with Designer closed.
    exit /b 1
)

rem -- Run. The embedded interpreter ignores PYTHONPATH because of its ._pth   --
rem -- file, so the path is injected in-process instead.                       --
"%PYEXE%" -c "import site,sys; site.addsitedir(sys.argv[1]); sys.argv=['whatsapp']+sys.argv[2:]; from whatsapp_core.cli import main; raise SystemExit(main())" "%PKG%" %*
exit /b %ERRORLEVEL%

rem -- Take the first location that exists; later calls never downgrade it. ---
:use
if defined PKG goto :eof
if exist "%~1" set "PKG=%~1"
goto :eof

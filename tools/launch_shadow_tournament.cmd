@echo off
setlocal
cd /d "%~dp0.."
rem This starts the local Shadow Tournament dashboard only. It never starts a broker or Demo worker.
set "SHADOW_TOURNAMENT_LOCAL=true"
poetry run streamlit run dashboard\app.py --server.headless false

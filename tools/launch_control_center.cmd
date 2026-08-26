@echo off
setlocal
cd /d "%~dp0.."
poetry run streamlit run dashboard\app.py --server.headless false

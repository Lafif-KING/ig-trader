# IG Trader Control Center MVP

The Control Center is a separate, read-only Streamlit dashboard. It explains
reviewed project status and public GitHub evidence without granting broker,
cloud, database, execution-lease, Demo, or Live authority.

## Start it locally on Windows

For the UI-MVP, double-click `tools\launch_control_center.cmd` in this clean
checkout. It starts only the local Streamlit process and opens the browser.
It does not start the trading robot, contact IG, or make a broker mutation.
Closing the browser does not send a Stop, Kill, Flatten, or any broker action.
Use `Ctrl+C` in the launcher window when you want to stop the local dashboard.

1. In **Windows PowerShell**, open the clean dashboard clone—not a protected
   trading checkout—and run:

   ```powershell
   cd C:\Users\AfifB\projects\ig-trader-dashboard
   ```

   This moves PowerShell into the dashboard clone. The safe expected result is a
   prompt ending in `ig-trader-dashboard>`.

2. Run:

   ```powershell
   poetry sync --with dashboard
   ```

   This installs locked local dependencies including Streamlit. It does not
   contact IG, Azure, PostgreSQL, or a broker.

3. Run:

   ```powershell
   poetry run streamlit run dashboard/app.py
   ```

   Your browser should open automatically at the local dashboard address. Press
   `Ctrl+C` in the same PowerShell window to stop it. Stop if a command reports
   an error and review the sanitized output before trying a different command.

The MVP reads committed `project/gates.json` and optional public GitHub status.
It makes no broker, Lightstreamer, PostgreSQL, Azure, or execution calls. A
GitHub workflow pass is technical evidence only; it never enables Shadow, Demo,
or Live execution.

## UI-MVP operator navigation

The local Control Center has eight operator pages: **Cockpit**, **Market
Scanner**, **Strategy Center**, **Positions**, **Performance**, **Decision
Explorer**, **Risk & Health**, and **Demo Operator**.

The normal source is reviewed project gates plus a sanitized local DQ-02
operator snapshot. It does not silently substitute fake data. Set
`CONTROL_CENTER_MODE=MOCK` only when developing the interface; every mock page
then displays `SIMULATED UI DATA` and all Demo controls remain disabled.

Optional external research summaries are supplied only through the local
`CONTROL_CENTER_RESEARCH_SUMMARIES` environment variable, which may list JSON
files separated by the normal Windows path separator. They are read-only status
evidence: even a file that claims approval cannot grant execution authority.

The Start button remains disabled until all documented authority gates pass. In
the current project state, it is disabled because the approved Demo strategy
registry is empty and execution authority is OFF. The Control Center never
calls an IG order endpoint; its local controls invoke only the existing DQ-02
controller boundary.

## Automatic updates and degraded mode

Public GitHub data is cached for 60 seconds and refreshes while its browser
session remains open. **Refresh public GitHub status** clears only that local
cache. If GitHub is unavailable, the app shows
`GITHUB DATA TEMPORARILY UNAVAILABLE` and still renders the reviewed static
roadmap from `project/gates.json`.

`project/gates.json` is the reviewed governance source. Public GitHub data can
update the displayed main SHA, pull requests, and workflow metadata, but cannot
modify a governance gate. `DEMO_EXECUTION` and `LIVE_EXECUTION` remain disabled
until a reviewed change explicitly says otherwise.

## Strategy Lab page

The **Strategy Lab** page reads only local generated files in
`artifacts/strategy_lab/`. It filters research evidence but has no execution or
promotion control. If artifacts have not been generated, it states that data is
unavailable rather than inventing results.

## Progress numbers

The dashboard intentionally does not calculate one “percent to Live” number.

- **Engineering foundation progress** is weighted technical evidence for
  FOUNDATION gates.
- **Trading-authority readiness** is weighted governance passes for SHADOW,
  DEMO, and LIVE gates. Disabled and unauthorized gates receive no credit.

## Container image

`Dockerfile.dashboard` is a non-root, read-only-source image serving Streamlit
on port 8501. It copies neither trading source nor credentials and has no
trading worker entrypoint. Build it locally with:

```powershell
docker build -f Dockerfile.dashboard .
```

Do not publish or deploy this image as part of the MVP.

# IG Trader Control Center MVP

The Control Center is a separate, read-only Streamlit dashboard. It explains
reviewed project status and public GitHub evidence without granting broker,
cloud, database, execution-lease, Demo, or Live authority.

## Start it locally on Windows

1. In **Windows PowerShell**, open the clean dashboard clone—not a protected
   trading checkout—and run:

   ```powershell
   cd C:\Users\AfifB\projects\ig-trader-dashboard
   ```

   This moves PowerShell into the dashboard clone. The safe expected result is a
   prompt ending in `ig-trader-dashboard>`.

2. Run:

   ```powershell
   poetry sync
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

## Signals Migrators (Opsgenie & PagerDuty) → FireHydrant

Scripts and helpers to migrate teams, users, on‑call schedules/rotations, overrides and escalation policies into FireHydrant Signals from Opsgenie or PagerDuty.  Scripts are written and maintained by Firehydrant Customer Success Engineering.

### Repo layout
- `signals-migrator-opsgenie.py` (OG → FH)
- `signals-migrator-pagerduty.py` (PD → FH)
- `migrate-teams.py` (legacy wrapper; uses the Opsgenie migrator)
- `config.env` (local credentials)
- `migrator_ledger.json`, `migrator_ledger_pd.json` (record what was created for safe revert)

---

## Quick start

1) Create a virtualenv and install deps

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2) Put API tokens in `config.env`

```ini
# Required for both migrators
FIREHYDRANT_API_KEY=fhb_xxx

# Opsgenie migrator
OPSGENIE_API_KEY=og_xxx
OPSGENIE_BASE_URL=https://api.opsgenie.com           # optional (defaults)

# PagerDuty migrator
PAGERDUTY_API_TOKEN=PD_xxx
PAGERDUTY_BASE_URL=https://api.pagerduty.com         # optional (defaults)
```

You can also set keys interactively:

```bash
# Opsgenie migrator
python3 signals-migrator-opsgenie.py --configure
# PagerDuty migrator
python3 signals-migrator-pagerduty.py --configure
```

---

## How it works (high‑level)

### Opsgenie → FireHydrant
- Fetch OG teams, users and schedules (including rotation details and time-restrictions)
- Interactive selection per team: create a new FH team or map to an existing one
- Add members to FH team (email‑based matching)
- Create on‑call schedules/rotations and best‑effort apply time‑restrictions
- Migrate schedule‑level and rotation‑level overrides (if not `--no-overrides`)
- Map OG escalation policies to FH policy payloads and create them

### PagerDuty → FireHydrant
- Fetch PD teams/services/schedules/users (grouping controlled by flags)
- Add members to FH team, create schedules and rotations, assign rotation members
- Migrate PD schedule overrides
- Enforce PD weekly boundaries in FH by overlaying PD on‑call segments as FH overrides (configurable)
- Optionally align the current FH active shift to PD “who’s on call now”

---

## Opsgenie migrator CLI

```bash
python3 signals-migrator-opsgenie.py [flags]
```

- `--set-fh-token VALUE`  Save `FIREHYDRANT_API_KEY` to `config.env`
- `--set-og-token VALUE`  Save `OPSGENIE_API_KEY` to `config.env`
- `--configure`           Interactive prompt to set keys
- `--preview-team NAME|ID` Show a detailed JSON preview to stdout for that OG team
- `--dry-run`             No writes. Generates previews:
  - If `--output FILE.json` is provided, writes a consolidated JSON.
  - Otherwise, writes one file per team under `previews/` plus an index file.
- `--output PATH`         Output path or directory for previews
- `--delete-team`, `--delete` VALUE[,CSV]  Delete FH team(s) by name or id
- `--delete-schedule ID --team-id TEAM_ID` Delete FH schedule by id
- `--restriction-strategy weekly|per-day`  Rotation restriction mapping strategy
- `--timezone-override TZ` Force schedule timezone (e.g. `America/New_York`)
- `--no-overrides`        Skip override migration
- `--align` / `--no-align` Enable/disable immediate on‑call alignment (default on)
- `--verify-only`         Validate connectivity; no writes
- `--verbose`             Debug payloads and full API responses
- `--force`               Skip confirmations for destructive actions
- `--overrides [FILTER] [--yes]`  Backfill OG overrides into FH after migration
  - `FILTER` can be a FH team name (apply to all its schedules) or schedule name (contains match)
  - Without `--yes`, shows a preview and asks to apply

---

## PagerDuty migrator CLI

```bash
python3 signals-migrator-pagerduty.py [flags]
```

Auth & setup
- `--set-fh-token VALUE`  Save `FIREHYDRANT_API_KEY`
- `--set-pd-token VALUE`  Save `PAGERDUTY_API_TOKEN`
- `--configure`           Interactive prompt to set keys and optional PD base URL

General
- `--pd-no-teams`         Treat PD as “no Teams” (group by services or global)
- `--pd-group-by teams|services`  Controls grouping bucket
- `--preview-team NAME|ID`  PD team preview to stdout
- `--dry-run` / `--output PATH`  Write `migration_preview_pd_*.json`
- `--verify-only`, `--verbose`, `--force`
- `--delete-team VALUE[,CSV]` or `--delete`
- `--revert-all`          Delete FH teams previously created by this migrator (uses `migrator_ledger_pd.json`)
- `--delete-schedule ID --team-id TEAM_ID`
- `--restriction-strategy weekly|per-day`, `--timezone-override TZ`
- `--no-overrides`        Skip PD overrides migration
- `--align` / `--no-align`

Alignment helpers
- `--align-now [NAME]`
  - `--team-id ID` or `--team NAME`
  - `--fh-schedule-id ID` or `--schedule NAME`
  - `--pd-schedule-id ID | --pd-schedule NAME`
  - `--pd-service-id ID | --pd-service NAME`
  - Aligns FH active shift to the current PD on‑call (resolves users by email; falls back to `/users/{id}` if PD omits emails in `/oncalls`).

Boundary parity (PD → FH)
- Environment flag: `PD_ENFORCE_BOUNDARIES=true` (default)
  - After schedule creation, the migrator fetches PD on‑call segments via `/oncalls?since&until` for the next N weeks and applies them as FH overrides to guarantee exact start/end parity with PD’s weekly handoff.
  - Disable with `PD_ENFORCE_BOUNDARIES=false` if you want raw FH rotation shifts only.

---

## Common workflows

Migrate Opsgenie teams interactively
```bash
python3 signals-migrator-opsgenie.py
```

Preview Opsgenie team (no writes)
```bash
python3 signals-migrator-opsgenie.py --preview-team "Customer Success"
```

Backfill Opsgenie overrides for a single FH team’s schedules
```bash
python3 signals-migrator-opsgenie.py --overrides "cweber-signals-migration" --yes
```

Migrate PagerDuty teams/services and enforce PD boundaries
```bash
python3 signals-migrator-pagerduty.py --pd-group-by teams
# or
python3 signals-migrator-pagerduty.py --pd-group-by services
```

Align a specific FH schedule to PD current on‑call
```bash
python3 signals-migrator-pagerduty.py --align-now "On-call Payment" \
  --team "Payment Processing" \
  --pd-schedule-id PPZHJEZ
```

Revert (delete) all FH teams created by the PD migrator
```bash
python3 signals-migrator-pagerduty.py --revert-all --force
```

---

## Output & ledgers
- Per‑team OG previews are written under `previews/` by default in `--dry-run`.
- PD consolidated preview: `migration_preview_pd_YYYYMMDDTHHMMSSZ.json` (pass `--output` to customize).
- Creation ledgers:
  - `migrator_ledger.json` (Opsgenie) and `migrator_ledger_pd.json` (PagerDuty)
  - Used by `--revert-all` and pruned automatically by maintenance scripts.

---

## Environment variables (advanced)
- `MIGRATOR_CONCURRENCY` (default 16)
- `TIMEZONE_OVERRIDE`
- `RESTRICTION_STRATEGY` (`weekly` | `per-day`)
- `PD_ENFORCE_BOUNDARIES=true|false`
- `DELETE_TEAM_IF_NO_EPS=true|false` (OG EPs)
- `EP_MIN_TIMEOUT_SECONDS` (OG EP step timeout clamping)

---

## Notes
- Users are matched by email. Ensure identity parity between OG/PD and FH.
- Creating teams is idempotent per run decision; if you re-run and choose “create” again with the same name, FH will allow duplicates. Prefer mapping to existing FH teams on subsequent migrations.
- FireHydrant API capabilities vary by tenant; where direct rotation edits are not available, the migrators use safe schedule‑level PATCH or overrides to achieve parity.

---

## Troubleshooting
- Set `--verbose` to print payloads and API responses.
- For PD “on‑call now” when `/oncalls` omits emails, the PD migrator resolves `/users/{id}` automatically.
- If you see “Not found” for legacy endpoints, the migrator already falls back to supported endpoints (schedule PATCH, shift PATCH, override POST).

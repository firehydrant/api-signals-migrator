## Opsgenie → FireHydrant mapping (Signals migration)

This document summarizes how Opsgenie entities are mapped into FireHydrant Signals by the migrator scripts, and highlights non‑1:1 areas and likely error states designers should account for.

### Core entities
- **Team (Opsgenie)** → **Team (FireHydrant)**
  - Members are added to the FH team.
  - Unknown users can be created (by email) or skipped.
- **User (Opsgenie)** → **User (FireHydrant)**
  - Matched primarily by email; created on demand if missing.
- **Schedule (Opsgenie)** → **On‑call Schedule (FireHydrant)**
  - Name and time zone carried over.
  - Created under a team; contains one or more rotations.
- **Rotation (Opsgenie)** → **Rotation (FireHydrant)**
  - Strategy mapped:
    - Weekly → weekly (handoff_day, handoff_time)
    - Daily → daily (handoff_time)
    - Other (hour/day lengths) → custom with ISO‑8601 `shift_duration`
  - Start time normalized (see edge cases).
- **Time restriction (Opsgenie rotation.timeRestriction)** → **Restrictions (FH rotation)**
  - Also optionally mirrored at schedule level for UI “Shift Hours”.
- **Override (Opsgenie schedule/rotation)** → **Override (FH rotation‑level)**
  - OG schedule‑level overrides are applied against the FH rotation (FH overrides are rotation‑scoped).
- **On‑calls (OG “who’s on‑call now”)** → **Active shift/user (FH)**
  - Used to align current on‑call; not a first‑class object in migration—achieved via shifts/memberships.
- **Escalation policy (Opsgenie)** → **Escalation policy (FireHydrant)**
  - Steps mapped to FH “notify” steps with resolved targets (schedules/users).

### Non‑1:1 areas and edge cases
- **Multiple rotations per OG schedule**
  - Some flows create a single FH schedule with one rotation; others may emit multiple FH schedules (e.g., “Name - Rotation N”). Designs should not assume strict 1:1 schedule/rotation cardinality.
- **Time restrictions shape differences**
  - OG may return a single object or an array; day bounds can be missing.
  - FH expects a normalized list of windows. Missing/loose data is defaulted (e.g., Mon–Fri or full week).
- **Strategy translation nuances**
  - Weekly/daily map cleanly; other units become custom with computed `PT…H` durations.
  - Very old or ambiguous rotation start times are clamped to “now” to avoid invalid windows.
- **Schedule‑level overrides in OG**
  - FH only supports rotation‑level overrides. Migration flattens schedule‑level OG overrides to a rotation context (can be lossy for complex OG semantics).
- **Team targets in escalation policies**
  - OG EP steps can target teams. FH EPs may not accept team targets directly; such targets may be skipped or need alternative handling.
- **EP step timeouts**
  - FH enforces minimum timeout granularity; OG minute values are normalized (seconds‑minimum safety). Extremely small OG delays may be increased.
- **User identity and presence**
  - Users missing in FH: created (via email) or skipped. Steps/schedules referencing unknown users are pruned, causing partial migrations.
- **Existing resource collisions**
  - Exact‑name schedule reuse is attempted. Case/spacing differences can lead to unexpected reuse or duplicates.
- **Time zones**
  - Invalid/missing OG time zones default to UTC (or an override). Bad TZs can cause `400 Bad Request` on FH schedule create.
- **Shift generation behavior**
  - FH may ignore rotation memberships at create‑time; migrator retries with PATCH/PUT to ensure shifts and membership are set.

### Typical error states to design for
- **Create/patch validation errors**
  - FH schedule create (`POST /teams/{team_id}/on_call_schedules`) can fail on:
    - Unsupported or missing `strategy` fields
    - Invalid `time_zone`
    - Inconsistent `restrictions` shape
    - Malformed or too‑old `start_time`
  - UX: show which field failed, surface payload details (name, tz, strategy), and propose defaults.
- **Unknown users**
  - If an OG user email doesn’t exist in FH and creation is disabled/fails, related memberships/targets are skipped.
  - UX: list skipped users and downstream effects (missing rotation members, EP steps).
- **Unresolvable targets in EPs**
  - Schedule targets are resolved by name→ID; mismatches break the step.
  - Team targets may be unsupported.
  - UX: show per‑step resolution status and what was omitted.
- **Partial migrations**
  - Some schedules/rotations/steps succeed while others are skipped.
  - UX: provide a per‑team summary with counts and actionable next steps.

### API footprint (for reference)
- **FireHydrant** (base: `https://api.firehydrant.io/v1`)
  - Teams: `GET/POST /teams`, `GET/PATCH/DELETE /teams/{id}`
  - Users: `GET/POST /users`
  - On‑call schedules: `GET/POST /teams/{team_id}/on_call_schedules`
  - Schedule item: `GET/PATCH/DELETE /teams/{team_id}/on_call_schedules/{schedule_id}`
  - Rotations: `GET/PUT /teams/{team_id}/on_call_schedules/{schedule_id}/rotations/{rotation_id}`
  - Rotation memberships: `POST /teams/{team_id}/on_call_schedules/{schedule_id}/rotations/{rotation_id}/memberships`
  - Rotation overrides: `POST /teams/{team_id}/on_call_schedules/{schedule_id}/rotations/{rotation_id}/overrides`
  - Shifts (under schedule): `GET/PATCH /teams/{team_id}/on_call_schedules/{schedule_id}/shifts[/ {shift_id}]`
  - Escalation policies: `GET/POST /teams/{team_id}/escalation_policies`
- **Opsgenie** (base: `https://api.opsgenie.com/v2`)
  - Teams: `GET /teams`, `GET /teams/{id}`
  - Users: `GET /users`
  - Schedules: `GET /schedules`, `GET /schedules/{id}`
  - Schedule overrides: `GET /schedules/{id}/overrides`
  - Schedule on‑calls: `GET /schedules/{id}/on-calls`
  - Schedule timeline: `GET /schedules/{id}/timeline?...`
  - Escalations: `GET /escalations?teamId={id}`

### Notes for designers
- **Surface precise validation feedback** from FH when schedule/rotation creation fails; it directly points to strategy/tz/restrictions issues.
- **Call out non‑migrated pieces** (team targets in EPs, unknown users, schedule‑level overrides) and provide remediation hints.
- **Summarize per‑team outcomes** (created, reused, skipped) to guide follow‑up actions.


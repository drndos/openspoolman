# agents.md — OpenSpoolMan

This document is for AI coding agents (and humans) making changes to **OpenSpoolMan**.
Follow it as the default “operating manual” when creating PRs.

## 1) Project intent (do not drift)
OpenSpoolMan augments SpoolMan with Bambu Lab / AMS awareness and optional NFC workflows:
- Keep all operations **local-first** (LAN where possible).
- NFC is **optional**; the web UI must remain fully usable without NFC.
- The system is an “adapter + UI” on top of SpoolMan, not a replacement.

If a proposed change alters any of these fundamentals, stop and propose it as a design discussion first.

---

## 2) Non-negotiables (hard rules)
### Security & privacy
- Never commit secrets (printer access codes, API keys, cookies, tokens, personal URLs).
- Do not log secrets. Mask them if you must log configuration.
- Treat everything coming from MQTT / HTTP as untrusted input.

### Backwards compatibility
- Preserve existing env vars and default behaviors unless explicitly versioned.
- UI behavior must remain functional for:
  - No NFC usage
  - SpoolMan available/unavailable (graceful handling)
  - AUTO_SPEND disabled

### Reliability
- Network calls must have **timeouts**, error handling, and retry/backoff where appropriate.
- Never introduce busy loops. Prefer event-driven updates or bounded polling.

---

## 3) Repository map (high-level)
Key folders/files you will interact with:
- `app.py` / `wsgi.py`: application entry points
- `templates/`, `static/`: server-rendered UI assets
- `mqtt_bambulab.py`: Bambu printer connectivity (LAN / MQTT)
- `spoolman_client.py`, `spoolman_service.py`: SpoolMan integration layer
- `filament.py`, `filament_usage_tracker.py`, `print_history.py`: domain logic
- `scripts/`: helper scripts (e.g., initialization / tooling)
- `data/`: runtime artifacts (DBs, mismatch logs)
- `tests/`: Python tests
- `e2e/`, `playwright.config.js`, `package.json`: end-to-end UI tests
- `docker-compose.yaml` / `compose.yaml` / `Dockerfile`: containerization
- `helm/openspoolman`: Helm chart

---

## 4) How to run locally (known-good paths)

### 4.1 Local Python run (development)
1. Configure environment (see §5). Create `config.env` from `config.env.template` or export env vars.
2. Start the server:
   - `python wsgi.py`

Notes:
- Default listen port is `8001` (to avoid clashing with SpoolMan).
- Depending on SSL mode and mapping you may also access `https://<host>:8443`.

### 4.2 Docker (deployment / reproducible dev)
- Configure env vars, then:
  - `docker compose up -d`

Use `docker compose port openspoolman 8001` to see mapped host port if needed.

### 4.3 Kubernetes (Helm)
- Use the bundled chart:
  - `helm dependency update helm/openspoolman`
  - `helm upgrade --install openspoolman helm/openspoolman -f values.yaml --namespace openspoolman --create-namespace`
- Validate:
  - `kubectl get pods -n openspoolman`

---

## 5) Configuration contract (environment variables)
### Required / core
- `OPENSPOOLMAN_BASE_URL`
  - HTTPS URL where OpenSpoolMan is reachable
  - **No trailing slash**
  - Required for NFC writes
- `PRINTER_ID`
  - Printer settings → Setting → Device → Printer SN
- `PRINTER_ACCESS_CODE`
  - Setting → LAN Only Mode → Access Code
  - (LAN Only Mode toggle may stay off)
- `PRINTER_IP`
  - Setting → LAN Only Mode → IP Address
- `SPOOLMAN_BASE_URL`
  - URL of SpoolMan without trailing slash

### Feature toggles
- `AUTO_SPEND`
  - `True` enables legacy slicer-estimate tracking.
- `TRACK_LAYER_USAGE`
  - `True` switches to per-layer tracking/consumption **only if** `AUTO_SPEND=True`.
  - If `AUTO_SPEND=False`, tracking remains disabled regardless of `TRACK_LAYER_USAGE`.
- `DISABLE_MISMATCH_WARNING`
  - `True` hides mismatch warnings in the UI (still detected and logged).
- `CLEAR_ASSIGNMENT_WHEN_EMPTY`
  - `True` clears SpoolMan assignment and resets AMS tray when the printer reports an empty slot.

### Data sources
- Print history DB default: `data/3d_printer_logs.db`
- Override via: `OPENSPOOLMAN_PRINT_HISTORY_DB`
- Mismatch log output: `data/filament_mismatch.json`

### Important operational note
If you change `OPENSPOOLMAN_BASE_URL`, NFC tags must be reconfigured.

---

## 6) SpoolMan integration contract (must remain stable)
### SpoolMan label workflow
- SpoolMan can print QR-code labels. When using them with OpenSpoolMan:
  - Set SpoolMan’s base URL to OpenSpoolMan **before** generating labels
  - Otherwise labels point back to SpoolMan, not OpenSpoolMan

### Required extra fields in SpoolMan
Agents must not “simplify away” these fields without an explicit migration plan.

Add these extra fields in SpoolMan:
- Filaments:
  - `type` (Choice)
  - `nozzle_temperature` (Integer Range)
  - `filament_id` (Text)
- Spools:
  - `tag` (Text)
  - `active_tray` (Text)

(Exact choice values are defined in the README; keep behavior compatible with existing installations.)

### Windows note (Bambu Studio)
Filament IDs can be sourced from Bambu Studio’s filament base directory (see README). Do not hardcode user paths; keep it documentation-only.

---

## 7) Filament matching rules (do not regress)
OpenSpoolMan matches SpoolMan spools to AMS tray metadata:
- Spool `material` must match AMS `tray_type` (main type).
- For Bambu filaments, AMS reports a sub-brand; it must match the spool’s sub-brand.
  - Model this either as:
    - `material = full Bambu material` (e.g., `PLA Wood`) and `type` empty, OR
    - `material = base` (e.g., `PLA`) and `type = add-on` (e.g., `Wood`)
- Parenthesized notes in `material` are ignored during matching (e.g., `PLA CF (recycled)`).

If matching fails:
- Prefer improving diagnostics and tooling.
- The UI warning can be hidden with `DISABLE_MISMATCH_WARNING=true` but mismatches must still be logged.

---

## 8) Change workflow for agents (how to work in this repo)

### 8.1 Before coding
1. Read `README.md` sections: installation, environment configuration, matching rules, AUTO_SPEND notes.
2. Identify the minimal module(s) involved:
   - Printer connectivity: `mqtt_bambulab.py`
   - SpoolMan calls: `spoolman_client.py` / `spoolman_service.py`
   - Domain logic: `filament*.py`, `print_history.py`
   - UI: `templates/`, `static/`
3. Decide whether you need:
   - Python tests (`tests/`)
   - E2E tests (`e2e/` via Playwright)

### 8.2 Coding standards (practical)
- Keep functions small and testable.
- Prefer explicit types where they improve clarity (especially for payloads).
- Validate external payloads defensively (missing keys, type mismatches).
- When reading runtime state (e.g., `PRINTER_STATE`, MQTT payloads), prefer accessing the original object via `.get(...)` rather than copying into temporary locals unless the value needs transformation; this keeps guard logic close to the source and avoids stale snapshots.
- Avoid introducing new dependencies without a strong justification.
- Keep logging structured and helpful; never leak secrets.

### 8.3 Testing expectations
Minimum expectations before PR:
- If logic changes: update/add Python tests under `tests/`.
- If UI changes: ensure at least a smoke check and, when possible, run the E2E suite.
- If env/config changes: update README + `config.env.template` accordingly.

Notes:
- Python tests are configured via `pytest.ini`.
- E2E is set up via `playwright.config.js` and `package.json`. Use the existing npm scripts rather than inventing new ones unless necessary.

### 8.4 PR checklist (agents must include in PR description)
- [ ] Scope is minimal; no unrelated refactors
- [ ] No secrets or sensitive values introduced
- [ ] Errors handled (timeouts, retries/backoff if applicable)
- [ ] Tests added/updated (or justification if not)
- [ ] README/config updated if behavior or configuration changed
- [ ] Docker/Helm impact considered (ports, env vars, volumes)
- [ ] Filament matching rules preserved (or explicitly enhanced with tests)

---

## 9) Deployment artifacts (keep in sync)
If you touch runtime behavior, check:
- Docker:
  - `Dockerfile`
  - `docker-compose.yaml` / `compose.yaml` env var passing and volumes
- Helm:
  - `helm/openspoolman` chart values and templates
  - Ensure env vars and defaults align with README

Do not silently change exposed ports or default bindings without updating:
- README
- Compose
- Helm chart

---

## 10) Troubleshooting guidance (for maintainers and future agents)
When debugging:
- Confirm `SPOOLMAN_BASE_URL` and `OPENSPOOLMAN_BASE_URL` have **no trailing slash**.
- Confirm printer values:
  - `PRINTER_IP` reachable from the OpenSpoolMan host/container
  - `PRINTER_ACCESS_CODE` correct
- Inspect mismatch log:
  - `data/filament_mismatch.json`
- Confirm print history DB path:
  - `data/3d_printer_logs.db` or `OPENSPOOLMAN_PRINT_HISTORY_DB`

For AUTO_SPEND / tracking:
- Ensure `AUTO_SPEND=True` before expecting any tracking.
- `TRACK_LAYER_USAGE=True` only matters when `AUTO_SPEND=True`.

---

## 11) What not to do (common failure modes)
- Do not hardcode user-specific paths, hostnames, or ports.
- Do not break “no NFC” operation.
- Do not require cloud access for core workflows.
- Do not change matching semantics without tests and clear migration notes.
- Do not broaden logs to include access codes or private URLs.

---

## 12) AMS tray assignment behavior
- Cloud prints already contain `ams_mapping` in their `project_file` payload, so OpenSpoolMan can map every logical filament to a tray immediately.
- Local prints (LAN mode) do not ship `ams_mapping` upfront, so we delay applying AMS mappings until the printer reports a concrete `tray_tar` (typically during stage 4 / filament change). That’s why the MQTT log often shows `tray_tar=255` for seconds and only flips to the real tray once the tray itself is loaded.

---

## 13) When you are unsure
Prefer these options, in order:
1. Add instrumentation and tests rather than guessing.
2. Make the smallest change that improves correctness.
3. Document assumptions in the PR description and in code comments where necessary.

End of file.

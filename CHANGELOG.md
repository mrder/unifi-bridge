# Changelog

Format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Versioning: `master` carries beta versions (`0.0.x`), released whenever a
milestone is reached there; `main` carries actual releases (`0.x.0`), cut from
`master` once a beta has proven itself. Nothing has graduated to `main` yet --
see TODO.md for what's still open before that happens.

## [Unreleased]

## [0.0.1] - 2026-09-14 (beta, master)

Initial public version. The core adoption loop works end-to-end against a
real controller.

### Added
- `south_adapter/dlink_webui.py`: full D-Link DGS-1250-28X management via its
  own web UI's HTTP endpoints -- VLAN CRUD, per-port VLAN tagging, port
  status/utilization (read), port config incl. enable/disable (write), LLDP,
  config save. Live-tested against real hardware.
- `north_adapter/inform_protocol.py` + `discovery_protocol.py`: UniFi
  discovery + inform wire protocol, both the default-key AES-CBC handshake
  and the post-adoption AES-GCM path (16-byte nonce, AAD = full header --
  confirmed by reading the controller's own crypto code, not guessed).
- `bridge_daemon.py`: the persistent process tying both together --
  discovery, waiting for the "Adopt" click, the full key-exchange handshake,
  ongoing inform loop reporting real switch state, and best-effort
  translation of controller-pushed config back onto the switch (currently:
  VLAN creation from `vlansToDeploy`; see TODO.md for the rest).
- `switch_profiles.py`: registry of supported switches and their fake UniFi
  identity, so adding a new switch model is additive, not a core-code change.
- `webui.py`: setup/status web UI (default container entrypoint) -- pick
  switch type, optional network scan, controller address, live status/log,
  version display, update check (checks GitHub only, never self-updates).
- `Dockerfile` / `docker-compose.yml` / `.env.example`: container deployment.
- Empirical finding, documented in README: the controller's legacy L2
  discovery listener accepts older/"classic" UniFi switch platforms
  (`USXG24`, `US24`, `US48`) but rejects newer "Enterprise XG" platforms
  (`USWED72`, `USWED76`) with "unknown model" even though they're valid
  catalog entries -- this is why an older model is impersonated rather than
  the closest capability match.

### Known limitations (see TODO.md)
- Config-push translation covers VLAN creation only; per-port VLAN
  assignment, PoE, STP, ACLs from a pushed config are logged, not applied.
- `cmd` responses (reboot/upgrade/setdefault) are logged, never executed.
- The Docker build/compose flow has not yet been run end-to-end on real
  Unraid hardware.

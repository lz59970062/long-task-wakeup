# Committee Discussion: Safe Daemon Upgrade and Proxy Injection

**Date**: 2026-07-22
**Project root**: `/home/lizhi_2024/program3/long-task-wakeup`
**Scope**: Review the uncommitted 0.4.3 changes for persistent proxy injection, systemd HUP hot reload, and safe upgrade behavior when callbacks are being delivered.
**User constraints**: Do not interrupt existing long-running tasks or live callback delivery workers during an upgrade; add defensive safeguards; review after implementation.

## Expert Panel

| Expert | Role | Personality | Primary lens | Signature approach | Agent ID | Status |
|---|---|---|---|---|---|---|
| Priya Nair | Python reliability engineer | Detail-obsessed | Process lifecycle, locks, crash recovery | Enumerates every signal and restart race | pending | pending |
| Mateo Silva | Platform security engineer | Security-paranoid | Environment-file secrets and privilege boundaries | Follows secret bytes from input to process and logs | pending | pending |
| Hannah Lee | Developer-experience maintainer | Pragmatic minimalist | CLI behavior, backwards compatibility, operational safety | Tests whether an operator can make the safe choice by default | pending | pending |

## Round 1: Independent Reviews

Pending expert responses.

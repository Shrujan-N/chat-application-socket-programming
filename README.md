# Real-Time Chat Application

**Team 5 · Section K**  
**Shrujan N · Sameer Deshpande · Soumya A**

A real-time chat application built with Python sockets, FastAPI, WebSockets, and SQLite. The repository contains both a terminal client/server implementation and a browser-based chat application. The web application brings together private and group messaging, account management, media sharing, message collaboration features, WebRTC call signaling, and operational monitoring.

## Contents

- [Overview](#overview)
- [Features](#features)
- [Technology stack](#technology-stack)
- [Architecture](#architecture)
- [Getting started](#getting-started)
- [Run the web application](#run-the-web-application)
- [Run the terminal client](#run-the-terminal-client)
- [Configuration](#configuration)
- [Testing](#testing)
- [Project documentation](#project-documentation)
- [Security and operational notes](#security-and-operational-notes)

## Overview

The project demonstrates real-time communication through web interface:

A browser UI served by FastAPI, with REST endpoints for account and application operations and WebSockets for live chat events.


## Features

### Accounts and contacts

- User registration and login.
- PBKDF2-HMAC-SHA256 password hashing, signed HS256 session tokens, session restoration, and logout.
- Account lockout after repeated failed login attempts (5 failures by default) and a password-reset flow using email delivery configuration.
- Contact requests, acceptance, removal, registered-user discovery, and profiles.
- Profile display names, status/bio text, and avatar upload/removal.

### Private and group messaging

- One-to-one and group conversations in the web UI.
- Group creation with a name, icon, and selected members.
- In the browser app: group creation, group listing, member-aware access, persistent group history, and live group messaging. The TCP client/server also provides group administration commands for adding/removing members, renaming groups, changing icons, and managing administrator roles.
- Persistent message history, live WebSocket delivery, and offline/reconnect synchronization.
- Message delivery/read receipts, typing indicators, retry handling, and message de-duplication.
- Message editing, deletion, and replies/threads with quoted previews.
- Emoji reactions with toggle behavior and live updates.
- `@username` group mentions, member-aware autocomplete, and persistent mention notifications.
- Search across contacts and messages.

### Media and notifications

- File and image attachments.
- Browser voice-message recording and playback.
- Searchable GIF and sticker picker with GIPHY integration.
- Browser push notifications through a service worker and VAPID configuration.
- Voice/video call UI with WebRTC signaling for private and group calls, including call history.

### Integrity, privacy, and operations

- SHA-256 message-content integrity checks.
- A basic browser-side encryption prototype using Web Crypto: ECDH P-256 and AES-GCM for direct-message key agreement, with conversation-scoped AES-GCM handling for group/offline paths. See the security notes below; this is not an independently audited protocol.
- Multiple active browser sessions per user with message and status synchronization.
- Health liveness/readiness probes, availability history, and rolling SLO reporting.
- Message-delivery latency measurements and reporting.
- Scalability primitives such as sharded connection registries, bounded lock pools, and a load/benchmark harness.
- SQLite and uploads backup/restore utility with archive validation and SHA-256 checksums.
- Rotating server logs and request diagnostics.

## Technology stack

| Area | Technology |
|---|---|
| Language | Python |
| Web backend | FastAPI, Uvicorn |
| Real-time web transport | WebSockets |
| Terminal transport | Python `socket` and `threading` |
| Browser UI | HTML, CSS, JavaScript |
| Browser cryptography | Web Crypto API (`ECDH`, `AES-GCM`, SHA-256) |
| Calling | WebRTC peer connections and WebSocket signaling |
| Persistence | SQLite |
| Media and uploads | FastAPI multipart uploads, local upload storage |
| Notifications | Web Push, service worker, VAPID |
| Automated tests | pytest, FastAPI TestClient, WebSocket/integration tests |

## Architecture

```text
                         Browser
                            |
                 HTML / CSS / JavaScript
                            |
                FastAPI REST + WebSocket
                            |
        +-------------------+-------------------+
        |                   |                   |
   Auth/services       Message handling     Media/calls
        |                   |                   |
        +-------------------+-------------------+
                            |
                         SQLite
                            |
                    Upload directories

       Terminal client <-- TCP socket --> Terminal server
                            |
                      Core database
```

The web runtime is centered on `App/main.py` and `App/static/`. Database schema and connection handling are maintained in `App/web_database.py` and `App/database.py`; authentication, profiles, push, calls, reactions, availability, performance, and scalability logic are split into service modules. The terminal implementation is in `App/server.py` and `App/client.py`.

## Getting started

### Requirements

- Python 3.11 or newer is recommended. The project has also been exercised with Python 3.13.
- Windows PowerShell commands below are written for a Windows development environment.
- A modern browser for the web application. Microphone/camera features require browser media permissions and a secure context (localhost or HTTPS).
- A GIPHY API key for live GIF/sticker search.
- SMTP credentials for outbound password-reset email.
- VAPID keys and a valid VAPID subject for browser push notifications.

### Create a virtual environment

Run from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

If PowerShell blocks activation for the current terminal, use the execution-policy procedure approved for your machine, or invoke the environment interpreter directly with `.\.venv\Scripts\python.exe`.

### Install dependencies

Install application dependencies:

```powershell
python -m pip install -r requirements.txt
```

Install development/test dependencies:

```powershell
python -m pip install -r requirements-dev.txt
python -m pip install pytest httpx2
```

`pytest` is used by the automated test files. `httpx2` is required by the installed Starlette TestClient version in environments that report that dependency during test collection.

## Run the web application

From the repository root, activate the virtual environment and install dependencies, then:

```powershell
Set-Location .\App
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open the application at:

- Chat UI: <http://127.0.0.1:8000/>
- Interactive API documentation: <http://127.0.0.1:8000/docs>
- OpenAPI schema: <http://127.0.0.1:8000/openapi.json>
- Liveness probe: <http://127.0.0.1:8000/health/live>
- Readiness probe: <http://127.0.0.1:8000/health/ready>

Keep the server terminal open while using the application. Stop the server with `Ctrl+C`.

### Web application flow

1. Register an account or sign in with an existing account.
2. Discover users and send/accept contact requests.
3. Select a contact to start a private conversation, or use **+ Group** to create a group and choose members.
4. Select a conversation to load its history and send messages or supported media.
5. Use the conversation controls for replies, reactions, mentions, calls, and other available actions.

The `/docs` page lists the available REST endpoints. WebSocket chat and call signaling use the application's `/ws/{username}` endpoint with a session token.

## Run the terminal client

The terminal implementation runs separately from the browser server. Open two PowerShell terminals, activate the same virtual environment in both, and set the working directory to `App`.

**Terminal 1 — start the TCP server:**

```powershell
Set-Location .\App
python server.py
```

**Terminal 2 — start a client:**

```powershell
Set-Location .\App
python client.py
```

The terminal server defaults to `127.0.0.1:5000`. Start additional client processes to test communication between users. Use the command help shown by the client for private-chat, group, and account operations.

## Configuration

Set environment variables before starting the relevant process. Do not commit credentials, private keys, database files, session tokens, or `.env` files.

| Variable | Purpose |
|---|---|
| `CHAT_DB_PATH` | Core SQLite database path. |
| `CHAT_WEB_DB_PATH` | Web SQLite database path; falls back to `CHAT_DB_PATH` when unset. |
| `JWT_SECRET` | Secret used to sign session tokens. Set a stable, high-entropy value outside source control. |
| `JWT_SECRET_FILE` | File path used to load/create the token-signing secret when `JWT_SECRET` is not set. |
| `SESSION_TIMEOUT_MINUTES` | Session validity duration; defaults to 30 minutes. |
| `LOCKOUT_MINUTES` | Account lockout period after repeated failed login attempts; defaults to 15 minutes. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | SMTP settings for password-reset email. |
| `RESET_BASE_URL` | Base URL inserted into password-reset links. |
| `GIPHY_API_KEY` | API key used by the GIF/sticker search integration. |
| `VAPID_KEY_FILE`, `VAPID_SUBJECT` | VAPID key file and contact subject for Web Push. |
| `PUSH_TTL_SECONDS`, `PUSH_ALLOWED_HOSTS`, `PUSH_ALLOW_ANY_ENDPOINT` | Push expiration and endpoint policy controls. Review before deployment. |
| `MAX_AVATAR_BYTES` | Avatar upload size limit; defaults to 2 MiB in the profile service. |

Additional settings for monitoring and load evaluation are documented in `App/AVAILABILITY.md`, `App/PERFORMANCE_LATENCY.md`, and `App/SCALABILITY.md`. These include `CHAT_SLA_TARGET_PERCENT`, `CHAT_SLA_WINDOW_DAYS`, `CHAT_HEALTH_CHECK_INTERVAL_SECONDS`, `CHAT_HEALTH_URL`, `CHAT_LATENCY_TARGET_MS`, `CHAT_LATENCY_WINDOW_HOURS`, `CHAT_LATENCY_MIN_SAMPLES`, `CHAT_MAX_CONNECTIONS`, `CHAT_REGISTRY_SHARDS`, and `CHAT_BROADCAST_BATCH_SIZE`. Configure the health monitor and web server consistently so they use the same availability-history database path.

## Testing

Run from the repository root with the development dependencies installed:

```powershell
python -m pytest -v
```

Run the isolated HTTP/WebSocket integration runner:

```powershell
python tests/run_integration_tests.py
```

Run focused suites when working on a feature:

```powershell
python -m pytest App/test_group_chat.py -v
python -m pytest App/test_user_mentions_web.py -v
python -m pytest App/test_message_reactions.py -v
python -m pytest App/test_voice_video_calls.py -v
python -m pytest App/test_e2e_encryption.py -v
python -m pytest App/test_backup_restore.py -v
python -m pytest App/test_reconnection.py -v
python -m pytest App/test_scalability.py -v
python -m pytest App/test_performance.py -v
python -m pytest App/test_availability.py -v
```

The repository also contains tests for authentication, sessions, password reset, profiles, push notifications, file/voice uploads, GIFs/stickers, user safety, message integrity, threads/replies, portability, and maintainability.

## Project documentation

- [`README_WEB_INTEGRATION.md`](README_WEB_INTEGRATION.md) — web application setup and integration test runner.
- [`App/MAINTAINABILITY.md`](App/MAINTAINABILITY.md) — module responsibilities and development guidelines.
- [`App/MESSAGE_THREADS.md`](App/MESSAGE_THREADS.md) — message replies and thread behavior.
- [`App/MULTI_DEVICE_SYNC.md`](App/MULTI_DEVICE_SYNC.md) — multi-device synchronization behavior and operational limits.
- [`App/USER_MENTIONS_WEB.md`](App/USER_MENTIONS_WEB.md) — group mentions and notifications.
- [`App/DISASTER_RECOVERY.md`](App/DISASTER_RECOVERY.md) — backup, validation, storage, and restoration procedure.
- [`App/AVAILABILITY.md`](App/AVAILABILITY.md) — health checks and rolling availability reporting.
- [`App/PERFORMANCE_LATENCY.md`](App/PERFORMANCE_LATENCY.md) — message delivery latency measurement.
- [`App/SCALABILITY.md`](App/SCALABILITY.md) — scalability architecture and benchmark instructions.
- [`documents/`](documents/) — project SRS, test plan, architecture diagram, and use-case diagrams.

## Security and operational notes

- The browser encryption work is a basic project implementation, not an independently audited end-to-end encryption protocol. Direct messages use browser ECDH P-256/AES-GCM; group and some offline-contact paths use a conversation-scoped AES-GCM key. The browser stores its key-pair material in localStorage. Key lifecycle, secure group-key distribution, multi-device key transfer/recovery, XSS-resistant key storage, and external cryptographic review are not complete. Do not treat this feature as a production security guarantee.
- Keep `JWT_SECRET` stable and private in a deployed environment. If the signing secret changes or is regenerated, existing sessions may no longer validate.
- Use HTTPS, secure deployment secrets, restricted CORS/origin policies, rate limiting, and access-controlled storage for production deployments. Review upload validation and file-serving policy before exposing the service publicly.
- Push notifications need valid VAPID configuration and browser permission. Password reset requires working SMTP configuration. GIF search requires a configured GIPHY API key.
- The availability and latency modules measure observed behavior; they do not guarantee an SLA or a particular latency under all workloads.
- The scalability benchmark includes simulated clients/gateway components. A successful simulation is not proof of 100,000 real users on a specific production deployment. Validate infrastructure, database behavior, network capacity, and real WebSocket load separately.
- The backup utility includes SQLite databases and uploads but does not encrypt archives or schedule off-site copies. Stop the application for an application-wide consistent backup and follow `App/DISASTER_RECOVERY.md` for restore procedures.
- Ongoing call sessions are managed in process memory; call history is persisted in SQLite. Multi-process deployments require shared signaling/pub/sub infrastructure for cross-worker event delivery. Peer-to-peer media connectivity may also require deployment-specific STUN/TURN configuration.

---

This repository is a course/team project and should be deployed only after environment-specific security, infrastructure, and operational review.

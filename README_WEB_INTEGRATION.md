# Web UI integration

This package contains the web UI and FastAPI/WebSocket backend for the chat application. It includes private and group messages, contact requests, group member/admin management, typing indicators, delivery/read receipts, offline message delivery, message edit/delete, search, and file uploads.

## Install and run

From the repository root in PowerShell:

```powershell
python -m pip install -r requirements.txt
Set-Location App
python -m uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/`. API documentation is available at `http://127.0.0.1:8000/docs`. Keep the terminal running while using the app.

## Run integration tests

From the repository root:

```powershell
python -m pip install -r requirements-dev.txt
python tests/run_integration_tests.py
```

The integration runner creates an isolated temporary database and test server; it does not modify the app's regular database. It covers HTTP endpoints, contacts, group management, search, uploads, WebSocket messaging, typing, receipts, editing/deletion, duplicate IDs, authorization checks, and offline delivery.

## Important limitation

This is a local/demo implementation. The user selector is **not authentication**; users can impersonate accounts by selecting their names. Do not expose this app publicly as-is. Before deployment, implement authenticated sessions, authorization based on the authenticated identity, CSRF/origin protections where applicable, rate limits, upload content validation, production secrets/configuration, and a deployment database strategy.

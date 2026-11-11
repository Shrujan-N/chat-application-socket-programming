# Web UI integration

The browser application uses the FastAPI/WebSocket backend and authenticated sessions. It includes private and group messaging, typing indicators, group administration, delivery/read receipts, offline message delivery, message editing and deletion, message replies, complete contact/group/message search, E2E encryption support, and file uploads.

## Install and run

From the repository root in PowerShell:

```powershell
python -m pip install -r requirements.txt
Set-Location App
python -m uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/`. API documentation is available at `http://127.0.0.1:8000/docs`. Register an account and log in; protected API and WebSocket operations use the authenticated session token.

## Browser features added

- **Typing indicators (FR-13):** private and group typing/typing-stop WebSocket events; indicators expire automatically if a stop event is missed.
- **Group management (FR-15):** group admins can rename groups, change icons, add/remove members, and promote/demote admins. The creator remains an admin.
- **Edit messages (FR-16):** senders can edit their own messages; integrity hashes are recalculated and the edited state is persisted and synchronized.
- **Delete messages (FR-17):** members can delete a message for themselves; only the sender can delete it for everyone. Per-user deletions are hidden in history, sync, and search.
- **Search (FR-20):** search results include contacts, group names, private messages, and group messages. E2E-encrypted message candidates are decrypted and filtered locally in the browser; the candidate window covers the 500 most recent authorized messages, while server-searchable plaintext matches are included from history.
- **Replies/threads (FR-24):** private and group messages can be replied to; previews and references are persisted and validated against the same conversation.

## Tests

From the repository root:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q App/test_group_chat.py App/test_message_threads.py App/test_reconnection.py App/test_data_integrity.py App/test_session.py
node --check App/static/browser_features.js
```

The browser feature tests use an isolated temporary database. They cover group admin operations, edit/delete permissions and persistence, group reply previews, typing-event routing, search authorization, and front-end feature hooks.

## Deployment note

The app has session-based authentication, but this repository is still a project/demo implementation. Before public deployment, review TLS and reverse-proxy configuration, CSRF/origin protections for cookie-authenticated flows (if introduced), rate limits, upload validation/storage, secret management, database backups, and operational monitoring.

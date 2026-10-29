# User Mentions (FR-25)

The browser group-chat UI supports `@username` mentions. Suggestions are drawn only from
members returned by the authenticated group-members endpoint. The server independently
resolves message mentions against current group membership; text that names a non-member
is not a notifiable mention.

When a group message is saved, the resolved mention list is stored with the message and a
notification record is stored for each mentioned member in the same SQLite transaction.
Online members receive a distinct WebSocket `mention_notification` event. Unread mention
notifications are available through `GET /api/mentions` and can be marked read with
`POST /api/mentions/{notification_id}/read`; they remain available across reloads until read.

Run the focused tests from the repository root:

```powershell
python -m pytest App/test_user_mentions_web.py -v
```

The migration adds `messages.mentioned_users` and creates `mention_notifications`; existing
private/group messages retain the default empty mention list.

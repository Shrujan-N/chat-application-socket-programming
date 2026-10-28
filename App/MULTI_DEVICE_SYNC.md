# Multi-device Synchronization (FR-26 / Issue #37)

## Behaviour

- Multiple WebSocket sessions for the same authenticated username can remain connected at once; opening a second device no longer disconnects the first.
- New outgoing messages are mirrored to the sender's other live devices, with the same message ID and SHA-256 content digest.
- Incoming messages are delivered to each connected recipient device. Existing persistence, checksum verification, reconnect/outbox retry, and missed-message synchronization remain in use.
- Sent, delivered, read, and delivery-latency status frames are fanned out to the sender's active devices. Read state is persisted in the messages table and message-receipt table, so subsequently loaded history reflects it.
- Per-device foreground visibility is tracked separately; the aggregate user visibility remains true if any connected device is visible, so push notifications are not triggered merely because one device is backgrounded.
- Profile updates are broadcast to all active devices for each affected account.

## Test

From the repository root run:

```powershell
python -m pytest App/test_reconnection.py -v
python -m pytest -v
```

The tests verify that two sessions can ping independently, a sender's second device sees a mirrored outgoing message, all recipient devices receive the incoming message, delivery/read statuses reach each sender device, and an unrelated user cannot forge a read receipt.

## Operational note

The current connection registry is in process memory. It synchronizes devices connected to the same running application process; a multi-worker or multi-server deployment would require a shared pub/sub layer (for example Redis) to propagate events between workers. Persisted message history remains the recovery path for a device that was offline.

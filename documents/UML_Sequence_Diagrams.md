# UML Sequence Diagrams — Chat Application

**Project:** Chat Application (Socket Programming)  
**Document ID:** UML-CHAT-SEQ-001  
**Version:** 1.0  
**Date:** 16 November 2026  
**Implementation baseline:** Current `main` branch; browser FastAPI/REST/WebSocket implementation.

These diagrams describe the current browser implementation. The terminal TCP client/server is a separate companion interface and is not represented in these two browser interaction flows.

## 1. Account login and protected-session validation

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant UI as Browser UI
    participant API as FastAPI Route / Middleware
    participant Auth as auth_service.py / security.py
    participant DB as SQLite User / Session Data

    User->>UI: Enter username and password
    UI->>API: POST /api/login {username, password}
    API->>Auth: authenticate(credentials)
    Auth->>DB: Load user, password hash and lockout state
    DB-->>Auth: User record and lockout state

    alt Account is locked
        Auth-->>API: Reject locked account
        API-->>UI: 423 Locked
        UI-->>User: Display account-lockout message
    else Credentials are invalid
        Auth->>DB: Update failed-login / lockout state
        DB-->>Auth: Update result
        Auth-->>API: Authentication failure
        API-->>UI: 401 Unauthorized
        UI-->>User: Display generic sign-in failure
    else Credentials are valid
        Auth->>DB: Record successful login / session state
        DB-->>Auth: Persisted
        Auth-->>API: Signed HS256 session token
        API-->>UI: 200 OK {token, expires_in}
        UI-->>User: Open authenticated application
    end

    User->>UI: Open a protected resource
    UI->>API: GET protected endpoint + Bearer token
    API->>Auth: Validate signature, expiry and revocation
    Auth->>DB: Check session / account state
    DB-->>Auth: Session state
    alt Token valid and authorized
        Auth-->>API: Authenticated identity / claims
        API-->>UI: 200 OK + resource data
        UI-->>User: Render protected resource
    else Token invalid, expired or revoked
        Auth-->>API: TokenError / unauthorized
        API-->>UI: 401 Unauthorized
        UI-->>User: Request sign-in again
    end
```

**Main controls represented:** password verification, account lockout, signed session-token issuance, token signature/expiry/revocation validation, and protected-route authorization. The login response and protected-resource status codes reflect the current SAD/API description.

## 2. Authenticated private/group message delivery, reply and receipts

```mermaid
sequenceDiagram
    autonumber
    actor Sender
    participant SUI as Sender Browser
    participant WS as FastAPI WebSocket Endpoint
    participant Guard as Session / Membership Validator
    participant DB as SQLite Messages / Receipts
    participant Hub as WebSocket Connection Manager
    participant RUI as Recipient Browser
    actor Recipient

    Sender->>SUI: Open conversation
    SUI->>WS: Connect /ws/{username}?token=...
    WS->>Guard: Validate token and username/path match
    Guard-->>WS: Authenticated identity
    WS-->>SUI: WebSocket accepted

    opt Recipient is online
        RUI->>WS: Connect with recipient session token
        WS->>Guard: Validate recipient session
        Guard-->>WS: Authenticated recipient identity
        WS-->>RUI: WebSocket accepted
    end

    Sender->>SUI: Send message (optional reply_to_msg_id)
    SUI->>WS: send_message or send_group_message frame
    WS->>Guard: Validate conversation membership and reply scope
    Guard-->>WS: Authorized / rejected

    alt Unauthorized or invalid frame
        WS-->>SUI: Error event / close 4401 or 4403 when authentication fails
        SUI-->>Sender: Show send failure
    else Authorized message
        opt Integrity metadata is supplied
            WS->>Guard: Verify message integrity hash
            Guard-->>WS: Integrity result
        end
        WS->>DB: Persist message and initial sent state
        DB-->>WS: Message ID / persisted row
        WS-->>SUI: status_update: sent
        WS->>Hub: Fan out message to recipient(s)
        Hub->>RUI: receive_message frame + reply preview
        RUI-->>Recipient: Render new message
        RUI->>WS: delivery_ack
        WS->>DB: Persist delivered receipt
        DB-->>WS: Receipt persisted
        WS->>Hub: Publish delivered status
        Hub-->>SUI: status_update: delivered

        Recipient->>RUI: Open / read conversation
        RUI->>WS: read_ack
        WS->>DB: Persist read receipt
        DB-->>WS: Receipt persisted
        WS->>Hub: Publish read status
        Hub-->>SUI: status_update: read
    end

    Note over WS,DB: For group messages, membership is checked and the message is fanned out to authorized group members.
    Note over SUI,RUI: Offline recipients receive messages through the application's history/reconnection synchronization path.
```

**Main controls represented:** authenticated WebSocket connection, token/path identity matching, private/group membership authorization, optional integrity validation, persistence before delivery, reply reference handling, and sent/delivered/read receipt updates.

## Implementation notes

- WebSocket authentication failures use close codes `4401` (invalid authentication) and `4403` (identity mismatch), as documented in the SAD.
- HTTP APIs remain responsible for account/resource management; WebSockets carry live messaging events.
- The diagram intentionally does not claim exactly-once network delivery. Message IDs, persistence and deduplication/receipt behavior are implementation mechanisms; transient network failure can still require reconnection and synchronization.
- These are design-level sequence diagrams aligned to the repository's documented interfaces, not packet captures or a guarantee that external providers are available.

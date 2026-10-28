# Message Threads / Replies (FR-24)

## How it works

- Use the **↩ Reply** action under a message to select it as the reply target.
- A reply preview appears above the message composer. Cancel it to return to normal sending.
- Sending a reply stores `reply_to_msg_id` with the new message. Existing databases are migrated automatically.
- The server only accepts a reply target from the same private conversation and verifies the target message integrity before returning its preview.
- Replies show a quoted author and content preview in live delivery and conversation history. Clicking the quote scrolls to and highlights the original message.
- Reply metadata is included in reconnect/sync responses, so quotes remain visible after a refresh or reconnect.

## Test

From the repository root, run:

```powershell
python -m pytest App/test_message_threads.py -v
python -m pytest -v
```

The patch adds reply metadata without changing existing message IDs, content hashes, status handling, retry deduplication, or the original message content.

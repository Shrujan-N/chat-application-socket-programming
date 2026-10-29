"""Small, side-effect-free helpers for FR-25 group @mentions."""
import re
import uuid

# Usernames accepted by registration: 3-30 ASCII letters, digits, underscores.
MENTION_PATTERN = re.compile(r"(?<![A-Za-z0-9_@])@([A-Za-z0-9_]{3,30})\b")


def extract_group_mentions(content: str, group_members, sender: str | None = None) -> list[str]:
    """Return unique, canonical usernames explicitly mentioned in this group.

    The server is authoritative: names are resolved only against the group's current
    members. Unknown users, duplicates, and self-mentions do not create alerts.
    """
    members_by_fold = {
        str(name).casefold(): str(name)
        for name in (group_members or [])
        if isinstance(name, str) and name.strip()
    }
    sender_fold = sender.casefold() if isinstance(sender, str) else None
    plain_text = re.sub(r"<[^>]*>", " ", str(content or ""))
    found: list[str] = []
    seen: set[str] = set()
    for match in MENTION_PATTERN.finditer(plain_text):
        canonical = members_by_fold.get(match.group(1).casefold())
        if not canonical or canonical.casefold() == sender_fold:
            continue
        folded = canonical.casefold()
        if folded not in seen:
            seen.add(folded)
            found.append(canonical)
    return found


def notification_id_for(msg_id: str, username: str) -> str:
    """Stable per-message/per-user identifier, so retries cannot duplicate alerts."""
    key = f"chat-user-mention:{msg_id}:{username.casefold()}"
    return "mention-" + uuid.uuid5(uuid.NAMESPACE_URL, key).hex

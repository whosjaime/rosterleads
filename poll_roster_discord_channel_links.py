"""
Scheduled Roster Discord channel poller for GitHub Actions.

This is for the non-live setup:
- GitHub Actions runs every 30 minutes.
- It checks configured Discord channels for recent messages.
- It extracts app.joinroster.co/jobs links only.
- It sends new Roster links to Monday.com using roster_to_monday_leads.py.
- It stores processed message IDs and URL hashes in roster_seen_state.json.

Required GitHub Secrets:
- DISCORD_BOT_TOKEN
- MONDAY_API_TOKEN
- ROSTER_DISCORD_ALLOWED_CHANNEL_IDS=123,456

Optional GitHub Secrets / env:
- MONDAY_BOARD_ID
- MONDAY_DEFAULT_GROUP_ID
- ROSTER_CHANNEL_GROUP_MAP
- ROSTER_DISCORD_LOOKBACK_LIMIT
- ROSTER_SEEN_STATE_FILE
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv

from roster_to_monday_leads import ROSTER_URL_REGEX, init_db, normalize_url, process_roster_url

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_ALLOWED_CHANNEL_IDS = [
    channel_id.strip()
    for channel_id in os.getenv("ROSTER_DISCORD_ALLOWED_CHANNEL_IDS", "").split(",")
    if channel_id.strip()
]
DISCORD_LOOKBACK_LIMIT = int(os.getenv("ROSTER_DISCORD_LOOKBACK_LIMIT", "50"))
SEEN_STATE_FILE = Path(os.getenv("ROSTER_SEEN_STATE_FILE", "roster_seen_state.json"))
DISCORD_API_BASE = "https://discord.com/api/v10"
MAX_STATE_ITEMS = int(os.getenv("MAX_STATE_ITEMS", "5000"))


def require_env() -> None:
    missing = []
    if not DISCORD_BOT_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not DISCORD_ALLOWED_CHANNEL_IDS:
        missing.append("ROSTER_DISCORD_ALLOWED_CHANNEL_IDS")
    if missing:
        raise RuntimeError(f"Missing required env values: {', '.join(missing)}")


def load_state() -> dict[str, Any]:
    if not SEEN_STATE_FILE.exists():
        return {"message_ids": [], "url_hashes": [], "updated_at": None}
    try:
        data = json.loads(SEEN_STATE_FILE.read_text(encoding="utf-8"))
        return {
            "message_ids": list(data.get("message_ids", [])),
            "url_hashes": list(data.get("url_hashes", [])),
            "updated_at": data.get("updated_at"),
        }
    except json.JSONDecodeError:
        return {"message_ids": [], "url_hashes": [], "updated_at": None}


def save_state(state: dict[str, Any]) -> None:
    state["message_ids"] = state.get("message_ids", [])[-MAX_STATE_ITEMS:]
    state["url_hashes"] = state.get("url_hashes", [])[-MAX_STATE_ITEMS:]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    SEEN_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def hash_url(url: str) -> str:
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def fetch_recent_messages(session: aiohttp.ClientSession, channel_id: str) -> list[dict[str, Any]]:
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    params = {"limit": min(max(DISCORD_LOOKBACK_LIMIT, 1), 100)}
    async with session.get(url, params=params) as response:
        if response.status >= 400:
            body = await response.text()
            raise RuntimeError(f"Discord fetch failed for channel {channel_id}: HTTP {response.status} {body}")
        return await response.json(content_type=None)


async def main() -> None:
    require_env()
    init_db()

    state = load_state()
    seen_message_ids = set(state.get("message_ids", []))
    seen_url_hashes = set(state.get("url_hashes", []))

    created_count = 0
    skipped_count = 0
    error_count = 0
    total_messages = 0
    total_urls_found = 0
    empty_content_messages = 0

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for channel_id in DISCORD_ALLOWED_CHANNEL_IDS:
            messages = await fetch_recent_messages(session, channel_id)
            total_messages += len(messages)
            print(f"Fetched {len(messages)} messages from Roster Discord channel {channel_id}")

            # Discord returns newest first. Process oldest first.
            for message in reversed(messages):
                message_id = str(message.get("id", ""))
                content = message.get("content") or ""

                if not content:
                    empty_content_messages += 1

                urls = ROSTER_URL_REGEX.findall(content)
                if urls:
                    print(f"Message {message_id} contains {len(urls)} Roster URL(s)")

                total_urls_found += len(urls)

                # Important: do NOT mark messages without URLs as seen.
                # If Message Content Intent was off, Discord can return empty content.
                # Marking those empty messages as seen would permanently skip them later.
                if not urls:
                    continue

                if message_id in seen_message_ids:
                    skipped_count += len(urls)
                    continue

                for url in urls:
                    url_digest = hash_url(url)
                    if url_digest in seen_url_hashes:
                        skipped_count += 1
                        continue

                    try:
                        created, result = await process_roster_url(
                            url=url,
                            channel_id=int(channel_id),
                            source_channel=f"Roster Discord channel {channel_id} message {message_id}",
                        )
                        print(result)

                        if created:
                            created_count += 1
                        else:
                            skipped_count += 1

                        seen_url_hashes.add(url_digest)
                    except Exception as exc:
                        error_count += 1
                        print(f"ERROR processing {url}: {exc}")

                if message_id:
                    seen_message_ids.add(message_id)

    state["message_ids"] = list(seen_message_ids)
    state["url_hashes"] = list(seen_url_hashes)
    save_state(state)

    print(
        f"Done. Messages fetched: {total_messages}, empty-content messages: {empty_content_messages}, "
        f"Roster URLs found: {total_urls_found}, created: {created_count}, skipped: {skipped_count}, errors: {error_count}"
    )

    if total_messages and empty_content_messages == total_messages:
        print(
            "WARNING: All fetched Discord messages had empty content. "
            "Turn on Message Content Intent in Discord Developer Portal > Bot, "
            "and make sure the bot can View Channel and Read Message History."
        )

    if error_count:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

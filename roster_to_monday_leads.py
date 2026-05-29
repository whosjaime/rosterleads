"""
Roster -> Monday.com lead capture.

Processes only https://app.joinroster.co/jobs links, tags Monday as:
- Sourced From = Roster
- Platform = Other
- Referral Bonus text column = text_mm33jner by default
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()


def env_value(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or str(value).strip() == "" else str(value).strip()


DISCORD_BOT_TOKEN = env_value("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = env_value("DISCORD_GUILD_ID")
AUTO_CAPTURE_LINKS = env_value("ROSTER_DISCORD_AUTO_CAPTURE_LINKS", "false").lower() == "true"
ALLOWED_CHANNEL_IDS = {
    int(x.strip()) for x in env_value("ROSTER_DISCORD_ALLOWED_CHANNEL_IDS").split(",") if x.strip().isdigit()
}

try:
    CHANNEL_GROUP_MAP: dict[str, str] = json.loads(env_value("ROSTER_CHANNEL_GROUP_MAP", "{}"))
except json.JSONDecodeError:
    CHANNEL_GROUP_MAP = {}

MONDAY_API_TOKEN = env_value("MONDAY_API_TOKEN")
MONDAY_BOARD_ID = env_value("MONDAY_BOARD_ID", "18405764077")
MONDAY_DEFAULT_GROUP_ID = env_value("MONDAY_DEFAULT_GROUP_ID", "group_mm1vwy0q")
MONDAY_API_URL = "https://api.monday.com/v2"
DATABASE_PATH = env_value("ROSTER_LEAD_DEDUP_DATABASE", "roster_lead_dedup.sqlite3")

COL_STATUS = env_value("COL_STATUS", "color_mm1v7b3s")
COL_MARKET = env_value("COL_MARKET", "color_mm3fkwv7")
COL_POST_DATE = env_value("COL_POST_DATE", "date_mm1v35kx")
COL_LINK_TO_JP = env_value("COL_LINK_TO_JP", "link_mm1v7vdj")
COL_COMPANY_CHANNEL = env_value("COL_COMPANY_CHANNEL", "text_mm1vyhy")
COL_EMAIL = env_value("COL_EMAIL", "email_mm1v1yzs")
COL_PRIMARY_SKILL = env_value("COL_PRIMARY_SKILL", "dropdown_mm1vf5c9")
COL_LOCATION_TYPE = env_value("COL_LOCATION_TYPE", "dropdown_mm1vrjm1")
COL_PLATFORM = env_value("COL_PLATFORM", "color_mm1vhds4")
COL_SOURCED_FROM = env_value("COL_SOURCED_FROM", "color_mm1vhjmn")
COL_CATEGORY = env_value("COL_CATEGORY", "color_mm1vcyn7")
COL_DESCRIPTION = env_value("COL_DESCRIPTION", "long_text_mm1v4f4k")
COL_ROLE_POSITION = env_value("COL_ROLE_POSITION", "dropdown_mm1v8vzh")
COL_REFERRAL_BONUS = env_value("COL_REFERRAL_BONUS", "text_mm33jner")

URL_REGEX = re.compile(r"https?://[^\s<>\"]+", re.I)
ROSTER_URL_REGEX = re.compile(r"https?://(?:www\.)?app\.joinroster\.co/jobs[^\s<>\"]*", re.I)
EMAIL_REGEX = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid", "rcm"}

ROLE_KEYWORDS = {
    "Short-Form Editor": ["short-form editor", "short form editor", "shorts editor", "reels editor", "tiktok editor"],
    "Long-Form Editor": ["long-form editor", "long form editor", "youtube editor"],
    "Thumbnail Designer": ["thumbnail", "thumbnail designer"],
    "Scriptwriter": ["scriptwriter", "script writer", "writer", "script"],
    "Producer": ["producer", "production", "production coordinator"],
    "Channel Manager": ["channel manager", "youtube manager"],
    "Strategist": ["strategist", "strategy"],
    "Video Editor": ["video editor", "editor", "editing"],
    "Social Media Manager": ["social media manager", "community manager"],
    "Personal Assistant": ["personal assistant", "executive assistant"],
    "Developer": ["developer", "software engineer", "coder"],
}
LOCATION_KEYWORDS = {"Remote": ["remote", "wfh"], "Onsite": ["onsite", "on-site", "in person", "in-person", "located in", "based in"], "Hybrid": ["hybrid"]}
CATEGORY_KEYWORDS = {"Agency": ["agency"], "Company": ["company", "brand", "business"], "Creator": ["creator", "influencer"], "YouTuber": ["youtuber", "youtube channel"], "Startup": ["startup"], "Personal Brand": ["personal brand"]}


@dataclass
class RosterLeadData:
    original_url: str
    normalized_url: str
    item_name: str
    role: str
    location_type: str
    category: str
    post_text: str
    company: str
    job_title: str
    job_location: str
    referral_bonus: str
    emails: list[str]
    group_id: str
    source_channel: str
    scraped_at: str
    post_date: str
    scrape_status: str


def require_env() -> None:
    missing = [name for name, value in {
        "DISCORD_BOT_TOKEN": DISCORD_BOT_TOKEN,
        "MONDAY_API_TOKEN": MONDAY_API_TOKEN,
        "MONDAY_BOARD_ID": MONDAY_BOARD_ID,
        "MONDAY_DEFAULT_GROUP_ID": MONDAY_DEFAULT_GROUP_ID,
    }.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required env values: {', '.join(missing)}")


def init_db() -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posted_roster_leads (
                normalized_url TEXT PRIMARY KEY,
                monday_item_id TEXT,
                item_name TEXT,
                created_at TEXT
            )
        """)
        conn.commit()


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query_items = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return urlunparse(((parsed.scheme or "https").lower(), parsed.netloc.lower().replace("www.", ""), parsed.path.rstrip("/"), "", urlencode(query_items, doseq=True), ""))


def is_roster_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.netloc.lower().replace("www.", "") == "app.joinroster.co" and parsed.path.rstrip("/").startswith("/jobs")


def is_duplicate(normalized_url: str) -> bool:
    with sqlite3.connect(DATABASE_PATH) as conn:
        return conn.execute("SELECT 1 FROM posted_roster_leads WHERE normalized_url = ?", (normalized_url,)).fetchone() is not None


def save_posted_lead(normalized_url: str, monday_item_id: str, item_name: str) -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO posted_roster_leads VALUES (?, ?, ?, ?)",
            (normalized_url, monday_item_id, item_name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def clean_text(value: str | None, max_len: int = 3000) -> str:
    return "" if not value else re.sub(r"\s+", " ", value).strip()[:max_len]


def detect_from_keywords(text: str, mapping: dict[str, list[str]], default: str) -> str:
    lower = text.lower()
    for label, keywords in mapping.items():
        if any(keyword in lower for keyword in keywords):
            return label
    return default


def status_value(label: str) -> dict[str, str]:
    return {"label": label}


def dropdown_value(label: str) -> dict[str, list[str]]:
    return {"labels": [label]}


def link_value(url: str, text: str) -> dict[str, str]:
    return {"url": url, "text": text or url}


def email_value(email: str) -> dict[str, str]:
    return {"email": email, "text": email}


def date_value(date_string: str) -> dict[str, str]:
    return {"date": date_string}


def get_group_id_for_channel(channel_id: int | None) -> str:
    return MONDAY_DEFAULT_GROUP_ID if channel_id is None else CHANNEL_GROUP_MAP.get(str(channel_id), MONDAY_DEFAULT_GROUP_ID)


def meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return clean_text(str(tag["content"]), 3000)
    return ""


def json_strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        out = []
        for v in value.values():
            out.extend(json_strings(v))
        return out
    if isinstance(value, list):
        out = []
        for v in value:
            out.extend(json_strings(v))
        return out
    return [clean_text(value, 1000)] if isinstance(value, str) and clean_text(value, 1000) else []


def json_find(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for k, v in value.items():
            if k.lower() in keys:
                if isinstance(v, str) and clean_text(v, 500):
                    return clean_text(v, 500)
                if isinstance(v, (int, float)):
                    return str(v)
                if isinstance(v, dict):
                    nested = json_find(v, {"name", "title"})
                    if nested:
                        return nested
            nested = json_find(v, keys)
            if nested:
                return nested
    if isinstance(value, list):
        for v in value:
            nested = json_find(v, keys)
            if nested:
                return nested
    return ""


def parse_json_data(soup: BeautifulSoup) -> tuple[list[Any], list[str]]:
    objects, texts = [], []
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        typ = (script.get("type") or "").lower()
        sid = (script.get("id") or "").lower()
        if not raw.strip() or ("ld+json" not in typ and "json" not in typ and sid != "__next_data__"):
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        objects.append(data)
        texts.extend(json_strings(data))
    return objects, texts


def extract_referral_bonus(text: str, objects: list[Any]) -> str:
    for obj in objects:
        found = json_find(obj, {"referralbonus", "referral_bonus", "referralfee", "referral_fee", "bonus", "reward", "bounty"})
        if found:
            text = f"{found} {text}"
            break
    patterns = [
        r"(?:referral\s*(?:bonus|fee)|bonus|reward|bounty)\s*[:\-]?\s*(\$?\s*[\d,]+(?:\.\d{1,2})?\s*(?:usd|cad)?)",
        r"(\$?\s*[\d,]+(?:\.\d{1,2})?\s*(?:usd|cad)?)\s*(?:referral\s*(?:bonus|fee)|bonus|reward|bounty)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            value = clean_text(m.group(1), 100)
            return value if value.startswith("$") else f"${value}"
    return ""


def extract_location(text: str, objects: list[Any]) -> str:
    for obj in objects:
        found = json_find(obj, {"joblocation", "location", "addresslocality", "addressregion", "addresscountry", "workplacetype"})
        if found:
            return found
    m = re.search(r"(?:Location|Based in|Located in)\s*[:\-]\s*([A-Za-z0-9,\s./+-]{2,100})", text, re.I)
    return clean_text(m.group(1), 250) if m else ""


def extract_company(title: str, text: str, objects: list[Any]) -> str:
    for obj in objects:
        found = json_find(obj, {"company", "companyname", "hiringorganization", "organization", "employer", "brand", "creator", "channel"})
        if found:
            return found
    m = re.search(r"\b(?:at|with|for)\s+([A-Z][A-Za-z0-9&.,'’\- ]{2,80})", f"{title} {text[:500]}")
    return clean_text(m.group(1), 250) if m else ""


def extract_job_title(title: str, objects: list[Any]) -> str:
    for obj in objects:
        found = json_find(obj, {"title", "jobtitle", "name", "position"})
        if found and "roster" not in found.lower():
            return found
    title = re.sub(r"\s*(?:\||-)\s*Roster.*$", "", clean_text(title, 250), flags=re.I)
    return title


async def fetch_roster_metadata(url: str) -> dict[str, Any]:
    headers = {"User-Agent": "Mozilla/5.0 Chrome/124.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25), headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                html = await response.text(errors="ignore")
                final_url = str(response.url)
                http_status = response.status
    except Exception as exc:
        return {"final_url": url, "title": "Roster Lead - Needs Review", "company": "", "job_title": "", "job_location": "", "post_text": "", "emails": [], "role": "Other", "location_type": "", "category": "Awaiting", "referral_bonus": "", "scrape_status": f"Fetch failed: {exc}"}

    soup = BeautifulSoup(html, "html.parser")
    title = meta(soup, "og:title", "twitter:title") or (clean_text(soup.title.string, 255) if soup.title and soup.title.string else "")
    description = meta(soup, "og:description", "description", "twitter:description")
    objects, json_text = parse_json_data(soup)
    page_text = clean_text(soup.get_text(" "), 12000)
    combined = clean_text(" ".join([title, description, page_text, " ".join(json_text)]), 12000)

    job_title = extract_job_title(title, objects)
    company = extract_company(title, combined, objects)
    job_location = extract_location(combined, objects)
    referral_bonus = extract_referral_bonus(combined, objects)
    role = detect_from_keywords(f"{job_title} {title} {description} {combined[:2000]}", ROLE_KEYWORDS, "Other")
    location_type = detect_from_keywords(f"{job_location} {combined[:2000]}", LOCATION_KEYWORDS, "")
    category = detect_from_keywords(combined, CATEGORY_KEYWORDS, "Awaiting")
    status = "Scraped Roster public metadata"
    if http_status >= 400:
        status = f"HTTP {http_status}; created fallback Roster lead"
    elif len(combined) < 40:
        status = "Limited Roster metadata; needs manual review"

    return {
        "final_url": final_url,
        "title": title or job_title or "Roster Lead - Needs Review",
        "company": company,
        "job_title": job_title,
        "job_location": job_location,
        "post_text": clean_text(description or combined, 3000),
        "emails": sorted(set(EMAIL_REGEX.findall(combined)))[:5],
        "role": role,
        "location_type": location_type,
        "category": category,
        "referral_bonus": referral_bonus,
        "scrape_status": status,
    }


def build_monday_column_values(lead: RosterLeadData) -> dict[str, Any]:
    values: dict[str, Any] = {
        COL_STATUS: status_value("New Leads"),
        COL_MARKET: status_value("Awaiting"),
        COL_POST_DATE: date_value(lead.post_date),
        COL_LINK_TO_JP: link_value(lead.original_url, "Roster Job Post"),
        COL_PLATFORM: status_value("Other"),
        COL_SOURCED_FROM: status_value("Roster"),
        COL_CATEGORY: status_value(lead.category if lead.category in {"Awaiting", "YouTuber", "Creator", "Company", "Agency", "Personal Brand", "Startup"} else "Awaiting"),
        COL_PRIMARY_SKILL: dropdown_value(lead.role or "Other"),
        COL_ROLE_POSITION: dropdown_value(lead.role or "Other"),
    }
    if lead.location_type:
        values[COL_LOCATION_TYPE] = dropdown_value(lead.location_type)
    if lead.company:
        values[COL_COMPANY_CHANNEL] = lead.company
    if lead.emails:
        values[COL_EMAIL] = email_value(lead.emails[0])
    if lead.referral_bonus:
        values[COL_REFERRAL_BONUS] = lead.referral_bonus

    description = [
        f"Roster Job Text / Metadata:\n{lead.post_text or 'No public job text found. Needs manual review.'}",
        "",
        "Lead Details:",
        f"Company / Creator: {lead.company or 'Unknown'}",
        f"Job Title: {lead.job_title or 'Unknown'}",
        f"Detected Role: {lead.role}",
        f"Job Location: {lead.job_location or 'Not detected'}",
        f"Detected Location Type: {lead.location_type or 'Not detected'}",
        f"Detected Category: {lead.category}",
        f"Referral Bonus: {lead.referral_bonus or 'Not listed'}",
        "Platform: Other",
        "Sourced From: Roster",
        f"Source Channel: {lead.source_channel}",
        f"Scrape Status: {lead.scrape_status}",
        f"Scraped At: {lead.scraped_at}",
        f"URL: {lead.original_url}",
    ]
    if lead.emails:
        description.append(f"Public Emails Found: {', '.join(lead.emails)}")
    values[COL_DESCRIPTION] = "\n".join(description)
    return values


async def create_monday_item(lead: RosterLeadData) -> str:
    mutation = """
    mutation CreateLead($board_id: ID!, $group_id: String!, $item_name: String!, $column_values: JSON!) {
        create_item(board_id: $board_id, group_id: $group_id, item_name: $item_name, column_values: $column_values) { id }
    }
    """
    payload = {"query": mutation, "variables": {"board_id": MONDAY_BOARD_ID, "group_id": lead.group_id, "item_name": clean_text(lead.item_name, 255) or "New Roster Lead", "column_values": json.dumps(build_monday_column_values(lead))}}
    async with aiohttp.ClientSession(headers={"Authorization": MONDAY_API_TOKEN, "Content-Type": "application/json"}) as session:
        async with session.post(MONDAY_API_URL, json=payload) as response:
            data = await response.json(content_type=None)
    if "errors" in data:
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    item_id = data.get("data", {}).get("create_item", {}).get("id")
    if not item_id:
        raise RuntimeError(f"Monday did not return item ID: {data}")
    return str(item_id)


async def process_roster_url(url: str, channel_id: int | None = None, source_channel: str = "Discord") -> tuple[bool, str]:
    if not is_roster_url(url):
        return False, f"Skipped non-Roster URL: {url}"
    normalized = normalize_url(url)
    if is_duplicate(normalized):
        return False, f"Duplicate skipped: {normalized}"

    metadata = await fetch_roster_metadata(url)
    final_url = metadata.get("final_url") or url
    final_normalized = normalize_url(final_url)
    if final_normalized != normalized and is_duplicate(final_normalized):
        return False, f"Duplicate skipped: {final_normalized}"

    company = metadata.get("company", "")
    job_title = metadata.get("job_title", "") or metadata.get("title", "")
    role = metadata.get("role", "Other")
    if company and job_title:
        item_name = f"{company} - {job_title}"
    elif job_title:
        item_name = f"Roster - {job_title}"
    elif company and role != "Other":
        item_name = f"{company} - {role}"
    else:
        item_name = "Roster Lead - Needs Review"

    now = datetime.now(timezone.utc)
    lead = RosterLeadData(
        original_url=final_url,
        normalized_url=final_normalized,
        item_name=item_name,
        role=role,
        location_type=metadata.get("location_type", ""),
        category=metadata.get("category", "Awaiting"),
        post_text=metadata.get("post_text", ""),
        company=company,
        job_title=job_title,
        job_location=metadata.get("job_location", ""),
        referral_bonus=metadata.get("referral_bonus", ""),
        emails=metadata.get("emails", []),
        group_id=get_group_id_for_channel(channel_id),
        source_channel=source_channel,
        scraped_at=now.isoformat(),
        post_date=now.date().isoformat(),
        scrape_status=metadata.get("scrape_status", "Unknown"),
    )
    item_id = await create_monday_item(lead)
    save_posted_lead(lead.normalized_url, item_id, lead.item_name)
    bonus = f" | Referral Bonus: {lead.referral_bonus}" if lead.referral_bonus else ""
    return True, f"Created Monday Roster lead: {lead.item_name}{bonus} | Item ID: {item_id}"


class RosterLeadBot(commands.Bot):
    async def setup_hook(self) -> None:
        if DISCORD_GUILD_ID.isdigit():
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Roster slash commands synced to Discord server {DISCORD_GUILD_ID}")
        else:
            await self.tree.sync()
            print("Roster slash commands synced globally.")


intents = discord.Intents.default()
if AUTO_CAPTURE_LINKS:
    intents.message_content = True
bot = RosterLeadBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready() -> None:
    print(f"Roster bot logged in as {bot.user}")


@bot.tree.command(name="rosterlead", description="Send a Roster job URL to Monday.com as a lead")
@app_commands.describe(url="Roster job URL from app.joinroster.co/jobs")
async def rosterlead_command(interaction: discord.Interaction, url: str) -> None:
    if not is_roster_url(url):
        await interaction.response.send_message("Send a valid Roster jobs URL from app.joinroster.co/jobs", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    source_channel = f"Discord #{getattr(interaction.channel, 'name', 'unknown')}"
    try:
        created, msg = await process_roster_url(url=url, channel_id=interaction.channel_id, source_channel=source_channel)
    except Exception as exc:
        await interaction.followup.send(f"Could not create Roster lead: {exc}", ephemeral=True)
        return
    await interaction.followup.send(("✅ " if created else "⚠️ ") + msg, ephemeral=True)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    await bot.process_commands(message)
    if not AUTO_CAPTURE_LINKS or (ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS):
        return
    urls = ROSTER_URL_REGEX.findall(message.content or "")
    for url in urls:
        try:
            created, msg = await process_roster_url(url=url, channel_id=message.channel.id, source_channel=f"Discord #{getattr(message.channel, 'name', 'unknown')}")
            await message.reply(("✅ " if created else "⚠️ ") + msg, mention_author=False)
        except Exception as exc:
            await message.reply(f"Could not create Roster lead: {exc}", mention_author=False)


def main() -> None:
    require_env()
    init_db()
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()

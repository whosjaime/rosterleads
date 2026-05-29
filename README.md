# Roster Leads Bot

Roster-specific version of the LinkLeads automation.

This repo watches a Discord channel for links from:

```text
https://app.joinroster.co/jobs
```

When it finds a new Roster job link, it creates a lead in Monday.com.

## What it sets in Monday

- **Sourced From** = `Roster`
- **Platform** = `Other`
- **Referral Bonus** = mapped to Monday text column `text_mm33jner`
- Job link, company/creator, role, location type, and metadata are also added when detectable.

## Required GitHub Secrets

Go to:

```text
Repo → Settings → Secrets and variables → Actions → New repository secret
```

Add these:

```env
DISCORD_BOT_TOKEN=your_discord_bot_token
MONDAY_API_TOKEN=your_monday_api_token
ROSTER_DISCORD_ALLOWED_CHANNEL_IDS=your_roster_channel_id
```

## Optional GitHub Secrets

Only add these if you need to override the defaults:

```env
MONDAY_BOARD_ID=18405764077
MONDAY_DEFAULT_GROUP_ID=group_mm1vwy0q
COL_REFERRAL_BONUS=text_mm33jner
ROSTER_DISCORD_LOOKBACK_LIMIT=50
```

If you want specific Discord channels to map to specific Monday groups:

```env
ROSTER_CHANNEL_GROUP_MAP={"123456789012345678":"group_mm1vwy0q"}
```

## Discord permissions needed

The Discord bot needs these permissions in the Roster channel:

```text
View Channel
Read Message History
Send Messages
```

Also make sure **Message Content Intent** is turned on in the Discord Developer Portal for the bot.

## GitHub Actions

The workflow runs every 30 minutes and can also be run manually:

```text
Actions → Poll Roster Discord Leads → Run workflow
```

## Files

```text
roster_to_monday_leads.py
poll_roster_discord_channel_links.py
.github/workflows/poll-roster-discord.yml
requirements.txt
.gitignore
```

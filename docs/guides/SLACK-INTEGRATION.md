# Slack Integration

Setup guide for Slack message search and sync.

---

## Overview

LifeOS integrates with Slack to:
- Index DM messages for semantic search
- Sync user profiles to CRM
- Track interaction history with colleagues

---

## Step 1: Create Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App**
3. Choose **From scratch**
4. Name: "LifeOS"
5. Select your workspace
6. Click **Create App**

---

## Step 2: Configure OAuth Scopes

1. In your app settings, go to **OAuth & Permissions**
2. Under **User Token Scopes**, add:
   - `users:read` - List users
   - `users:read.email` - Get user emails (for CRM linking)
   - `im:history` - Read DM messages
   - `im:read` - List DMs

**Note**: Use **User Token Scopes**, not Bot Token Scopes. LifeOS needs to access your messages as you, not as a bot.

---

## Step 3: Install to Workspace

1. Go to **OAuth & Permissions**
2. Click **Install to Workspace**
3. Review and authorize the permissions
4. Copy the **User OAuth Token** (starts with `xoxp-`)

---

## Step 4: Get Workspace ID

Find your workspace ID:

1. Open Slack in a browser
2. Look at the URL: `https://app.slack.com/client/T02XXXXX/...`
3. The `T02XXXXX` part is your Team ID

Or use the API:
```bash
curl -H "Authorization: Bearer xoxp-your-token" \
  https://slack.com/api/auth.test | jq '.team_id'
```

---

## Step 5: Configure Environment

Add to your `.env`:

```bash
SLACK_USER_TOKEN=xoxp-your-user-oauth-token
SLACK_TEAM_ID=T02XXXXX
```

---

## Step 6: Run Initial Sync

```bash
# Dry run (shows what would sync)
uv run python scripts/sync_slack.py

# Execute sync
uv run python scripts/sync_slack.py --execute
```

This will:
1. Sync all Slack users to SourceEntity
2. Index DM messages to ChromaDB
3. Link Slack users to existing PersonEntity records by email

---

## Step 7: Link Entities

After initial sync, link Slack users to CRM records:

```bash
# Dry run
uv run python scripts/link_slack_entities.py

# Execute
uv run python scripts/link_slack_entities.py --execute
```

This matches Slack users to PersonEntity by email address.

---

## Step 8: Verify

Test the integration:

```bash
# Check Slack status
curl http://localhost:8000/api/slack/status | jq

# Search Slack messages
curl -X POST http://localhost:8000/api/slack/search \
  -H "Content-Type: application/json" \
  -d '{"query": "project update", "top_k": 10}' | jq

# List conversations
curl http://localhost:8000/api/slack/conversations | jq
```

---

## Troubleshooting

### "missing_scope" Error

**Cause**: Token doesn't have required scopes.

**Solution**:
1. Go to your Slack app settings
2. Add missing scopes under **User Token Scopes**
3. Reinstall app to workspace
4. Update token in `.env`

### "invalid_auth" Error

**Cause**: Token is invalid or expired.

**Solution**:
1. Go to Slack app settings → **OAuth & Permissions**
2. Copy the current User OAuth Token
3. Update `.env`
4. Restart server

### Users Not Linking to CRM

**Cause**: Email addresses don't match.

**Solution**:
1. Check Slack user has an email set
2. Check PersonEntity has matching email
3. Link manually via CRM UI or merge records

### No Messages Found

**Cause**: Slack rate limiting or no DM history.

**Solution**:
1. Check you have DMs in the workspace
2. Wait and retry (rate limits reset)
3. Check logs for errors: `tail -f logs/lifeos-api.log`

---

## Sync Schedule

Slack syncs run during the nightly unified sync (3 AM):

1. **Phase 1**: `sync_slack.py` - Sync users and messages
2. **Phase 2**: `link_slack_entities.py` - Link users to CRM

Manual sync:
```bash
curl -X POST http://localhost:8000/api/slack/sync
```

---

## Security Notes

- **Never commit** your Slack token
- The token has access to your DMs - keep it secure
- `.env` is in `.gitignore` by default

---

## Next Steps

- [Launchd Setup](LAUNCHD-SETUP.md) for automated syncs
- [First Run Guide](../getting-started/FIRST-RUN.md)

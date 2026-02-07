# Google OAuth Setup

Step-by-step guide for setting up Google OAuth for Calendar, Gmail, and Drive integration.

---

## Overview

LifeOS uses Google OAuth to access:
- **Google Calendar** - Upcoming events, meeting prep
- **Gmail** - Email search, draft creation
- **Google Drive** - File search

You can configure separate credentials for personal and work accounts.

---

## Step 1: Create Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** → **New Project**
3. Name it (e.g., "LifeOS Personal" or "LifeOS Work")
4. Click **Create**

---

## Step 2: Enable APIs

1. Go to **APIs & Services** → **Library**
2. Search and enable each API:
   - **Google Calendar API**
   - **Gmail API**
   - **Google Drive API**

---

## Step 3: Configure OAuth Consent Screen

1. Go to **APIs & Services** → **OAuth consent screen**
2. Select **External** (or Internal if using Workspace)
3. Fill in required fields:
   - **App name**: LifeOS
   - **User support email**: Your email
   - **Developer contact**: Your email
4. Click **Save and Continue**

### Scopes

Add these scopes:
- `https://www.googleapis.com/auth/calendar.readonly`
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/gmail.compose`
- `https://www.googleapis.com/auth/drive.readonly`

### Test Users

In "Test users" section, add your email address.

---

## Step 4: Create Credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app**
4. Name: "LifeOS Desktop"
5. Click **Create**
6. Click **Download JSON**
7. Save as `config/credentials-personal.json` (or `credentials-work.json`)

---

## Step 5: Configure Environment

Add to your `.env`:

```bash
# Personal account
GOOGLE_CREDENTIALS_PERSONAL=./config/credentials-personal.json
GOOGLE_TOKEN_PERSONAL=./config/token-personal.json

# Work account (optional)
GOOGLE_CREDENTIALS_WORK=./config/credentials-work.json
GOOGLE_TOKEN_WORK=./config/token-work.json
```

---

## Step 6: Authenticate

Run the authentication script:

```bash
# For personal account
uv run python scripts/google_auth.py --account personal

# For work account
uv run python scripts/google_auth.py --account work
```

This will:
1. Open a browser for Google sign-in
2. Request permission for the scopes
3. Save the token to the configured path

---

## Step 7: Verify

Test the integration:

```bash
# Check calendar
curl "http://localhost:8000/api/calendar/upcoming?days=7" | jq

# Check Gmail
curl "http://localhost:8000/api/gmail/search?q=test&account=personal" | jq

# Check Drive
curl "http://localhost:8000/api/drive/search?q=document&account=personal" | jq
```

---

## Troubleshooting

### "Access blocked: This app's request is invalid"

**Cause**: OAuth consent screen not configured or app not published.

**Solution**:
1. Ensure you added yourself as a test user
2. Use the same email for test user and sign-in

### "Token has been expired or revoked"

**Cause**: OAuth token expired.

**Solution**: Re-run authentication script:
```bash
uv run python scripts/google_auth.py --account personal
```

### "Quota exceeded"

**Cause**: Too many API requests.

**Solution**: Wait 24 hours or request quota increase in Cloud Console.

### "Invalid credentials"

**Cause**: credentials.json file incorrect or missing.

**Solution**:
1. Verify file exists at configured path
2. Re-download from Google Cloud Console
3. Check JSON is valid: `cat config/credentials-personal.json | jq`

---

## Security Notes

- **Never commit** `credentials-*.json` or `token-*.json` files
- These files are in `.gitignore` by default
- Tokens auto-refresh but may need re-auth after 7 days if app is in "Testing" mode

---

## Multi-Account Setup

For separate personal and work accounts:

1. Create two Google Cloud projects
2. Configure each with OAuth credentials
3. Set both in `.env`:
   ```bash
   GOOGLE_CREDENTIALS_PERSONAL=./config/credentials-personal.json
   GOOGLE_TOKEN_PERSONAL=./config/token-personal.json
   GOOGLE_CREDENTIALS_WORK=./config/credentials-work.json
   GOOGLE_TOKEN_WORK=./config/token-work.json
   ```
4. Use `account=personal` or `account=work` in API calls:
   ```bash
   curl "http://localhost:8000/api/gmail/search?q=test&account=work"
   ```

---

## Next Steps

- [Slack Integration](SLACK-INTEGRATION.md)
- [First Run Guide](../getting-started/FIRST-RUN.md)

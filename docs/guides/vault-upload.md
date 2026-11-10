# Vault page: browse folders and drop files in

**Status:** Complete
**Audience:** Operator

The page at `/vault` shows the vault's folder tree and accepts any file by drag and drop (or the Upload button, which opens the file picker on a phone). A file is saved in the folder it was dropped on, then processed in the background into a sidecar note beside it.

## What happens to a dropped file

1. **Saved as-is.** The original file is stored unchanged in the chosen folder. A name already taken gets ` (1)`, ` (2)`, and so on; nothing is overwritten.
2. **Text extracted.** PDF, Word (`.docx`), Excel (`.xlsx`), CSV, email (`.eml`), HTML, and plain-text and code files are read. Other types are stored without text.
3. **Analyzed** (when `LIFEOS_VAULT_UPLOAD_AI` is true and `ANTHROPIC_API_KEY` is set). The orchestrator model returns a summary, tags, suggested actions with due dates, dates, people mentioned, and a suggested folder. A scanned PDF with no text layer is sent to the model as a document, and an image is sent as an image, so no OCR program is needed.
4. **Sidecar note written.** `<file>.md` in the same folder holds frontmatter (`type: upload`, source file, date, tags), the analysis, and the extracted text. It links back to the file.
5. **Indexed.** The note is added to the search index so vault search and chat find the file's content. If indexing is unavailable the page says so, and the nightly sync indexes it.

Suggested actions, dates, and people are **suggestions in the note only**. Nothing is added to the task list or the CRM.

The suggested folder is chosen from the vault's existing top two folder levels. The page shows a **Move there** button; a file is never moved on its own. Moving takes the sidecar note along and never overwrites.

## Safety

- Every path is vault-relative and must resolve inside the vault root. Absolute paths, `..`, hidden folders (`.obsidian`, `.git`), and symlinks that leave the vault are refused.
- A file name is reduced to a safe base name. Program and script types are refused (`LIFEOS_VAULT_UPLOAD_BLOCKED_EXTENSIONS`), so one cannot be placed in a folder that OneDrive syncs to other devices.
- Files above `LIFEOS_VAULT_UPLOAD_MAX_MB` are refused and leave no partial file.
- Stored files are served as attachments, except PDFs, images, and plain text on request, always with `X-Content-Type-Options: nosniff` and a sandboxing `Content-Security-Policy`, so a stored HTML or SVG file cannot run script on the site's origin.
- The file's text is sent to the model as data with an instruction not to follow directions found in it. The reply is validated field by field, and model text is escaped so it cannot open a frontmatter block or heading in the note.
- `Personal/Journal/` stays reserved when the journal pipeline is configured, the same rule as `POST /api/vault/write`.

The page has no login of its own. Put an authenticated front door (for example Cloudflare Access) in front of the site before exposing it.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/vault/tree?path=` | One folder's subfolders and files, all types |
| POST | `/api/vault/upload` | Multipart: `folder` and up to 25 `files`; returns a job id per file |
| GET | `/api/vault/jobs/{id}` | Processing status and result |
| GET | `/api/vault/download?path=&inline=` | The stored file |
| POST | `/api/vault/move` | `{path, to_folder}`; moves the file and its sidecar note |

## Settings

See [Configuration](configuration.md): `LIFEOS_VAULT_UPLOAD_MAX_MB`, `LIFEOS_VAULT_UPLOAD_BLOCKED_EXTENSIONS`, `LIFEOS_VAULT_UPLOAD_AI`.

## Limits

Processing jobs are held in memory, so a restart forgets running jobs; the stored file stays and a note is written only for jobs that finished. A moved note is re-indexed by the next sync, not at once.

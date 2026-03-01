# AdVault Local

A local-first tool for searching, downloading, and transcribing Meta Ad Library ads.

```
Search Meta Ads → Select ads → Download media → Transcribe with Whisper
```

All data is stored locally under `./data/{brand}/{ad_id}/`.

---

## Requirements

| Dependency | Install |
|---|---|
| Python 3.11+ | https://python.org |
| ffmpeg | See below |
| Meta API access token | https://developers.facebook.com/ |

### Install ffmpeg

**macOS (Homebrew)**
```bash
brew install ffmpeg
```

**Ubuntu / Debian**
```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
```

**Windows**
Download from https://ffmpeg.org/download.html and add to PATH.

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <repo-url> ad-ripper
cd ad-ripper
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

> **Note:** `openai-whisper` installs PyTorch (CPU build) automatically.
> For GPU acceleration install the appropriate torch version manually before running pip install.

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:
```
META_ACCESS_TOKEN=your_token_here
```

#### Getting a Meta Access Token

1. Go to https://developers.facebook.com/
2. Create an app (type: "Other" → "Consumer")
3. Add the **Ads Library API** product
4. Generate a User Access Token with `ads_read` permission
5. For long-lived tokens use the token debugger / exchange endpoint

### 4. Initialise the database (optional — the server does this automatically)

```bash
python scripts/init_db.py
```

### 5. Start the server

```bash
uvicorn app.main:app --reload
```

Open your browser at: **http://localhost:8000**

---

## Usage

1. **Search** — Enter a brand name (e.g. `Nike`), choose country and filters, click **Search**.
2. **Select** — Check the ads you want to download.
3. **Download Selected** — Downloads media files; each result is stored at `./data/{brand}/{ad_id}/`.
4. **Transcribe** — Click the **Transcribe** button on a downloaded ad to run Whisper locally.

### Download statuses

| Status | Meaning |
|---|---|
| `downloaded` | Media file saved to disk |
| `snapshot_only` | No direct media URL found; visit the snapshot link manually |
| `done` | Downloaded + transcribed |
| `failed` | Error — see the error message on the card |

### Saved files per ad

```
data/
└── Nike/
    └── 1234567890/
        ├── metadata.json      # Ad metadata from Meta API
        ├── media.mp4          # Downloaded video (if available)
        ├── transcript.txt     # Plain text transcript (after Transcribe)
        └── transcript.srt     # SRT with timestamps (after Transcribe)
```

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `META_ACCESS_TOKEN` | *(required)* | Meta API token |
| `META_ADS_API_VERSION` | `v21.0` | API version |
| `DATABASE_URL` | `sqlite:///./app.db` | SQLite DB path |
| `WHISPER_MODEL` | `base` | Whisper model size |
| `FFMPEG_PATH` | `ffmpeg` | Path to ffmpeg binary |
| `DOWNLOAD_TIMEOUT_SECONDS` | `60` | HTTP timeout per download |
| `DOWNLOAD_MAX_RETRIES` | `3` | Retry count for downloads |
| `DATA_DIR` | `./data` | Root directory for saved files |

---

## API reference

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/` | Web UI |
| GET | `/api/search` | Search Meta Ad Library + upsert to DB |
| POST | `/api/download` | Download selected ads |
| POST | `/api/transcribe` | Transcribe a downloaded ad |
| GET | `/api/ads` | List ads from local DB |
| GET | `/api/jobs` | List download/transcription jobs |

Interactive docs: http://localhost:8000/docs

---

## Limitations

- **No headless browser**: The downloader fetches snapshot HTML and looks for video/image tags. Some ad creatives are served dynamically and will only result in `snapshot_only` status.
- **Token expiry**: Meta User Access Tokens expire. Use long-lived tokens or automate refresh.
- **Rate limits**: The Meta Ads API has rate limits. The client backs off on 429 responses.
- **Whisper on CPU**: Transcription of long videos can be slow on CPU. Use a smaller model (`tiny`) for speed or a GPU machine for large batches.

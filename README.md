# Scholion — Your Personal Research Library

*From the Greek σχόλιον — the marginal commentaries that scholars wrote alongside the texts they studied.*

Scholion turns your Zotero bibliography into a browsable, searchable, AI-enhanced personal research library — deployed to the web in under 15 minutes, with no local installation or command-line experience required.

[![Use this template](https://img.shields.io/badge/Use%20this%20template-2ea44f?style=for-the-badge&logo=github)](../../generate)

---

## What you get

| Feature | Stage |
|---------|-------|
| Interactive explorer — filter, browse, timeline, map view | 1 (now) |
| Automatic Zotero sync every 6 hours | 1 (now) |
| Research scope + questions stored alongside your library | 1 (now) |
| Full-text search across your PDFs | 3 (planned) |
| Citation discovery — what are you missing? | 4 (planned) |
| AI-powered reading support and commentary | 5–6 (planned) |

---

## Setup (15 minutes, no terminal needed)

### Prerequisites

| Requirement | Where to get it |
|-------------|----------------|
| GitHub account | [github.com/signup](https://github.com/signup) |
| Zotero account with synced library | [zotero.org](https://www.zotero.org) |
| Zotero API key (read+write) | [zotero.org/settings/keys](https://www.zotero.org/settings/keys) — create a key with read+write access |
| Zotero library ID | Shown on the API keys page next to your key |

### Step 1 — Fork this template

Click **"Use this template"** at the top of this page (or the button above), then name your new repository (e.g. `my-research-library`).

### Step 2 — Add your Zotero API key as a secret

In your new repository:

1. Go to **Settings → Secrets and variables → Actions**
2. Click **New repository secret**
3. Name: `ZOTERO_API_KEY` | Value: your key from [zotero.org/settings/keys](https://www.zotero.org/settings/keys)

> **Why read+write?** Read access imports your bibliography. Write access enables annotation sync (Stage 2+) without needing to reconfigure later.

### Step 3 — Run the First-time setup workflow

1. Go to the **Actions** tab in your repository
2. Click **First-time setup** in the left sidebar
3. Click **Run workflow** (top right)
4. Fill in:
   - **Zotero library ID** — the number shown next to your API key on zotero.org
   - **Library type** — `user` for a personal library, `group` for a shared group
   - **Collection name** — optional: leave blank to import your entire library
   - **Research library name** — what to call your library in the UI
   - **Research scope** — optional: 1–3 sentences about your project

5. Click **Run workflow**

The workflow will:
- Validate your Zotero credentials
- Import your bibliography into `data/inventory.json`
- Generate the interactive explorer
- Enable GitHub Pages
- Create a welcome issue with your library URL

### Step 4 — Wait for deployment (1–2 minutes)

GitHub Pages deploys automatically. A welcome issue will appear in your repository with a link to your live library.

### Step 5 — Open your library

Your library is live at:

```
https://YOUR_USERNAME.github.io/YOUR_REPO_NAME/
```

---

## Keeping your library up to date

Your library syncs automatically from Zotero every 6 hours. Any items you add to Zotero will appear in your explorer within a few hours.

To sync manually: **Actions → Zotero sync → Run workflow**

---

## Architecture

Everything runs on GitHub's free infrastructure — no servers, no subscriptions, no local installation.

```
Your Zotero library (cloud)
    ↓  GitHub Actions (every 6 hours or on demand)
data/inventory.json  ← metadata for all your items
    ↓  generate_explore.py
data/explore.html    ← interactive browser-based explorer
    ↓  deploy-pages.yml
https://username.github.io/repo-name/  ← your live library
```

### Cost

| Stage | Cost |
|-------|------|
| Stages 1–4 (setup, explore, read, search) | **Free** |
| OCR for scanned documents (optional Google Vision) | ~$0.0015/page (your own key) |
| AI features — translation, commentary (Stages 5–6) | ~$0.01–$1/doc (your own API key) |

You own your repository and your data. No vendor lock-in.

---

## Project structure

```
.github/workflows/
  setup.yml           ← first-time setup wizard
  zotero-sync.yml     ← automatic 6-hourly sync
  extract.yml         ← PDF processing (Stage 3)
  deploy-pages.yml    ← publish to GitHub Pages

scripts/
  zotero_sync.py      ← Zotero API → inventory.json
  generate_explore.py ← builds the interactive explorer HTML

src/
  zotero_client.py    ← Zotero API client

data/
  corpus_config.json  ← library name and settings
  inventory.json      ← bibliography (populated by sync)
  research_scope.json ← your research questions
  annotations.json    ← bookmarks and notes (Stage 2+)
  explore.html        ← generated interactive explorer
  reader.html         ← document reader (Stage 3)

docs/
  strategy.md                    ← product strategy
  plan-stage1-onboarding.md      ← this stage
  plan-stage2-bibliography.md    ← coming next
  plan-stage3-pdf-processing.md
  plan-stage4-search.md
```

---

## Next stages

- **[Stage 2: Explore](docs/plan-stage2-bibliography.md)** — enhanced timeline, geographic map, tag cloud, BibTeX import
- **[Stage 3: Read](docs/plan-stage3-pdf-processing.md)** — PDFs processed and readable in the browser, full-text extraction, OCR for scanned documents
- **[Stage 4: Search](docs/plan-stage4-search.md)** — full-text search across your entire corpus
- **Stage 5+** — citation discovery, AI commentary

---

## Principles

1. **Zero local install** — everything runs on GitHub (free tier) + free cloud services
2. **Progressive value** — each stage is useful on its own; nothing is gated on a later stage
3. **No payment for core features** — Stages 1–4 are free; LLM features use your own API key
4. **Humanities-first UX** — designed for researchers, not developers
5. **Your data, your repo** — you own the repository; data is plain JSON + HTML

---

*Built for humanities researchers. Inspired by the scholia tradition.*

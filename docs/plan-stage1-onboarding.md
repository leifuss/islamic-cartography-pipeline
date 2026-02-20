# Stage 1: Onboarding — "I have a GitHub account and a bibliography"

> **Goal:** A humanities researcher with no terminal experience deploys
> their own Scholion instance in under 15 minutes, using only a web
> browser.

## 1. User Story

*As a researcher, I want to set up my own personal research library
platform so that I can explore my bibliography in new ways, without
needing to install software or use a command line.*

## 2. Prerequisites (what the user needs before they start)

| Requirement | How to get it | Time |
|-------------|---------------|------|
| GitHub account | Sign up at github.com | 2 min |
| Zotero account with synced library | Most researchers already have this | 0 min |
| Zotero API key (read+write) | Click-through at zotero.org/settings/keys | 2 min |
| Zotero Library ID | Shown on the API keys page | 0 min |

**Important:** Request **read+write** API access from the start.  Write
access is needed for annotation sync (cross-cutting) and citation import
(Stage 5).  This avoids a confusing re-configuration step later.

## 3. The Onboarding Flow

```
Step 1: Click "Use this template" on the Scholion GitHub template
        ↓
Step 2: Name your repo (e.g., "my-reading-library")
        ↓
Step 3: The repo is created. A welcome issue appears with setup
        instructions.  It links to:
        ↓
Step 4: A setup page (GitHub Pages or external) that walks through:
        a) "Paste your Zotero API key here"
        b) "What is your Zotero library ID?"  (with a screenshot
           showing where to find it)
        c) "Is this a group or personal library?"
        d) Click "Configure" → this calls the GitHub API to set
           repo secrets (via a GitHub App or OAuth flow)
        ↓
Step 5: Setup triggers the first sync workflow.  The welcome issue
        updates: "Syncing your Zotero library... done! 247 items
        imported."
        ↓
Step 6: GitHub Pages deploys. The welcome issue updates with the
        link: "Your library is live at
        https://username.github.io/my-reading-library/"
```

### Total time: ~10–15 minutes

### What they have at the end of Stage 1:
- A GitHub repository they own
- `data/inventory.json` populated from Zotero
- GitHub Pages serving the explorer at their own URL
- Automated 6-hourly sync with Zotero

## 4. Implementation Plan

### 4.1 Create the Template Repository

**Task:** Convert the current `islamic-cartography-pipeline` repo into a
GitHub template that others can fork.

- [ ] Create a new clean repo `scholion-template` (or similar)
- [ ] Strip all Islamic Cartography-specific data (inventory, texts, pdfs)
- [ ] Keep all scripts, workflows, and HTML templates
- [ ] **Parameterise hardcoded values** in workflows: `COLLECTION_NAME`
      and `ZOTERO_LIBRARY_TYPE` must come from repo secrets (set by setup
      wizard), not be hardcoded as `CambridgeCore_Citation_04Nov2025` and
      `group`.  All workflow `.env` creation steps should reference
      `${{ secrets.ZOTERO_LIBRARY_TYPE }}` and
      `${{ secrets.COLLECTION_NAME }}`.
- [ ] Update `data/corpus_config.json` source type from `zotero_local`
      to `zotero_api` and set placeholder values
- [ ] Add `.github/ISSUE_TEMPLATE/welcome.md` for the auto-created issue
- [ ] Add `data/.gitkeep` files for empty directories
- [ ] Mark repo as a "template repository" in GitHub settings
- [ ] Write a `README.md` that serves as both documentation and landing page

**Key decisions:**
- The template should include a minimal `inventory.json` (empty array)
  so the explorer renders without errors on first deploy.
- The `.env.template` should have clear comments but no real values.
- Workflows should be present but disabled (or gracefully no-op)
  until secrets are configured.

### 4.2 Build the Setup Wizard

**Two approaches, in order of preference:**

#### Option A: Web-based setup page (recommended)

A simple static page (hosted on the main project site or as part of the
template's GitHub Pages) that:

1. Authenticates with GitHub via OAuth (read/write repo secrets scope)
2. Asks for Zotero credentials with inline help
3. Validates the Zotero API key by making a test call
4. Sets the repo secrets via GitHub API
5. Triggers the first `zotero-sync.yml` workflow run
6. Shows a progress spinner and links to the deployed site

**Tech:** Static HTML + vanilla JS. GitHub OAuth app for auth.
The page itself could be hosted on the main Scholion project site.

**Pros:** Smoothest UX. User never sees GitHub's settings UI.
**Cons:** Requires a GitHub OAuth App registration. Needs a server
for the OAuth callback (or use a serverless function).

#### Option B: Guided manual setup with validation

A `setup.yml` workflow that:

1. Is triggered manually from the Actions tab
2. Takes Zotero API key and library ID as workflow inputs
3. Validates the credentials
4. Sets repo secrets via `gh secret set` (requires appropriate permissions)
5. Triggers the first sync

**Pros:** No external dependencies. Works entirely within GitHub.
**Cons:** Requires the user to navigate to the Actions tab, which is
less discoverable.

**Hybrid approach:** Start with Option B (simpler to build), add Option A
later as the polished onboarding experience.

#### Option C: GitHub App (future)

A Scholion GitHub App that the user installs on their repo. The app
handles secrets configuration and can also manage webhook-based triggers.

This is the most polished approach but requires maintaining a running
service and GitHub App registration.

### 4.3 First-Run Workflow

A new workflow `.github/workflows/setup.yml`:

```yaml
name: First-time setup
on:
  workflow_dispatch:
    inputs:
      zotero_api_key:
        description: "Your Zotero API key"
        required: true
      zotero_library_id:
        description: "Your Zotero library ID"
        required: true
      zotero_library_type:
        description: "Library type"
        required: true
        default: "group"
        type: choice
        options:
          - group
          - user
      collection_name:
        description: "Collection name (leave blank for entire library)"
        required: false
```

Steps:
1. Validate Zotero credentials (test API call)
2. Store as repository secrets (including `ZOTERO_LIBRARY_TYPE`)
3. Save research scope + questions to `data/research_scope.json`
   (see `docs/plan-cross-cutting-research-scope.md`)
4. Run initial Zotero sync
4. Generate explorer HTML
5. Enable GitHub Pages
6. Create/update the welcome issue with the live URL
7. Enable the scheduled sync workflow

### 4.4 Welcome Issue

Auto-created by a workflow on repo creation (or first push):

```markdown
# Welcome to Scholion!

Your personal research library is almost ready.

## Setup checklist

- [ ] **Get your Zotero API key**
  Go to [zotero.org/settings/keys](https://www.zotero.org/settings/keys)
  and create a new key with read access to your library.

- [ ] **Find your library ID**
  [Screenshot: where to find the library ID on Zotero's settings page]

- [ ] **Run the setup wizard**
  Go to the **Actions** tab → **First-time setup** → **Run workflow**
  Paste your API key and library ID.

- [ ] **View your library**
  Once setup completes, your library will be live at:
  `https://YOUR_USERNAME.github.io/YOUR_REPO_NAME/`

## What happens next?

Your Zotero library syncs automatically every 6 hours. Any items
you add to Zotero will appear in your dashboard within a few hours.

When you're ready for more features, check out the
[Stage 2 guide](docs/guide-stage2.md): timeline views, geographic
visualisation, and more ways to explore your bibliography.
```

### 4.5 Explorer Graceful Empty State

The explorer HTML needs to handle an empty or minimal inventory
gracefully:

- Show "No items yet — run the setup wizard to import your bibliography"
- Show a progress indicator when sync is running
- Celebrate the first successful import ("247 items imported!")

### 4.6 Documentation

- `README.md` — Primary landing page. "What is this? How do I start?"
- `docs/guide-setup.md` — Step-by-step with screenshots
- `docs/guide-stage2.md` — Teaser for next stage
- A 3-minute video walkthrough (record after implementation)

## 5. Testing Plan

| Test case | Method |
|-----------|--------|
| Template creation works | Fork the template to a test account |
| Setup wizard validates bad API keys | Provide an invalid key, expect clear error |
| First sync populates inventory | Run with a real Zotero library |
| Explorer renders with 0 items | Deploy with empty inventory |
| Explorer renders with 1 item | Sync a library with 1 item |
| Explorer renders with 500 items | Sync a large library |
| GitHub Pages deploys automatically | Push to main, check Pages |
| Scheduled sync runs on time | Wait 6 hours (or trigger manually) |

## 6. Open Questions

1. **GitHub OAuth App:** Who registers it? Under what GitHub
   organisation?  (Could use Leifuss's personal account initially.)

2. **Template naming:** `scholion-template`? `scholion`?
   `your-research-library`?

3. **Mendeley/Endnote in Stage 1 or 2?** If we support BibTeX upload
   in Stage 1, that covers Mendeley/Endnote users immediately (they
   can export to BibTeX). Native API adapters can come in Stage 2.

4. **GitHub Pages custom domain:** Should we document how to set this
   up? (It's a GitHub settings toggle, not code.)

## 7. Dependencies

- GitHub template repository feature (available)
- GitHub Actions (available, free tier: 2000 min/month)
- GitHub Pages (available, free for public repos)
- Zotero web API (available, free)
- Current pipeline scripts (available, just refactored to cloud-native)

## 8. Estimated Effort

| Task | Effort |
|------|--------|
| Template repo creation + cleanup | 1 day |
| Setup workflow (Option B) | 1 day |
| Welcome issue automation | 0.5 day |
| Explorer empty-state handling | 0.5 day |
| Documentation + screenshots | 1 day |
| Testing across accounts | 1 day |
| **Total** | **~5 days** |

Web-based wizard (Option A) adds ~3 days.

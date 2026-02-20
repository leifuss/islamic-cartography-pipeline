# Setting up your Scholion library: a 5-minute guide

*No technical experience needed. You will use only your web browser.*

---

## Before you start

You need three things:

1. **A GitHub account** — free at [github.com/signup](https://github.com/signup). If you already have one, log in now.
2. **Your Zotero library** synced to the cloud — if you use Zotero, this is almost certainly already the case.
3. **A Zotero API key** — this is a password that allows Scholion to read your library. You'll create it in Step 2 below.

---

## Step 1 — Create your personal copy of Scholion

Go to the Scholion template repository on GitHub and click the green **"Use this template"** button near the top right of the page. Then click **"Create a new repository"**.

You'll be asked to name your repository. Choose something like `my-reading-library` or `dissertation-bibliography`. It can be anything you like.

Click **"Create repository"**. GitHub will create your own private copy of Scholion. This is the repository where your library will live.

---

## Step 2 — Get your Zotero API key

Open a new browser tab and go to [zotero.org/settings/keys](https://www.zotero.org/settings/keys). Log in to Zotero if prompted.

Click **"Create new private key"**.

On the next screen:
- Give the key a name (e.g. "Scholion")
- Under **Personal Library**, tick **Allow library access** and select **Read/Write**
- If you use a Zotero group library, also tick **Allow library access** under the relevant group

Click **"Save Key"**.

You'll see a long string of letters and numbers — something like `aBcDeFgH1234567890`. **Copy it now.** You won't be able to see it again after leaving this page (though you can always create a new one).

While you're on this page, also note your **User ID** — it's the number shown in the grey box at the top of the page (e.g. `12345678`). You'll need this in Step 4.

---

## Step 3 — Store your API key securely

Go back to your new Scholion repository on GitHub.

Click **Settings** (in the top navigation bar of your repository, not the main GitHub settings).

In the left sidebar, click **Secrets and variables**, then **Actions**.

Click **New repository secret**.

- **Name:** `ZOTERO_API_KEY`
- **Secret:** paste the key you copied from Zotero

Click **Add secret**.

Your API key is now stored securely. GitHub will use it to connect to your Zotero library, but it won't be visible to anyone — including you — after this point.

---

## Step 4 — Run the setup wizard

In your repository, click the **Actions** tab (in the top navigation bar).

In the left sidebar, click **First-time setup**.

Click the **Run workflow** button (you may need to click a grey dropdown first).

A form will appear. Fill it in:

| Field | What to enter |
|-------|---------------|
| **Zotero library ID** | The User ID number you noted in Step 2 |
| **Library type** | `user` for a personal Zotero library; `group` if you're using a shared group |
| **Collection name** | Leave blank to import your entire library, or type the exact name of a collection |
| **Research library name** | Whatever you'd like to call your library (e.g. "Ottoman Cartography — PhD") |
| **Research scope** | Optional: one or two sentences describing your project |

Click **Run workflow** (the green button).

---

## Step 5 — Wait a few minutes

The setup will take 2–5 minutes depending on the size of your library. You can watch it run by clicking on the workflow run that appears.

When it finishes, a new **Issue** will appear in your repository (look for the speech-bubble icon at the top). It will contain a link to your live library — something like:

```
https://yourusername.github.io/my-reading-library/
```

Click that link. Your library is live.

---

## What happens next

Your library will **sync automatically from Zotero every 6 hours**. Any item you add to Zotero will appear in your Scholion explorer within a few hours — no further action needed.

To sync immediately at any time: go to **Actions → Zotero sync → Run workflow**.

---

## Troubleshooting

**"The workflow failed" / red X**

Click on the failed run to see the error message. Common causes:

- *ZOTERO_API_KEY not set* — go back to Step 3 and make sure the secret name is exactly `ZOTERO_API_KEY` (no spaces, no quotes).
- *Library not found* — double-check your library ID and library type (`user` vs. `group`).
- *Invalid API key* — the key may have been copied incorrectly. Go to [zotero.org/settings/keys](https://www.zotero.org/settings/keys), delete the old key, and create a new one.

**The library link gives a "404 Page not found" error**

GitHub Pages can take up to 5 minutes to deploy after the setup workflow finishes. Wait a moment and refresh the page.

**My Zotero items are there but some look incomplete**

Scholion imports whatever metadata Zotero has. If items are missing authors, dates, or abstracts, the fix is in Zotero — correct the metadata there and it will appear in Scholion on the next sync.

---

*For further help, open an issue in the Scholion template repository.*

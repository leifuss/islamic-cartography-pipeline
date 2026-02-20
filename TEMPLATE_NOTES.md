# Template Preparation Notes

When the time comes to create the public `scholion-template` repository, the following should guide what to include and exclude.

## Do NOT include in the template

- **Personal PDF collection** (stored in Git LFS) — these are private research materials
- **Old/experimental code** that predates the current pipeline architecture
- **Personal docs and notes** that are project-specific rather than template-generic
- **LFS configuration** tied to the current repo's LFS objects
- **`IMPLEMENTATION_STATUS.md`, `PIPELINE_STATUS.md`, `LESSONS_LEARNED.md`** — these are dev-process artefacts, not template scaffolding

## DO include in the template

- Core pipeline scaffolding (`src/`, `scripts/`)
- `config.yaml` (with placeholder values)
- `requirements.txt` / `requirements-rag.txt`
- `Makefile`
- `README.md` (rewritten for template users)
- Deployment config (`Procfile`, `nixpacks.toml`, `railway.json`) — optional, but useful

## Process (when ready)

1. Create a fresh GitHub repo (`scholion-template` or similar) — **not a fork**
2. Copy in only the files listed above
3. Rewrite `README.md` for a new user starting from scratch
4. Enable "Template repository" in Settings (works on fresh repos)
5. Tag a release

## Timing

Strip-down is planned for after all pipeline phases are implemented.

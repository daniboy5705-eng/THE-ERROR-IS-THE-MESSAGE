# 📦 Repo Dump Tool

Dumps **everything** — all issues, all pull requests (with reviews + inline code comments),
all releases + artifacts, every comment thread, and every embedded image/video/attachment —
from any GitHub repository **back into the repository itself** under `dump/`.

Zero dependencies. Pure Python 3 stdlib. Runs entirely inside GitHub Actions,
so it works **from a mobile phone with two taps** — no computer needed.

## What it captures

| Area | What you get |
|------|--------------|
| Issues | Open + closed, full markdown body, complete comment thread, labels, authors, dates |
| Pull Requests | Open + closed, body, comment thread, review approvals, inline review comments, base/head |
| Releases | Notes, tags, authors, and **all release artifacts downloaded** |
| Media | Every image/video/attachment embedded in issues, PRs, comments and releases — downloaded into `media/` with all links rewritten to relative paths |
| Raw data | Full JSON snapshots of every object for archival / re-import |
| Index | `dump.json` summary + GitHub Actions step summary on every run |

## Files

- `tools/repo-dump/repo_dump.py` — the dumper (stdlib only)
- `.github/workflows/repo-dump.yml` — the mobile-friendly trigger

## Usage from a phone (2 taps)

1. Open your repository on GitHub → **Actions** tab → **📦 Repo Dump** → **Run workflow**
   - Optional: type a different `owner/name` repo to dump, toggle media, set max file size
2. Tap **Run** → wait ~1-3 minutes → the dump appears as a new commit under `dump/<timestamp>/`

That's it. Trigger it on a schedule, before a migration, or before deleting anything —
a full, self-contained archive lands inside the repo every time.

## Usage from a terminal (optional)

```bash
export GH_TOKEN=ghp_xxx
python3 tools/repo-dump/repo_dump.py --repo owner/name
```

Useful flags: `--include-media false` · `--max-file-mb 500` · `--out dump` · `--max-items 10` (quick test)

## Notes

- Works on public repos with the built-in `GITHUB_TOKEN`; for private repos run the workflow inside that repo.
- Files larger than the `max_file_mb` input are skipped and logged in `dump.json` (never crashes the run).
- Failed media downloads are logged, never fatal.

#!/usr/bin/env python3
"""
Repo Dump Tool - dumps ALL issues, pull requests, releases, comment threads and
embedded media from a GitHub repository back into the repository itself.

Zero dependencies (Python 3 stdlib only). Designed to run inside GitHub Actions
so it can be triggered from a mobile phone (Actions tab -> Run workflow).

Usage:
  python3 repo_dump.py --repo OWNER/NAME --token TOKEN [--include-media true] [--max-file-mb 200]
                       [--out dump] [--max-items N] [--no-raw]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
UA = "repo-dump-tool/1.0"


def log(msg):
    print("[repo-dump] %s" % msg, flush=True)


def slugify(text, max_len=60):
    s = re.sub(r"[^\w\-.]+", "-", (text or "").strip().lower()).strip("-.")
    return (s[:max_len] or "item")


# ----------------------------------------------------------------------------- API layer
def api_get(url, token, binary=False):
    """GET with retry + rate-limit awareness. Returns (data, link_header)."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json" if not binary else "application/octet-stream")
    req.add_header("User-Agent", UA)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    last_err = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read()
                link = r.headers.get("Link", "") or ""
                try:
                    remaining = int(r.headers.get("X-RateLimit-Remaining", "999"))
                except ValueError:
                    remaining = 999
                if remaining < 5:
                    try:
                        reset = int(r.headers.get("X-RateLimit-Reset", "0"))
                        wait = max(reset - time.time(), 0) + 2
                        if 0 < wait <= 180:
                            log("rate limit low, waiting %ds" % wait)
                            time.sleep(wait)
                    except ValueError:
                        pass
                if binary:
                    return raw, link
                return (json.loads(raw) if raw else None), link
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                return None, ""
            if e.code in (403, 429) or e.code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            log("HTTP %s on %s" % (e.code, url))
            return None, ""
        except Exception as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    log("GET failed after retries: %s (%s)" % (url, last_err))
    return None, ""


def paginate(url, token, limit=None):
    out = []
    while url:
        items, link = api_get(url, token)
        if items is None:
            break
        if isinstance(items, dict):
            items = items.get("items", [items])
        out.extend(items)
        if limit and len(out) >= limit:
            return out[:limit]
        m = re.search(r'<([^>]+)>; rel="next"', link)
        url = m.group(1) if m else None
    return out


# ----------------------------------------------------------------------------- media handling
MEDIA_PATTERNS_TEMPLATE = [
    r"https://github\.com/user-attachments/(?:assets|files)/[^\s\)\]\"'<>\\]+",
    r"https://private-user-images\.githubusercontent\.com/[^\s\)\]\"'<>\\]+",
    r"https://user-images\.githubusercontent\.com/[^\s\)\]\"'<>\\]+",
    r"https://github\.com/{owner}/{repo}/assets/[^\s\)\]\"'<>\\]+",
    r"https://github\.com/{owner}/{repo}/releases/download/[^\s\)\]\"'<>\\]+",
]

TRAILING_PUNCT = ".,;:!?)"


def find_media_urls(text, owner, repo):
    """Return ordered unique list of attachment URLs found in a markdown body."""
    if not text:
        return []
    urls = []
    for tpl in MEDIA_PATTERNS_TEMPLATE:
        pat = tpl.format(owner=re.escape(owner), repo=re.escape(repo))
        for m in re.finditer(pat, text):
            u = m.group(0)
            while u and u[-1] in TRAILING_PUNCT:
                u = u[:-1]
            if u and u not in urls:
                urls.append(u)
    return urls


def media_filename(url, seen_names):
    """Pick a stable, human-friendly filename for an attachment URL."""
    path = urllib.parse.urlsplit(url).path
    base = os.path.basename(path) or "attachment"
    base = urllib.parse.unquote(base)
    base = slugify(base, 80) or "attachment"
    if "." not in base:
        base += ".bin"
    name = base
    i = 1
    while name in seen_names:
        stem, ext = os.path.splitext(base)
        name = "%s-%d%s" % (stem, i, ext)
        i += 1
    return name


def download_media(url, token, dest_dir, max_bytes, seen_names, url_map, failures):
    if url in url_map:
        return
    name = media_filename(url, seen_names)
    dest = os.path.join(dest_dir, name)
    # GitHub attachment CDNs (user-attachments, objects.githubusercontent.com) REJECT
    # requests that forward an Authorization header across their cross-host redirects
    # (HTTP 400). So: try UNAUTHENTICATED first (covers all public attachments), and
    # only fall back to an authenticated request (private repos / private-user-images).
    modes = [False, True] if token else [False]
    last_err = None
    for use_auth in modes:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        if use_auth and token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                chunks = []
                total = 0
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise OverflowError("file exceeds --max-file-mb limit")
                    chunks.append(chunk)
            with open(dest, "wb") as f:
                for c in chunks:
                    f.write(c)
            seen_names.add(name)
            url_map[url] = name
            log("saved media -> %s (%d bytes%s)" % (name, total, ", authed" if use_auth else ""))
            return
        except OverflowError as e:
            failures.append({"url": url, "reason": str(e)})
            log("skipped media %s: %s" % (url, e))
            return
        except Exception as e:
            last_err = e
            time.sleep(1)
    failures.append({"url": url, "reason": str(last_err)})
    log("media download failed %s: %s" % (url, last_err))


def rewrite_media(text, url_map, run_dir, doc_path):
    """Replace every already-saved attachment URL in text with a relative path into the dump media dir."""
    if not text:
        return text
    for url, fname in url_map.items():
        if url in text:
            rel = os.path.relpath(os.path.join(run_dir, "media", fname), os.path.dirname(doc_path))
            text = text.replace(url, rel)
    return text


# ----------------------------------------------------------------------------- markdown writers
def fmt_user(u):
    if not u:
        return "_ghost_"
    return "[%s](%s)" % (u.get("login", "unknown"), u.get("html_url", ""))


def write_doc(path, title, meta_lines, body, comments, url_map, run_dir):
    lines = ["# %s" % title, ""]
    lines += ["- **%s:** %s" % (k, v) for k, v in meta_lines]
    lines += ["", "---", "", rewrite_media(body or "_(no body)_", url_map, run_dir, path), ""]
    if comments:
        lines += ["---", "", "## Comment Thread (%d)" % len(comments), ""]
        for c in comments:
            head = "### %s - %s" % (fmt_user(c.get("user")), (c.get("created_at") or "")[:19].replace("T", " "))
            lines += [head, ""]
            if c.get("_kind"):
                lines += ["_%s_" % c["_kind"], ""]
            lines += [rewrite_media(c.get("body") or "", url_map, run_dir, path), "", ""]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def safe_number(n, prefix=""):
    return "%s%04d" % (prefix, n)


# ----------------------------------------------------------------------------- main dump logic
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"), help="GitHub token")
    ap.add_argument("--out", default="dump", help="output root dir (default: dump)")
    ap.add_argument("--include-media", default="true")
    ap.add_argument("--max-file-mb", type=int, default=200)
    ap.add_argument("--max-items", type=int, default=None, help="(testing) cap issues and PRs")
    ap.add_argument("--no-raw", action="store_true", help="skip raw JSON snapshots")
    args = ap.parse_args()

    owner, repo = args.repo.strip().rstrip("/").split("/")[-2:]
    include_media = str(args.include_media).lower() not in ("false", "0", "no")
    token = args.token
    max_bytes = args.max_file_mb * 1024 * 1024
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(args.out, stamp)
    media_dir = os.path.join(run_dir, "media")
    raw_dir = os.path.join(run_dir, "raw")
    os.makedirs(media_dir, exist_ok=True)
    if not args.no_raw:
        os.makedirs(raw_dir, exist_ok=True)

    log("dumping %s/%s -> %s" % (owner, repo, run_dir))

    # ---- issues + PRs (the list endpoint returns both; PRs carry a pull_request key)
    issues_raw = paginate("%s/repos/%s/%s/issues?state=all&per_page=100" % (API, owner, repo), token, args.max_items)
    issues = [i for i in issues_raw if "pull_request" not in i]
    prs = [i for i in issues_raw if "pull_request" in i]
    log("found %d issues, %d pull requests" % (len(issues), len(prs)))

    # ---- releases
    releases = paginate("%s/repos/%s/%s/releases?per_page=100" % (API, owner, repo), token)
    log("found %d releases" % len(releases))

    url_map = {}
    seen_names = set()
    failures = []

    # ---- dump issues
    for it in issues:
        n = it["number"]
        title = it.get("title") or "issue-%d" % n
        comments = paginate("%s/repos/%s/%s/issues/%d/comments?per_page=100" % (API, owner, repo, n), token)
        doc_path = os.path.join(run_dir, "issues", "%s-%s.md" % (safe_number(n), slugify(title)))
        meta = [
            ("Number", "#%d" % n), ("State", it.get("state")), ("Author", fmt_user(it.get("user"))),
            ("Created", (it.get("created_at") or "")[:19]), ("Closed", (it.get("closed_at") or "")[:19] or "-"),
            ("Labels", ", ".join("`%s`" % l.get("name") for l in it.get("labels", [])) or "-"),
            ("URL", it.get("html_url")),
        ]
        write_doc(doc_path, "Issue: %s" % title, meta, it.get("body"), comments, url_map, run_dir)
        if not args.no_raw:
            for obj, fname in ((it, "issue.json"), (comments, "comments.json")):
                with open(os.path.join(raw_dir, "issue-%d-%s" % (n, fname)), "w", encoding="utf-8") as f:
                    json.dump(obj, f, indent=2)

    # ---- dump PRs (issue-comments + reviews + inline review comments = complete thread)
    for pr in prs:
        n = pr["number"]
        title = pr.get("title") or "pr-%d" % n
        base = "%s/repos/%s/%s/pulls/%d" % (API, owner, repo, n)
        detail, _ = api_get(base, token)
        comments = paginate("%s/repos/%s/%s/issues/%d/comments?per_page=100" % (API, owner, repo, n), token)
        reviews = paginate(base + "/reviews?per_page=100", token)
        review_comments = paginate(base + "/comments?per_page=100", token)
        thread = []
        for c in comments:
            c["_kind"] = "comment"
            thread.append(c)
        for r in reviews:
            thread.append({"user": r.get("user"), "created_at": r.get("submitted_at"),
                           "body": r.get("body"), "_kind": "review: %s" % (r.get("state", "").lower() or "commented")})
        for rc in review_comments:
            thread.append({"user": rc.get("user"), "created_at": rc.get("created_at"),
                           "body": "_inline on `%s`:_ %s" % (rc.get("path", ""), rc.get("body") or ""),
                           "_kind": "review comment"})
        thread.sort(key=lambda c: c.get("created_at") or "")
        doc_path = os.path.join(run_dir, "pulls", "%s-%s.md" % (safe_number(n, "PR"), slugify(title)))
        meta = [
            ("Number", "#%d" % n), ("State", pr.get("state")), ("Author", fmt_user(pr.get("user"))),
            ("Created", (pr.get("created_at") or "")[:19]),
            ("Merged", ((pr.get("pull_request") or {}).get("merged_at") or "-")[:19]),
            ("Base", (detail or {}).get("base", {}).get("ref", "?") if detail else "?"),
            ("Head", (detail or {}).get("head", {}).get("ref", "?") if detail else "?"),
            ("URL", pr.get("html_url")),
        ]
        write_doc(doc_path, "Pull Request: %s" % title, meta, pr.get("body"), thread, url_map, run_dir)
        if not args.no_raw:
            for obj, fname in ((detail, "pr.json"), (comments, "comments.json"),
                               (reviews, "reviews.json"), (review_comments, "review-comments.json")):
                with open(os.path.join(raw_dir, "pr-%d-%s" % (n, fname)), "w", encoding="utf-8") as f:
                    json.dump(obj, f, indent=2)

    # ---- dump releases (+ download artifacts)
    for rel in releases:
        tag = rel.get("tag_name") or rel.get("name") or slugify(rel.get("published_at") or "release")
        doc_path = os.path.join(run_dir, "releases", "%s.md" % slugify(tag, 80))
        assets = rel.get("assets", []) or []
        meta = [
            ("Tag", tag), ("Name", rel.get("name") or "-"), ("Author", fmt_user(rel.get("user"))),
            ("Published", (rel.get("published_at") or "")[:19]), ("Prerelease", str(rel.get("prerelease", False))),
            ("URL", rel.get("html_url")),
            ("Assets", ", ".join("%s (%d bytes)" % (a.get("name"), a.get("size", 0)) for a in assets) or "-"),
        ]
        write_doc(doc_path, "Release: %s" % (rel.get("name") or tag), meta, rel.get("body"), [], url_map, run_dir)
        if not args.no_raw:
            with open(os.path.join(raw_dir, "release-%s.json" % slugify(tag, 50)), "w", encoding="utf-8") as f:
                json.dump(rel, f, indent=2)
        if include_media:
            for a in assets:
                u = a.get("browser_download_url")
                if u:
                    download_media(u, token, media_dir, max_bytes, seen_names, url_map, failures)

    # ---- scan bodies + saved comment threads for embedded media
    if include_media:
        texts = [it.get("body") for it in issues]
        texts += [pr.get("body") for pr in prs]
        texts += [rel.get("body") for rel in releases]
        for url in find_media_urls("\n".join(t or "" for t in texts), owner, repo):
            download_media(url, token, media_dir, max_bytes, seen_names, url_map, failures)
        if not args.no_raw and os.path.isdir(raw_dir):
            for fn in os.listdir(raw_dir):
                if fn.endswith(".json") and ("comments" in fn or "reviews" in fn):
                    try:
                        arr = json.load(open(os.path.join(raw_dir, fn), encoding="utf-8"))
                        for c in (arr or []):
                            for url in find_media_urls(c.get("body") or "", owner, repo):
                                download_media(url, token, media_dir, max_bytes, seen_names, url_map, failures)
                    except Exception as e:
                        log("media scan skip %s: %s" % (fn, e))

    # ---- index / summary
    index = {
        "tool": "repo-dump-tool/1.0",
        "repo": "%s/%s" % (owner, repo),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "issues": len(issues), "pull_requests": len(prs), "releases": len(releases),
        "media_files": len(url_map), "media_failures": failures,
    }
    with open(os.path.join(run_dir, "dump.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)

    summary = (
        "## Repo Dump Summary\n\n"
        "- Repository: **%s/%s**\n- Issues: **%d**\n- Pull requests: **%d**\n- Releases: **%d**\n"
        "- Media saved: **%d**\n- Media failures: **%d**\n- Dump location: `dump/%s/`\n"
        % (owner, repo, len(issues), len(prs), len(releases), len(url_map), len(failures), stamp)
    )
    ghs = os.environ.get("GITHUB_STEP_SUMMARY")
    if ghs:
        with open(ghs, "a", encoding="utf-8") as f:
            f.write(summary)
    print(summary, flush=True)
    log("DONE")


if __name__ == "__main__":
    main()

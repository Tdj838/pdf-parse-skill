#!/usr/bin/env python3
"""publish_to_github.py -- publish the submission bundle as a public GitHub repo.

Uses only the REST API, so no git binary and no SSH key are needed: create the
repository, then upload every file through the contents endpoint.

    python3 publish_to_github.py --token <PAT> --repo pdf-parse-skill
    GITHUB_TOKEN=... python3 publish_to_github.py --repo pdf-parse-skill --dry-run

The token needs "create repository" plus read/write access to the new repo's
contents (a fine-grained token scoped to that repository is enough; a classic
token needs the `public_repo` scope). It is used once and never written to disk.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.dirname(HERE)
DEFAULT_EXCLUDE = {".git", "__pycache__", ".dsh", ".tmp-skills"}


def request(method: str, url: str, token: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "pdf-parse-publisher",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return exc.code, parsed


def collect_files(root: str, exclude: set[str]) -> list[tuple[str, str]]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude]
        for name in filenames:
            if name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            files.append((rel, full))
    return sorted(files)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    ap.add_argument("--repo", default="pdf-parse-skill")
    ap.add_argument("--dir", default=BUNDLE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--description", default=(
        "PDF -> evidence-anchored structure: a dependency-free layout reader that "
        "reconstructs two-column reading order and keeps a page+bbox anchor for every block. "
        "Submission for the product-arena challenge 阅读01｜文献文档解析."))
    args = ap.parse_args(argv)

    if not args.token and not args.dry_run:
        print("error: no token (pass --token or set GITHUB_TOKEN)", file=sys.stderr)
        return 2

    files = collect_files(args.dir, DEFAULT_EXCLUDE)
    print("files to publish:", len(files))

    if args.dry_run:
        for rel, _full in files:
            print("  ", rel)
        return 0

    status, user = request("GET", API + "/user", args.token)
    if status != 200:
        print("auth failed: HTTP %s %s" % (status, user), file=sys.stderr)
        return 2
    owner = user["login"]
    print("authenticated as:", owner)

    status, repo = request("GET", "%s/repos/%s/%s" % (API, owner, args.repo), args.token)
    if status == 200:
        print("repository already exists:", repo["html_url"])
    else:
        status, repo = request("POST", API + "/user/repos", args.token, {
            "name": args.repo,
            "description": args.description,
            "private": False,
            "has_issues": True,
            "has_wiki": False,
            "auto_init": False,
        })
        if status not in (200, 201):
            print("create failed: HTTP %s %s" % (status, repo), file=sys.stderr)
            return 2
        print("repository created:", repo["html_url"])
        time.sleep(2)  # let the repo settle before the first contents write

    html_url = repo["html_url"]
    default_branch = repo.get("default_branch") or "main"

    # make sure GITHUB_URL.txt exists inside the tree we publish
    url_file = os.path.join(args.dir, "GITHUB_URL.txt")
    with open(url_file, "w", encoding="utf-8") as fh:
        fh.write(html_url + "\nSKILL.md\n")

    failures = []
    for rel, full in collect_files(args.dir, DEFAULT_EXCLUDE):
        with open(full, "rb") as fh:
            raw = fh.read()
        # No "branch" key on purpose: an empty repository has no commit on its
        # default branch yet, and naming a branch that does not exist is refused
        # (403 "Resource not accessible by personal access token" for a
        # fine-grained token). Omitting it targets the default branch, which is
        # what creates the first commit.
        payload = {
            "message": "Add %s" % rel,
            "content": base64.b64encode(raw).decode("ascii"),
        }
        status, body = request(
            "PUT", "%s/repos/%s/%s/contents/%s" % (API, owner, args.repo, rel),
            args.token, payload)
        # an existing file needs its blob sha
        if status == 422 and isinstance(body, dict) and "sha" in str(body.get("message", "")):
            status, existing = request(
                "GET", "%s/repos/%s/%s/contents/%s" % (API, owner, args.repo, rel),
                args.token)
            if status == 200:
                payload["sha"] = existing["sha"]
                status, body = request(
                    "PUT", "%s/repos/%s/%s/contents/%s" % (API, owner, args.repo, rel),
                    args.token, payload)
        if status not in (200, 201):
            failures.append((rel, status, str(body)[:160]))
            print("  FAIL", rel, status, str(body)[:120])
        else:
            print("  ok  ", rel)

    print("\nrepository:", html_url)
    if failures:
        print("failed files:", len(failures))
        for rel, status, msg in failures:
            print("  -", rel, status, msg)
        return 1
    print("all files published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Environment handling."""

import os
import sys


def require_env(names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)


def public_raw_url(repo_path):
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ["GITHUB_REF_NAME"]
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{repo_path}"

"""Private storage for Reddit data in a PUBLIC repo: encrypted Actions artifacts (90 days).

Search Console data, the findquestions list and the Reddit drafts must never be committed.
GitHub artifacts of a public repo are downloadable by any GitHub user, so everything written
here is Fernet-encrypted with the secret REDDIT_ARTIFACT_KEY; only the ledger (status, url,
a short question hash) is in git.

  writer (reddit_draft.py)  -> reddit-out/batch.enc, reddit-out/pool.enc   (the workflow uploads them)
  reader (publish.py)       -> downloads the artifact of a batch by name via the REST API
                               (needs `actions: read` and GITHUB_TOKEN) and decrypts it.
"""

import io
import json
import os
import zipfile
from pathlib import Path

from core import http

OUT_DIR = Path("reddit-out")
RETENTION_DAYS = 90


class StoreError(RuntimeError):
    pass


def _fernet():
    from cryptography.fernet import Fernet
    key = os.environ.get("REDDIT_ARTIFACT_KEY")
    if not key:
        raise StoreError("REDDIT_ARTIFACT_KEY is not set (cannot encrypt/decrypt Reddit data)")
    return Fernet(key.encode())


def encrypt(obj):
    return _fernet().encrypt(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def decrypt(blob):
    from cryptography.fernet import InvalidToken
    try:
        return json.loads(_fernet().decrypt(blob).decode("utf-8"))
    except InvalidToken:
        raise StoreError("Could not decrypt the Reddit artifact (wrong REDDIT_ARTIFACT_KEY?)") from None


def write(name, obj):
    """Write reddit-out/<name>.enc (encrypted). The workflow uploads the directory as an artifact."""
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{name}.enc"
    path.write_bytes(encrypt(obj))
    return path


def batch_id_of(question_id):
    """Draft ids are <YYYYMMDD><n>; the batch (artifact) is reddit-batch-<YYYYMMDD>."""
    return str(question_id)[:8]


def artifact_name(batch):
    return f"reddit-batch-{batch}"


def _api(path):
    return f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}"


def _headers():
    return {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}


def find_artifact(name):
    """Newest non-expired artifact with this name, or None."""
    data = http.request("GET", _api("/actions/artifacts"), "gh-artifacts", headers=_headers(),
                        params={"name": name, "per_page": 10}).json()
    live = [a for a in data.get("artifacts", []) if not a.get("expired")]
    return max(live, key=lambda a: a["created_at"]) if live else None


def download(artifact):
    resp = http.request("GET", _api(f"/actions/artifacts/{artifact['id']}/zip"), "gh-artifact-zip", headers=_headers())
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        return {n: z.read(n) for n in z.namelist()}


def read(name, filename):
    """Decrypted JSON of <filename> in the newest artifact called <name>; None if there is none (expired)."""
    art = find_artifact(name)
    if not art:
        return None
    files = download(art)
    if filename not in files:
        raise StoreError(f"Artifact {name} has no {filename}")
    return decrypt(files[filename])


def load_batch(batch):
    return read(artifact_name(batch), "batch.enc")


def load_pool():
    """Leftover findquestions candidates from earlier weeks ({} if none / expired)."""
    return read("reddit-pool", "pool.enc") or {}

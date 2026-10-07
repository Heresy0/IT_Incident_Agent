"""Create local credentials without printing secrets; never overwrite existing identities."""
import hashlib
import json
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main():
    registry, credentials = ROOT / ".auth.local.json", ROOT / ".demo-credentials.local.json"
    if registry.exists() or credentials.exists():
        print("Local identity files already exist; preserved.")
        return
    rows, public = [], []
    for tenant, user, role in (("team_a", "alice", "operator"), ("team_a", "bob", "user"), ("team_b", "alice", "user")):
        token = secrets.token_urlsafe(32)
        identity = {"tenant_id": tenant, "user_id": user, "role": role}
        rows.append({**identity, "token": token})
        public.append({**identity, "token_sha256": hashlib.sha256(token.encode()).hexdigest()})
    credentials.write_text(json.dumps({"principals": rows}, indent=2), encoding="utf-8")
    registry.write_text(json.dumps({"principals": public}, indent=2), encoding="utf-8")
    print(f"Created server registry: {registry}")
    print(f"Created private demo credentials: {credentials}")
    print("Copy a token from the private file into the web page. Keep both files local.")


if __name__ == "__main__":
    main()

"""Demo bearer authentication. Identity is resolved exclusively by the server."""
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

ROOT = Path(__file__).resolve().parents[2]
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    role: str = "user"

    def bind(self, request):
        for key in ("tenant_id", "user_id"):
            if request.get(key) is not None and request[key] != getattr(self, key):
                raise HTTPException(403, "请求身份与访问令牌不一致。")
        return {**request, "tenant_id": self.tenant_id, "user_id": self.user_id}


def get_current_principal(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "请先输入访问令牌。", headers={"WWW-Authenticate": "Bearer"})
    if len(credentials.credentials) > 512:
        raise HTTPException(401, "访问令牌无效。", headers={"WWW-Authenticate": "Bearer"})
    try:
        path = Path(os.getenv("INCIDENT_AUTH_FILE", os.getenv("RESEARCH_AUTH_FILE", str(ROOT / ".auth.local.json"))))
        rows = json.loads(path.read_text(encoding="utf-8"))["principals"]
        if not rows:
            raise ValueError()
        for row in rows:
            if not all(IDENTIFIER.fullmatch(row[key]) for key in ("tenant_id", "user_id")):
                raise ValueError()
            if not re.fullmatch(r"[0-9a-f]{64}", row["token_sha256"]) or row.get("role", "user") not in {"user", "operator"}:
                raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "服务端身份配置不可用，请先运行演示身份初始化脚本。") from None
    digest = hashlib.sha256(credentials.credentials.encode()).hexdigest()
    for row in rows:
        if hmac.compare_digest(digest, row["token_sha256"]):
            return Principal(row["tenant_id"], row["user_id"], row.get("role", "user"))
    raise HTTPException(401, "访问令牌无效或已撤销。", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.get("/me")
def me(principal: Principal = Depends(get_current_principal)):
    return {"tenant_id": principal.tenant_id, "user_id": principal.user_id, "role": principal.role}

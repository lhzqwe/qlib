from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests


AUTH_STORE_VERSION = 1
OPENAI_CODEX_PROVIDER = "openai-codex"
OPENAI_CODEX_CLIENT_ID = "codex-cli"
OPENAI_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_STATE_DIR = Path.home() / ".qlib" / "codex_daily_autoresearch" / "auth"
DEFAULT_CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"


class CodexAuthError(RuntimeError):
    pass


class CodexProfileNotFound(CodexAuthError):
    pass


class CodexReauthRequiredError(CodexAuthError):
    pass


@dataclass
class CodexProfile:
    access: str
    refresh: str
    provider: str = OPENAI_CODEX_PROVIDER
    profile_type: str = "oauth"
    expires: Optional[int] = None
    email: Optional[str] = None
    account_id: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CodexProfile":
        return cls(
            access=str(payload.get("access") or ""),
            refresh=str(payload.get("refresh") or ""),
            provider=str(payload.get("provider") or OPENAI_CODEX_PROVIDER),
            profile_type=str(payload.get("type") or payload.get("profile_type") or "oauth"),
            expires=int(payload["expires"]) if isinstance(payload.get("expires"), int) else None,
            email=str(payload["email"]) if isinstance(payload.get("email"), str) else None,
            account_id=str(payload["account_id"]) if isinstance(payload.get("account_id"), str) else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "type": self.profile_type,
            "provider": self.provider,
            "access": self.access,
            "refresh": self.refresh,
        }
        if self.expires is not None:
            payload["expires"] = int(self.expires)
        if self.email:
            payload["email"] = self.email
        if self.account_id:
            payload["account_id"] = self.account_id
        return payload


def resolve_state_dir(state_dir: Optional[str] = None) -> Path:
    path = Path(state_dir).expanduser() if state_dir else DEFAULT_STATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_auth_store_path(state_dir: Optional[str] = None) -> Path:
    return resolve_state_dir(state_dir) / "profiles.json"


def resolve_auth_lock_path(state_dir: Optional[str] = None) -> Path:
    return resolve_state_dir(state_dir) / "profiles.lock"


def resolve_codex_auth_path(codex_auth_path: Optional[str] = None) -> Path:
    return Path(codex_auth_path).expanduser() if codex_auth_path else DEFAULT_CODEX_AUTH_PATH


@contextmanager
def auth_store_lock(
    state_dir: Optional[str] = None,
    timeout_seconds: float = 10.0,
    poll_seconds: float = 0.1,
) -> Iterator[None]:
    lock_path = resolve_auth_lock_path(state_dir)
    fd = None
    start = time.monotonic()
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            break
        except FileExistsError:
            if time.monotonic() - start >= timeout_seconds:
                raise TimeoutError("Timed out waiting for auth store lock: %s" % lock_path)
            time.sleep(poll_seconds)

    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _coerce_store(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"version": AUTH_STORE_VERSION, "profiles": {}}
    version = raw.get("version")
    profiles = raw.get("profiles")
    return {
        "version": int(version) if isinstance(version, int) else AUTH_STORE_VERSION,
        "profiles": profiles if isinstance(profiles, dict) else {},
    }


def load_auth_store(state_dir: Optional[str] = None) -> Dict[str, Any]:
    path = resolve_auth_store_path(state_dir)
    if not path.exists():
        return _coerce_store(None)
    try:
        return _coerce_store(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return _coerce_store(None)


def save_auth_store(store: Dict[str, Any], state_dir: Optional[str] = None) -> None:
    path = resolve_auth_store_path(state_dir)
    path.write_text(json.dumps(_coerce_store(store), indent=2), encoding="utf-8")


def build_profile_id(email: Optional[str] = None) -> str:
    return "%s:%s" % (OPENAI_CODEX_PROVIDER, email) if email else "%s:default" % OPENAI_CODEX_PROVIDER


def list_profiles(provider: Optional[str] = None, state_dir: Optional[str] = None) -> List[Tuple[str, CodexProfile]]:
    store = load_auth_store(state_dir)
    profiles = []
    for profile_id, raw_profile in sorted(store["profiles"].items()):
        if not isinstance(raw_profile, dict):
            continue
        profile = CodexProfile.from_dict(raw_profile)
        if provider and profile.provider != provider:
            continue
        profiles.append((profile_id, profile))
    return profiles


def get_profile(profile_id: str, state_dir: Optional[str] = None) -> Optional[CodexProfile]:
    raw_profile = load_auth_store(state_dir)["profiles"].get(profile_id)
    if not isinstance(raw_profile, dict):
        return None
    return CodexProfile.from_dict(raw_profile)


def upsert_profile(profile_id: str, profile: CodexProfile, state_dir: Optional[str] = None) -> CodexProfile:
    with auth_store_lock(state_dir):
        store = load_auth_store(state_dir)
        store["profiles"][profile_id] = profile.to_dict()
        save_auth_store(store, state_dir)
    return profile


def is_profile_expired(profile: CodexProfile, skew_ms: int = 60_000) -> bool:
    return profile.expires is not None and profile.expires <= int(time.time() * 1000) + skew_ms


def _decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    if not token or token.count(".") < 2:
        raise ValueError("Invalid JWT token")
    payload = token.split(".")[1]
    return json.loads(_decode_base64url(payload).decode("utf-8"))


def get_jwt_expiry_ms(token: str) -> Optional[int]:
    try:
        payload = decode_jwt_payload(token)
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return int(float(exp) * 1000)
    return None


def get_jwt_email(token: str) -> Optional[str]:
    try:
        payload = decode_jwt_payload(token)
    except (ValueError, json.JSONDecodeError):
        return None
    email = payload.get("email")
    if isinstance(email, str) and email:
        return email
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        nested_email = profile.get("email")
        if isinstance(nested_email, str) and nested_email:
            return nested_email
    return None


def load_codex_auth_tokens(codex_auth_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = resolve_codex_auth_path(codex_auth_path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    auth_mode = raw.get("auth_mode")
    if auth_mode not in (None, "chatgpt"):
        return None
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        return None
    return tokens


def import_codex_auth_profile(
    state_dir: Optional[str] = None,
    profile_id: Optional[str] = None,
    codex_auth_path: Optional[str] = None,
) -> Optional[Tuple[str, CodexProfile]]:
    tokens = load_codex_auth_tokens(codex_auth_path)
    if not tokens:
        return None
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(refresh, str) or not refresh:
        return None
    profile = CodexProfile(
        access=access,
        refresh=refresh,
        expires=get_jwt_expiry_ms(access),
        email=get_jwt_email(access),
        account_id=str(tokens["account_id"]) if isinstance(tokens.get("account_id"), str) else None,
    )
    selected_profile_id = profile_id or build_profile_id(profile.email)
    upsert_profile(selected_profile_id, profile, state_dir)
    return selected_profile_id, profile


def pick_profile_id(explicit_profile_id: Optional[str] = None, state_dir: Optional[str] = None) -> Optional[str]:
    if explicit_profile_id:
        return explicit_profile_id
    profiles = list_profiles(provider=OPENAI_CODEX_PROVIDER, state_dir=state_dir)
    if not profiles:
        return None
    non_default = [profile_id for profile_id, _ in profiles if not profile_id.endswith(":default")]
    if non_default:
        return non_default[0]
    return profiles[0][0]


def get_active_codex_profile(
    profile_id: Optional[str] = None,
    state_dir: Optional[str] = None,
    auto_import: bool = True,
) -> Tuple[str, CodexProfile]:
    selected_profile_id = pick_profile_id(profile_id, state_dir)
    profile = get_profile(selected_profile_id, state_dir) if selected_profile_id else None
    if profile is None and auto_import:
        imported = import_codex_auth_profile(state_dir=state_dir, profile_id=profile_id)
        if imported:
            return imported
        selected_profile_id = pick_profile_id(profile_id, state_dir)
        profile = get_profile(selected_profile_id, state_dir) if selected_profile_id else None
    if selected_profile_id is None or profile is None:
        raise CodexProfileNotFound("No OpenAI Codex OAuth profile is available. Import ~/.codex/auth.json or log in first.")
    if is_profile_expired(profile):
        return refresh_codex_profile(selected_profile_id, state_dir=state_dir)
    return selected_profile_id, profile


def _build_profile_from_token_payload(payload: Dict[str, Any], previous_profile: Optional[CodexProfile] = None) -> CodexProfile:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise CodexAuthError("Token response did not include an access_token.")
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = previous_profile.refresh if previous_profile is not None else None
    if not isinstance(refresh, str) or not refresh:
        raise CodexReauthRequiredError("Token response did not include a refresh token. Please re-authorize Codex.")
    expires = None
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)):
        expires = int((time.time() + float(expires_in)) * 1000)
    if expires is None:
        expires = get_jwt_expiry_ms(access)
    email = get_jwt_email(access) or (previous_profile.email if previous_profile is not None else None)
    account_id = payload.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        account_id = previous_profile.account_id if previous_profile is not None else None
    return CodexProfile(
        access=access,
        refresh=refresh,
        expires=expires,
        email=email,
        account_id=account_id,
    )


def _post_token_request(form: Dict[str, str], requests_post=requests.post) -> Dict[str, Any]:
    try:
        response = requests_post(
            OPENAI_CODEX_TOKEN_URL,
            data=form,
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise CodexAuthError("Token request failed: %s" % exc) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": getattr(response, "text", "")}
    if response.status_code >= 400:
        error = payload.get("error") or getattr(response, "reason", "request_failed")
        description = payload.get("error_description") or payload.get("message")
        detail = "%s: %s" % (error, description) if description else str(error)
        raise CodexAuthError("Token exchange failed: %s" % detail)
    return payload


def refresh_codex_profile(
    profile_id: str,
    state_dir: Optional[str] = None,
    force: bool = False,
    requests_post=requests.post,
) -> Tuple[str, CodexProfile]:
    with auth_store_lock(state_dir):
        store = load_auth_store(state_dir)
        raw_profile = store["profiles"].get(profile_id)
        if not isinstance(raw_profile, dict):
            raise CodexProfileNotFound("Codex profile not found: %s" % profile_id)
        profile = CodexProfile.from_dict(raw_profile)
        if not force and not is_profile_expired(profile):
            return profile_id, profile
        if not profile.refresh:
            raise CodexReauthRequiredError("Codex profile does not have a refresh token. Please re-authorize.")
        try:
            payload = _post_token_request(
                {
                    "grant_type": "refresh_token",
                    "client_id": OPENAI_CODEX_CLIENT_ID,
                    "refresh_token": profile.refresh,
                },
                requests_post=requests_post,
            )
        except CodexAuthError as exc:
            raise CodexReauthRequiredError("Codex OAuth refresh failed. Please import or refresh ~/.codex/auth.json.") from exc
        refreshed = _build_profile_from_token_payload(payload, previous_profile=profile)
        store["profiles"][profile_id] = refreshed.to_dict()
        save_auth_store(store, state_dir)
        return profile_id, refreshed

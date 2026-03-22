from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from auth import (
    OPENAI_CODEX_RESPONSES_URL,
    CodexAuthError,
    CodexProfile,
    CodexReauthRequiredError,
    get_active_codex_profile,
    refresh_codex_profile,
)


def _normalize_sse_line(raw_line: Any) -> str:
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8", errors="replace")
    return str(raw_line)


def _extract_error_detail(payload: Dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or json.dumps(error))
    if error:
        return str(error)
    message = payload.get("message")
    if isinstance(message, str) and message:
        return message
    return json.dumps(payload)


def parse_sse_response(response: requests.Response) -> Dict[str, Any]:
    event_name = None
    data_lines = []
    last_response = None

    def flush_event() -> Optional[Dict[str, Any]]:
        nonlocal event_name, data_lines, last_response
        if not data_lines:
            return None
        raw_data = "\n".join(data_lines).strip()
        event_name = event_name or ""
        data_lines = []
        if not raw_data or raw_data == "[DONE]":
            return None
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise CodexAuthError("Malformed SSE payload: %s" % raw_data) from exc
        response_payload = payload.get("response")
        if isinstance(response_payload, dict):
            last_response = response_payload
        event_type = payload.get("type") or event_name
        if event_type == "response.completed" and isinstance(response_payload, dict):
            return response_payload
        if event_type in ("response.failed", "error"):
            raise CodexAuthError("Codex request failed: %s" % _extract_error_detail(payload))
        return None

    for raw_line in response.iter_lines():
        line = _normalize_sse_line(raw_line).strip()
        if not line:
            completed = flush_event()
            if completed is not None:
                return completed
            event_name = None
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())

    completed = flush_event()
    if completed is not None:
        return completed
    if last_response is not None and last_response.get("status") == "completed":
        return last_response
    raise CodexAuthError("Codex stream ended without a completed response.")


def extract_response_text(response_json: Dict[str, Any]) -> str:
    parts = []
    for item in response_json.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            for content_item in content:
                if not isinstance(content_item, dict):
                    continue
                if content_item.get("type") in ("output_text", "text", "input_text"):
                    text = content_item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        elif isinstance(content, str) and content.strip():
            parts.append(content.strip())
    output_text = response_json.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        parts.append(output_text.strip())
    return "\n".join(parts).strip()


def extract_json_payload(text: str) -> Dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise CodexAuthError("Codex response did not contain a JSON object.")
    candidate = stripped[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise CodexAuthError("Unable to parse Codex JSON payload.") from exc


class CodexResponsesClient:
    def __init__(
        self,
        auth_profile_id: Optional[str] = None,
        state_dir: Optional[str] = None,
        endpoint: str = OPENAI_CODEX_RESPONSES_URL,
        timeout: float = 120.0,
        max_retries: int = 1,
    ) -> None:
        self.auth_profile_id = auth_profile_id
        self.state_dir = state_dir
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max_retries

    def _build_payload(self, system_prompt: str, user_prompt: str, model: str) -> Dict[str, Any]:
        return {
            "model": model,
            "instructions": system_prompt,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_prompt}],
                }
            ],
            "stream": True,
            "store": False,
        }

    def _post(self, payload: Dict[str, Any], profile: CodexProfile, requests_post=requests.post) -> Dict[str, Any]:
        headers = {
            "Authorization": "Bearer %s" % profile.access,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "Qlib-Codex-Autoresearch/0.1",
        }
        if profile.account_id:
            headers["ChatGPT-Account-Id"] = profile.account_id
        try:
            response = requests_post(
                self.endpoint,
                headers=headers,
                json=payload,
                stream=True,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise CodexAuthError("Codex request failed: %s" % exc) from exc

        if response.status_code == 401:
            raise CodexReauthRequiredError("Codex access token was rejected.")

        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {"error": getattr(response, "text", "")}
            raise CodexAuthError("Codex request failed: %s" % _extract_error_detail(body))
        return parse_sse_response(response)

    def request_candidates(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        requests_post=requests.post,
    ) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
        payload = self._build_payload(system_prompt, user_prompt, model)
        profile_id, profile = get_active_codex_profile(
            profile_id=self.auth_profile_id,
            state_dir=self.state_dir,
            auto_import=True,
        )
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response_json = self._post(payload, profile, requests_post=requests_post)
                response_text = extract_response_text(response_json)
                parsed = extract_json_payload(response_text)
                return parsed, response_text, response_json
            except CodexReauthRequiredError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                profile_id, profile = refresh_codex_profile(
                    profile_id,
                    state_dir=self.state_dir,
                    force=True,
                    requests_post=requests_post,
                )
        raise CodexReauthRequiredError(
            "Codex OAuth credentials are no longer valid. Refresh ~/.codex/auth.json and retry."
        ) from last_error

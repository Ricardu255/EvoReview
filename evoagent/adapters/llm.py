"""JSON model client with auditable usage accounting.

Supports OpenAI-compatible Chat Completions and the native Anthropic Messages
API. Both transports return the same parsed JSON object and ledger records.
"""
import json
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from ..agent.telemetry import ExecutionLedger


# Transient server/rate-limit failures worth retrying; other 4xx responses
# are deterministic client errors (bad key, unsupported parameter, ...).
RETRYABLE_HTTP_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
MAX_RETRY_DELAY_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 30.0
# Reasoning models can spend their whole output budget on reasoning and
# return empty or truncated content; retrying with a doubled budget escapes
# the runaway without weakening the per-call policy for other failures.
MAX_OUTPUT_TOKEN_CAP = 65536

FENCE_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
TRAILING_COMMA = re.compile(r",\s*([}\]])")


def parse_model_json(content: str, finish_reason: str = "") -> Dict[str, Any]:
    """Best-effort parse of model output into a JSON object.

    Despite ``response_format: json_object``, real models still wrap JSON in
    markdown fences, append prose, emit raw control characters inside
    evidence strings, or leave trailing commas before a closing brace.
    Parsing therefore tries strict JSON, fenced blocks and the first balanced
    JSON object (with ``strict=False``), then a trailing-comma repair, in
    that order.
    """
    if not isinstance(content, str):
        raise ValueError("model content is %s, not a string" % type(content).__name__)

    def load(candidate: str) -> Dict[str, Any]:
        value = json.loads(candidate, strict=False)
        if not isinstance(value, dict):
            raise ValueError("model JSON root is not an object")
        return value

    def load_repaired(candidate: str) -> Dict[str, Any]:
        try:
            return load(candidate)
        except ValueError:
            return load(TRAILING_COMMA.sub(r"\1", candidate))

    errors = []
    for candidate in (content, *(block.strip() for block in FENCE_BLOCK.findall(content))):
        try:
            return load_repaired(candidate)
        except (ValueError, TypeError) as exc:
            errors.append(str(exc)[:120])
    start = content.find("{")
    if start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(content)):
            char = content[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return load_repaired(content[start:index + 1])
                    except (ValueError, TypeError) as exc:
                        errors.append(str(exc)[:120])
                    break
    raise ValueError(
        "invalid JSON (finish_reason=%s): %s | content preview: %s"
        % (finish_reason or "unknown", "; ".join(errors[-2:]), content[:200])
    )


class JsonChatClient:
    def __init__(
        self, base_url: str, api_key: str, model: str,
        provider: str = "openai-compatible", timeout: int = 60,
        extra_headers: Optional[Dict[str, str]] = None,
        retries: int = 2, backoff_seconds: float = 1.0,
        extra_payload: Optional[Dict[str, Any]] = None,
        api_style: str = "openai",
    ):
        self.api_style = str(api_style or "openai").strip().lower()
        if self.api_style not in {"openai", "anthropic"}:
            raise ValueError("unsupported LLM API style: %s" % api_style)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.retries = max(0, int(retries))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.extra_payload = dict(extra_payload or {})

    def complete_json(
        self, role: str, system: str, user: str,
        ledger: Optional[ExecutionLedger] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = self._build_payload(system, user, max_tokens)
        headers = self._build_headers()
        last_message = ""
        current_max_tokens = max_tokens
        if self.api_style == "anthropic":
            current_max_tokens = int(payload["max_tokens"])
        initial_max_tokens = current_max_tokens
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            delay = 0.0
            retryable = False
            request_payload = dict(payload)
            if current_max_tokens and current_max_tokens != initial_max_tokens:
                request_payload["max_tokens"] = int(current_max_tokens)
            data = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                self.base_url + self._endpoint(), data=data,
                headers=headers, method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                content, finish_reason = self._extract_content(body)
                try:
                    result = parse_model_json(content, finish_reason)
                except (TypeError, ValueError) as exc:
                    last_message = "%s returned an invalid JSON response: %s" % (
                        self.provider, exc,
                    )
                    if finish_reason == "length" and current_max_tokens:
                        # Reasoning consumed the output budget; retry with a
                        # doubled budget to let the content complete.
                        retryable = True
                        current_max_tokens = min(
                            MAX_OUTPUT_TOKEN_CAP, int(current_max_tokens) * 2,
                        )
                    elif content is None or not content.strip():
                        # Degenerate empty or whitespace-only response: a
                        # plain retry (budget escalation cannot fix an
                        # empty answer).
                        retryable = True
                else:
                    if ledger:
                        ledger.record_model(
                            role, self.provider, self.model, body.get("usage") or {},
                            int((time.monotonic() - started) * 1000), True,
                        )
                    return result
            except urllib.error.HTTPError as exc:
                detail = exc.read(1000).decode("utf-8", errors="replace")
                last_message = "%s API returned HTTP %d: %s" % (
                    self.provider, exc.code, detail,
                )
                retryable = exc.code in RETRYABLE_HTTP_CODES
                if retryable:
                    delay = self._retry_delay(attempt, self._retry_after(exc))
            except (urllib.error.URLError, socket.timeout) as exc:
                last_message = "%s JSON request failed: %s" % (self.provider, exc)
                retryable = True
                delay = self._retry_delay(attempt)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                # Malformed HTTP body or response envelope: not transient.
                last_message = "%s JSON request failed: %s" % (self.provider, exc)
            if ledger:
                ledger.record_model(
                    role, self.provider, self.model, {},
                    int((time.monotonic() - started) * 1000), False, last_message,
                )
            if not retryable or attempt >= self.retries:
                break
            time.sleep(delay)
        raise RuntimeError(last_message)

    def _build_payload(
        self, system: str, user: str, max_tokens: Optional[int],
    ) -> Dict[str, Any]:
        if self.api_style == "anthropic":
            instruction = "Return only a single valid JSON object."
            payload: Dict[str, Any] = {
                "model": self.model,
                "temperature": 0,
                "max_tokens": int(max_tokens or 4096),
                "system": (
                    system.rstrip() + "\n\n" + instruction if system else instruction
                ),
                "messages": [{"role": "user", "content": user}],
            }
        else:
            payload = {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            }
            if max_tokens:
                payload["max_tokens"] = int(max_tokens)
        payload.update(self.extra_payload)
        return payload

    def _build_headers(self) -> Dict[str, str]:
        if self.api_style == "anthropic":
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        else:
            headers = {
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        headers.update(self.extra_headers)
        return headers

    def _endpoint(self) -> str:
        return "/messages" if self.api_style == "anthropic" else "/chat/completions"

    def _extract_content(self, body: Dict[str, Any]) -> Tuple[Optional[str], str]:
        if self.api_style == "anthropic":
            blocks = body.get("content") or []
            if not isinstance(blocks, list):
                raise ValueError("Anthropic content is not a list")
            content = "".join(
                str(block.get("text", "")) for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
            stop = str(body.get("stop_reason") or "")
            return content, {"max_tokens": "length"}.get(stop, stop)
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return message.get("content"), str(choice.get("finish_reason") or "")

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
        headers = getattr(exc, "headers", None)
        raw = headers.get("Retry-After") if headers else None
        if not raw:
            return None
        try:
            return min(MAX_RETRY_AFTER_SECONDS, max(0.0, float(raw)))
        except (TypeError, ValueError):
            return None

    def _retry_delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None:
            return retry_after
        return min(MAX_RETRY_DELAY_SECONDS, self.backoff_seconds * (2 ** attempt))

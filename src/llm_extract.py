"""Build the extraction prompt and send it to a model.

Supported backends (selected via ``ELCAP_LLM_CLI``, default ``auto``):

- ``api``        the Anthropic API through the official SDK, authenticated by
                 ``ANTHROPIC_API_KEY``. This is the only backend that works on
                 a hosted web server, where no CLI is installed and nobody is
                 logged in; ``auto`` prefers it whenever a key is set.

- ``compat``     any OpenAI-compatible gateway (Open WebUI, LiteLLM, vLLM, a
                 university or company AI service): POST /chat/completions
                 with ``ELCAP_COMPAT_BASE_URL`` / ``ELCAP_COMPAT_API_KEY``.
- ``opencode``   `opencode run` (stdin prompt, NDJSON events on stdout)
- ``claude``     Claude Code `claude -p` (stdin prompt, reply on stdout)
- ``codex``      OpenAI Codex `codex exec -` (stdin prompt, final msg on stdout)
- ``gemini``     Google Gemini CLI (non-TTY stdin = single prompt)
- ``auto``       the Anthropic API if a key is set, else a configured gateway,
                 else the first CLI available

Every backend receives the prompt on **stdin** so it works with long
statements even where the OS limits command-line length (Windows: ~32 KB).

The LLM is used only to *read* the statement and return line items as JSON.
The credit metrics themselves are computed deterministically in Python.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

#: The 19 monetary line items the scorecard needs.
LINE_ITEM_FIELDS = (
    "total_revenue", "net_income", "interest_expense", "taxes",
    "depreciation", "amortization", "ebit", "current_assets",
    "current_liabilities", "total_liabilities", "total_equity",
    "intangible_assets", "goodwill", "line_of_credit",
    "current_portion_lt_debt", "current_finance_leases",
    "long_term_debt", "long_term_finance_leases", "operating_cash_flow",
)

EXTRACT_JSON_RULES = """You are a financial statement data extraction engine. Below is the raw text of an audited financial statement.

Extract the key financial line items for the MOST RECENT COMPLETE FISCAL YEAR only.

Return ONLY a single JSON object with exactly these keys:
{
  "company_name": "string or null",
  "fiscal_year": "YYYY or null",
  "statement_basis": "complete" | "abbreviated" | "unknown",
  "unit_scale": "raw" | "thousands" | "millions" | "billions",
  "total_revenue": number or null,
  "net_income": number or null,
  "interest_expense": number or null,
  "taxes": number or null,
  "depreciation": number or null,
  "amortization": number or null,
  "ebit": number or null,
  "current_assets": number or null,
  "current_liabilities": number or null,
  "total_liabilities": number or null,
  "total_equity": number or null,
  "intangible_assets": number or null,
  "goodwill": number or null,
  "line_of_credit": number or null,
  "current_portion_lt_debt": number or null,
  "current_finance_leases": number or null,
  "long_term_debt": number or null,
  "long_term_finance_leases": number or null,
  "operating_cash_flow": number or null,
  "absent_line_items": ["field_name", ...],
  "sources": {"field_name": "verbatim label and figure you took it from", ...},
  "notes": ["short caveat", ...]
}

STATEMENT BASIS - set "statement_basis" to:
- "complete": an ordinary going-concern set - balance sheet, income statement
  and usually a statement of cash flows for an operating entity.
- "abbreviated": anything less than that, including SEC Rule 3-05/3-14 exhibits
  ("Statements of Revenues and Direct Operating Expenses", "Statement of Assets
  Acquired and Liabilities Assumed"), carve-out or divested-business statements,
  and any presentation that omits liabilities the business actually owes.
  Ratios from these documents are misleading, so they are flagged for review.
- "unknown": you cannot tell.

UNITS - read this carefully:
- Report every number EXACTLY AS PRINTED in the statement. Do NOT multiply it out.
- "unit_scale" describes what those printed numbers mean:
    the statement header says "(in thousands)"  -> "thousands"
    the statement header says "(in millions)"   -> "millions"
    the statement shows whole dollars           -> "raw"
- Example: a statement headed "(in thousands)" showing revenue of 2,843 must
  return "total_revenue": 2843 with "unit_scale": "thousands". NEVER 2843000.
- The caller applies the multiplier. Returning a pre-multiplied number with a
  non-raw unit_scale produces an answer that is wrong by 1000x.

null vs. absent - this distinction matters:
- Use a NUMBER when you can read the figure.
- List a field in "absent_line_items" when the statement clearly shows the
  company has no such item (e.g. a balance sheet with no borrowings at all ->
  list line_of_credit, current_portion_lt_debt, current_finance_leases,
  long_term_debt, long_term_finance_leases; no income tax line -> list taxes).
  Those are treated as zero.
- Use null ONLY when the relevant statement or section is missing or you
  genuinely cannot tell. null leaves the metric blank rather than scoring it.
- A field must not be both a number and in absent_line_items.

OTHER RULES:
- Numbers must be plain numerics: no $ signs, no commas, no parentheses.
  A figure shown as (1,484,458) is negative: -1484458.
- interest_expense: POSITIVE for interest EXPENSE. If the statement reports net
  interest INCOME, return it as a NEGATIVE number (a negative expense).
- ebit: the scorecard defines EBIT as net income + interest + taxes, and the
  caller computes exactly that when you return null. So only fill "ebit" in
  when the statement shows an operating-income subtotal struck BEFORE interest
  expense - a normal "Income (loss) from operations" above an "Interest
  expense" line. Return that subtotal EXACTLY AS PRINTED. Do not add interest,
  taxes or anything else back to it: a pre-interest subtotal already excludes
  interest, so adding it again overstates EBIT and the EBIT margin.
  Return NULL whenever interest expense sits inside the operating expense
  subtotal, which is how utilities, cooperatives and many not-for-profits
  present it ("Total cost of electric service" and "Other expenses" often
  include interest, so "Operating Margins" is an after-interest figure). Using
  an after-interest subtotal as EBIT understates EBIT margin badly - one such
  co-op scores -0.01% instead of 6.3%.
  If you are not certain the subtotal is pre-interest, return null.
  Never return 0 for ebit. Zero means "no pre-interest subtotal" - say null.
- taxes: INCOME taxes only ("provision for income taxes", "income tax
  expense"). Property, franchise, payroll, excise, gross-receipts and "taxes
  other than income" are operating costs, not add-backs - exclude them and, if
  the entity pays no income tax at all (a co-op, an S corporation, a
  partnership), list "taxes" in absent_line_items and say so in "notes".
- depreciation / amortization: prefer the add-back lines in the statement of
  cash flows, else the income statement line.
  A COMBINED line - "Depreciation and amortization", "Depreciation, depletion
  and amortization", "D&A" - must NOT be dropped. Put the whole combined amount
  in "depreciation" and list "amortization" in absent_line_items, noting the
  combination in "sources". Both feed EBITDA identically, so the total is what
  matters; skipping a combined line understates EBITDA and mis-rates coverage
  and leverage.
- total_liabilities: the balance sheet "Total liabilities" line. If only
  "Total liabilities and stockholders' equity" is shown, subtract total equity.
- total_equity: whatever the entity calls its residual ownership balance -
  "Total stockholders' equity (deficit)", "Members' equity", "Partners'
  capital", "Net parent investment", "Net assets", "Total equity". Subsidiary
  and carve-out statements rarely use the word "stockholders"; do not return
  null just because that heading is missing. A negative balance is normal.
- operating_cash_flow: "Net cash provided by (used in) operating activities".
- Most recent fiscal year = the LAST complete year column (e.g. 2011 in a
  FY2011/FY2010 pair). Never mix columns from different years.
- "sources": one short verbatim string per field you filled in, e.g.
  "Total revenue 707,868 (Statements of Operations, year ended Dec 31 2011)".
- Do NOT invent data.
- Output the JSON object and nothing else."""

SCHEMA_KEYS = {
    "company_name", "fiscal_year", "statement_basis", "unit_scale",
    "absent_line_items", "sources", "notes", *LINE_ITEM_FIELDS,
}


class ExtractionError(Exception):
    """Raised when the LLM extraction fails after retries."""


class LLMExtractionBackendError(RuntimeError):
    """Raised when a requested LLM CLI cannot be located or invoked."""


# --------------------------------------------------------------------------
# Backend registry
# --------------------------------------------------------------------------

BACKENDS: dict[str, dict] = {
    "opencode": {
        "label": "opencode",
        "env_path": "OPENCODE_PATH",
        "exe_names": ("opencode", "opencode.cmd", "opencode.exe"),
        "known_paths": (
            r"C:\Users\sapta\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe",
        ),
        "install_hint": "Install from https://opencode.ai or set OPENCODE_PATH.",
    },
    "claude": {
        "label": "Claude Code",
        "env_path": "CLAUDE_PATH",
        "exe_names": ("claude", "claude.exe", "claude.cmd"),
        "known_paths": (),
        "install_hint": "Install Claude Code (https://claude.com/claude-code) or set CLAUDE_PATH.",
    },
    "codex": {
        "label": "OpenAI Codex",
        "env_path": "CODEX_PATH",
        "exe_names": ("codex", "codex.exe", "codex.cmd"),
        "known_paths": (),
        "install_hint": "Install the Codex CLI (https://developers.openai.com/codex) or set CODEX_PATH.",
    },
    "gemini": {
        "label": "Google Gemini CLI",
        "env_path": "GEMINI_PATH",
        "exe_names": ("gemini", "gemini.exe", "gemini.cmd"),
        "known_paths": (),
        "install_hint": "Install the Gemini CLI (https://github.com/google-gemini/gemini-cli) or set GEMINI_PATH.",
    },
}

PREFERRED_ORDER = ("opencode", "claude", "codex", "gemini")

# --------------------------------------------------------------------------
# The Anthropic API backend
#
# The CLI backends assume a developer machine: an executable on PATH and a
# logged-in session. A web server has neither, so the same prompt goes over
# the API instead. Everything downstream - JSON parsing, unit handling, the
# metric arithmetic - is shared with the CLI path, so the two cannot drift.
# --------------------------------------------------------------------------

#: Backend name for the Anthropic API path.
API_BACKEND = "api"

#: Model used when no other is asked for.
DEFAULT_API_MODEL = "claude-opus-5"

#: Models offered in the web app's picker, most capable first.
API_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")

#: Room for adaptive thinking plus the JSON object. The reply itself is a few
#: thousand tokens; thinking is billed against the same ceiling.
API_MAX_TOKENS = 16_000

#: Beta flag for server-side refusal fallbacks. A statement extraction is not
#: the kind of request that gets declined, but a decline would otherwise
#: return no scorecard at all, so the fallback is left on.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ALL_BACKENDS = (API_BACKEND, "compat") + PREFERRED_ORDER


def api_key_available() -> bool:
    """Whether an Anthropic API key is present in the environment."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


class _ApiRunner:
    """Send the prompt to the Anthropic API and return the text reply."""

    cli = API_BACKEND

    def __init__(self, model: str | None = None) -> None:
        self.model = (model or os.environ.get("ELCAP_MODEL")
                      or DEFAULT_API_MODEL)

    def run(self, prompt: str, timeout_seconds: int) -> str:
        client = self._client(timeout_seconds)
        request = dict(
            model=self.model,
            max_tokens=API_MAX_TOKENS,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            message = self._send(client, request, with_fallbacks=True)
        except Exception as exc:
            if not _is_bad_request(exc):
                raise
            # Beta flags move. A rejected beta must not cost the user their
            # scorecard, so the same request goes again without it.
            message = self._send(client, request, with_fallbacks=False)

        if getattr(message, "stop_reason", None) == "refusal":
            detail = getattr(getattr(message, "stop_details", None),
                             "explanation", "") or ""
            raise LLMExtractionBackendError(
                "The model declined to read this document. " + detail)
        return "".join(block.text for block in message.content
                       if getattr(block, "type", None) == "text").strip()

    @staticmethod
    def _send(client, request: dict, with_fallbacks: bool):
        # Streaming, because a long statement plus adaptive thinking can run
        # past the SDK's non-streaming HTTP timeout.
        if with_fallbacks:
            with client.beta.messages.stream(
                betas=[FALLBACK_BETA], fallbacks="default", **request
            ) as stream:
                return stream.get_final_message()
        with client.messages.stream(**request) as stream:
            return stream.get_final_message()

    @staticmethod
    def _client(timeout_seconds: int):
        try:
            import anthropic
        except ImportError as exc:
            raise LLMExtractionBackendError(
                "The 'anthropic' package is required for the API backend. "
                "Install it with: pip install anthropic"
            ) from exc
        if not api_key_available():
            raise LLMExtractionBackendError(
                "No ANTHROPIC_API_KEY is set. Put one in the environment, or "
                "in .streamlit/secrets.toml when running the web app."
            )
        return anthropic.Anthropic(timeout=float(timeout_seconds))

    def describe(self) -> str:
        return f"Anthropic API (model={self.model})"


def _is_bad_request(exc: Exception) -> bool:
    """Whether an SDK exception is a 400, as opposed to auth/network/5xx."""
    return (getattr(exc, "status_code", None) == 400
            or type(exc).__name__ == "BadRequestError")


# --------------------------------------------------------------------------
# The OpenAI-compatible gateway backend
#
# University and company AI gateways (Open WebUI, LiteLLM, vLLM, Azure OpenAI
# and the rest) nearly all speak POST /chat/completions rather than the
# Anthropic Messages API, and issue their own keys. This backend sends the
# same prompt there. The reply is parsed by the same code, so a gateway
# fronting Claude produces the same scorecard as the Anthropic API does.
# --------------------------------------------------------------------------

#: Backend name for an OpenAI-compatible gateway.
COMPAT_BACKEND = "compat"

#: Environment variables that configure it.
COMPAT_BASE_URL_VAR = "ELCAP_COMPAT_BASE_URL"
COMPAT_KEY_VAR = "ELCAP_COMPAT_API_KEY"
COMPAT_MODEL_VAR = "ELCAP_COMPAT_MODEL"
#: A JSON object of extra request headers. This is how a gateway behind
#: Cloudflare Access is reached: put its service-token pair
#: ("CF-Access-Client-Id" / "CF-Access-Client-Secret") in here.
COMPAT_HEADERS_VAR = "ELCAP_COMPAT_HEADERS"


def compat_configured() -> bool:
    """Whether a compatible gateway has both a base URL and a key."""
    return bool(os.environ.get(COMPAT_BASE_URL_VAR, "").strip()
                and os.environ.get(COMPAT_KEY_VAR, "").strip())


class _CompatRunner:
    """Send the prompt to an OpenAI-compatible /chat/completions endpoint."""

    cli = COMPAT_BACKEND

    def __init__(self, model: str | None = None) -> None:
        self.base_url = os.environ.get(COMPAT_BASE_URL_VAR, "").strip().rstrip("/")
        self.api_key = os.environ.get(COMPAT_KEY_VAR, "").strip()
        self.model = (model or os.environ.get(COMPAT_MODEL_VAR) or "").strip()

    def run(self, prompt: str, timeout_seconds: int) -> str:
        import requests

        if not self.base_url or not self.api_key:
            raise LLMExtractionBackendError(
                "The compatible-gateway backend needs both a base URL and an "
                f"API key ({COMPAT_BASE_URL_VAR} and {COMPAT_KEY_VAR}).")
        if not self.model:
            raise LLMExtractionBackendError(
                "The compatible-gateway backend needs a model name "
                f"({COMPAT_MODEL_VAR}); gateways do not agree on a default.")

        try:
            response = requests.post(
                self._endpoint(),
                headers=self._headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    # Transcription, not composition: the same statement should
                    # read the same way twice.
                    "temperature": 0,
                    "stream": False,
                },
                timeout=timeout_seconds,
            )
        except Exception as exc:
            raise LLMExtractionBackendError(
                f"Could not reach {self.base_url}: {exc}") from exc

        if response.status_code != 200:
            raise LLMExtractionBackendError(self._explain(response))
        return self._reply(response)

    def _endpoint(self) -> str:
        # Accept a base URL given with or without the /chat/completions tail,
        # because every gateway documents it differently.
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        raw = os.environ.get(COMPAT_HEADERS_VAR, "").strip()
        if raw:
            try:
                extra = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LLMExtractionBackendError(
                    f"{COMPAT_HEADERS_VAR} is not valid JSON: {exc}") from exc
            if not isinstance(extra, dict):
                raise LLMExtractionBackendError(
                    f"{COMPAT_HEADERS_VAR} must be a JSON object of headers.")
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    def _explain(self, response) -> str:
        """Turn a failed response into something the user can act on."""
        body = (response.text or "")[:300]
        # A gateway behind Cloudflare Access answers an unauthenticated call
        # with its sign-in page, not a 401. Without this the user sees "not
        # valid JSON" and has no idea their key was never even looked at.
        if ("cloudflareaccess" in response.url
                or "cloudflareaccess" in body
                or "Cloudflare Access" in body):
            return (
                f"{self.base_url} is behind Cloudflare Access, which "
                "redirected the request to a sign-in page before the gateway "
                "saw the API key. A key alone cannot get through: the "
                "deployment also needs a Cloudflare Access service token, set "
                f"as CF-Access-Client-Id and CF-Access-Client-Secret in "
                f"{COMPAT_HEADERS_VAR}. Ask whoever runs the gateway for one.")
        if response.status_code in (401, 403):
            return (f"{self.base_url} rejected the API key "
                    f"({response.status_code}). {body}")
        if response.status_code == 404:
            return (f"No /chat/completions endpoint at {self._endpoint()} "
                    f"(404). Check the base URL. {body}")
        return f"{self.base_url} returned {response.status_code}: {body}"

    def _reply(self, response) -> str:
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMExtractionBackendError(
                f"{self.base_url} did not return JSON: "
                f"{(response.text or '')[:300]}") from exc
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMExtractionBackendError(
                f"Unexpected reply shape from {self.base_url}: "
                f"{json.dumps(payload)[:300]}") from exc
        content = message.get("content")
        if isinstance(content, list):
            # Some gateways return the OpenAI "parts" shape.
            content = "".join(part.get("text", "") for part in content
                              if isinstance(part, dict))
        return (content or "").strip()

    def describe(self) -> str:
        return f"{self.base_url} (model={self.model})"



class _LLMRunner:
    """Resolve a CLI backend and run a single prompt with it."""

    def __init__(self, cli: str, model: str | None = None) -> None:
        self.cli = self._resolve(cli)
        self.model = model
        self._exe: str | None = None

    @property
    def exe(self) -> str:
        if self._exe is None:
            self._exe = self._find()
        return self._exe

    # -- backend selection ------------------------------------------------

    @staticmethod
    def _resolve(cli: str) -> str:
        requested = (cli or "auto").strip().lower()
        if requested == "auto":
            for name in PREFERRED_ORDER:
                if _find_cli(name) is not None:
                    return name
            raise LLMExtractionBackendError(
                "No way to reach a model. Set ANTHROPIC_API_KEY to use the "
                "Anthropic API, point ELCAP_COMPAT_BASE_URL/"
                "ELCAP_COMPAT_API_KEY at an OpenAI-compatible gateway, or "
                "install one of "
                + ", ".join(PREFERRED_ORDER)
                + " and set ELCAP_LLM_CLI to choose it explicitly."
            )
        if requested not in BACKENDS:
            raise LLMExtractionBackendError(
                f"Unknown LLM CLI {requested!r}. Supported values: auto, "
                + ", ".join(PREFERRED_ORDER)
            )
        return requested

    def _find(self) -> str:
        exe = _find_cli(self.cli)
        if exe is None:
            info = BACKENDS[self.cli]
            raise LLMExtractionBackendError(
                f"Could not locate the {info['label']} executable. "
                + info["install_hint"]
            )
        return exe

    # -- invocation --------------------------------------------------------

    def build_command(self) -> list[str]:
        flags = []
        if self.model:
            flags = ["--model", self.model]
        if self.cli == "opencode":
            return [self.exe, "run", "--format", "json", "--dir", os.getcwd()] + flags
        if self.cli == "claude":
            return [self.exe, "-p"] + flags
        if self.cli == "codex":
            return [self.exe, "exec", "-", "--skip-git-repo-check", "--ephemeral"] + flags
        if self.cli == "gemini":
            # Non-TTY stdin is treated as a single headless prompt.
            return [self.exe] + flags
        raise LLMExtractionBackendError(f"Unhandled backend: {self.cli}")

    def run(self, prompt: str, timeout_seconds: int) -> str:
        cmd = self.build_command()
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
        reply = _output_to_reply(
            self.cli, result.stdout or "", result.stderr or "", result.returncode
        )
        return reply

    def describe(self) -> str:
        info = BACKENDS[self.cli]
        parts = [info["label"]]
        if self.model:
            parts.append(f"model={self.model}")
        parts.append(f"({self.exe})")
        return " ".join(parts)


def _output_to_reply(
    backend: str, stdout: str, stderr: str, returncode: int
) -> str:
    """Extract the assistant's text reply from a backend's process output."""
    if backend == "opencode":
        reply = _latest_message_text(_parse_ndjson_events(stdout))
        if reply:
            return reply
        if returncode != 0 and not stdout.strip():
            raise LLMExtractionBackendError(
                f"opencode exited with code {returncode}: {stderr.strip()[:400]}"
            )
        return stdout.strip()

    text = stdout.strip()
    if not text and returncode != 0:
        raise LLMExtractionBackendError(
            f"LLM CLI ({backend}) exited with code {returncode}: {stderr.strip()[:400]}"
        )
    return text


def _find_cli(name: str) -> str | None:
    """Locate a backend executable via env override, known paths, then PATH."""
    info = BACKENDS[name]
    env_path = os.environ.get(info["env_path"])
    if env_path and os.path.isfile(env_path):
        return env_path
    for cand in info["known_paths"]:
        if os.path.isfile(cand):
            return cand
    for exe_name in info["exe_names"]:
        found = _which(exe_name)
        if found:
            return found
    return None


def _which(name: str) -> str | None:
    path = os.environ.get("PATH", "")
    for directory in path.split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _parse_ndjson_events(raw: str) -> list[dict]:
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            events.append(obj)
        except json.JSONDecodeError:
            continue
    return events


def _latest_message_text(events: list[dict]) -> str:
    """Concatenate text parts belonging to the most recent assistant message."""
    last_message_id = None
    for ev in reversed(events):
        if ev.get("type") == "text":
            last_message_id = ev.get("part", {}).get("messageID")
            break
    if last_message_id is None:
        return ""
    text_parts = []
    for ev in events:
        if ev.get("type") != "text":
            continue
        part = ev.get("part", {})
        if part.get("messageID") == last_message_id:
            text_parts.append(part.get("text", ""))
    return "".join(text_parts).strip()


# --------------------------------------------------------------------------
# Public helpers used by main.py / CLI
# --------------------------------------------------------------------------

def resolve_backend(cli: str) -> str:
    """Return the backend name that would be used for the given selection."""
    requested = (cli or "auto").strip().lower()
    if requested in (API_BACKEND, COMPAT_BACKEND):
        return requested
    if requested == "auto":
        if api_key_available():
            return API_BACKEND
        if compat_configured():
            return COMPAT_BACKEND
    return _LLMRunner(requested, model=None).cli


def make_runner(cli: str, model: str | None = None):
    """Build the runner for a backend selection: API, gateway, or CLI."""
    resolved = resolve_backend(cli)
    if resolved == API_BACKEND:
        return _ApiRunner(model=model)
    if resolved == COMPAT_BACKEND:
        return _CompatRunner(model=model)
    return _LLMRunner(cli, model=model)


def available_backends() -> list[str]:
    """Backends that could actually run right now, best first."""
    ready = [API_BACKEND] if api_key_available() else []
    if compat_configured():
        ready.append(COMPAT_BACKEND)
    ready += [name for name in PREFERRED_ORDER if _find_cli(name) is not None]
    return ready


#: Statement text longer than this is truncated before being sent to the model.
MAX_PROMPT_CHARS = 250_000


def build_extract_prompt(statement_text: str,
                         max_chars: int = MAX_PROMPT_CHARS) -> str:
    """Wrap the statement text in the extraction instructions.

    Very long filings are truncated; the model is told so explicitly rather
    than being left to silently score a half-read statement.
    """
    excerpt = statement_text
    truncated = ""
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars]
        truncated = (
            f"\n\n[NOTE: the statement was truncated at {max_chars:,} characters. "
            "Use null for any figure whose section is not in the text above.]"
        )
    return (f"{EXTRACT_JSON_RULES}\n\nFINANCIAL STATEMENT TEXT:\n---\n"
            f"{excerpt}\n---{truncated}")


def run_llm_prompt(
    prompt: str,
    model: str | None = None,
    cli: str = "auto",
    timeout_seconds: int = 600,
) -> str:
    """Invoke the selected LLM CLI headlessly and return its text reply.

    Args:
        prompt: The full prompt text (sent over stdin, never as argv).
        model: Optional model id (``--model`` flag for the backend).
        cli: Backend name ('auto', 'api', 'opencode', 'claude', 'codex',
            'gemini').
        timeout_seconds: Timeout for the subprocess.

    Returns:
        The assistant's final text reply.
    """
    return make_runner(cli, model=model).run(
        prompt, timeout_seconds=timeout_seconds)


def extract_json_from_reply(reply: str) -> dict:
    """Parse the JSON object out of an LLM reply (which may contain prose).

    Robust to extra diagnostics/context that some CLIs (e.g. codex config
    summaries) prepend / append to stdout: tries a direct parse, fenced JSON
    blocks, and a raw JSON decode starting at every '{', then picks the best
    match by how many of our expected schema keys it contains.
    """
    reply = (reply or "").strip()
    if not reply:
        raise ValueError("Empty LLM reply.")

    candidates: list[dict] = []

    def try_parse(text: str) -> None:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                candidates.append(obj)
        except json.JSONDecodeError:
            pass

    # 1) Direct parse of the whole reply
    try_parse(reply)

    # 2) Fenced ```json blocks
    for m in re.finditer(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", reply, re.DOTALL):
        try_parse(m.group(1))

    # 3) Raw JSON object starting at every '{' (handles prefix/suffix prose)
    for m in re.finditer(r"\{", reply):
        sliced = reply[m.start():]
        try:
            obj, _ = json.JSONDecoder().raw_decode(sliced)
            if isinstance(obj, dict):
                candidates.append(obj)
        except json.JSONDecodeError:
            continue

    if not candidates:
        raise ValueError(f"Could not parse JSON from reply: {reply[:500]}")

    # Pick the object with the most expected schema keys; tie-break by length.
    def score(obj: dict) -> tuple[int, int]:
        return (len(SCHEMA_KEYS & set(obj.keys())), len(obj))

    candidates.sort(key=score, reverse=True)
    best = candidates[0]
    if score(best)[0] < 1:
        raise ValueError(f"Reply JSON does not match the expected schema: {reply[:500]}")
    return best


def sanitize_extraction(obj: dict) -> dict:
    """Resolve contradictions in a raw extraction object.

    A field that carries a number must not also be listed as absent; when the
    model does both, the explicit number wins.
    """
    cleaned = dict(obj)
    absent = cleaned.get("absent_line_items") or []
    if isinstance(absent, str):
        absent = [absent]
    if isinstance(absent, (list, tuple, set)):
        cleaned["absent_line_items"] = [
            name for name in absent
            if name in LINE_ITEM_FIELDS and cleaned.get(name) is None
        ]
    else:
        cleaned["absent_line_items"] = []
    return cleaned


def extract_financials(
    statement_text: str,
    model: str | None = None,
    cli: str = "auto",
    max_retries: int = 3,
    timeout_seconds: int = 600,
) -> "object":
    """Extract structured financials from statement text using an LLM CLI.

    Returns an `ExtractedFinancials` instance (see compute_metrics).
    Raises ExtractionError if all attempts fail or the result is invalid.
    """
    from compute_metrics import ExtractedFinancials

    attempts = 0
    last_error: Exception | None = None
    while attempts < max_retries:
        attempts += 1
        try:
            prompt = build_extract_prompt(statement_text)
            reply = run_llm_prompt(
                prompt, model=model, cli=cli, timeout_seconds=timeout_seconds
            )
            obj = sanitize_extraction(extract_json_from_reply(reply))
            fin = ExtractedFinancials.from_dict(obj)
            _validate_extracted(fin)
            return fin
        except LLMExtractionBackendError:
            # Configuration/install problem: retrying will not help, fail fast.
            raise
        except Exception as exc:
            last_error = exc
            if isinstance(exc, subprocess.TimeoutExpired):
                timeout_seconds = min(timeout_seconds * 2, 1800)
            if attempts < max_retries:
                time.sleep(3)
    raise ExtractionError(
        f"Financial extraction failed after {max_retries} attempts "
        f"(cli={cli}, model={model}): {last_error}"
    ) from last_error


#: Relative difference above which two independent reads are said to disagree.
DISAGREEMENT_TOLERANCE = 0.005


def compare_extractions(first, second) -> list[str]:
    """Describe where two independent extractions of the same statement differ.

    Both are ``ExtractedFinancials`` in full dollars, so the comparison is
    unit-safe.

    Two kinds of difference, reported in that order because they need
    different responses. A FIGURE difference means one of the passes misread
    the statement and the number in the workbook may be wrong. A COVERAGE
    difference means the passes agree on the arithmetic but not on whether an
    item exists, which changes only whether a metric is scored or left blank.
    """
    from compute_metrics import MONETARY_FIELDS

    figures: list[str] = []
    coverage: list[str] = []
    for name in MONETARY_FIELDS:
        a, b = getattr(first, name), getattr(second, name)
        if a is None and b is None:
            continue
        label = name.replace("_", " ")
        if a is None or b is None:
            found, missing = (first, second) if b is None else (second, first)
            absent = name in found.absent_line_items
            coverage.append(
                f"COVERAGE {label}: one pass "
                + ("judged it absent (0)" if absent
                   else f"read {_fmt_opt(getattr(found, name))}")
                + ", the other could not determine it"
            )
            continue
        scale = max(abs(a), abs(b), 1.0)
        if abs(a - b) / scale > DISAGREEMENT_TOLERANCE:
            figures.append(f"FIGURE {label}: {a:,.0f} vs {b:,.0f}")
    return figures + coverage


def extract_financials_verified(
    statement_text: str,
    model: str | None = None,
    cli: str = "auto",
    timeout_seconds: int = 600,
) -> tuple["object", list[str]]:
    """Extract twice and report line items the two passes disagree on.

    Reading a statement is the only non-deterministic step in the pipeline, so
    a second independent pass is the cheapest available check on it. The first
    pass is returned; the caller decides what to do with the disagreements.
    """
    first = extract_financials(statement_text, model=model, cli=cli,
                               timeout_seconds=timeout_seconds)
    second = extract_financials(statement_text, model=model, cli=cli,
                                timeout_seconds=timeout_seconds)
    return first, compare_extractions(first, second)


def _fmt_opt(value) -> str:
    return "(none)" if value is None else f"{value:,.0f}"


def _validate_extracted(fin) -> None:
    """Basic sanity validation of an extraction result."""
    numeric_fields = [
        "total_revenue", "net_income", "current_assets", "current_liabilities",
        "total_liabilities", "total_equity", "operating_cash_flow",
    ]
    if all(getattr(fin, f) is None for f in numeric_fields):
        raise ValueError("Extraction result contains no financial figures.")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    from extract_pdf_text import extract_pdf_text

    pdf = sys.argv[1] if len(sys.argv) > 1 else None
    if not pdf:
        print("Usage: llm_extract.py <pdf_path> [--cli backend] [--model model]")
        sys.exit(1)
    cli_arg = os.environ.get("ELCAP_LLM_CLI", "auto")
    model_arg = os.environ.get("ELCAP_MODEL")
    text = extract_pdf_text(pdf)
    prompt = build_extract_prompt(text)
    print(f"Prompt length: {len(prompt)} chars", file=sys.stderr)
    print(f"Using CLI: {resolve_backend(cli_arg)} (ELCAP_LLM_CLI={cli_arg})", file=sys.stderr)
    fin = extract_financials(text, model=model_arg, cli=cli_arg)
    print(fin)
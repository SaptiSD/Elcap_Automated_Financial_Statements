"""Parsing of LLM replies -- no CLI is invoked."""

import json

import pytest

from compute_metrics import ExtractedFinancials
from llm_extract import (BACKENDS, PREFERRED_ORDER, build_extract_prompt,
                         compare_extractions, extract_json_from_reply,
                         sanitize_extraction)

GOOD = {
    "company_name": "Acme Co",
    "fiscal_year": "2024",
    "unit_scale": "thousands",
    "total_revenue": 45000,
    "net_income": 1200,
}


def test_plain_json_reply():
    assert extract_json_from_reply(json.dumps(GOOD))["total_revenue"] == 45000


def test_fenced_json_reply():
    reply = f"Here you go:\n```json\n{json.dumps(GOOD)}\n```\nHope that helps."
    assert extract_json_from_reply(reply)["company_name"] == "Acme Co"


def test_json_with_leading_cli_chatter():
    """Some CLIs print a config banner before the answer."""
    reply = f"workdir: /tmp\nmodel: x\n--------\n{json.dumps(GOOD)}"
    assert extract_json_from_reply(reply)["fiscal_year"] == "2024"


def test_best_schema_match_wins_over_other_objects():
    reply = f'{{"status": "ok"}}\n{json.dumps(GOOD)}'
    assert extract_json_from_reply(reply)["total_revenue"] == 45000


def test_empty_reply_raises():
    with pytest.raises(ValueError):
        extract_json_from_reply("   ")


def test_unparseable_reply_raises():
    with pytest.raises(ValueError):
        extract_json_from_reply("I could not read that statement, sorry.")


def test_offschema_json_raises():
    with pytest.raises(ValueError):
        extract_json_from_reply('{"foo": 1, "bar": 2}')


# --------------------------------------------------------------------------
# absent_line_items hygiene
# --------------------------------------------------------------------------

def test_sanitize_drops_absent_flags_that_have_values():
    """A field cannot be both 'not on the statement' and a number."""
    cleaned = sanitize_extraction(
        {"long_term_debt": 500, "absent_line_items": ["long_term_debt", "taxes"]})
    assert cleaned["absent_line_items"] == ["taxes"]


def test_sanitize_drops_unknown_field_names():
    cleaned = sanitize_extraction({"absent_line_items": ["ebitda", "taxes"]})
    assert cleaned["absent_line_items"] == ["taxes"]


def test_sanitize_handles_a_bare_string():
    assert sanitize_extraction({"absent_line_items": "taxes"})[
        "absent_line_items"] == ["taxes"]


def test_sanitize_survives_a_bad_type():
    assert sanitize_extraction({"absent_line_items": 7})["absent_line_items"] == []


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------

def test_prompt_carries_the_statement_and_the_unit_rule():
    prompt = build_extract_prompt("Total revenue 707,868")
    assert "Total revenue 707,868" in prompt
    assert "NEVER 2843000" in prompt


def test_long_statements_are_truncated_and_flagged():
    prompt = build_extract_prompt("STATEMENT" * 500, max_chars=1000)
    assert "truncated at 1,000 characters" in prompt
    body = prompt.split("---")[1]
    assert len(body.strip()) == 1000


# --------------------------------------------------------------------------
# Two-pass comparison
# --------------------------------------------------------------------------

def make(**kwargs):
    return ExtractedFinancials.from_dict({"unit_scale": "raw", **kwargs})


def test_identical_passes_agree():
    assert compare_extractions(make(total_revenue=100),
                               make(total_revenue=100)) == []


def test_small_differences_are_tolerated():
    assert compare_extractions(make(total_revenue=1_000_000),
                               make(total_revenue=1_000_100)) == []


def test_material_differences_are_reported():
    diffs = compare_extractions(make(total_revenue=1_000_000),
                                make(total_revenue=1_500_000))
    assert len(diffs) == 1 and diffs[0].startswith("FIGURE total revenue")


def test_a_value_found_by_only_one_pass_is_a_coverage_difference():
    diffs = compare_extractions(make(long_term_debt=500), make())
    assert diffs[0].startswith("COVERAGE long term debt")
    assert "read 500" in diffs[0]


def test_absent_versus_unknown_is_described_as_such():
    found = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "absent_line_items": ["long_term_debt"]})
    diffs = compare_extractions(found, make())
    assert "judged it absent (0)" in diffs[0]


def test_figure_differences_rank_above_coverage_ones():
    """The misread number matters more than an unscored metric."""
    a = make(total_revenue=1_000_000, long_term_debt=500)
    b = make(total_revenue=1_500_000)
    diffs = compare_extractions(a, b)
    assert diffs[0].startswith("FIGURE") and diffs[1].startswith("COVERAGE")


def test_comparison_is_unit_safe():
    """The same statement read as thousands and as dollars must agree."""
    thousands = ExtractedFinancials.from_dict(
        {"unit_scale": "thousands", "total_revenue": 45_000})
    dollars = ExtractedFinancials.from_dict(
        {"unit_scale": "raw", "total_revenue": 45_000_000})
    assert compare_extractions(thousands, dollars) == []


# --------------------------------------------------------------------------
# Backend registry
# --------------------------------------------------------------------------

def test_every_preferred_backend_is_registered():
    assert set(PREFERRED_ORDER) == set(BACKENDS)


@pytest.mark.parametrize("name", PREFERRED_ORDER)
def test_backend_entries_are_complete(name):
    info = BACKENDS[name]
    assert info["exe_names"] and info["env_path"] and info["install_hint"]


# --------------------------------------------------------------------------
# Per-backend invocation and reply parsing
#
# Only opencode is installed here, so these pin the two things that differ
# between backends without needing any of them present: the argv we build and
# how we pull the assistant's reply out of that CLI's stdout. Running a real
# claude / codex / gemini binary end to end is still untested.
# --------------------------------------------------------------------------

from llm_extract import (LLMExtractionBackendError, _LLMRunner,
                         _latest_message_text, _output_to_reply,
                         _parse_ndjson_events, resolve_backend)


def runner_for(cli, model=None, exe="STUB"):
    r = _LLMRunner(cli, model=model)
    r._exe = exe          # skip the PATH lookup; the binary need not exist
    return r


@pytest.mark.parametrize("cli,expected_tail", [
    ("opencode", ["run", "--format", "json"]),
    ("claude", ["-p"]),
    ("codex", ["exec", "-", "--skip-git-repo-check", "--ephemeral"]),
    ("gemini", []),
])
def test_each_backend_builds_its_own_command(cli, expected_tail):
    cmd = runner_for(cli).build_command()
    assert cmd[0] == "STUB"
    for token in expected_tail:
        assert token in cmd


@pytest.mark.parametrize("cli", PREFERRED_ORDER)
def test_model_flag_is_passed_to_every_backend(cli):
    assert runner_for(cli, model="some-model").build_command()[-2:] == [
        "--model", "some-model"]


@pytest.mark.parametrize("cli", PREFERRED_ORDER)
def test_the_prompt_is_never_put_on_the_command_line(cli):
    """Windows caps argv at ~32KB; statements are far longer, so the prompt
    only ever travels over stdin."""
    cmd = runner_for(cli).build_command()
    assert not any("FINANCIAL STATEMENT TEXT" in part for part in cmd)


def test_selecting_a_backend_does_not_require_it_to_be_installed():
    assert resolve_backend("codex") == "codex"


def test_an_unknown_backend_name_is_rejected():
    with pytest.raises(LLMExtractionBackendError, match="Unknown LLM CLI"):
        resolve_backend("gpt5-cli")


# -- reply extraction ------------------------------------------------------

def test_opencode_reply_is_assembled_from_ndjson_events():
    events = "\n".join(json.dumps(e) for e in [
        {"type": "text", "part": {"messageID": "m1", "text": "stale"}},
        {"type": "text", "part": {"messageID": "m2", "text": '{"total_'}},
        {"type": "text", "part": {"messageID": "m2", "text": 'revenue": 1}'}},
    ])
    assert _output_to_reply("opencode", events, "", 0) == '{"total_revenue": 1}'


def test_opencode_falls_back_to_raw_stdout_when_not_ndjson():
    assert _output_to_reply("opencode", '{"total_revenue": 1}', "", 0) == \
        '{"total_revenue": 1}'


def test_opencode_noise_lines_are_skipped():
    raw = 'not json at all\n{"type":"text","part":{"messageID":"m","text":"ok"}}'
    assert _latest_message_text(_parse_ndjson_events(raw)) == "ok"


@pytest.mark.parametrize("cli", ["claude", "codex", "gemini"])
def test_plain_stdout_backends_return_stdout(cli):
    assert _output_to_reply(cli, "  {\"a\": 1}  ", "", 0) == '{"a": 1}'


@pytest.mark.parametrize("cli", PREFERRED_ORDER)
def test_a_failed_launch_reports_the_backend_and_its_stderr(cli):
    with pytest.raises(LLMExtractionBackendError, match="not logged in"):
        _output_to_reply(cli, "", "not logged in", 1)


@pytest.mark.parametrize("cli", ["claude", "codex", "gemini"])
def test_output_still_wins_over_a_nonzero_exit(cli):
    """A CLI that prints the answer then exits non-zero (deprecation notice,
    telemetry failure) must not lose the answer."""
    assert _output_to_reply(cli, '{"a": 1}', "warning", 1) == '{"a": 1}'


# --------------------------------------------------------------------------
# The OpenAI-compatible gateway backend
#
# No gateway is reachable from a test run, so these pin the parts that decide
# whether a real one works: the URL we POST to, the headers we send, and how
# we read the two reply shapes and the failures. The Cloudflare Access case
# has its own test because that failure arrives as a 200 with a sign-in page,
# which every generic error message reports as "not valid JSON".
# --------------------------------------------------------------------------

from llm_extract import (COMPAT_BASE_URL_VAR, COMPAT_HEADERS_VAR,  # noqa: E402
                         COMPAT_KEY_VAR, COMPAT_MODEL_VAR, _CompatRunner,
                         compat_configured)


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv(COMPAT_BASE_URL_VAR, "https://gw.example.edu/api")
    monkeypatch.setenv(COMPAT_KEY_VAR, "sk-test")
    monkeypatch.setenv(COMPAT_MODEL_VAR, "claude-sonnet-5")
    monkeypatch.delenv(COMPAT_HEADERS_VAR, raising=False)
    return _CompatRunner()


class FakeResponse:
    def __init__(self, status=200, payload=None, text="", url=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.url = url or "https://gw.example.edu/api/chat/completions"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_a_gateway_needs_both_a_url_and_a_key(monkeypatch):
    monkeypatch.delenv(COMPAT_BASE_URL_VAR, raising=False)
    monkeypatch.delenv(COMPAT_KEY_VAR, raising=False)
    assert not compat_configured()
    monkeypatch.setenv(COMPAT_BASE_URL_VAR, "https://gw.example.edu/api")
    assert not compat_configured()
    monkeypatch.setenv(COMPAT_KEY_VAR, "sk-test")
    assert compat_configured()


def test_the_completions_path_is_added_once(gateway, monkeypatch):
    assert gateway._endpoint() == "https://gw.example.edu/api/chat/completions"
    monkeypatch.setenv(COMPAT_BASE_URL_VAR,
                       "https://gw.example.edu/api/chat/completions")
    assert _CompatRunner()._endpoint() == \
        "https://gw.example.edu/api/chat/completions"


def test_extra_headers_are_merged_in(gateway, monkeypatch):
    monkeypatch.setenv(COMPAT_HEADERS_VAR,
                       '{"CF-Access-Client-Id": "abc", "CF-Access-Client-Secret": "xyz"}')
    headers = _CompatRunner()._headers()
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["CF-Access-Client-Id"] == "abc"
    assert headers["CF-Access-Client-Secret"] == "xyz"


def test_unparseable_extra_headers_are_reported(gateway, monkeypatch):
    monkeypatch.setenv(COMPAT_HEADERS_VAR, "CF-Access-Client-Id: abc")
    with pytest.raises(LLMExtractionBackendError, match="not valid JSON"):
        _CompatRunner()._headers()


def test_a_missing_model_is_refused_before_the_request(gateway, monkeypatch):
    monkeypatch.delenv(COMPAT_MODEL_VAR, raising=False)
    with pytest.raises(LLMExtractionBackendError, match="needs a model"):
        _CompatRunner().run("prompt", timeout_seconds=5)


def test_the_reply_is_read_from_the_first_choice(gateway):
    response = FakeResponse(payload={
        "choices": [{"message": {"content": '  {"total_revenue": 1}  '}}]})
    assert gateway._reply(response) == '{"total_revenue": 1}'


def test_a_parts_shaped_reply_is_joined(gateway):
    response = FakeResponse(payload={"choices": [{"message": {"content": [
        {"type": "text", "text": '{"total_'},
        {"type": "text", "text": 'revenue": 1}'},
    ]}}]})
    assert gateway._reply(response) == '{"total_revenue": 1}'


def test_an_unexpected_reply_shape_says_so(gateway):
    with pytest.raises(LLMExtractionBackendError, match="Unexpected reply shape"):
        gateway._reply(FakeResponse(payload={"error": "no model"}))


def test_a_cloudflare_access_bounce_is_named(gateway):
    """Access answers an unauthenticated call with its sign-in page and a 200,
    so the key is never even looked at. Saying 'not valid JSON' would send the
    user hunting for a bug in their key."""
    response = FakeResponse(
        status=200, text="<title>Sign in - Cloudflare Access</title>",
        url="https://example.cloudflareaccess.com/cdn-cgi/access/login/gw")
    message = gateway._explain(response)
    assert "Cloudflare Access" in message
    assert "service token" in message
    assert COMPAT_HEADERS_VAR in message


def test_a_rejected_key_and_a_wrong_url_are_told_apart(gateway):
    assert "rejected the API key" in gateway._explain(FakeResponse(status=401))
    assert "Check the base URL" in gateway._explain(FakeResponse(status=404))


# --------------------------------------------------------------------------
# A key from the wrong place
#
# The commonest deployment mistake is a gateway key in ANTHROPIC_API_KEY. The
# API answers that with a 401 that reads exactly like a revoked key, and the
# retry loop treats a failure as transient, so the report arrives three
# attempts later and points at the wrong thing.
# --------------------------------------------------------------------------

from llm_extract import (_ApiRunner, _is_auth_failure,  # noqa: E402
                         looks_like_an_anthropic_key)


class FakeAuthError(Exception):
    status_code = 401


def test_an_anthropic_key_is_told_from_a_gateway_key():
    assert looks_like_an_anthropic_key("sk-ant-api03-abc")
    assert not looks_like_an_anthropic_key("sk-3bd31311dfc3461a9fbf4fd7")
    assert looks_like_an_anthropic_key("")        # unset is not "wrong"


def test_auth_failures_are_recognised_by_status_and_by_name():
    assert _is_auth_failure(FakeAuthError())
    assert _is_auth_failure(type("AuthenticationError", (Exception,), {})())
    assert _is_auth_failure(type("PermissionDeniedError", (Exception,), {})())
    assert not _is_auth_failure(ValueError("nope"))


def test_a_foreign_key_is_named_as_the_cause(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-3bd31311dfc3461a9fbf4fd7")
    message = _ApiRunner._explain_auth()
    assert "not an Anthropic key" in message
    assert COMPAT_KEY_VAR in message


def test_a_real_looking_key_gets_the_ordinary_explanation(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-abc")
    message = _ApiRunner._explain_auth()
    assert "not an Anthropic key" not in message
    assert "console.anthropic.com" in message


def test_a_rejected_key_is_not_retried(monkeypatch):
    """LLMExtractionBackendError is the 'do not retry' signal, so the
    conversion has to happen inside the runner, not at the call site."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-abc")
    runner = _ApiRunner(model="claude-opus-5")
    monkeypatch.setattr(_ApiRunner, "_client", staticmethod(lambda _t: object()))
    def boom(*_args, **_kwargs):
        raise FakeAuthError("API key is invalid.")
    monkeypatch.setattr(_ApiRunner, "_send", staticmethod(boom))
    with pytest.raises(LLMExtractionBackendError, match="rejected the API key"):
        runner.run("prompt", timeout_seconds=5)

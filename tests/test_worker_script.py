"""Tests for the PC worker in worker/. No SDK, browser or network: fake query and fake HTTP."""

import asyncio
import json
import sys

import pytest

from worker import agent, api as api_mod, gate as gate_mod, worker as W


# ---- fake SDK messages (matched by class name, like the real ones) ----

class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


class ToolResultBlock:
    def __init__(self, tool_use_id, content):
        self.tool_use_id, self.content, self.is_error = tool_use_id, content, False


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class UserMessage(AssistantMessage):
    pass


class ResultMessage:
    def __init__(self, result, is_error=False):
        self.result, self.is_error = result, is_error


def make_query(replies, prompts=None, options_seen=None):
    """replies: list of lists of messages, one list per session."""
    it = iter(replies)

    async def query(*, prompt, options=None):
        if prompts is not None:
            prompts.append(prompt)
        if options_seen is not None:
            options_seen.append(options)
        for msg in next(it):
            yield msg

    return query


def reply_json(**kw):
    base = {"status": "filled", "reason_code": "none", "reason": "", "final_url": "https://careers.example.com/apply",
            "fields_filled": [{"label": "Name", "value": "Sean"}], "unanswered_required": [], "cv_uploaded": True}
    base.update(kw)
    return ResultMessage(json.dumps(base))


def snapshot_msgs(url="https://careers.example.com/apply", n=1):
    return [
        AssistantMessage([ToolUseBlock(f"t{n}", "mcp__playwright__browser_snapshot", {})]),
        UserMessage([ToolResultBlock(f"t{n}", f"- Page URL: {url}\n- textbox \"Name\" [ref=e2]\n")]),
    ]


# ---- fake HTTP ----

class FakeServer:
    def __init__(self, claims, answers):
        self.claims = list(claims)
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("/api/worker", 1)[1]
        self.calls.append((method, path, headers, body))
        if path == "/claim":
            if not self.claims:
                return 204, b""
            return 200, json.dumps(self.claims.pop(0)).encode()
        if path == "/cv":
            return 200, b"%PDF-fake"
        if path.endswith("/heartbeat"):
            return 200, b'{"lease_until": "x"}'
        if path.endswith("/attempts"):
            return 200, json.dumps(self.answers.pop(0)).encode()
        if path.endswith("/release"):
            return 200, b"{}"
        return 404, b""

    def paths(self, suffix):
        return [c for c in self.calls if c[1].endswith(suffix)]


def make_claim(channels=1, submit_allowed=False, phase="fill"):
    return {
        "job_id": 7, "application_id": 3, "lease_token": "lease-abc", "lease_until": "x",
        "phase": phase, "submit_allowed": submit_allowed,
        "job": {"title": "Pentester", "company": "Acme", "company_tldr": "t", "description": "d",
                "source": "linkedin", "url": "https://x.example/j"},
        "channels": [{"index": i, "kind": "company_job" if i == 0 else "job_url",
                      "target": f"https://careers.example.com/{i}"} for i in range(channels)],
        "answers": {"full_name": "Sean Cohen", "years_experience": "0 to 1 years"},
        "cover_letter": "Dear team",
        "cv": {"filename": "cv.pdf", "size": 9},
    }


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "server": "https://srv.example", "token": "SECRET-TOKEN", "user_data_dir": str(tmp_path / "prof"),
        "browser": "msedge", "executable_path": None, "mcp_package": "@playwright/mcp@0.0.84",
        "model": "sonnet", "max_jobs": 10, "max_minutes": 300,
    }))
    return path


def png_in(tmp_path, name="filled.png"):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG-bytes-" + name.encode())
    return p


# ---- main flow ----

def test_claim_204_runs_no_session(cfgfile):
    server = FakeServer([], [])
    q = make_query([])
    assert W.main(["--config", str(cfgfile), "--once"], query=q, transport=server) == 0
    assert [c[1] for c in server.calls] == ["/claim"]


def test_two_channels_captcha_then_filled(cfgfile, tmp_path):
    shot = png_in(tmp_path)
    server = FakeServer([make_claim(channels=2)], [{"next_index": 1}, {"done": True, "status": "ready_to_submit"}])
    shot_msgs = [
        AssistantMessage([ToolUseBlock("s", "mcp__playwright__browser_take_screenshot", {"filename": "filled.png"})]),
        UserMessage([ToolResultBlock("s", f"- [Screenshot of viewport]({shot})")]),
    ]
    q = make_query([
        [reply_json(status="needs_help", reason_code="captcha", reason="captcha")],
        shot_msgs + [reply_json()],
    ])
    assert W.main(["--config", str(cfgfile), "--once"], query=q, transport=server) == 0
    posts = server.paths("/attempts")
    assert len(posts) == 2
    first = json.dumps(posts[0][3][:3000].decode("latin1"))
    assert 'name=\\"lease_token\\"' in first
    body2 = posts[1][3]
    text2 = body2.decode("latin1")
    assert '"index": 1' in text2
    assert "lease-abc" in text2
    assert b"\x89PNG-bytes-filled.png" in body2
    assert '"status": "filled"' in text2


def test_max_jobs_one_claims_once(cfgfile):
    server = FakeServer([make_claim(), make_claim()], [{"done": True}, {"done": True}])
    q = make_query([[reply_json()], [reply_json()]])
    W.main(["--config", str(cfgfile), "--max-jobs", "1"], query=q, transport=server)
    assert len(server.paths("/claim")) == 1


def test_snapshot_url_overrides_final_url(cfgfile):
    server = FakeServer([make_claim()], [{"done": True}])
    q = make_query([snapshot_msgs("https://real.example.org/form") + [reply_json()]])
    W.main(["--config", str(cfgfile)], query=q, transport=server)
    text = server.paths("/attempts")[0][3].decode("latin1")
    assert '"final_url": "https://real.example.org/form"' in text


def test_submitted_without_screenshot_is_needs_help(cfgfile):
    server = FakeServer([make_claim(submit_allowed=True)], [{"done": True}])
    q = make_query([[reply_json(status="submitted")]])
    W.main(["--config", str(cfgfile)], query=q, transport=server)
    text = server.paths("/attempts")[0][3].decode("latin1")
    assert '"status": "needs_help"' in text
    assert "without a screenshot" in text


def test_usage_limit_releases_and_stops(cfgfile):
    server = FakeServer([make_claim(), make_claim()], [])
    q = make_query([[ResultMessage("Claude usage limit reached|1760000000", is_error=True)]])
    assert W.main(["--config", str(cfgfile)], query=q, transport=server) == 0
    assert len(server.paths("/release")) == 1
    assert len(server.paths("/claim")) == 1
    assert not server.paths("/attempts")


def test_prompts_and_no_token(cfgfile):
    prompts = []
    server = FakeServer([make_claim()], [{"done": True}])
    q = make_query([[reply_json()]], prompts)
    W.main(["--config", str(cfgfile)], query=q, transport=server)
    p = prompts[0]
    assert "stop before the final submit" in p
    for value in ("Sean Cohen", "0 to 1 years"):
        assert value in p
    assert "cv.pdf" in p and "SECRET-TOKEN" not in p and "lease-abc" not in p


def test_submit_phase_prompt_and_gate(cfgfile):
    prompts = []
    server = FakeServer([make_claim(submit_allowed=False, phase="submit")], [{"done": True}])
    q = make_query([[reply_json()]], prompts)
    W.main(["--config", str(cfgfile)], query=q, transport=server)
    assert "click the final submit control once" in prompts[0]
    assert gate_mod.Gate(True).submit_allowed


# ---- config and mcp ----

def test_latest_refused(cfgfile):
    data = json.loads(cfgfile.read_text())
    data["mcp_package"] = "@playwright/mcp@latest"
    cfgfile.write_text(json.dumps(data))
    with pytest.raises(SystemExit):
        W.load_config(cfgfile)


def test_mcp_config_windows_and_browser(cfgfile, monkeypatch):
    cfg = W.load_config(cfgfile)
    monkeypatch.setattr(sys, "platform", "win32")
    entry = agent.mcp_config(cfg, "C:/out")["playwright"]
    assert entry["command"] == "cmd" and entry["args"][:3] == ["/c", "npx", "-y"]
    assert "@playwright/mcp@0.0.84" in entry["args"]
    i = entry["args"].index("--browser")
    assert entry["args"][i + 1] == "msedge"
    assert entry["args"][entry["args"].index("--user-data-dir") + 1] == cfg.user_data_dir
    monkeypatch.setattr(sys, "platform", "linux")
    assert agent.mcp_config(cfg, "o")["playwright"]["command"] == "npx"
    cfg.executable_path = "C:/brave.exe"
    args = agent.mcp_config(cfg, "o")["playwright"]["args"]
    assert "--executable-path" in args and "--browser" not in args and "--user-data-dir" in args


def test_extract_json_fenced():
    assert agent.extract_json('Here:\n```json\n{"a": 1}\n```\n') == {"a": 1}


def test_smoke_ok_and_gate_denies_other_tools(cfgfile, capsys):
    q = make_query([[ResultMessage("Example Domain")]])
    assert W.main(["--config", str(cfgfile), "--smoke"], query=q) == 0
    assert "tool calls:" in capsys.readouterr().out
    g = gate_mod.Gate(False, tools=W.SMOKE_TOOLS)
    assert isinstance(g.decide("browser_click", {"ref": "e1"}), gate_mod.Deny)


# ---- gate ----

SNAP = '- button "Submit application" [ref=e9]\n- button "Next" [ref=e4]\n- button "Easy Apply" [ref=e3]\n'


def observed(allowed, url="https://careers.example.com/apply", snap=SNAP):
    g = gate_mod.Gate(allowed)
    g.observe("mcp__playwright__browser_snapshot", {}, f"- Page URL: {url}\n{snap}")
    return g


def test_gate_final_button():
    assert isinstance(observed(False).decide("browser_click", {"ref": "e9"}), gate_mod.Deny)
    assert isinstance(observed(True).decide("browser_click", {"ref": "e9"}), gate_mod.Allow)


def test_gate_unknown_ref_denied_both_modes():
    for allowed in (False, True):
        assert isinstance(observed(allowed).decide("browser_click", {"ref": "e99"}), gate_mod.Deny)


def test_gate_easy_apply_on_linkedin_denied_both_modes():
    for allowed in (False, True):
        g = observed(allowed, url="https://www.linkedin.com/jobs/view/1")
        assert isinstance(g.decide("mcp__playwright__browser_click", {"ref": "e3"}), gate_mod.Deny)


def test_gate_next_allowed_and_enter():
    g = observed(False)
    assert isinstance(g.decide("browser_click", {"ref": "e4"}), gate_mod.Allow)
    assert isinstance(g.decide("browser_press_key", {"key": "Enter"}), gate_mod.Deny)
    assert isinstance(g.decide("browser_press_key", {"key": "Tab"}), gate_mod.Allow)
    assert isinstance(observed(True).decide("browser_press_key", {"key": "Enter"}), gate_mod.Allow)


def test_gate_budget_and_denied_count():
    g = observed(True)
    for _ in range(45):
        g.decide("browser_snapshot", {})
    assert g.denied == 0
    d = g.decide("browser_snapshot", {})
    assert isinstance(d, gate_mod.Deny) and "budget" in d.reason
    assert g.denied == 1 and g.tool_calls == 46


def test_gate_denies_unlisted_tools_and_bad_urls():
    g = gate_mod.Gate(True)
    assert isinstance(g.decide("browser_evaluate", {}), gate_mod.Deny)
    assert isinstance(g.decide("browser_navigate", {"url": "file:///etc/passwd"}), gate_mod.Deny)
    assert isinstance(g.decide("browser_navigate", {"url": "https://a.example"}), gate_mod.Allow)


def test_api_multipart_and_transport_errors():
    seen = {}

    def transport(method, url, headers, body):
        seen.update(method=method, url=url, headers=headers, body=body)
        return 409, b"bad lease"

    client = api_mod.Api("https://srv.example/", "tok", transport)
    with pytest.raises(api_mod.ApiError) as err:
        client.heartbeat(1, "x")
    assert err.value.status == 409
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["url"] == "https://srv.example/api/worker/jobs/1/heartbeat"


def test_run_session_timeout(cfgfile):
    async def slow(*, prompt, options=None):
        await asyncio.sleep(5)
        yield ResultMessage("x")

    with pytest.raises(agent.AgentError):
        agent.run_session("p", gate=gate_mod.Gate(False), mcp_config={}, cwd=".", model="m",
                          timeout_s=0.05, query=slow)


# ---- review fixes: every tool is gated ----

def test_nothing_preapproved_so_every_call_reaches_the_gate(cfgfile):
    seen = []
    agent.run_session("p", gate=gate_mod.Gate(False), mcp_config={}, cwd=".", model="m",
                      timeout_s=5, query=make_query([[ResultMessage("{}")]], options_seen=seen))
    assert seen[0].allowed_tools == []
    assert callable(seen[0].can_use_tool)


def test_gate_counts_and_denies_former_preapproved_tools():
    g = gate_mod.Gate(False, max_tool_calls=3)
    assert isinstance(g.decide("mcp__playwright__browser_type", {"ref": "e2", "text": "x", "submit": True}), gate_mod.Deny)
    assert isinstance(g.decide("mcp__playwright__browser_navigate", {"url": "file:///C:/Users"}), gate_mod.Deny)
    assert isinstance(g.decide("mcp__playwright__browser_snapshot", {}), gate_mod.Allow)
    assert isinstance(g.decide("mcp__playwright__browser_snapshot", {}), gate_mod.Deny)
    assert g.tool_calls == 4


def test_gate_upload_only_claim_paths(tmp_path):
    cv = tmp_path / "cv" / "cv.pdf"
    cover = tmp_path / "cover-letters" / "7.txt"
    g = gate_mod.Gate(False, upload_paths=[cv, cover])
    up = "mcp__playwright__browser_file_upload"
    assert isinstance(g.decide(up, {"paths": [str(cv)]}), gate_mod.Allow)
    assert isinstance(g.decide(up, {"paths": [str(tmp_path / "cv" / ".." / "cv" / "cv.pdf")]}), gate_mod.Allow)
    assert isinstance(g.decide(up, {"paths": [str(cover)]}), gate_mod.Allow)
    assert isinstance(g.decide(up, {"paths": [str(cv), "/etc/passwd"]}), gate_mod.Deny)
    assert isinstance(g.decide(up, {"paths": [str(tmp_path / "config.json")]}), gate_mod.Deny)
    assert isinstance(gate_mod.Gate(False).decide(up, {"paths": [str(cv)]}), gate_mod.Deny)


def test_process_job_gate_gets_cv_and_cover_paths(cfgfile, monkeypatch):
    made = []
    real = gate_mod.Gate

    def spy(*args, **kwargs):
        made.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(W.gate_mod, "Gate", spy)
    server = FakeServer([make_claim()], [{"done": True}])
    W.main(["--config", str(cfgfile), "--once"], query=make_query([[reply_json()]]), transport=server)
    names = sorted(p.name for p in map(__import__("pathlib").Path, made[0]["upload_paths"]))
    assert names == ["7.txt", "cv.pdf"]


FORM_SNAP = (
    '- button "Submit →" [ref=s1]\n- button "Send application!" [ref=s2]\n'
    '- button "Apply for this job" [ref=s3]\n- button "שלחו" [ref=s4]\n'
    '- button "הגשת מועמדות" [ref=s5]\n- button "SUBMIT  APPLICATION ›" [ref=s6]\n'
    '- button "Next" [ref=s7]\n'
)


def test_gate_final_labels_by_prefix():
    g = observed(False, snap=FORM_SNAP)
    for ref in ("s1", "s2", "s4", "s6"):
        assert isinstance(g.decide("browser_click", {"ref": ref}), gate_mod.Deny), ref
    assert isinstance(g.decide("browser_click", {"ref": "s7"}), gate_mod.Allow)
    # On a listing (few text boxes, nothing uploaded) Apply opens the form.
    assert isinstance(g.decide("browser_click", {"ref": "s3"}), gate_mod.Allow)
    assert isinstance(g.decide("browser_click", {"ref": "s5"}), gate_mod.Allow)
    assert gate_mod.normalize_label("  Submit   Application -> ") == "submit application"


def test_gate_apply_denied_once_cv_uploaded(tmp_path):
    cv = tmp_path / "cv.pdf"
    g = gate_mod.Gate(False, upload_paths=[cv])
    g.observe("browser_snapshot", {}, "- Page URL: https://careers.example.com/a\n" + FORM_SNAP)
    assert isinstance(g.decide("browser_file_upload", {"paths": [str(cv)]}), gate_mod.Allow)
    for ref in ("s3", "s5"):
        assert isinstance(g.decide("browser_click", {"ref": ref}), gate_mod.Deny), ref
    assert isinstance(observed(True, snap=FORM_SNAP).decide("browser_click", {"ref": "s3"}), gate_mod.Allow)


def test_gate_denies_enter_and_space_in_fill_sessions():
    g = gate_mod.Gate(False)
    for key in ("Enter", "Space", " ", "Control+Enter", "NumpadEnter"):
        assert isinstance(g.decide("browser_press_key", {"key": key}), gate_mod.Deny), key
    assert isinstance(g.decide("browser_press_key", {"key": "ArrowDown"}), gate_mod.Allow)
    assert isinstance(gate_mod.Gate(True).decide("browser_press_key", {"key": "Space"}), gate_mod.Allow)


def test_api_error_on_one_job_does_not_end_the_run(cfgfile):
    server = FakeServer([make_claim(), dict(make_claim(), job_id=8)], [{"done": True}])
    real = server.__call__
    failed = []

    def flaky(method, url, headers, body):
        if url.endswith("/jobs/7/attempts"):
            failed.append(url)
            return 500, b"boom"
        return real(method, url, headers, body)

    q = make_query([[reply_json()], [reply_json()]])
    assert W.main(["--config", str(cfgfile), "--once"], query=q, transport=flaky) == 0
    assert failed and any(c[1] == "/jobs/7/release" for c in server.calls)
    assert any(c[1] == "/jobs/8/attempts" for c in server.calls)


def test_network_error_on_claim_stops_once_run(cfgfile):
    def down(method, url, headers, body):
        raise OSError("connection refused")

    assert W.main(["--config", str(cfgfile), "--once"], query=make_query([]), transport=down) == 1

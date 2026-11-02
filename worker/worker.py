"""Auto-job PC worker: claims queued jobs from the server and applies in a real browser.

Run from this folder: py -3 worker.py --smoke | --once | --loop
"""

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from . import agent, api as api_mod, gate as gate_mod
except ImportError:  # run as a script from the worker folder
    import agent
    import api as api_mod
    import gate as gate_mod

log = logging.getLogger("autojob-worker")

HEARTBEAT_SECONDS = 120
PER_JOB_SECONDS = 25 * 60
FILL_TIMEOUT_S = 600
SUBMIT_TIMEOUT_S = 900
LOOP_POLL_SECONDS = 600
STATUSES = ("filled", "submitted", "needs_help", "failed")
SMOKE_TOOLS = ("browser_navigate", "browser_snapshot")


def base_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "autojob-worker" if local else Path.home() / ".autojob-worker"


@dataclass
class Config:
    server: str
    token: str
    user_data_dir: str
    browser: str = "msedge"
    executable_path: str | None = None
    mcp_package: str = ""
    model: str = "sonnet"
    max_jobs: int = 10
    max_minutes: int = 300


def _expand(value: str) -> str:
    def repl(m):
        return os.environ.get(m.group(1), m.group(0))

    return os.path.expandvars(re.sub(r"%([A-Za-z_]+)%", repl, value))


def load_config(path) -> Config:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    package = str(data.get("mcp_package", ""))
    if not re.fullmatch(r"@playwright/mcp@\d+\.\d+\.\d+\S*", package):
        raise SystemExit(
            f"mcp_package must pin an exact version like @playwright/mcp@0.0.84, got {package!r}"
        )
    default_dir = str(base_dir() / "browser-profile")
    return Config(
        server=data["server"],
        token=data["token"],
        user_data_dir=_expand(data.get("user_data_dir") or default_dir),
        browser=data.get("browser") or "msedge",
        executable_path=_expand(data["executable_path"]) if data.get("executable_path") else None,
        mcp_package=package,
        model=data.get("model") or "sonnet",
        max_jobs=int(data.get("max_jobs", 10)),
        max_minutes=int(data.get("max_minutes", 300)),
    )


def build_payload(reply, claim_channel, session_kind, session) -> dict:
    """Attempt payload from the agent's reply and the gate's own observations."""
    if not isinstance(reply, dict):
        reply = {}
    status = reply.get("status") if reply.get("status") in STATUSES else "needs_help"
    reason_code = reply.get("reason_code") or ("none" if status in ("filled", "submitted") else "other")
    reason = str(reply.get("reason") or "")
    final_url = str(reply.get("final_url") or claim_channel["target"])
    if session.last_url and session.last_url != final_url:
        reason = (reason + f" (page URL per the browser: {session.last_url})").strip()
        final_url = session.last_url
    if status == "submitted" and session_kind != "fill_and_submit":
        status, reason_code = "needs_help", "other"
        reason = "agent reported submitted in a fill-only session"
    if status == "submitted" and not any(
        Path(p.replace("\\", "/")).name == "submitted.png" for p in session.screenshots
    ):
        status, reason_code = "needs_help", "other"
        reason = "agent reported submitted without a screenshot"
    return {
        "index": claim_channel["index"],
        "kind": claim_channel["kind"],
        "target": claim_channel["target"],
        "session": session_kind,
        "status": status,
        "reason_code": reason_code,
        "reason": reason,
        "final_url": final_url,
        "submit_control": str(reply.get("submit_control") or ""),
        "fields_filled": reply.get("fields_filled") or [],
        "unanswered_required": reply.get("unanswered_required") or [],
        "cv_uploaded": bool(reply.get("cv_uploaded")),
        "denied_tool_calls": session.denied,
        "tool_calls": session.tool_calls,
    }


def _resolve_shots(session, out_dir: Path) -> list[Path]:
    found = []
    for raw in session.screenshots:
        p = Path(raw)
        if not p.is_file():
            p = out_dir / Path(raw.replace("\\", "/")).name
        if p.is_file() and p not in found:
            found.append(p)
    return found


class Heartbeat:
    def __init__(self, api, job_id, lease_token, interval=HEARTBEAT_SECONDS):
        self.api, self.job_id, self.lease_token, self.interval = api, job_id, lease_token, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self.interval):
            self.beat()

    def beat(self):
        try:
            self.api.heartbeat(self.job_id, self.lease_token)
        except Exception as exc:  # a missed beat must not kill the session
            log.warning("heartbeat failed: %s", exc)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


def _ensure_cv(api, claim, work: Path):
    cv = claim.get("cv")
    if not cv:
        return None
    dest = work / "cv" / Path(cv["filename"]).name
    if not dest.is_file() or dest.stat().st_size != cv.get("size"):
        api.download_cv(dest)
    return dest.resolve()


def process_job(api, cfg, claim, work: Path, query=None) -> None:
    """Run every channel of one claim. Raises UsageLimit or AgentError after releasing."""
    job_id, lease = claim["job_id"], claim["lease_token"]
    phase = claim.get("phase", "fill")
    submit_allowed = bool(claim.get("submit_allowed")) or phase == "submit"
    session_kind = "fill_and_submit" if submit_allowed else "fill"
    cv_path = _ensure_cv(api, claim, work)
    cover_dir = work / "cover-letters"
    cover_dir.mkdir(parents=True, exist_ok=True)
    cover_path = cover_dir / f"{job_id}.txt"
    cover_path.write_text(claim.get("cover_letter") or "", encoding="utf-8")
    channels = list(claim.get("channels") or [])
    started = time.monotonic()
    pos = 0
    with Heartbeat(api, job_id, lease) as beat:
        while pos < len(channels):
            channel = channels[pos]
            if time.monotonic() - started > PER_JOB_SECONDS:
                api.release(job_id, lease, "per-job time limit reached")
                return
            beat.beat()
            out_dir = work / "shots" / f"{job_id}-{channel['index']}"
            out_dir.mkdir(parents=True, exist_ok=True)
            llm_dir = work / "llm-work"
            llm_dir.mkdir(parents=True, exist_ok=True)
            prompt = agent.fill_prompt(
                claim["job"], claim.get("answers") or {}, claim.get("cover_letter") or "",
                str(cv_path or ""), str(cover_path), channel["target"], submit_allowed,
            )
            gate = gate_mod.Gate(submit_allowed, upload_paths=[p for p in (cv_path, cover_path) if p])
            log.info("job %s channel %s %s (%s)", job_id, channel["index"], channel["kind"], session_kind)
            try:
                session = agent.run_session(
                    prompt, gate=gate, mcp_config=agent.mcp_config(cfg, str(out_dir)),
                    cwd=llm_dir, model=cfg.model,
                    timeout_s=SUBMIT_TIMEOUT_S if submit_allowed else FILL_TIMEOUT_S,
                    query=query,
                )
            except (agent.UsageLimit, agent.AgentError) as exc:
                api.release(job_id, lease, str(exc)[:300])
                raise
            try:
                reply = agent.extract_json(session.text)
            except ValueError:
                reply = {}
            payload = build_payload(reply, channel, session_kind, session)
            if not reply:
                payload["reason"] = "agent reply was not a JSON object"
            log.info(
                "result %s (%s); tool calls %s; denied submit attempts: %s",
                payload["status"], payload["reason_code"], payload["tool_calls"], payload["denied_tool_calls"],
            )
            answer = api.post_attempt(job_id, lease, payload, _resolve_shots(session, out_dir))
            if "next_index" in answer:
                nxt = int(answer["next_index"])
                pos = next((i for i, c in enumerate(channels) if c["index"] == nxt), len(channels))
                continue
            return


def smoke(cfg, work: Path, query=None) -> int:
    out_dir = work / "shots" / "smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    (work / "llm-work").mkdir(parents=True, exist_ok=True)
    gate = gate_mod.Gate(False, max_tool_calls=10, tools=SMOKE_TOOLS)
    try:
        session = agent.run_session(
            agent.smoke_prompt(), gate=gate, mcp_config=agent.mcp_config(cfg, str(out_dir)),
            cwd=work / "llm-work", model=cfg.model, timeout_s=180, query=query,
        )
    except (agent.AgentError, agent.UsageLimit) as exc:
        print(f"smoke failed: {exc}")
        return 1
    print(f"reply: {session.text.strip()}")
    print(f"tool calls: {session.tool_calls}")
    ok = "example domain" in session.text.lower()
    print("smoke ok" if ok else "smoke failed: reply did not contain Example Domain")
    return 0 if ok else 1


def run(cfg, api, work: Path, *, max_jobs, max_minutes, loop=False, query=None, sleep=time.sleep) -> int:
    deadline = time.monotonic() + max_minutes * 60
    done = 0
    host = socket.gethostname()
    while done < max_jobs and time.monotonic() < deadline:
        try:
            claim = api.claim(host)
        except (api_mod.ApiError, OSError, ValueError) as exc:
            log.error("claim failed: %s", exc)
            if loop:
                sleep(LOOP_POLL_SECONDS)
                continue
            return 1
        if claim is None:
            log.info("nothing queued")
            if loop:
                sleep(LOOP_POLL_SECONDS)
                continue
            break
        done += 1
        try:
            process_job(api, cfg, claim, work, query=query)
        except agent.UsageLimit as exc:
            reset = f", resets at epoch {exc.reset_epoch}" if exc.reset_epoch else ""
            log.error("usage limit reached, stopping the run%s", reset)
            return 0
        except agent.AgentError as exc:
            log.error("agent unavailable, stopping the run: %s", exc)
            return 1
        except (api_mod.ApiError, OSError, ValueError) as exc:
            # One job's server or network error must not end the run.
            log.error("job %s failed: %s", claim.get("job_id"), exc)
            try:
                api.release(claim["job_id"], claim["lease_token"], f"worker error: {exc}"[:300])
            except Exception as release_exc:  # noqa: BLE001 - the lease expires on its own
                log.warning("release after error failed: %s", release_exc)
    return 0


def main(argv=None, *, query=None, transport=None) -> int:
    parser = argparse.ArgumentParser(description="Auto-job PC worker")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--smoke", action="store_true")
    parser.add_argument("--max-jobs", type=int)
    parser.add_argument("--max-minutes", type=int)
    args = parser.parse_args(argv)

    work = base_dir()
    work.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), logging.FileHandler(work / "worker.log", encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.smoke:
        return smoke(cfg, work, query=query)
    client = api_mod.Api(cfg.server, cfg.token, **({"transport": transport} if transport else {}))
    return run(
        cfg, client, work,
        max_jobs=args.max_jobs or cfg.max_jobs,
        max_minutes=args.max_minutes or cfg.max_minutes,
        loop=args.loop, query=query,
    )


if __name__ == "__main__":
    sys.exit(main())

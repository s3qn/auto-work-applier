# Auto-job PC worker

Runs on Sean's Windows 11 PC. It claims queued jobs from the server, opens a dedicated
Edge profile on the real screen and fills application forms with a Claude agent. With
`auto_submit` off it stops before the final submit and the server shows the result as
ready to submit. Copy this folder to `%LOCALAPPDATA%\autojob-worker\` (the worker keeps
its logs, CV copy and screenshots there too). Needs Python 3.13, Node 24 and Edge.

1. Install the Claude CLI and log in with the subscription:
   `npm i -g @anthropic-ai/claude-code`, then run `claude` once and sign in.
2. Install the Python dependency: `py -3 -m pip install -r requirements.txt`
3. Create the browser profile and log in by hand to LinkedIn, AllJobs and JobMaster:
   `"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --user-data-dir=%LOCALAPPDATA%\autojob-worker\browser-profile`
   Then close every window of that Edge. To use Brave instead, set `executable_path` in
   `config.json` to `C:\Program Files\BraveSoftware\Brave-Browser\application\brave.exe`
   (log in with that same executable and profile, and turn Shields off for the three sites).
4. On the server run `uv run python -m autojob.set_worker_token`. Copy
   `config.example.json` to `config.json` and paste the token into it.
5. Check the setup: `py -3 worker.py --smoke`. Success: Edge opens, the output shows
   `reply: Example Domain`, `tool calls: N` and `smoke ok`, exit code 0.
   Then try one real run with `auto_submit` off: `py -3 worker.py --once`. Success: the log
   shows a result per job and the dashboard lists the application as ready to submit
   with a `filled.png` screenshot. "nothing queued" means no job is waiting.
6. Schedule it nightly: `powershell -ExecutionPolicy Bypass -File .\install-task.ps1`
   (task `AutoJobWorker`, 02:05, wakes the PC). That is five minutes after the server's
   02:00 pilot timer: on a pilot night every claim belongs to the pilot, nothing is
   submitted, and the server stops handing out jobs at the pilot cap or at 07:00. Keep
   `max_jobs` in `config.json` at or above the pilot cap set on the Pilot page. The task needs you signed in (locking the
   screen is fine, signing out is not). Keep the laptop on AC power: on battery it sleeps
   after 10 minutes.

Log: `%LOCALAPPDATA%\autojob-worker\worker.log`. The `@playwright/mcp` version is pinned
in `config.json` (`mcp_package`); `@latest` is refused.

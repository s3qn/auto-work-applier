"""Configure the SMTP credentials the email apply channel sends through.

Usage: uv run python -m autojob.set_smtp
"""

from __future__ import annotations

import getpass
import sys

from autojob.apply import mailer
from autojob.config import load_config


def main() -> int:
    config = load_config()
    defaults = mailer.SMTP_DEFAULTS

    host = input(f"SMTP host [{defaults['host']}]: ").strip() or defaults["host"]
    port_raw = input(f"SMTP port [{defaults['port']}]: ").strip()
    port = int(port_raw) if port_raw else defaults["port"]
    username = input(f"Username [{defaults['username']}]: ").strip() or defaults["username"]
    from_address = input(f"From address [{username}]: ").strip() or username
    password = getpass.getpass("App password: ")
    if not password:
        print("Password cannot be empty.")
        return 1

    values = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "from_address": from_address,
    }
    path = mailer.save_smtp_config(config, values)
    print(f"SMTP config saved to {path}")
    print(
        "Google Workspace requires an App Password with 2-step verification on: "
        "https://myaccount.google.com/apppasswords"
    )

    send_test = input("Send a test email to the from address now? [y/N]: ").strip().lower()
    if send_test == "y":
        draft = {
            "to": from_address,
            "subject": "auto-job SMTP test",
            "body": "This is a test email from auto-job's SMTP setup.",
            "attachment_name": None,
        }
        msg = mailer.build_message(draft, from_address=from_address, cv_path=None)
        try:
            mailer.send_message(msg, values)
        except mailer.EmailSendError as exc:
            print(f"Test email failed: {exc}")
            return 1
        print(f"Test email sent to {from_address}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Answer bank and cover-letter generation tests. No real claude CLI call:
`run_claude` is always a fake, same pattern as tests/test_llm.py."""

from __future__ import annotations

import json

from autojob.apply import answers as answers_module
from autojob.llm.cli import ClaudeCliError


# --- answer bank: consistency and honesty --------------------------------

def test_seed_answer_bank_years_experience_is_low_and_honest():
    bank = answers_module.seed_answer_bank({})
    years_answer = bank["years_experience"].lower()
    assert "0 to 1" in years_answer
    # Guard against a regression that quietly inflates this: nothing here
    # should claim several years of professional experience.
    for inflated in ("3 years", "5 years", "several years", "many years"):
        assert inflated not in years_answer


def test_seed_answer_bank_has_all_four_recurring_categories():
    bank = answers_module.seed_answer_bank({})
    assert set(bank.keys()) == {"years_experience", "visa_sponsorship", "relocation", "salary_expectation"}


def test_seed_answer_bank_uses_profile_values_when_present():
    profile = {"willing_to_relocate": "Yes, anywhere in Israel", "salary_expectation": "25000 NIS/month"}
    bank = answers_module.seed_answer_bank(profile)
    assert bank["relocation"] == "Yes, anywhere in Israel"
    assert bank["salary_expectation"] == "25000 NIS/month"
    # Untouched categories keep their conservative defaults.
    assert "0 to 1" in bank["years_experience"]


def test_seed_answer_bank_is_deterministic_for_the_same_profile():
    profile = {"salary_expectation": "Open"}
    first = answers_module.seed_answer_bank(profile)
    second = answers_module.seed_answer_bank(profile)
    assert first == second


def test_build_answer_set_merges_profile_and_bank():
    profile = {"full_name": "Sean Larch", "email": "sean@example.com"}
    bank = {"years_experience": "0 to 1 years"}
    merged = answers_module.build_answer_set(profile, bank)
    assert merged["full_name"] == "Sean Larch"
    assert merged["years_experience"] == "0 to 1 years"


# --- honesty guard on the generation prompt --------------------------------

def test_generate_application_content_prompt_includes_honest_profile_and_no_fabrication_rule(monkeypatch, tmp_path):
    captured = {}

    def fake_run_claude(prompt, *, model, cwd, timeout, allowed_tools=None):
        captured["prompt"] = prompt
        return json.dumps({"cover_letter": "Dear team, ...", "screening_answers": {}})

    monkeypatch.setattr(answers_module, "run_claude", fake_run_claude)

    profile_answers = {"full_name": "Sean Larch", "city": "Tel Aviv"}
    answer_bank = answers_module.seed_answer_bank(profile_answers)
    job = {"title": "Junior Penetration Tester", "company": "Acme", "description": "Pentest things."}

    answers_module.generate_application_content(
        profile_answers, answer_bank, job, "Acme does security consulting.", work_dir=tmp_path
    )

    prompt = captured["prompt"]
    assert "JUNIOR penetration tester" in prompt
    assert "HTB CPTS" in prompt
    assert "Do not fabricate" in prompt
    assert "0 to 1" in prompt
    assert "Sean Larch" in prompt
    assert "Acme" in prompt


def test_generate_application_content_returns_parsed_fields(monkeypatch, tmp_path):
    payload = {
        "cover_letter": "Dear Acme, I am excited to apply.",
        "screening_answers": {"How did you hear about us?": "Automated job search"},
    }
    monkeypatch.setattr(
        answers_module, "run_claude", lambda prompt, **kw: json.dumps(payload)
    )
    content = answers_module.generate_application_content(
        {"full_name": "Sean"}, answers_module.seed_answer_bank({}), {"title": "Pentester"}, "tldr", work_dir=tmp_path
    )
    assert content["cover_letter"] == "Dear Acme, I am excited to apply."
    assert content["screening_answers"] == {"How did you hear about us?": "Automated job search"}


def test_generate_application_content_falls_back_honestly_on_cli_failure(monkeypatch, tmp_path):
    def raising_run_claude(*a, **k):
        raise ClaudeCliError("boom")

    monkeypatch.setattr(answers_module, "run_claude", raising_run_claude)

    content = answers_module.generate_application_content(
        {"full_name": "Sean Larch"},
        answers_module.seed_answer_bank({}),
        {"title": "Junior Penetration Tester", "company": "Acme"},
        "tldr",
        work_dir=tmp_path,
    )
    assert content["screening_answers"] == {}
    letter = content["cover_letter"].lower()
    assert "htb cpts" in letter
    assert "do not have professional pentest experience yet" in letter
    for inflated in ("3 years", "5 years", "several years", "many years"):
        assert inflated not in letter

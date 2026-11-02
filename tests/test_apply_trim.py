from autojob.apply import answers
from autojob.apply.trim import short_variant, trim_cover_letter

LETTER = (
    "Dear team,\n\n"
    "I am a junior pentester. I hold CPTS and PNPT. I do not have professional experience.\n\n"
    "I have no paid pentest work yet. I built a home AD lab. As a jr. tester I learn fast.\n\n"
    "Thank you.\n\nSean"
)


def test_trim_removes_junior_and_extra_experience_sentences():
    out = trim_cover_letter(LETTER)
    assert "junior" not in out.lower() and "jr" not in out.lower()
    assert out.count("professional experience") + out.count("no paid") == 1
    assert "I hold CPTS and PNPT." in out
    assert "\n\n\n" not in out


def test_trim_hebrew_junior():
    assert "ג'וניור" not in trim_cover_letter("שלום. אני ג'וניור בתחום. יש לי תעודות.")


def test_trim_only_removes_words():
    from collections import Counter
    out = Counter(trim_cover_letter(LETTER).split())
    assert not (out - Counter(LETTER.split()))


def test_short_variant_limit():
    body = "\n\n".join(" ".join(f"Sentence {p} {i} about security work." for i in range(12)) for p in range(8))
    letter = f"Dear team,\n\n{body}\n\nThank you.\n\nSean"
    short = short_variant(letter, 1000)
    assert len(short) <= 1000
    assert short.startswith("Dear team")


def test_prompt_has_new_rule():
    prompt = answers._build_prompt({}, {}, {"title": "t"}, "")
    assert "Never use the word junior" in prompt


def test_trim_word_boundaries_geresh_and_line_breaks():
    assert "slack" in trim_cover_letter("I use slack daily. I lack depth. I lack more.")
    assert "I lack depth." in trim_cover_letter("I use slack daily. I lack depth. I lack more.")
    assert "I lack more" not in trim_cover_letter("I lack depth. I lack more.")
    assert "I know notes yet to come." in trim_cover_letter("I know notes yet to come.")
    assert "ג’וניור" not in trim_cover_letter("שלום. אני ג’וניור בתחום. יש.")
    assert "ג׳וניור" not in trim_cover_letter("שלום. אני ג׳וניור בתחום. יש.")
    assert trim_cover_letter("Hi.\n\nThanks.\nBest,\nSean") == "Hi.\n\nThanks.\nBest,\nSean"

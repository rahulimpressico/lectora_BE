"""Shared text and pacing utilities for A1."""
import re


def count_words(text: str) -> int:
    return len(re.findall(r"\w+", text))


def words_to_minutes(word_count: int, wpm: int = 180) -> float:
    """Convert words to reading minutes. 180 wpm = 1 min per NAIC CE standard."""
    return round(word_count / wpm, 1)


def wpm_from_rule_pack(rule_pack: dict, default: int = 180) -> int:
    """Derive reading speed (wpm) from rule pack: wpm = words_per_credit_hour / 50."""
    try:
        wph = (rule_pack.get("content_rules") or {}).get("words_per_credit_hour")
        if wph:
            return max(1, int(round(float(wph) / 50.0)))
    except (TypeError, ValueError):
        pass
    return default


def to_snake(text: str) -> str:
    s = re.sub(r"[^\w\s]", "", text.lower())
    return re.sub(r"\s+", "_", s.strip())[:60]

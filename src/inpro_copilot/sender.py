"""Who sent the invoice? Spotting fake senders.

Business e-mail compromise works like this: a fraudster registers a domain that LOOKS like the
supplier's (coolbIue.nl, azure-interiors.com, rn instead of m ...) and sends a real-looking invoice
with their own bank account. People read what they expect to see. This module compares domains the
way a computer should: character by character, after undoing the usual visual tricks.
"""
from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "ymail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "icloud.com", "me.com", "aol.com", "rediffmail.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de",
    "gmx.net", "mail.com", "zoho.com", "zohomail.in", "yandex.com",
}
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "ac", "edu"}          # co.in, com.au, co.uk ...
# characters that look alike on screen (incl. Cyrillic/Greek letters used in spoofed domains)
_CONFUSABLE = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "5": "s", "а": "a", "е": "e", "о": "o", "р": "p",
                             "с": "c", "х": "x", "у": "y", "і": "l", "ӏ": "l", "ο": "o", "ν": "v", "α": "a"})


def domain_of(address: str | None) -> str:
    a = (address or "").strip().lower()
    return a.rsplit("@", 1)[-1].strip(" >") if "@" in a else ""


def registrable(domain: str) -> str:
    """mail.billing.coolblue.nl -> coolblue.nl ; accounts.tata.co.in -> tata.co.in"""
    parts = [p for p in (domain or "").lower().split(".") if p]
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def skeleton(domain: str) -> str:
    """What a domain looks like to a human: look-alike characters folded together."""
    d = unicodedata.normalize("NFKC", registrable(domain)).lower()
    d = d.replace("rn", "m").replace("vv", "w").translate(_CONFUSABLE)
    return re.sub(r"[^a-z0-9.]", "", d)


def domains_in_text(text: str) -> set[str]:
    """Websites and e-mail domains printed on the invoice itself."""
    found = set()
    for m in re.finditer(r"(?:www\.|https?://|@)([a-z0-9][a-z0-9.-]*\.[a-z]{2,})", text or "", re.I):
        found.add(registrable(m.group(1)))
    return found


def compare(sender_domain: str, known: set[str]) -> tuple[str, str | None]:
    """-> ('known' | 'lookalike' | 'unknown', the known domain it matches or imitates)."""
    sd = registrable(sender_domain)
    known = {registrable(k) for k in known if k}
    if sd in known:
        return "known", sd
    sk, sname = skeleton(sd), sd.split(".")[0]
    for k in sorted(known):
        kname = k.split(".")[0]
        if skeleton(k) == sk or (len(kname) >= 4 and fuzz.ratio(sname, kname) >= 85) or \
                (len(kname) >= 5 and kname in sname and sname != kname):
            return "lookalike", k
    return "unknown", None

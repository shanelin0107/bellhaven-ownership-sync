"""Address and name normalization shared by the matcher and the review app."""

import re
from difflib import SequenceMatcher

# Canonical forms. Both sides of a comparison go through the same map, so it
# only matters that a concept lands on one token -- not which token wins.
STREET_TOKENS = {
    "street": "st", "str": "st",
    "road": "rd",
    "avenue": "ave", "av": "ave",
    "drive": "dr",
    "boulevard": "blvd", "boul": "blvd",
    "lane": "ln",
    "place": "pl",
    "court": "ct",
    "circle": "cir",
    "parkway": "pkwy", "pky": "pkwy",
    "highway": "hwy",
    "square": "sq",
    "terrace": "ter",
    "trail": "trl",
    "pk": "pike",
    "turnpike": "pike",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne",
    "southwest": "sw", "southeast": "se",
}

# Unit designators carry no matching signal and vary wildly in the wild.
UNIT_RE = re.compile(
    r"(?:\b(?:suite|ste|unit|apt|apartment|bldg|building)\b|#)\s*[\w-]*", re.I)


def norm_street(s):
    if not s:
        return ""
    s = UNIT_RE.sub(" ", s.lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(STREET_TOKENS.get(t, t) for t in s.split())


def house_number(s):
    """Leading street number -- the most stable part of any address."""
    m = re.match(r"\s*(\d+)", s or "")
    return m.group(1) if m else ""


def street_core(s):
    """Street tokens minus the house number and the type suffix.

    '3313 Wilmington Pike' -> 'wilmington'.  Survives Pike/Pk drift even when
    the abbreviation map misses, which is what makes tier 2 worth having.
    """
    toks = norm_street(s).split()
    toks = [t for t in toks if not t.isdigit()]
    types = set(STREET_TOKENS.values()) | {"st", "rd", "ave", "dr", "blvd", "ln",
                                           "pl", "ct", "cir", "pkwy", "hwy", "sq",
                                           "ter", "trl", "pike"}
    core = [t for t in toks if t not in types]
    return " ".join(core or toks)


def norm_city(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


# Words that say nothing about which facility this is.
NAME_NOISE = {
    "the", "of", "at", "and", "a",
    "senior", "living", "care", "center", "centre", "health", "healthcare",
    "nursing", "rehabilitation", "rehab", "home", "community", "communities",
    "campus", "manor", "house", "place", "village", "gardens", "estates",
    "commons", "court", "terrace", "retirement", "services", "group", "llc", "inc",
}


def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"\(parent account\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def name_tokens(s):
    return {t for t in norm_name(s).split() if t not in NAME_NOISE and len(t) > 2}


def name_similarity(a, b):
    """Blend of sequence ratio and distinctive-token overlap.

    Pure sequence ratio rates 'Amberly Manor' vs 'Amberly Manor' at 1.0 no
    matter where they are, which is exactly the trap -- so callers must gate
    this behind a geographic check. Token overlap keeps 'Bellhaven Shores of
    Erie' close to 'Harborview Shores of Erie' via the shared 'shores'/'erie'.
    """
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = name_tokens(a), name_tokens(b)
    jac = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return round(0.5 * seq + 0.5 * jac, 4)

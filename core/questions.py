"""Question detection, language, normalisation and dedupe hashing (DE + EN)."""

import hashlib
import re
import unicodedata

# Words that open a question. German first, English second (the owner's list plus the usual extras).
INTERROGATIVES_DE = {
    "wie", "was", "welche", "welcher", "welches", "welchen", "welchem", "warum", "wieso", "weshalb", "wann", "wo",
    "wer", "wozu", "wofür", "womit", "wodurch", "wieviel", "wieviele", "kann", "soll", "sollte", "ist", "sind",
    "gibt", "muss", "darf", "lohnt", "passt", "reicht", "hilft",
}
INTERROGATIVES_EN = {
    "how", "what", "which", "why", "when", "where", "who", "can", "should", "does", "do", "is", "are", "will",
    "would", "could", "whats", "hows",
}
INTERROGATIVES = INTERROGATIVES_DE | INTERROGATIVES_EN

# Function words that tell the two languages apart in a short query.
_DE_WORDS = {"und", "der", "die", "das", "den", "dem", "ein", "eine", "einen", "ich", "nicht", "für", "mit", "bei", "von",
             "zu", "im", "auf", "oder", "man", "mein", "meine", "bettwäsche", "bett", "decke", "kissen", "schlafen"}
_EN_WORDS = {"and", "the", "a", "an", "of", "for", "with", "my", "i", "to", "in", "on", "or", "your", "you", "bed", "bedding"}
_UMLAUT_RE = re.compile(r"[äöüßÄÖÜ]")


def _tokens(text):
    return re.findall(r"[\wäöüßÄÖÜ]+", text.lower())


def is_question(text):
    text = (text or "").strip()
    toks = _tokens(text)
    return bool(toks) and (text.endswith("?") or toks[0] in INTERROGATIVES)


def language(text):
    """'de' or 'en'. A German opener or umlaut decides; otherwise count function words; default English."""
    toks = _tokens(text)
    if not toks:
        return "en"
    if toks[0] in INTERROGATIVES_DE - INTERROGATIVES_EN or _UMLAUT_RE.search(text):
        return "de"
    de, en = sum(t in _DE_WORDS for t in toks), sum(t in _EN_WORDS for t in toks)
    return "de" if de > en else "en"


def normalize(text):
    """Comparison form: casefolded, NFC, punctuation and a trailing '?' removed, single spaces."""
    t = unicodedata.normalize("NFC", (text or "")).casefold()
    t = re.sub(r"[^\w\säöüß]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def qhash(text):
    """Short non-reversible fingerprint used to dedupe questions in the (public) ledger."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:16]


def parse_pasted(text):
    """Split a pasted findquestions list into single questions.

    A workflow_dispatch text box may collapse newlines, so besides lines we also split on
    '|' / ';' and after every '?'. Leading numbering/bullets are removed. Deduped, order kept."""
    out, seen = [], set()
    for chunk in re.split(r"[\n\r|;]+", text or ""):
        for part in re.split(r"(?<=\?)\s+", chunk):
            q = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", part).strip()
            if len(q) >= 8 and normalize(q) not in seen:
                seen.add(normalize(q))
                out.append(q)
    return out

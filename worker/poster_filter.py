"""Conservative source-aware poster classification for owner-direct discovery.

A source label is a collection signal, not independent proof of legal ownership.
"""
from __future__ import annotations

import re

AGENT_EVIDENCE = re.compile(
    r"\b(?:"
    r"realtor(?:s)?|real estate (?:agent|broker|professional|brokerage)|"
    r"listing agent|seller'?s agent|buyer'?s agent|buyer agent|"
    r"broker(?:age)?|brokered by|represented by (?:an? )?(?:agent|broker|realtor)|"
    r"listed by (?:an? )?(?:agent|broker|realtor)|"
    r"mls(?:\s*(?:#|no\.?|number|id))|dre\s*(?:#|no\.?|number)|"
    r"licensed (?:real estate )?(?:agent|broker|realtor)|"
    r"compass real estate|keller williams|coldwell banker|century\s*21|"
    r"re\s*/\s*max|sotheby'?s international realty|engel\s*&\s*v[oö]lkers|"
    r"exp realty|london foster"
    r")\b",
    re.IGNORECASE,
)

# These phrases explicitly deny agent representation and should not be mistaken
# for evidence that the poster is an agent (e.g., "no agents please").
OWNER_NO_AGENT = re.compile(
    r"\b(?:no|without|not represented by|not using|don't need|do not need)\s+"
    r"(?:any\s+)?(?:real estate\s+)?(?:agents?|realtors?|brokers?)\b[^.\n]{0,48}",
    re.IGNORECASE,
)

EXPLICIT_AGENT_LABELS = {
    "agent", "real estate agent", "realtor", "broker", "brokerage",
    "listing_agent", "listing agent", "property_manager", "property manager",
    "agency", "company", "developer",
}

SOURCE_OWNER_SIGNALS = {
    "craigslist": "craigslist_purveyor_owner_search",
    "forsalebyowner": "forsalebyowner_dedicated_owner_listing_source",
    "byowner": "byowner_owner_direct_source_path",
}


def classify_poster(source_key: str, title: str = "", description: str = "",
                    poster_label: str = "") -> dict[str, object]:
    """Return a conservative role decision and non-sensitive evidence codes.

    Decisions are `reject_agent`, `owner_candidate`, or `hold_ambiguous`.
    Owner candidates remain OWNER_UNVERIFIED; no decision here verifies title.
    """
    source = (source_key or "").strip().lower()
    label = re.sub(r"\s+", " ", (poster_label or "").strip().lower())
    text = " ".join((title or "", description or "", poster_label or ""))
    text = OWNER_NO_AGENT.sub(" ", text)

    if label in EXPLICIT_AGENT_LABELS or AGENT_EVIDENCE.search(text):
        return {
            "decision": "reject_agent",
            "reason_code": "explicit_agent_or_broker_evidence",
            "evidence_codes": ["agent_role_label_or_brokerage_language"],
            "poster_type": "AGENT",
            "poster_evidence": "Explicit agent/broker evidence detected in listing text or poster label; seller contact was not retained.",
        }

    signal = SOURCE_OWNER_SIGNALS.get(source)
    if signal:
        return {
            "decision": "owner_candidate",
            "reason_code": "configured_owner_source_signal",
            "evidence_codes": [signal],
            "poster_type": "OWNER_UNVERIFIED",
            "poster_evidence": (
                f"Configured source signal: {signal}. This indicates an owner-focused source/search path only; "
                "owner identity still requires human verification."
            ),
        }

    return {
        "decision": "hold_ambiguous",
        "reason_code": "no_strong_owner_source_signal",
        "evidence_codes": ["source_or_poster_signal_missing"],
        "poster_type": "UNKNOWN",
        "poster_evidence": "No configured owner-source signal was available; held for review and not eligible for sale.",
    }

import logging

logger = logging.getLogger("mia-dancing-cow")


def _is_ending(text: str) -> bool:
    text = text.strip()
    end_phrases = [
        "bye", "goodbye", "good bye", "okay bye", "ok bye",
        "thanks for your time", "thank you for your time",
        "have a great day", "have a wonderful day",
        "take care", "talk to you soon",
        "really appreciate your time",
        "बाय", "बाय बाय", "ओके बाय", "अलविदा",
        "धन्यवाद", "शुक्रिया", "ठीक है बाय", "थैंक यू",
    ]
    for phrase in end_phrases:
        if text == phrase:
            return True
        if text.endswith(phrase):
            return True
    return False


def _extract_text(ev) -> str:
    text = ""
    if hasattr(ev, 'content'):
        content = ev.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, str):
                    parts.append(c)
                elif hasattr(c, 'text'):
                    parts.append(c.text)
                elif hasattr(c, 'content'):
                    parts.append(str(c.content))
            text = " ".join(parts)
    elif hasattr(ev, 'text'):
        text = ev.text or ""
    elif hasattr(ev, 'transcript'):
        text = ev.transcript or ""
    return text.lower()

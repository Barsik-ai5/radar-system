"""Remove the source's map promotion from otherwise usable posts."""

import re

_INVISIBLE = "\u200b\ufeff"
_MAP_NAME = (
    r"(?:(?:актуальн\w*|обновл[её]нн\w*|интерактивн\w*)\s+)?карта\s+"
    r"(?:(?:всех|активн\w*|текущ\w*|действующ\w*)\s+)*(?:угроз|опасност)\w*\b"
)
_DASH = r"[-‐‑‒–—―−]"
_MAP_URL = (
    rf"(?<![\w@./-])(?:https?://)?(?:www\.)?radar{_DASH}vrv\.ru"
    r"(?![\w@-]|\.\w)(?:[/?#][^\s<>\[\]{}()«»]*)?"
)
_MAP_PROMO = re.compile(
    rf"(?<!\w)(?:[ \t]*(?:{_DASH}|[|🗺️📍📌🔗👉➡]|\*{{1,2}}))*[ \t]*"
    rf"(?:{_MAP_NAME}(?:\s*[*_»”\]]*\s*{_DASH}?\s*:?\s*[\[(«]?\s*{_MAP_URL}"
    rf"|[ \t]*[*_»”\]]*[ \t]*[.!]?(?=[ \t]*\r?$))"
    rf"|{_MAP_URL})[)\]»”*_]*",
    re.IGNORECASE | re.MULTILINE,
)
_MAP_HEADING = re.compile(
    rf"^[^\w]*(?:(?:постфактум|сводка|инфо)\s*:\s*[^\w]*)?{_MAP_NAME}", re.IGNORECASE
)
_MAP_LINK_LABEL = re.compile(
    rf"(?:{_MAP_NAME}|(?:смотреть|открыть|посмотреть|перейти\s+на)\s+карт[уе]\b"
    r"(?:\s+(?:активных\s+)?(?:угроз|опасностей))?)",
    re.IGNORECASE,
)


def is_map_announcement(text: str) -> bool:
    return bool(_MAP_HEADING.match(text.translate(dict.fromkeys(map(ord, _INVISIBLE)))))


def mask_map_promotion(text: str, links=()) -> str:
    """Replace promotion with spaces at the same offsets, for Telegram link entities."""
    positions = [i for i, char in enumerate(text) if char not in _INVISIBLE]
    visible = "".join(text[i] for i in positions)
    spans = [(positions[m.start()], positions[m.end() - 1] + 1) for m in _MAP_PROMO.finditer(visible)]
    for start, end, url in links:
        if re.fullmatch(_MAP_URL, url.strip(), re.IGNORECASE) and _MAP_LINK_LABEL.fullmatch(text[start:end].strip()):
            spans.append((start, end))

    chars = list(text)
    for start, end in spans:
        chars[start:end] = [char if char in "\r\n" else " " for char in chars[start:end]]
    return "".join(chars)

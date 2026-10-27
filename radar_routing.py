"""Pure, deterministic geographic routing rules for the radar bot.

This module intentionally has no Telegram, AI, or environment dependencies so
routing can be exercised offline without loading the production bot.
"""

from __future__ import annotations

import re


REGION_KEYWORDS = {
    "BELGOROD": [
        "белгород", "белгородск", "шебекин", "валуй", "грайворон", "оскол", "губкин",
        "волоконов", "борисов", "ивнянск", "ракитн", "ракитянск", "краснояруж",
        "красненск", "алексеев", "короч", "вейделев", "ровен", "чернян", "прохоров",
        "строител", "октябрьск", "томаров", "тавров", "дубов", "майск", "разумн",
        "беломестн", "стрелецк", "головчин", "бессонов", "маслов", "уразов", "бирюч",
        "вознесеновк", "таволжанк", "нежегол", "яковлевск", "красная яруга", "новый оскол",
        "старый оскол", "красногвардейск", "ивня"
    ],
    "KURSK": [
        "курск", "курчат", "судж", "рыльск", "обоян", "льгов", "коренев", "глушков",
        "беловск", "железногорск", "фатеж", "щигр", "дмитриев", "тимск", "горшечн",
        "поныр", "медвен", "золотухин", "мантуров", "солнцев", "черемисин", "касторн",
        "пристен", "прямицын", "теткин", "тёткин", "халино", "конышев", "большесолдат",
        "хомутов", "крупец", "клевен"
    ],
    "MSK": [
        "москв", "московск", "moscow", "подмосков", "тинао", "зеленоград",
        "подольск", "люберц", "королев", "химк", "балаших", "мытищ", "красногорск",
        "одинцов", "домодедов", "раменск", "ступин", "кашир", "коломн", "чехов",
        "серпухов", "истринск", "можайск", "наро-фоминск", "нарофоминск", "воскресенск",
        "щелков", "щёлков", "пушкин", "сергиев", "дмитров", "клинск", "солнечногорск",
        "ногинск", "электростал", "орехово-зуев", "павловский посад", "егорьевск",
        "зарайск", "луховиц", "протвин", "пущин", "рузск", "шатур", "талдом", "дубненск",
        "фрязин", "жуковск", "бронниц", "видное", "котельник", "реутов", "долгопрудн",
        "лобненск", "кубинк", "алабин", "внуков", "шереметьев"
    ],
    "SPB": [
        "петербург", "санкт", "питер", "ленинград", "ленинградск", "ленобласт", "лен област",
        "лен. област", "выборг", "гатчин", "кронштадт", "лужск", "кингисепп", "волхов",
        "тихвин", "всеволожск", "мурин", "кудров", "тосн", "кириш", "сосновый бор",
        "приозерск", "подпорож", "лодейн", "бокситогорск", "сланц", "волосов", "ломоносов",
        "колпин", "сестрорецк", "петергоф", "усть-луг", "сертолов", "отрадн", "шлиссельбург"
    ]
}

REGION_EXACT_WORDS = {
    "SPB": ["спб", "с-пб", "луга", "луге", "лугу", "лугой"],
    "MSK": [
        "мск", "мкад", "истра", "истре", "истру", "истрой", "клин", "клина", "клину", "клином",
        "клине", "руза", "рузе", "рузу", "рузой", "дубна", "дубне", "дубну", "дубной",
        "лобня", "лобне", "лобню", "лобней"
    ],
    "BELGOROD": ["ивня", "ивне", "ивню", "ивней"],
    "KURSK": ["тим", "тима", "тиме", "тимом", "тиму"]
}

# Towns and district centers in nearby regions/countries. These are used to
# reject foreign subject segments even when the oblast name is omitted.
FOREIGN_REGION_KEYWORDS = [
    # Oryol region
    "орловск", "орел", "орёл", "кромы", "шахов", "знаменк", "хардиков", "тросн",
    "глазунов", "малоархангельск", "дмитровск", "хотынец", "болхов", "верховь", "ливенск",
    # Bryansk region
    "брянск", "клинц", "унеч", "карачев", "стародуб", "севск", "суземк", "трубчевск",
    "навля", "брасов", "локот", "комарич", "дятьков", "жуковк", "погар", "почеп",
    # Voronezh region
    "воронеж", "борисоглебск", "поворин", "новохоперск", "новохопёрск", "талов", "семилук",
    "хохол", "нижнедевицк", "репьев", "аннинск", "бутурлинов", "павловск", "калач",
    "богучар", "кантемиров", "лиски", "острогожск", "россош", "бобров", "эртил", "панин",
    "рамон", "подгоренск",
    # Sumy region
    "сумы", "сумск", "ахтыр", "ахтырк", "тростян", "лебедин", "белополь", "путивл",
    "краснополь", "глухов", "конотоп", "ромн", "шостк", "недригайлов", "бурын",
    "великописарев",
    # Kharkiv region
    "харьков", "харьковск", "волчанск", "купянск", "изюм", "балаклея", "чугун", "дергач",
    "липцы", "золочев", "богодух", "барвенков", "печенег", "казачья лопан", "мереф",
    "люботин", "песочин", "чугуев", "красноград", "новая водолаг", "шелков",
    # Other neighboring and commonly referenced regions
    "краснодар", "анап", "темрюк", "таман", "новороссийск", "геленджик", "туапсе", "сочи",
    "адлер", "ейск", "славянск-на-кубани", "черноморск", "крым", "севастопол", "керч",
    "феодоси", "евпатори", "симферопол", "джанко", "саки", "ялт", "ростов", "таганрог",
    "батайск", "миллеров", "морозовск", "каменск-шахтинск", "липецк", "елец", "тамбов",
    "мичуринск", "саратов", "энгельс", "волгоград", "астрахан", "самар", "сызран", "тольятт",
    "пенз", "ульянов", "барышск", "перм", "удмурт", "ижевск", "воткинск", "чуваш", "чебоксар",
    "мордов", "саранск", "марий", "йошкар", "киров", "екатеринбург", "свердловск", "челябинск",
    "тюмен", "курган", "нижегород", "дзержинск", "казан", "татарстан", "елабуг", "нижнекамск",
    "башкортостан", "уфа", "салават", "оренбург", "орск", "тульск", "тула", "алексин", "узлов",
    "калуж", "калуг", "людинов", "шайковк", "рязан", "смоленск", "тверск", "тверь", "торопец",
    "ярослав", "ивановск", "владимирск", "костромск", "псков", "новгородск", "мурманск",
    "архангельск", "карели", "ставропол", "дагестан", "каспийск", "чечн", "грозн", "осети",
    "моздок", "ингушет", "кабардин", "лнр", "луганск", "белокуракин", "алчевск", "днр", "донецк",
    "макеевк", "горловк", "мариупол", "запорож", "мелитопол", "бердянск", "энергодар", "херсон",
    "геническ", "скадовск"
]

OWN_REGION_PREFIX_MAP = {
    "KURSK": "курск",
    "BELGOROD": "белгород",
    "MSK": "москов",
    "SPB": "ленинград"
}

ANY_REGION_UNIT_PAT = (
    r"(?:[А-ЯЁа-яё\-]+(?:\s+[А-ЯЁа-яё\-]+)?\s+(?:област\w+|\bобл\.?|\bобл\b|кра\w+|республик\w+|\bресп\.?|\bресп\b|автономн\w+\s+округ\w*|\bао\b)|"
    r"(?:республик\w+|\bресп\.?|\bресп\b)\s+[А-ЯЁа-яё\-]+(?:\s+[А-ЯЁа-яё\-]+)?|"
    r"\bЛНР\b|\bДНР\b)"
)

INVISIBLE_CHARS_RE = re.compile("[\u200b\u200c\u200d\ufeff]")
DASH_VARIANTS_RE = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
REGION_UNIT_RE = re.compile(ANY_REGION_UNIT_PAT, re.IGNORECASE)
ORIGIN_MARKER_RE = re.compile(
    r"\b(?:от|из|со\s+стороны|покинул\w*|уш[её]л\s+из|вылетел\w*\s+из)\s+",
    re.IGNORECASE
)
ORIGIN_DIRECTION_RE = re.compile(
    r"\b(?:в\s+сторону|в\s+направлении|курсом\s+на|курс\s+на|далее\s+на|на)\s+",
    re.IGNORECASE
)
INBOUND_DIRECTION_RE = re.compile(
    r"\b(?:в\s+сторону|в\s+направлении|курсом\s+на|курс\s+на|далее\s+на|на|"
    r"прош[её]л\w*\s+(?:бпла|дрон\w*|беспилот\w*)\s+на|подл[её]т\w*\s+к)\s+",
    re.IGNORECASE
)
SUBJECT_SEPARATOR_RE = re.compile(r"\s+-\s+|:\s*", re.IGNORECASE)
EVENT_WORD_RE = re.compile(
    r"бпла|дрон|фпв|fpv|беспилот|опасност|тревог|отбой|сбит|сбил|пво|ракет|авиац|атак|"
    r"фиксац|прол[её]т|обстрел|взрыв|баллист|сирен|оповещ|пострадал|поврежден|уничтож|угроз|отменен|снят",
    re.IGNORECASE
)


def normalize_routing_text(text: str) -> str:
    """Remove invisible formatting characters and standardize Unicode dashes."""
    if not text:
        return ""
    text = INVISIBLE_CHARS_RE.sub("", str(text))
    return DASH_VARIANTS_RE.sub("-", text)


def _matches_keyword(text: str, keyword: str) -> bool:
    if len(keyword) <= 4 and " " not in keyword:
        return bool(re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE))
    return bool(re.search(rf"\b{re.escape(keyword)}", text, re.IGNORECASE))


def has_region_keywords(text: str, reg: str) -> bool:
    """Loose geographic mention check, retained for non-routing cleanup rules."""
    text = normalize_routing_text(text)
    if not text or reg not in REGION_KEYWORDS:
        return False
    for keyword in REGION_EXACT_WORDS.get(reg, []):
        if re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE):
            return True
    return any(_matches_keyword(text, keyword) for keyword in REGION_KEYWORDS[reg])


def has_any_geography(text: str) -> bool:
    """Return whether text names a known region, neighboring locality, or region unit."""
    text = normalize_routing_text(text)
    if REGION_UNIT_RE.search(text):
        return True
    for reg in REGION_KEYWORDS:
        if has_region_keywords(text, reg):
            return True
    return any(_matches_keyword(text, keyword) for keyword in FOREIGN_REGION_KEYWORDS)


def _reply_current_section(text: str) -> str:
    match = re.search(r"текущее\s+обновление\s*:\s*", text, re.IGNORECASE)
    if not match:
        return ""
    current = text[match.end():]
    return current if has_any_geography(current) else ""


def _without_origin_clauses(text: str) -> str:
    """Remove origin descriptions, retaining a later explicit movement vector."""
    text = normalize_routing_text(text)
    for _ in range(12):
        origin = ORIGIN_MARKER_RE.search(text)
        if not origin:
            break
        sentence_end_match = re.search(r"[.!?;\n]", text[origin.end():])
        sentence_end = origin.end() + sentence_end_match.start() if sentence_end_match else len(text)
        direction = ORIGIN_DIRECTION_RE.search(text, origin.end(), sentence_end)
        if direction:
            text = text[:origin.start()] + " " + text[direction.start():]
        else:
            text = text[:origin.start()] + " " + text[sentence_end:]
    return text


def _subject_text(text: str) -> str:
    text = normalize_routing_text(text)
    match = SUBJECT_SEPARATOR_RE.search(text)
    return text[:match.start()] if match else text


def _has_inbound_target(text: str, reg: str) -> bool:
    for direction in INBOUND_DIRECTION_RE.finditer(text):
        sentence_end = re.search(r"[.!?;\n]", text[direction.end():])
        end = direction.end() + sentence_end.start() if sentence_end else len(text)
        if has_region_keywords(text[direction.end():end], reg):
            return True
    return False


def is_region_threat_target(text: str, reg: str) -> bool:
    """Check whether *reg* is the alert target, never merely the launch origin.

    A reply with named geography in its current update is classified on that
    update alone, preventing the replied-to post from routing stale geography.
    """
    text = normalize_routing_text(text)
    if not text or reg not in REGION_KEYWORDS:
        return False

    current = _reply_current_section(text)
    if current:
        text = current

    target_text = _without_origin_clauses(text)
    if not EVENT_WORD_RE.search(target_text):
        return False

    if has_region_keywords(_subject_text(target_text), reg):
        return True

    if _has_inbound_target(target_text, reg):
        return True

    # Posts without a formal subject/event dash commonly use prose such as
    # “опасность по БПЛА в Курской области”. Origin clauses are already gone.
    if not SUBJECT_SEPARATOR_RE.search(target_text) and has_region_keywords(target_text, reg):
        return True
    return False


def _is_foreign_item(item: str, reg: str) -> bool:
    if not item.strip():
        return False
    own_prefix = OWN_REGION_PREFIX_MAP[reg]
    for match in REGION_UNIT_RE.finditer(item):
        if own_prefix not in match.group(0).lower():
            return True
    return any(_matches_keyword(item, keyword) for keyword in FOREIGN_REGION_KEYWORDS)


def has_foreign_keywords(text: str, target_reg: str) -> bool:
    """Broad check for foreign geography outside origin clauses."""
    text = normalize_routing_text(text)
    current = _reply_current_section(text)
    if current:
        text = current
    text = _without_origin_clauses(text)
    return any(_is_foreign_item(item, target_reg) for item in re.split(r"[,;\n]+", text))


def has_foreign_subject_geography(text: str, target_reg: str) -> bool:
    """Check the subject/headline for foreign region names and known localities."""
    subject = _subject_text(normalize_routing_text(text))
    return any(_is_foreign_item(item, target_reg) for item in re.split(r"[,;]+", subject))


def strip_foreign_from_comma_lists(body: str, reg: str) -> str:
    """Drop a foreign comma-list segment as a unit while preserving target areas.

    For “Кромы, ... Орловская область” all of the unverified locality list is
    removed. In a mixed region-level announcement such as “Курская область,
    Белгородская область, Орловская область” the target region is retained.
    """
    body = normalize_routing_text(body).strip()
    if not body or reg not in REGION_KEYWORDS:
        return body

    separator = re.search(r"\s+-\s+", body)
    if not separator:
        separator = re.search(r":\s*", body)
    if separator:
        subject, tail = body[:separator.start()], body[separator.end():]
        delimiter = body[separator.start():separator.end()]
    else:
        subject, tail, delimiter = body, "", ""

    pieces = re.split(r"(,\s*)", subject)
    kept: list[str] = []
    last_target_piece = -1
    for piece in pieces:
        if not piece or re.fullmatch(r",\s*", piece):
            kept.append(piece)
            continue
        if has_region_keywords(piece, reg):
            kept.append(piece)
            last_target_piece = len(kept) - 1
            continue
        if _is_foreign_item(piece, reg):
            if last_target_piece >= 0:
                kept = kept[:last_target_piece + 1]
            else:
                kept = []
            continue
        kept.append(piece)

    cleaned_subject = "".join(kept).strip(" ,")
    cleaned_subject = re.sub(r"\s{2,}", " ", cleaned_subject)
    if cleaned_subject and tail:
        return f"{cleaned_subject}{delimiter}{tail}".strip()
    if cleaned_subject:
        return cleaned_subject
    return tail.strip() if tail else ""

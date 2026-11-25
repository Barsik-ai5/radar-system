"""Offline routing regression tests. No bot, AI client, or Telegram API is loaded."""

import ast
from pathlib import Path

from source_map import is_map_announcement, mask_map_promotion
from radar_routing import (
    has_foreign_keywords,
    has_foreign_subject_geography,
    has_region_keywords,
    is_multi_region_drone_summary,
    is_region_threat_target,
    normalize_routing_text,
    strip_foreign_from_comma_lists,
)


CASES = [
    (
        "Орловская область с точкой вылета в Курской области",
        "Кромы, Шахово, Добрый, Знаменка, Хардиково, Орловская область – опасность по БПЛА от Поныри, Курская область.",
        {"KURSK": False, "BELGOROD": False},
    ),
    (
        "Орловские сёла без названия области, Курск указан только как точка вылета",
        "Кромы, Шахово, Добрый, Знаменка, Хардиково – опасность по БПЛА от Поныри, Курская область.",
        {"KURSK": False, "BELGOROD": False},
    ),
    (
        "Ульяновская область, Мордовия как точка вылета",
        "Ульяновская область – опасность по БПЛА от Республики Мордовия.",
        {"KURSK": False, "BELGOROD": False},
    ),
    (
        "Нижегородская область",
        "Нижегородская область – опасность по БПЛА.",
        {"KURSK": False, "BELGOROD": False},
    ),
    (
        "Воронежская область и местный маршрут",
        "Поворино, Воронежская область – прошёл БПЛА на Борисоглебск.",
        {"KURSK": False, "BELGOROD": False},
    ),
    (
        "Ответ с чужой географией не наследует регион исходного сообщения",
        "Исходное сообщение: Курская область – опасность по БПЛА от Поныри.\n\n"
        "Текущее обновление: Ещё пролёты БПЛА в направлении Новохопёрск, "
        "предварительно далее на Саратовскую область.",
        {"KURSK": False, "BELGOROD": False},
    ),
    (
        "Курская цель, Ивнянский район только в точке вылета",
        "Обоянь, Медвенка, Курск, Пристень, Солнцево, Тим, Щигры, Курская область – "
        "опасность по БПЛА от Ивнянского района.",
        {"KURSK": True, "BELGOROD": False},
    ),
    (
        "Курск и Халино",
        "г.Курск, аэ.Халино, Курская область – опасность по БПЛА.",
        {"KURSK": True, "BELGOROD": False},
    ),
    (
        "Белгородская цель, Суджанский район и Курская область в источнике",
        "Шебекино, Белгородский район, Белгородская область – опасность по БПЛА "
        "от Суджанского района, Курская область.",
        {"KURSK": False, "BELGOROD": True},
    ),
    (
        "Явный вектор движения в Курскую область",
        "Орловская область – фиксация БПЛА курсом на Железногорск, Курская область.",
        {"KURSK": True, "BELGOROD": False},
    ),
    (
        "Общий отбой направляется по каждой целевой области без чужих областей",
        "Курская область, Белгородская область, Орловская область – отбой опасности по БПЛА.",
        {"KURSK": True, "BELGOROD": True},
    ),
]

POST_PROCESSOR_REGEXES = {
    "RED_COMBAT_RE",
    "LIVE_URGENT_RE",
    "DIRECTION_RE",
    "MILITARY_CONTEXT_RE",
    "YELLOW_TRIGGER_RE",
    "NEGATED_CLEAR_RE",
    "POSITIVE_CLEAR_RE",
    "STRIP_CLEAR_CLAUSE_RE",
    "NEGATED_THREAT_RE",
    "REFUTED_EVENT_RE",
    "ACTIVE_REGIME_RE",
    "RETROSPECTIVE_SUMMARY_RE",
    "DAMAGE_SUMMARY_MARKERS_RE",
    "EVENT_WORD_RE",
    "SIGNATURE_LINE_PATTERNS",
    "INLINE_SIGNATURES_RE",
}


def load_post_processor():
    """Load the production cleanup/signal function without importing the bot."""
    source_path = Path(__file__).with_name("main.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"clean_source_text", "post_process_region_text"}:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in POST_PROCESSOR_REGEXES
            for target in node.targets
        ):
            selected.append(node)

    namespace = {
        "re": __import__("re"),
        "normalize_routing_text": normalize_routing_text,
        "strip_foreign_from_comma_lists": strip_foreign_from_comma_lists,
        "has_region_keywords": has_region_keywords,
        "has_foreign_keywords": has_foreign_keywords,
        "is_multi_region_drone_summary": is_multi_region_drone_summary,
        "is_map_announcement": is_map_announcement,
        "mask_map_promotion": mask_map_promotion,
        "BOTS_CONFIG": {
            "KURSK": {"short_region": "Курская область"},
            "BELGOROD": {"short_region": "Белгородская область"},
        },
    }
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["clean_source_text"], namespace["post_process_region_text"]


def run() -> None:
    clean_source, post_process = load_post_processor()
    for index, (description, text, expected) in enumerate(CASES, start=1):
        actual = {
            region: is_region_threat_target(text, region)
            for region in ("KURSK", "BELGOROD")
        }
        assert actual == expected, (
            f"Кейс {index}: {description}\n"
            f"Ожидалось: {expected}\nПолучено: {actual}"
        )

        if index in (1, 2):
            for region in ("KURSK", "BELGOROD"):
                cleaned = strip_foreign_from_comma_lists(text, region)
                assert "Кромы" not in cleaned and "Шахово" not in cleaned, (
                    f"Кейс {index}: чужие населённые пункты остались в сегменте: {cleaned}"
                )

        if index == 10:
            cleaned = strip_foreign_from_comma_lists(text, "KURSK")
            assert "Орловская область" not in cleaned
            assert "курсом на Железногорск" in cleaned

        if index == 11:
            for region, expected_subject in (
                ("KURSK", "Курская область"),
                ("BELGOROD", "Белгородская область"),
            ):
                cleaned = strip_foreign_from_comma_lists(text, region)
                assert cleaned.startswith(expected_subject)
                assert "Орловская область" not in cleaned
                assert has_foreign_subject_geography(cleaned, region) is False
                print(f"PASS {index:02}/11 {region}: {cleaned}")
        else:
            statuses = ", ".join(
                f"{region}={'ОТПРАВИТЬ' if actual[region] else 'ИГНОР'}"
                for region in ("KURSK", "BELGOROD")
            )
            print(f"PASS {index:02}/11 {description}: {statuses}")

    for case_index, region, expected_light in (
        (7, "KURSK", "🟡"),
        (8, "KURSK", "🟡"),
        (9, "BELGOROD", "🟡"),
        (10, "KURSK", "🔴"),
        (11, "KURSK", "🟢"),
        (11, "BELGOROD", "🟢"),
    ):
        processed = post_process(region, CASES[case_index - 1][1])
        assert processed.startswith(expected_light), (
            f"Кейс {case_index}: ожидался сигнал {expected_light}, получено: {processed}"
        )
        assert is_region_threat_target(processed, region), (
            f"Кейс {case_index}: обработанный текст не прошел финальный валидатор: {processed}"
        )
        if case_index == 10:
            assert "Орловская область" not in processed
            assert "Железногорск" in processed and "Курская область" in processed
        if case_index == 11:
            assert "Орловская область" not in processed
            assert has_foreign_subject_geography(processed, region) is False
        print(f"PASS Финальный фильтр кейса {case_index:02}/11 {region}: {processed}")

    origin_only = CASES[2][1]
    ai_hallucination = "🟡 Курская область: опасность по БПЛА от Республики Мордовия."
    assert not is_region_threat_target(origin_only, "KURSK")
    assert is_region_threat_target(ai_hallucination, "KURSK")
    assert not (
        is_region_threat_target(origin_only, "KURSK")
        and is_region_threat_target(ai_hallucination, "KURSK")
    ), "Финальный source/output gate должен блокировать выдуманный ИИ регион."
    print("PASS Финальный фильтр блокирует регион, добавленный ИИ без исходной цели")

    normalized = normalize_routing_text("Курская\u200b область – тревога")
    assert normalized == "Курская область - тревога"
    print("PASS Нормализация: невидимый символ удалён, en-dash приведён к стандарту")
    message = "🟡 Курская область. Плановая проверка систем оповещения."
    for footer in (
        " Карта опасностей – radar-vrv.ru",
        "\n🗺️ Карта активных угроз:\nhttps://www.radar-vrv.ru/map?view=all",
        " | КАРТА ОПАСНОСТЕЙ",
    ):
        assert clean_source(message + footer) == clean_source(message)
        assert post_process("KURSK", message + footer) == post_process("KURSK", message)
    map_post = "🔴 Карта активных угроз от радара ВРВ\nСведения за последние 20 минут."
    assert clean_source(map_post) == ""
    assert post_process("KURSK", map_post) == ""
    print("PASS Ссылка на карту удаляется до и после обработки, отдельный пост с картой пропускается")

    overnight = (
        "⚪ В течение прошедшей ночи с 20.00 мск 20 ноября до 08.00 мск 21 ноября "
        "дежурными силами ПВО перехвачены и уничтожены 275 украинских "
        "беспилотных летательных аппаратов самолетного типа над территориями "
    )
    summaries = (
        overnight + "Белгородской, Курской, Ленинградской и Московской областей.",
        overnight + "... Ленинградской",
        "За сутки сбито 18 БПЛА над территориями Курской области и Республики Крым.",
        "Сводка за ночь: 18 БПЛА уничтожены.\n"
        "– 12 над Ленинградской областью,\n– 6 над Псковской областью.",
        "За ночь над территорией России уничтожено 18 БПЛА.",
    )
    for summary in summaries:
        assert clean_source(summary) == "", summary

    single_region_posts = (
        "Над территорией Ленинградской области сбито 49 БПЛА. Боевая работа продолжается.",
        "По сообщению Минобороны России, за ночь сбито 3 БПЛА над территориями "
        "Выборгского и Кингисеппского районов Ленинградской области.",
        "3 БПЛА сбиты в Ленинградской области. Ленинградская область сообщает об итогах.",
        "Над Белгородской областью сбито 2 БПЛА, прибывших со стороны Курской области.",
        "Курская область, Белгородская область, Орловская область – отбой опасности по БПЛА.",
    )
    for regional_post in single_region_posts:
        assert clean_source(regional_post) == regional_post.replace("–", "-"), regional_post
    print("PASS Межрегиональные сводки блокируются до Gemini, локальные сообщения и отбой проходят")
    print(f"Успешно: {len(CASES)}/{len(CASES)} сценариев")


if __name__ == "__main__":
    run()

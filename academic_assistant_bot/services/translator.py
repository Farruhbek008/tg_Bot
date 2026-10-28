import hashlib
import json
import logging
import re
from typing import Dict, List, Optional

import aiohttp
from sqlalchemy import select

from database.db import async_session_maker
from database.models import TranslationCache

logger = logging.getLogger(__name__)


# Полный словарь дисциплин ВУЗов (HEMIS: узбекский латиница/кириллица -> русский)
SUBJECT_DICTIONARY: Dict[str, str] = {
    "ma'lumotlar bazasi": "Базы данных",
    "malumotlar bazasi": "Базы данных",
    "ma'lumotlar bazasini boshqarish tizimlari": "Системы управления базами данных",
    "ma'lumotlar tuzilmasi va algoritmlar": "Структуры данных и алгоритмы",
    "algoritmlar va ma'lumotlar tuzilmasi": "Алгоритмы и структуры данных",
    "algoritmlarni loyihalash va tahlil qilish": "Проектирование и анализ алгоритмов",
    "kompyuter tarmoqlari": "Компьютерные сети",
    "kompyuter tarmoqlari va telekommunikatsiya": "Компьютерные сети и телекоммуникации",
    "diskret tuzilmalar": "Дискретные структуры",
    "diskret matematika": "Дискретная математика",
    "diskret matematika va matematik mantiq": "Дискретная математика и математическая логика",
    "chiziqli algebra": "Линейная алгебра",
    "chiziqli algebra va analitik geometriya": "Линейная алгебра и аналитическая геометрия",
    "elektronika va sxemalar": "Электроника и схемотехника",
    "elektronika va sxemotexnika": "Электроника и схемотехника",
    "raqamli sxemotexnika": "Цифровая схемотехника",
    "raqamli mantiqiy dizayn": "Цифровое логическое проектирование",
    "dasturlash": "Программирование",
    "dasturlash 1": "Программирование 1",
    "dasturlash 2": "Программирование 2",
    "dasturlash asoslari": "Основы программирования",
    "obyektga yo'naltirilgan dasturlash": "Объектно-ориентированное программирование",
    "amaliy dasturlash": "Прикладное программирование",
    "tizimli dasturlash": "Системное программирование",
    "web dasturlash": "Веб-программирование",
    "veb dasturlash": "Веб-программирование",
    "veb texnologiyalari": "Веб-технологии",
    "internet dasturlash": "Интернет-программирование",
    "sun'iy intellekt": "Искусственный интеллект",
    "sun'iy intellekt asoslari": "Основы искусственного интеллекта",
    "mashinali o'qitish": "Машинное обучение",
    "kiberxavfsizlik": "Кибербезопасность",
    "kiberxavfsizlik asoslari": "Основы кибербезопасности",
    "axborot xavfsizligi": "Информационная безопасность",
    "axborot xavfsizligi asoslari": "Основы информационной безопасности",
    "kriptografiya asoslari": "Основы криптографии",
    "operatsion tizimlar": "Операционные системы",
    "kompyuter arxitekturasi": "Архитектура компьютера",
    "kompyuter tashkil etilishi va arxitekturasi": "Организация и архитектура компьютера",
    "dasturiy injiniring": "Программная инженерия",
    "dasturiy ta'minot muhandisligi": "Программная инженерия",
    "dasturiy vositalarni testlash": "Тестирование программного обеспечения",
    "tizimli tahlil va loyihalash": "Системный анализ и проектирование",
    "mobil ilovalarni ishlab chiqish": "Разработка мобильных приложений",
    "bulutli hisoblash": "Облачные вычисления",
    "bulutli texnologiyalar": "Облачные технологии",
    "katta ma'lumotlar": "Большие данные (Big Data)",
    "kompyuter grafikasi": "Компьютерная графика",
    "kompyuter ko'rishi": "Компьютерное зрение",
    "mikroprotsessorlar": "Микропроцессоры",
    "mikrokontrollerlar": "Микроконтроллеры",
    "o'rnatilgan tizimlar": "Встраиваемые системы",
    "oliy matematika": "Высшая математика",
    "matematik analiz": "Математический анализ",
    "hisob": "Математический анализ (Calculus)",
    "kalyulus": "Математический анализ (Calculus)",
    "differensial tenglamalar": "Дифференциальные уравнения",
    "ehtimollar nazariyasi va matematik statistika": "Теория вероятностей и математическая статистика",
    "sonli usullar": "Численные методы",
    "fizika": "Физика",
    "umumiy fizika": "Общая физика",
    "amaliy fizika": "Прикладная физика",
    "xorijiy til": "Иностранный язык",
    "xorijiy til (ingliz tili)": "Иностранный язык (английский)",
    "ingliz tili": "Английский язык",
    "amaliy ingliz tili": "Практический английский язык",
    "akademik yozuv": "Академическое письмо",
    "o'zbek tili": "Узбекский язык",
    "rus tili": "Русский язык",
    "falsafa": "Философия",
    "o'zbekistonning eng yangi tarixi": "Новейшая история Узбекистана",
    "dinshunoslik": "Религиоведение",
    "ma'naviyat asoslari": "Основы духовности",
    "ekologiya": "Экология",
    "ekologiya va atrof-muhit muhofazasi": "Экология и охрана окружающей среды",
    "hayot faoliyati xavfsizligi": "Безопасность жизнедеятельности",
    "fuqarolik mudofaasi": "Гражданская оборона",
    "jismoniy tarbiya va sport": "Физическая культура и спорт",
    "iqtisodiyot nazariyasi": "Экономическая теория",
    "raqamli iqtisodiyot": "Цифровая экономика",
    "menejment asoslari": "Основы менеджмента",
    "axborot texnologiyalari": "Информационные технологии",
    "axborot tizimlari": "Информационные системы",
    "o'quv amaliyoti": "Учебная практика",
    "ishlab chiqarish amaliyoti": "Производственная практика",
    "diplom oldi amaliyoti": "Преддипломная практика",
    "bitiruv malakaviy ishi": "Выпускная квалификационная работа",
}

# Типы учебных занятий (HEMIS -> Русский)
TRAINING_TYPE_MAP: Dict[str, str] = {
    "ma'ruza": "Лекция",
    "maruza": "Лекция",
    "ma'ruza mashg'uloti": "Лекционное занятие",
    "маъруза": "Лекция",
    "amaliy": "Практическое занятие",
    "amaliy mashg'ulot": "Практическое занятие",
    "amaliy ish": "Практическая работа",
    "амалий": "Практическое занятие",
    "laboratoriya": "Лабораторная работа",
    "laboratoriya mashg'uloti": "Лабораторная работа",
    "laboratoriya ishi": "Лабораторная работа",
    "лаборатория": "Лабораторная работа",
    "seminar": "Семинар",
    "семинар": "Семинар",
    "mustaqil ta'lim": "Самостоятельная работа",
    "mustaqil ish": "Самостоятельная работа",
    "мустақил таълим": "Самостоятельная работа",
    "kurs ishi": "Курсовая работа",
    "kurs loyihasi": "Курсовой проект",
    "trening": "Тренинг",
    "konsultatsiya": "Консультация",
    "imtihon": "Экзамен",
    "nazorat": "Контрольная работа",
}

# Типы предметов (Majburiy / Tanlov -> Русский)
SUBJECT_TYPE_MAP: Dict[str, str] = {
    "majburiy": "Обязательный",
    "мажбурий": "Обязательный",
    "tanlov": "По выбору",
    "танлов": "По выбору",
    "fakultativ": "Факультативный",
    "факультатив": "Факультативный",
}

# Статусы заданий в HEMIS
TASK_STATUS_MAP: Dict[str, str] = {
    "yangi": "🆕 Новое",
    "new": "🆕 Новое",
    "topshirilmagan": "⏳ Не сдано",
    "berilgan": "📌 Выдано",
    "bajarilmoqda": "🔄 В процессе",
    "yuborilgan": "📤 Отправлено",
    "topshirilgan": "📤 Отправлено",
    "topshirildi": "📤 Отправлено",
    "bajarildi": "✅ Выполнено",
    "baholangan": "✅ Оценено",
    "baholandi": "✅ Оценено",
    "tekshirilgan": "✅ Проверено",
    "qaytarilgan": "🔄 На доработку",
    "qayta topshirish": "🔄 Пересдача",
    "muddati o'tgan": "⚠️ Срок истёк",
    "muddati tugagan": "⚠️ Срок истёк",
}

# Частые академические фразы и шаблоны в названиях тем, заданий и ресурсов
PHRASE_REPLACEMENTS: List[tuple[re.Pattern, str]] = [
    (
        re.compile(r"\b(\d+)\s*-\s*amaliy\s+(?:ish|topshiriq|mashg['’`]?ulot)\b", re.I),
        r"Практическая работа №\1",
    ),
    (
        re.compile(r"\b(\d+)\s*-\s*amaliy\b", re.I),
        r"Практическое занятие №\1",
    ),
    (
        re.compile(r"\b(\d+)\s*-\s*laboratoriya\s+(?:ishi|topshirig['’`]?i|mashg['’`]?uloti)\b", re.I),
        r"Лабораторная работа №\1",
    ),
    (
        re.compile(r"\b(\d+)\s*-\s*(?:laboratoriya|lab)\b", re.I),
        r"Лабораторная работа №\1",
    ),
    (
        re.compile(r"\b(\d+)\s*-\s*mustaqil\s+(?:ish|ta['’`]?lim|topshiriq)\b", re.I),
        r"Самостоятельная работа №\1",
    ),
    (
        re.compile(r"\b(\d+)\s*-\s*ma['’`]?ruza\b", re.I),
        r"Лекция №\1",
    ),
    (
        re.compile(r"\b(\d+)\s*-\s*mavzu\b", re.I),
        r"Тема №\1",
    ),
    (
        re.compile(r"\b(\d+)\s*-\s*topshiriq\b", re.I),
        r"Задание №\1",
    ),
    (
        re.compile(r"\b(\d+)\s*-\s*vazifa\b", re.I),
        r"Задача №\1",
    ),
    (
        re.compile(r"\b(\d+)\s*-\s*bino\b", re.I),
        r"Корпус \1",
    ),
    (
        re.compile(r"\b([A-Za-zА-Яа-я])\s*-\s*bino\b", re.I),
        r"Корпус \1",
    ),
    (
        re.compile(r"\b(\d+[A-Za-zА-Яа-я]?)\s*-\s*xona\b", re.I),
        r"ауд. \1",
    ),
    (
        re.compile(r"\bxona\b", re.I),
        "ауд.",
    ),
    (
        re.compile(r"\bbino\b", re.I),
        "корпус",
    ),
    (
        re.compile(r"\bma['’`]?ruzalar?\s+matni\b", re.I),
        "Текст лекций",
    ),
    (
        re.compile(r"\btaqdimot(?:lar)?\b", re.I),
        "Презентация",
    ),
    (
        re.compile(r"\bo['’`]?quv[\s-]+uslubiy\s+majmua\b", re.I),
        "Учебно-методический комплекс",
    ),
    (
        re.compile(r"\buslubiy\s+ko['’`]?rsatma(?:lar)?\b", re.I),
        "Методические указания",
    ),
    (
        re.compile(r"\buslubiy\s+qo['’`]?llanma\b", re.I),
        "Методическое пособие",
    ),
    (
        re.compile(r"\bo['’`]?quv\s+qo['’`]?llanma\b", re.I),
        "Учебное пособие",
    ),
    (
        re.compile(r"\bdarslik\b", re.I),
        "Учебник",
    ),
    (
        re.compile(r"\bsillabus\b", re.I),
        "Силлабус (рабочая программа)",
    ),
    (
        re.compile(r"\bfan\s+dasturi\b", re.I),
        "Программа дисциплины",
    ),
    (
        re.compile(r"\bishchi\s+o['’`]?quv\s+dastur(?:i)?\b", re.I),
        "Рабочая учебная программа",
    ),
    (
        re.compile(r"\bnazorat\s+savollari\b", re.I),
        "Контрольные вопросы",
    ),
    (
        re.compile(r"\btest\s+savollari\b", re.I),
        "Тестовые вопросы",
    ),
    (
        re.compile(r"\boraliq\s+nazorat\b", re.I),
        "Промежуточный контроль",
    ),
    (
        re.compile(r"\byakuniy\s+nazorat\b", re.I),
        "Итоговый контроль",
    ),
    (
        re.compile(r"\bjoriy\s+nazorat\b", re.I),
        "Текущий контроль",
    ),
    (
        re.compile(r"\bmustaqil\s+ta['’`]?lim\s+topshiriqlari\b", re.I),
        "Задания для самостоятельной работы",
    ),
    (
        re.compile(r"\bmustaqil\s+ish\s+mavzulari\b", re.I),
        "Темы самостоятельных работ",
    ),
    (
        re.compile(r"\bmustaqil\s+(?:ish|ta['’`]?lim)\b", re.I),
        "Самостоятельная работа",
    ),
    (
        re.compile(r"\bamaliy\s+mashg['’`]?ulot(?:lar)?\b", re.I),
        "Практические занятия",
    ),
    (
        re.compile(r"\blaboratoriya\s+ishi\b", re.I),
        "Лабораторная работа",
    ),
    (
        re.compile(r"\blaboratoriya\s+mashg['’`]?uloti\b", re.I),
        "Лабораторное занятие",
    ),
    (
        re.compile(r"\bglossariy\b", re.I),
        "Глоссарий",
    ),
    (
        re.compile(r"\basosiy\s+adabiyotlar\b", re.I),
        "Основная литература",
    ),
    (
        re.compile(r"\bqo['’`]?shimcha\s+adabiyotlar\b", re.I),
        "Дополнительная литература",
    ),
    (
        re.compile(r"\badabiyotlar\s+ro['’`]?yxati\b", re.I),
        "Список литературы",
    ),
    (
        re.compile(r"\btopshiriq(?:lar)?\b", re.I),
        "Задание",
    ),
    (
        re.compile(r"\bkatta\s+o['’`]?qituvchi\b", re.I),
        "ст. преподаватель",
    ),
    (
        re.compile(r"\bstajyor[\s-]+o['’`]?qituvchi\b", re.I),
        "преподаватель-стажёр",
    ),
    (
        re.compile(r"\bo['’`]?qituvchi\b", re.I),
        "преподаватель",
    ),
    (
        re.compile(r"\bkafedra\s+mudiri\b", re.I),
        "зав. кафедрой",
    ),
]

# Признаки того, что текст содержит узбекские слова и требует автоперевода
UZBEK_MARKER_REGEX = re.compile(
    r"(?:o['’`‘]|g['’`‘]|sh|ch|[ўқғҳЎҚҒҲ]|\b(?:va|uchun|bo['’`‘]yicha|haqida|turlari|usullari|asoslari|tizimi|tizimlari|tahlili|ishlash|yaratish|tuzish|hisoblash|o['’`‘]rganish|boshqarish|foydalanish|qo['’`‘]llash|ma['’`‘]lumot|tarmoq|dastur|fani|fanidan|amaliy|mustaqil|ma['’`‘]ruza|mavzu|vazifa|topshiriq|nazorat|jadval|qism|bo['’`‘]lim|kirish|xulosa)\b)",
    re.IGNORECASE,
)


class AcademicTranslator:
    """
    Сервис полной русификации данных из системы HEMIS:
    1. Мгновенно переводит типовые дисциплины, типы занятий, аудитории, статусы и термины по словарю.
    2. Автоматически переводит произвольные узбекские названия тем, заданий и описаний на русский язык
       с сохранением в SQLite-кэш (`translation_cache`) и память.
    """

    def __init__(self) -> None:
        self._mem_cache: Dict[str, str] = {}
        self._cache_loaded: bool = False

    @staticmethod
    def _normalize_apostrophes(text: str) -> str:
        return (
            text.replace("’", "'")
            .replace("‘", "'")
            .replace("`", "'")
            .replace("ʻ", "'")
            .replace("ʼ", "'")
        )

    @staticmethod
    def _make_hash(text: str) -> str:
        return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()

    async def load_cache_from_db(self) -> None:
        if self._cache_loaded:
            return
        try:
            async with async_session_maker() as session:
                stmt = select(TranslationCache)
                result = await session.execute(stmt)
                for row in result.scalars().all():
                    self._mem_cache[row.source_hash] = row.translated_text
            self._cache_loaded = True
        except Exception as exc:
            logger.debug("Translation cache load note: %s", exc)

    def translate_training_type(self, raw_type: str) -> str:
        if not raw_type:
            return "Учебное занятие"
        norm = self._normalize_apostrophes(raw_type.strip()).lower()
        if norm in TRAINING_TYPE_MAP:
            return TRAINING_TYPE_MAP[norm]
        if any(k in norm for k in ("ma'ruza", "maruza", "маъруза", "лекц", "lecture")):
            return "Лекция"
        if any(k in norm for k in ("amaliy", "амалий", "практ", "practical")):
            return "Практическое занятие"
        if any(k in norm for k in ("laboratoriya", "лаборатор", "lab")):
            return "Лабораторная работа"
        if any(k in norm for k in ("seminar", "семинар")):
            return "Семинар"
        if any(k in norm for k in ("mustaqil", "мустақил", "самостоят", "срс")):
            return "Самостоятельная работа"
        if any(k in norm for k in ("kurs", "курс")):
            return "Курсовая работа"
        return self.apply_dictionary_rules(raw_type)

    def translate_subject_type(self, raw_type: str) -> str:
        if not raw_type:
            return "Обязательный"
        norm = self._normalize_apostrophes(raw_type.strip()).lower()
        for k, v in SUBJECT_TYPE_MAP.items():
            if k in norm:
                return v
        return "Обязательный"

    def translate_status(self, raw_status: str) -> str:
        if not raw_status:
            return "📌 Активно"
        norm = self._normalize_apostrophes(raw_status.strip()).lower()
        for k, v in TASK_STATUS_MAP.items():
            if k in norm:
                return v
        # Убираем узбекские дубли в скобках, если были
        cleaned = re.sub(
            r"\b(?:Yangi|Yuborilgan|Baholangan|Topshirilmagan)\s*\(([^)]+)\)",
            r"\1",
            raw_status,
            flags=re.I,
        )
        return cleaned

    def translate_room(self, raw_room: str) -> str:
        if not raw_room:
            return "Не указана"
        norm = self._normalize_apostrophes(raw_room.strip())
        if norm.lower() in ("ko'rsatilmagan", "yo'q", "-", "none", "null"):
            return "Не указана"
        res = self.apply_dictionary_rules(norm)
        return res

    def apply_dictionary_rules(self, text: str) -> str:
        if not text:
            return ""
        norm = self._normalize_apostrophes(text.strip())
        lower_norm = norm.lower()

        # 1. Точное совпадение по словарю предметов
        if lower_norm in SUBJECT_DICTIONARY:
            return SUBJECT_DICTIONARY[lower_norm]

        # 2. Точное совпадение по типам занятий
        if lower_norm in TRAINING_TYPE_MAP:
            return TRAINING_TYPE_MAP[lower_norm]

        # 3. Замена известных предметов внутри строки (например, "Ma'lumotlar bazasi (tanlov)")
        result = norm
        for uz_subj, ru_subj in sorted(
            SUBJECT_DICTIONARY.items(), key=lambda x: len(x[0]), reverse=True
        ):
            if uz_subj in result.lower():
                pattern = re.compile(re.escape(uz_subj), re.IGNORECASE)
                result = pattern.sub(ru_subj, result)

        # 4. Применение шаблонных фраз
        for regex, replacement in PHRASE_REPLACEMENTS:
            result = regex.sub(replacement, result)

        return result.strip()

    def needs_remote_translation(self, text: str) -> bool:
        if not text or len(text.strip()) < 3:
            return False
        norm = self._normalize_apostrophes(text.strip())
        # Если в тексте есть латинские буквы (кроме аббревиатур вроде SQL, HTML, CSS, Python, C++, PHP, API, LAN, WAN, OSI, TCP, IP, PDF, IT, GPA, AI)
        cleaned_lat = re.sub(
            r"\b(?:SQL|NoSQL|HTML|CSS|JS|JavaScript|Python|Java|C\+\+|C#|PHP|API|REST|JSON|XML|HTTP|HTTPS|TCP|IP|UDP|DNS|DHCP|OSI|LAN|WAN|VPN|VLAN|SSH|FTP|SMTP|Linux|Windows|Git|Docker|UML|ERP|CRM|AI|ML|GPU|CPU|RAM|ROM|SSD|PDF|DOCX|PPTX|GPA|IT|IEEE|ISO|ASCII|UTF)\b",
            "",
            norm,
            flags=re.IGNORECASE,
        )
        if UZBEK_MARKER_REGEX.search(cleaned_lat):
            return True
        # Если больше 35% букв — латиница, скорее всего текст на узбекском (латиница)
        letters = [ch for ch in cleaned_lat if ch.isalpha()]
        if letters:
            latin_cnt = sum(1 for ch in letters if "a" <= ch.lower() <= "z")
            if latin_cnt >= 4 and (latin_cnt / len(letters)) > 0.35:
                return True
        return False

    async def _translate_via_gtx(self, text: str) -> Optional[str]:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "uz",
            "tl": "ru",
            "dt": "t",
            "q": text[:1500],
        }
        try:
            timeout = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if isinstance(data, list) and data and isinstance(data[0], list):
                            parts = [
                                seg[0]
                                for seg in data[0]
                                if isinstance(seg, list) and seg and seg[0]
                            ]
                            translated = "".join(parts).strip()
                            if translated:
                                return translated
        except Exception as exc:
            logger.debug("GTX translate note: %s", exc)
        return None

    async def translate_text(self, text: str) -> str:
        """
        Переводит любой текст из HEMIS на русский язык (сначала по словарю,
        затем при наличии узбекских слов — через переводчик с кэшированием в SQLite).
        """
        if not text or not text.strip():
            return ""

        await self.load_cache_from_db()

        # Сначала применяем мгновенные словарные и регулярные правила
        rule_based = self.apply_dictionary_rules(text)
        if not self.needs_remote_translation(rule_based):
            return rule_based

        h = self._make_hash(rule_based)
        if h in self._mem_cache:
            return self._mem_cache[h]

        translated = await self._translate_via_gtx(rule_based)
        if not translated:
            translated = rule_based

        # Пост-обработка: первая буква заглавная, если в оригинале была заглавная
        if translated and rule_based and rule_based[0].isupper():
            translated = translated[0].upper() + translated[1:]

        self._mem_cache[h] = translated
        try:
            async with async_session_maker() as session:
                stmt = select(TranslationCache).where(
                    TranslationCache.source_hash == h
                )
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if not existing:
                    session.add(
                        TranslationCache(
                            source_hash=h,
                            source_text=rule_based[:1000],
                            translated_text=translated[:1000],
                        )
                    )
                    await session.commit()
        except Exception as exc:
            logger.debug("Could not save translation cache: %s", exc)

        return translated


translator = AcademicTranslator()

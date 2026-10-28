import asyncio
import html
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
import pytz
from sqlalchemy import select

from config import settings
from database.db import async_session_maker
from database.models import UserSettings
from services.translator import translator

logger = logging.getLogger(__name__)

WEEKDAY_NAMES_RU = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]

FILE_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".zip",
    ".rar",
    ".7z",
    ".txt",
    ".jpg",
    ".jpeg",
    ".png",
)


class HemisAuthError(Exception):
    pass


class HemisClient:
    """
    Клиент для работы с системой HEMIS (student.fstu.uz и my.hemis.uz).
    Полностью русифицирует все получаемые данные (предметы, расписание, типы занятий,
    аудитории, задания, статусы и учебные ресурсы).
    Корректно извлекает реальные ссылки на файлы из REST API и веб-страниц HEMIS
    и скачивает их с поддержкой веб-сессии Yii2 и автоопределением формата файла.
    """

    def __init__(self) -> None:
        self.base_url = settings.hemis_base_url.rstrip("/")
        self.login = settings.hemis_login
        self.password = settings.hemis_password
        self.tz = pytz.timezone(settings.timezone)

        self._session: Optional[aiohttp.ClientSession] = None
        self._bearer_token: Optional[str] = None
        self._is_authenticated: bool = False
        self._web_authenticated: bool = False
        self._auth_lock = asyncio.Lock()

        # Кэш текущего семестра (например, code="13", number=3)
        self._default_semester_code: Optional[str] = None
        self._semesters_map: Dict[str, str] = {
            str(i): str(10 + i) for i in range(1, 9)
        }
        # Персональный выбор семестра пользователями Telegram: {user_id: "13"}
        self._user_semester: Dict[int, str] = {}
        self._db_loaded_users: set[int] = set()
        # Кэш точных ссылок на страницы ресурсов и заданий каждого предмета
        self._subject_links_cache: Dict[str, Dict[str, str]] = {}

    async def load_user_semester_from_db(self, user_id: int) -> None:
        if user_id <= 0 or user_id in self._db_loaded_users:
            return
        self._db_loaded_users.add(user_id)
        try:
            async with async_session_maker() as session:
                stmt = select(UserSettings).where(
                    UserSettings.user_id == user_id
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row and row.semester_code:
                    self._user_semester[user_id] = row.semester_code
        except Exception as exc:
            logger.debug("Load semester from DB note: %s", exc)

    async def save_user_semester_to_db(
        self, user_id: int, sem_code: str
    ) -> None:
        if user_id <= 0:
            return
        try:
            async with async_session_maker() as session:
                stmt = select(UserSettings).where(
                    UserSettings.user_id == user_id
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row:
                    row.semester_code = sem_code
                else:
                    session.add(
                        UserSettings(
                            user_id=user_id,
                            semester_code=sem_code,
                        )
                    )
                await session.commit()
        except Exception as exc:
            logger.debug("Save semester to DB note: %s", exc)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=settings.hemis_timeout)
            cookie_jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=cookie_jar,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,uz;q=0.5",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _login_web_session(self) -> bool:
        """
        Выполняет полноценный веб-вход через форму /dashboard/login (Yii2),
        сохраняя сессионные куки для доступа к страницам /education/resources и скачиванию файлов.
        """
        session = await self._get_session()
        login_page_url = f"{self.base_url}/dashboard/login"
        try:
            async with session.get(
                login_page_url,
                params={"l": "ru-RU"},
                headers={"Accept-Language": "ru-RU,ru;q=0.9"},
            ) as get_resp:
                final_get_url = str(get_resp.url)
                html_text = await get_resp.text()
                if (
                    "login" not in final_get_url.lower()
                    and "logout" in html_text.lower()
                ):
                    self._web_authenticated = True
                    self._is_authenticated = True
                    return True

                soup = BeautifulSoup(html_text, "html.parser")
                form = soup.find("form")
                form_data: Dict[str, str] = {}

                if form:
                    for inp in form.find_all("input"):
                        inp_name = inp.get("name")
                        if not inp_name:
                            continue
                        inp_type = (inp.get("type") or "text").lower()
                        if inp_type == "hidden":
                            form_data[inp_name] = inp.get("value", "")
                        elif (
                            "login" in inp_name.lower()
                            or "username" in inp_name.lower()
                        ):
                            form_data[inp_name] = str(self.login)
                        elif (
                            inp_type == "password"
                            or "password" in inp_name.lower()
                        ):
                            form_data[inp_name] = self.password

                if not any("login" in k.lower() for k in form_data):
                    csrf_input = soup.find("input", {"name": "_csrf-frontend"})
                    csrf_meta = soup.find("meta", {"name": "csrf-token"})
                    csrf_token = (
                        csrf_input["value"]
                        if csrf_input and csrf_input.has_attr("value")
                        else (
                            csrf_meta["content"]
                            if csrf_meta and csrf_meta.has_attr("content")
                            else ""
                        )
                    )
                    form_data = {
                        "_csrf-frontend": csrf_token,
                        "FormStudentLogin[login]": str(self.login),
                        "FormStudentLogin[password]": self.password,
                        "FormStudentLogin[rememberMe]": "1",
                    }

                post_url = login_page_url
                if form and form.get("action"):
                    post_url = urljoin(final_get_url, form.get("action"))

            async with session.post(
                post_url,
                data=form_data,
                headers={
                    "Referer": final_get_url,
                    "Origin": self.base_url,
                    "Accept-Language": "ru-RU,ru;q=0.9",
                },
                allow_redirects=True,
            ) as post_resp:
                final_url = str(post_resp.url)
                body_text = await post_resp.text()
                if (
                    "login" not in final_url.lower()
                    or "logout" in body_text.lower()
                    or "/education/" in body_text.lower()
                ):
                    self._web_authenticated = True
                    self._is_authenticated = True
                    logger.info("Authenticated via HEMIS Web Session.")
                    return True
        except Exception as exc:
            logger.debug("Web session auth note: %s", exc)
        return False

    async def authenticate(self, force: bool = False) -> bool:
        async with self._auth_lock:
            if self._is_authenticated and not force:
                return True

            session = await self._get_session()

            # 1. Пробуем REST API (student.fstu.uz или my.hemis.uz)
            for host in dict.fromkeys([self.base_url, "https://student.fstu.uz"]):
                api_login_url = f"{host.rstrip('/')}/rest/v1/auth/login"
                try:
                    async with session.post(
                        api_login_url,
                        json={
                            "login": str(self.login),
                            "password": self.password,
                        },
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            token = (
                                (data.get("data") or {}).get("token")
                                or data.get("token")
                            )
                            if token:
                                self._bearer_token = token
                                self._is_authenticated = True
                                logger.info(
                                    "Authenticated via HEMIS REST API (%s).",
                                    host,
                                )
                                break
                except Exception as exc:
                    logger.debug(
                        "REST API auth attempt on %s failed: %s", host, exc
                    )

            # 2. Обязательно выполняем веб-вход на student.fstu.uz для HTML-страниц и скачивания файлов
            await self._login_web_session()

            if not self._is_authenticated:
                raise HemisAuthError(
                    "Не удалось авторизоваться в HEMIS. Проверьте логин и пароль в .env."
                )

        if not self._default_semester_code:
            self._default_semester_code = "13"
            await self._detect_current_semester()

        return True

    def _build_headers(self, is_rest: bool = True) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept-Language": "ru-RU,ru;q=0.9,uz;q=0.5",
        }
        if is_rest and self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers

    async def _request_json(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        try:
            await self.authenticate()
        except Exception as exc:
            logger.warning("HEMIS auth error before JSON request: %s", exc)
            return {}

        session = await self._get_session()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        clean_params = {"l": "ru-RU"}
        for k, v in (params or {}).items():
            if v is not None:
                clean_params[k] = v

        for attempt in range(2):
            try:
                async with session.get(
                    url,
                    headers=self._build_headers(is_rest=True),
                    params=clean_params,
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        await self.authenticate(force=True)
                        continue
                    if resp.status >= 400:
                        return {}
                    data = await resp.json(content_type=None)
                    return data if isinstance(data, dict) else {}
            except Exception as exc:
                logger.debug("JSON request failed (%s): %s", endpoint, exc)
                if attempt == 0:
                    continue
        return {}

    async def _request_html(
        self, path_or_url: str, params: Optional[Dict[str, Any]] = None
    ) -> str:
        try:
            await self.authenticate()
            if not self._web_authenticated:
                await self._login_web_session()
        except Exception as exc:
            logger.warning("HEMIS auth error before HTML request: %s", exc)
            return ""

        session = await self._get_session()
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"

        clean_params = {"l": "ru-RU"}
        for k, v in (params or {}).items():
            if v is not None:
                clean_params[k] = v

        for attempt in range(2):
            try:
                async with session.get(
                    url,
                    headers=self._build_headers(is_rest=False),
                    params=clean_params,
                ) as resp:
                    final_url = str(resp.url)
                    if (
                        resp.status in (401, 403)
                        or "/dashboard/login" in final_url
                    ) and attempt == 0:
                        await self._login_web_session()
                        continue
                    if resp.status >= 400:
                        return ""
                    text = await resp.text()
                    if "FormStudentLogin" in text and attempt == 0:
                        await self._login_web_session()
                        continue
                    return text
            except Exception as exc:
                logger.debug("HTML request failed (%s): %s", path_or_url, exc)
        return ""

    # -------------------------------------------------------------------------
    # УПРАВЛЕНИЕ СЕМЕСТРАМИ
    # -------------------------------------------------------------------------
    async def _detect_current_semester(self) -> None:
        for i in range(1, 9):
            self._semesters_map[str(i)] = str(10 + i)

        # 1. Пробуем /rest/v1/account/me
        try:
            me_data = await self._request_json("/rest/v1/account/me")
            profile = me_data.get("data") or {}
            sem = profile.get("semester") or {}
            code = str(sem.get("code") or sem.get("id") or "")
            if code:
                self._default_semester_code = code
                logger.info(
                    "Detected current semester from profile: code=%s (%s)",
                    code,
                    sem.get("name"),
                )
                return
        except Exception as exc:
            logger.debug("Profile semester check note: %s", exc)

        # 2. Пробуем веб-страницу /education/subjects
        try:
            html_text = await self._request_html("/education/subjects")
            if html_text:
                soup = BeautifulSoup(html_text, "html.parser")
                for a in soup.select(
                    "ul.pagination li a, .btn-group a, a[href*='semester']"
                ):
                    href = a.get("href", "")
                    m = re.search(r"semester=(\d+)", href)
                    label = a.get_text(strip=True)
                    if m and label.isdigit():
                        self._semesters_map[label] = m.group(1)
                    parent_li = a.find_parent("li")
                    classes = " ".join(
                        (a.get("class") or [])
                        + ((parent_li.get("class") if parent_li else []) or [])
                    )
                    if (
                        "active" in classes
                        or "btn-success" in classes
                        or "bg-green" in classes
                    ) and m:
                        self._default_semester_code = m.group(1)
        except Exception as exc:
            logger.debug("HTML semester detection note: %s", exc)

        if not self._default_semester_code:
            self._default_semester_code = "13"

    def get_user_semester_code(self, user_id: int) -> str:
        return (
            self._user_semester.get(user_id)
            or self._default_semester_code
            or "13"
        )

    def get_user_semester_number(self, user_id: int) -> int:
        code = self.get_user_semester_code(user_id)
        for num_str, c_val in self._semesters_map.items():
            if c_val == code:
                return int(num_str)
        if code.isdigit() and int(code) > 10:
            return int(code) - 10
        return int(code) if code.isdigit() else 3

    def set_user_semester_by_number(self, user_id: int, sem_num: int) -> str:
        code = self._semesters_map.get(str(sem_num), str(10 + sem_num))
        self._user_semester[user_id] = code
        return code

    @staticmethod
    def _matches_semester(
        row: Dict[str, Any], target_code: str, target_num: int
    ) -> bool:
        sem = (
            row.get("semester")
            or row.get("_semester")
            or row.get("educationSemester")
        )
        if not sem:
            return True
        if isinstance(sem, dict):
            code = str(sem.get("code") or sem.get("id") or "")
            name = str(sem.get("name") or "")
            if code and (code == target_code or code == str(target_num)):
                return True
            if f"{target_num}-" in name or f"{target_num} " in name:
                return True
            return False
        sem_str = str(sem)
        return sem_str in (target_code, str(target_num))

    # -------------------------------------------------------------------------
    # ПРЕДМЕТЫ ВЫБРАННОГО СЕМЕСТРА (С ПОЛНЫМ ПЕРЕВОДОМ НА РУССКИЙ)
    # -------------------------------------------------------------------------
    async def get_semester_subjects(
        self, user_id: int = 0
    ) -> List[Dict[str, Any]]:
        await self.load_user_semester_from_db(user_id)
        sem_code = self.get_user_semester_code(user_id)
        sem_num = self.get_user_semester_number(user_id)
        subjects_map: Dict[str, Dict[str, Any]] = {}

        for ep in [
            "/rest/v1/education/subject-list",
            "/rest/v1/education/subjects",
            "/rest/v1/education/performance",
        ]:
            data = await self._request_json(ep, params={"semester": sem_code})
            rows = data.get("data") or []
            if isinstance(rows, list) and rows:
                for r in rows:
                    if not isinstance(r, dict) or not self._matches_semester(
                        r, sem_code, sem_num
                    ):
                        continue
                    subj_obj = (
                        r.get("subject") or r.get("curriculumSubject") or r
                    )
                    subj_id = str(
                        subj_obj.get("id")
                        or r.get("subject_id")
                        or r.get("id")
                        or ""
                    )
                    curriculum_subj_id = str(r.get("id") or "")
                    raw_subj_name = str(
                        subj_obj.get("name") or r.get("name") or ""
                    ).strip()
                    if not raw_subj_name:
                        continue
                    ru_subj_name = await translator.translate_text(raw_subj_name)
                    raw_type = (r.get("subjectType") or {}).get(
                        "name", "Обязательный"
                    )
                    ru_type = translator.translate_subject_type(raw_type)

                    key = subj_id or raw_subj_name.lower()
                    entry = subjects_map.setdefault(
                        key,
                        {
                            "id": subj_id or ru_subj_name,
                            "curriculum_id": curriculum_subj_id,
                            "name": ru_subj_name,
                            "raw_name": raw_subj_name,
                            "type": ru_type,
                            "load": r.get("total_acload")
                            or r.get("acload")
                            or 180,
                            "credit": r.get("credit") or 6.0,
                            "resources_count": int(
                                r.get("resources_count")
                                or r.get("resourceCount")
                                or 0
                            ),
                            "tasks_count": int(
                                r.get("tasks_count") or r.get("taskCount") or 0
                            ),
                            "submitted_tasks": int(
                                r.get("submitted_tasks") or 0
                            ),
                            "overall_score": r.get("overallScore") or 0,
                            "max_score": r.get("max_ball") or 100,
                            "resources_url": "",
                            "tasks_url": "",
                        },
                    )
                    if r.get("overallScore") is not None:
                        entry["overall_score"] = r.get("overallScore")

        try:
            html_text = await self._request_html(
                "/education/subjects", params={"semester": sem_code}
            )
            if html_text:
                soup = BeautifulSoup(html_text, "html.parser")
                for box in soup.select(".box, .card, .panel"):
                    title_el = box.select_one(
                        ".box-title, .card-title, h3, h4"
                    )
                    if not title_el:
                        continue
                    raw_subj_name = title_el.get_text(" ", strip=True)
                    if not raw_subj_name or len(raw_subj_name) < 3:
                        continue
                    ru_subj_name = await translator.translate_text(raw_subj_name)

                    res_link = box.select_one("a[href*='resource']")
                    task_link = box.select_one("a[href*='task']")
                    res_href = (
                        urljoin(f"{self.base_url}/", res_link["href"].lstrip("/"))
                        if res_link and res_link.get("href")
                        else ""
                    )
                    task_href = (
                        urljoin(
                            f"{self.base_url}/", task_link["href"].lstrip("/")
                        )
                        if task_link and task_link.get("href")
                        else ""
                    )

                    subj_id = ""
                    for link in [
                        res_link,
                        task_link,
                        *box.find_all("a", href=True),
                    ]:
                        if not link:
                            continue
                        href = link.get("href", "")
                        m_subj = re.search(r"subject=(\d+)", href)
                        if m_subj:
                            subj_id = m_subj.group(1)
                            break

                    res_count = 0
                    tasks_str = "0 / 0"
                    for li in box.select("li, tr, .list-group-item"):
                        txt = li.get_text(" ", strip=True)
                        badge = li.select_one(
                            ".badge, .label, span.pull-right"
                        )
                        badge_txt = badge.get_text(strip=True) if badge else ""
                        if "Resurslar" in txt or "Ресурс" in txt:
                            m_num = re.search(r"(\d+)", badge_txt or txt)
                            if m_num:
                                res_count = int(m_num.group(1))
                        elif "Topshiriqlar" in txt or "Задани" in txt:
                            tasks_str = badge_txt or "0 / 0"

                    key = subj_id or raw_subj_name.lower()
                    existing = subjects_map.get(key)
                    if not existing:
                        for _, v in subjects_map.items():
                            if v["name"].lower() == ru_subj_name.lower() or v.get(
                                "raw_name", ""
                            ).lower() == raw_subj_name.lower():
                                existing = v
                                if subj_id:
                                    existing["id"] = subj_id
                                break
                    if existing:
                        if res_count:
                            existing["resources_count"] = res_count
                        existing["tasks_badge"] = tasks_str
                        if res_href:
                            existing["resources_url"] = res_href
                        if task_href:
                            existing["tasks_url"] = task_href
                    else:
                        subjects_map[key] = {
                            "id": subj_id or ru_subj_name,
                            "curriculum_id": "",
                            "name": ru_subj_name,
                            "raw_name": raw_subj_name,
                            "type": "Обязательный",
                            "load": 180,
                            "credit": 6.0,
                            "resources_count": res_count,
                            "tasks_badge": tasks_str,
                            "overall_score": 0,
                            "max_score": 100,
                            "resources_url": res_href,
                            "tasks_url": task_href,
                        }

                    if subj_id and (res_href or task_href):
                        self._subject_links_cache[f"{sem_code}_{subj_id}"] = {
                            "resources_url": res_href,
                            "tasks_url": task_href,
                        }
        except Exception as exc:
            logger.debug("HTML subjects parse note: %s", exc)

        if not subjects_map:
            sched = await self._fetch_schedule_raw(semester_code=sem_code)
            for item in sched:
                s_name = item.get("subject", "").strip()
                s_id = item.get("subject_id", "") or s_name
                if s_name and s_name not in subjects_map:
                    subjects_map[s_name] = {
                        "id": s_id,
                        "curriculum_id": "",
                        "name": s_name,
                        "raw_name": s_name,
                        "type": "Обязательный",
                        "load": 180,
                        "credit": 6.0,
                        "resources_count": 0,
                        "tasks_badge": "—",
                        "overall_score": 0,
                        "max_score": 100,
                        "resources_url": "",
                        "tasks_url": "",
                    }

        return list(subjects_map.values())

    # -------------------------------------------------------------------------
    # РАСПИСАНИЕ (На сегодня / завтра / неделю с переводом на русский)
    # -------------------------------------------------------------------------
    async def get_schedule(
        self, target_date: Optional[date] = None, user_id: int = 0
    ) -> List[Dict[str, str]]:
        await self.load_user_semester_from_db(user_id)
        if target_date is None:
            target_date = datetime.now(self.tz).date()

        sem_code = self.get_user_semester_code(user_id)
        lessons = await self._fetch_schedule_raw(semester_code=sem_code)
        target_str = target_date.strftime("%Y-%m-%d")
        target_ru = target_date.strftime("%d.%m.%Y")

        day_lessons = [
            item
            for item in lessons
            if item.get("iso_date") == target_str
            or item.get("date") == target_ru
        ]
        day_lessons.sort(key=lambda x: x.get("time", ""))
        return day_lessons

    async def get_week_schedule(
        self, week_offset: int = 0, user_id: int = 0
    ) -> Tuple[date, date, Dict[str, List[Dict[str, str]]]]:
        await self.load_user_semester_from_db(user_id)
        now_date = datetime.now(self.tz).date() + timedelta(weeks=week_offset)
        start_of_week = now_date - timedelta(days=now_date.weekday())
        end_of_week = start_of_week + timedelta(days=6)

        sem_code = self.get_user_semester_code(user_id)
        lessons = await self._fetch_schedule_raw(semester_code=sem_code)

        by_date: Dict[date, List[Dict[str, str]]] = {}
        for item in lessons:
            iso_str = item.get("iso_date")
            if not iso_str:
                continue
            try:
                d = datetime.strptime(iso_str[:10], "%Y-%m-%d").date()
                if start_of_week <= d <= end_of_week:
                    by_date.setdefault(d, []).append(item)
            except ValueError:
                continue

        grouped: Dict[str, List[Dict[str, str]]] = {}
        for d in sorted(by_date.keys()):
            day_items = sorted(by_date[d], key=lambda x: x.get("time", ""))
            weekday_ru = WEEKDAY_NAMES_RU[d.weekday()]
            day_label = f"{weekday_ru} ({d.strftime('%d.%m.%Y')})"
            grouped[day_label] = day_items

        return start_of_week, end_of_week, grouped

    async def _fetch_schedule_raw(
        self, semester_code: Optional[str] = None
    ) -> List[Dict[str, str]]:
        for params in [
            {"semester": semester_code, "limit": 200},
            {"semester": semester_code},
            {"limit": 200},
        ]:
            try:
                payload = await self._request_json(
                    "/rest/v1/education/schedule", params=params
                )
                items = payload.get("data", [])
                if isinstance(items, dict):
                    items = items.get("items") or items.get("schedule") or []
                if isinstance(items, list) and items:
                    parsed: List[Dict[str, str]] = []
                    for row in items:
                        if not isinstance(row, dict):
                            continue
                        ts = row.get("lesson_date")
                        if isinstance(ts, (int, float)):
                            dt = datetime.fromtimestamp(ts, self.tz).date()
                            iso_date = dt.strftime("%Y-%m-%d")
                            ru_date = dt.strftime("%d.%m.%Y")
                        else:
                            iso_date = str(ts or "")[:10]
                            try:
                                dt = datetime.strptime(
                                    iso_date, "%Y-%m-%d"
                                ).date()
                                ru_date = dt.strftime("%d.%m.%Y")
                            except ValueError:
                                ru_date = iso_date

                        pair = row.get("lessonPair") or {}
                        start_t = pair.get("start_time", "")
                        end_t = pair.get("end_time", "")
                        time_str = (
                            f"{start_t} - {end_t}".strip(" -")
                            or translator.apply_dictionary_rules(
                                str(pair.get("name", "Пара"))
                            )
                        )
                        subj_obj = row.get("subject") or {}
                        raw_subj = str(subj_obj.get("name") or "Предмет").strip()
                        ru_subj = await translator.translate_text(raw_subj)

                        raw_room = str(
                            (row.get("auditorium") or {}).get(
                                "name", "Не указана"
                            )
                        )
                        ru_room = translator.translate_room(raw_room)

                        raw_ltype = str(
                            (row.get("trainingType") or {}).get(
                                "name", "Учебное занятие"
                            )
                        )
                        ru_ltype = translator.translate_training_type(raw_ltype)

                        raw_teacher = str(
                            (row.get("employee") or {}).get("name", "Не указан")
                        )
                        ru_teacher = translator.apply_dictionary_rules(
                            raw_teacher
                        )

                        parsed.append(
                            {
                                "iso_date": iso_date,
                                "date": ru_date,
                                "time": time_str,
                                "subject": ru_subj,
                                "subject_id": str(subj_obj.get("id") or ""),
                                "room": ru_room,
                                "lesson_type": ru_ltype,
                                "teacher": ru_teacher,
                            }
                        )
                    if parsed:
                        return parsed
            except Exception as exc:
                logger.debug("REST schedule note: %s", exc)

        # HTML-фолбэк: парсинг страницы /education/time-table
        try:
            html_text = await self._request_html(
                "/education/time-table", params={"semester": semester_code}
            )
            if html_text:
                soup = BeautifulSoup(html_text, "html.parser")
                parsed_html: List[Dict[str, str]] = []
                for box in soup.select(".box, .panel, .card"):
                    header_el = box.select_one(
                        ".box-title, .panel-title, .card-title, h3, h4"
                    )
                    if not header_el:
                        continue
                    header_txt = header_el.get_text(" ", strip=True)
                    m_date = re.search(
                        r"(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})", header_txt
                    )
                    if not m_date:
                        continue
                    d_raw = m_date.group(1)
                    try:
                        if "." in d_raw:
                            dt = datetime.strptime(d_raw, "%d.%m.%Y").date()
                        else:
                            dt = datetime.strptime(d_raw, "%Y-%m-%d").date()
                        iso_date = dt.strftime("%Y-%m-%d")
                        ru_date = dt.strftime("%d.%m.%Y")
                    except ValueError:
                        continue

                    for li in box.select("li.list-group-item, tr"):
                        txt = li.get_text(" | ", strip=True)
                        if not txt:
                            continue
                        m_time = re.search(
                            r"(\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}|\d{1,2}:\d{2})",
                            txt,
                        )
                        time_str = m_time.group(1) if m_time else "По расписанию"
                        parts = [
                            p.strip()
                            for p in txt.split("|")
                            if p.strip() and p.strip() != time_str
                        ]
                        raw_subj = parts[0] if parts else "Учебное занятие"
                        ru_subj = await translator.translate_text(raw_subj)
                        raw_ltype = parts[1] if len(parts) > 1 else "Занятие"
                        ru_ltype = translator.translate_training_type(raw_ltype)
                        raw_room = parts[2] if len(parts) > 2 else "Не указана"
                        ru_room = translator.translate_room(raw_room)
                        raw_teacher = parts[3] if len(parts) > 3 else "Не указан"
                        ru_teacher = translator.apply_dictionary_rules(
                            raw_teacher
                        )

                        parsed_html.append(
                            {
                                "iso_date": iso_date,
                                "date": ru_date,
                                "time": time_str,
                                "subject": ru_subj,
                                "subject_id": "",
                                "room": ru_room,
                                "lesson_type": ru_ltype,
                                "teacher": ru_teacher,
                            }
                        )
                if parsed_html:
                    return parsed_html
        except Exception as exc:
            logger.debug("HTML schedule parse note: %s", exc)

        return []

    @staticmethod
    def format_schedule_list(lessons: List[Dict[str, str]], title: str) -> str:
        safe_title = html.escape(title)
        if not lessons:
            return f"📅 <b>{safe_title}</b>\n\n🎉 В этот день учебных пар по расписанию нет!"

        lines = [f"📅 <b>{safe_title}</b>\n"]
        for idx, item in enumerate(lessons, start=1):
            t_str = html.escape(item.get("time", ""))
            subj_str = html.escape(item.get("subject", "Предмет"))
            room_str = html.escape(item.get("room", "Не указана"))
            ltype_str = html.escape(item.get("lesson_type", "Занятие"))
            teacher_str = html.escape(item.get("teacher", "Не указан"))
            lines.append(
                f"<b>{idx}. 🕒 {t_str}</b> — <b>{subj_str}</b>\n"
                f"   🏛 Аудитория: <code>{room_str}</code> | 📚 {ltype_str}\n"
                f"   👨‍🏫 Преподаватель: {teacher_str}"
            )
        return "\n\n".join(lines)

    # -------------------------------------------------------------------------
    # ВСПОМОГАТЕЛЬНЫЙ ПАРСЕР ФАЙЛОВ ИЗ JSON И HTML HEMIS
    # -------------------------------------------------------------------------
    def _extract_files_from_json_row(
        self, row: Dict[str, Any], default_title: str
    ) -> List[Dict[str, str]]:
        """
        Рекурсивно извлекает все реальные ссылки на файлы из JSON-объекта ресурса или задания HEMIS.
        Никогда не выдумывает несуществующие URL.
        """
        found: List[Dict[str, str]] = []
        seen_u: set[str] = set()

        def _add_candidate(raw_url: str, raw_name: str = "") -> None:
            if not raw_url or not isinstance(raw_url, str):
                return
            u = raw_url.strip()
            if not u or u.startswith(("#", "javascript:")):
                return
            if not u.startswith(("http://", "https://")):
                if u.startswith("/") or any(
                    k in u.lower()
                    for k in (
                        "storage",
                        "upload",
                        "static",
                        "file",
                        "download",
                        *FILE_EXTENSIONS,
                    )
                ):
                    u = urljoin(f"{self.base_url}/", u.lstrip("/"))
                else:
                    return
            if u in seen_u:
                return
            seen_u.add(u)

            fn = (raw_name or "").strip()
            if not fn or fn.lower() in (
                "yuklab olish",
                "скачать",
                "fayl",
                "файл",
                "download",
            ):
                parsed_path = unquote(urlparse(u).path)
                base_n = parsed_path.rstrip("/").split("/")[-1]
                if any(base_n.lower().endswith(ext) for ext in FILE_EXTENSIONS):
                    fn = base_n
                else:
                    fn = f"{default_title}.pdf"
            found.append(
                {
                    "name": translator.apply_dictionary_rules(fn),
                    "url": u,
                }
            )

        def _walk(obj: Any) -> None:
            if isinstance(obj, dict):
                u_cand = (
                    obj.get("url")
                    or obj.get("download_url")
                    or obj.get("downloadUrl")
                    or obj.get("file_url")
                    or obj.get("link")
                    or obj.get("href")
                )
                if isinstance(u_cand, str) and u_cand.strip():
                    _add_candidate(
                        u_cand,
                        str(
                            obj.get("name")
                            or obj.get("filename")
                            or obj.get("title")
                            or default_title
                        ),
                    )
                for k in ("file", "path", "src"):
                    val = obj.get(k)
                    if isinstance(val, str) and val.strip():
                        _add_candidate(
                            val,
                            str(
                                obj.get("name")
                                or obj.get("filename")
                                or default_title
                            ),
                        )
                for k, v in obj.items():
                    if k not in ("subject", "semester", "trainingType", "employee"):
                        _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)

        _walk(row)

        # Также проверяем текстовые поля comment / description на наличие прямых http-ссылок
        for text_field in ("comment", "description", "content"):
            val = row.get(text_field)
            if isinstance(val, str) and "http" in val:
                for m in re.findall(r"https?://[^\s\"'<>]+", val):
                    _add_candidate(m, default_title)

        return found

    @staticmethod
    def _is_valid_html_resource_link(href: str, a_tag: Any) -> bool:
        """
        Отличает реальную ссылку на скачивание файла в таблице /education/resources
        от ссылок пагинации, сортировки и переключения семестров.
        """
        if not href:
            return False
        h_low = href.strip().lower()
        if not h_low or h_low.startswith(("#", "javascript:", "mailto:")):
            return False

        # Исключаем ссылки пагинации и сортировки таблиц Yii2
        if "sort=" in h_low or "page=" in h_low or "/education/subjects" in h_low:
            return False

        # Если ссылка ведет просто на страницу списка ресурсов (/education/resources?subject=123&semester=13)
        # без указания конкретного файла (id=, file=, download=, filename=), это вкладка фильтра, а не файл!
        if "/education/resources" in h_low and not any(
            param in h_low
            for param in ("id=", "file=", "download", "code=", "task=")
        ):
            return False

        # Проверяем явные признаки ссылки на скачивание файла
        if any(ext in h_low for ext in FILE_EXTENSIONS):
            return True
        if any(
            k in h_low
            for k in (
                "/storage/",
                "/upload",
                "/static/",
                "/files/",
                "download",
                "file",
                "id=",
            )
        ):
            return True
        if a_tag is not None:
            if a_tag.get("data-pjax") == "0" or a_tag.has_attr("download"):
                return True
            if a_tag.select_one(
                "i.fa-download, i.fa-cloud-download, i.fa-file, i.fa-paperclip, span.glyphicon-download"
            ):
                return True
            btn_text = a_tag.get_text(" ", strip=True).lower()
            if any(
                w in btn_text
                for w in ("yuklab", "скачать", "download", "fayl", "файл", "очиш", "ochish")
            ):
                return True
        if h_low.startswith(("http://", "https://")) and "student.fstu.uz" not in h_low:
            return True

        return False

    # -------------------------------------------------------------------------
    # ЗАДАНИЯ (С ПОЛНЫМ ПЕРЕВОДОМ НА РУССКИЙ)
    # -------------------------------------------------------------------------
    async def get_assignments(self, user_id: int = 0) -> List[Dict[str, Any]]:
        await self.load_user_semester_from_db(user_id)
        sem_code = self.get_user_semester_code(user_id)
        sem_num = self.get_user_semester_number(user_id)
        collected: List[Dict[str, Any]] = []
        seen_ids = set()

        async def _parse_task_row(
            row: Dict[str, Any], default_subject: str = "Дисциплина"
        ) -> Optional[Dict[str, Any]]:
            if not isinstance(row, dict):
                return None
            if not self._matches_semester(row, sem_code, sem_num):
                return None

            t_id = str(row.get("id") or row.get("task_id") or "")
            raw_subj_name = (
                (row.get("subject") or {}).get("name")
                or (row.get("curriculumSubject") or {}).get("name")
                or default_subject
            )
            subj_name = await translator.translate_text(str(raw_subj_name))

            raw_title = (
                row.get("name")
                or row.get("title")
                or (row.get("trainingType") or {}).get("name")
                or "Учебное задание"
            )
            title = await translator.translate_text(str(raw_title))

            raw_training_type = (
                (row.get("trainingType") or {}).get("name")
                or row.get("type")
                or ""
            )
            training_type = (
                translator.translate_training_type(str(raw_training_type))
                if raw_training_type
                else ""
            )

            deadline_ts = row.get("deadline") or row.get("attempt_limit")
            if isinstance(deadline_ts, (int, float)) and deadline_ts > 100000:
                deadline_str = datetime.fromtimestamp(
                    deadline_ts, self.tz
                ).strftime("%d.%m.%Y %H:%M")
            else:
                deadline_str = str(deadline_ts or "Не указан")
                deadline_str = translator.apply_dictionary_rules(deadline_str)

            status_obj = (
                row.get("taskStatus")
                or row.get("TaskStatus")
                or row.get("studentTaskActivity")
                or {}
            )
            if isinstance(status_obj, dict):
                status_name = status_obj.get("name") or ""
            else:
                status_name = str(status_obj or "")

            if not status_name:
                st_code = str(
                    row.get("_task_status") or row.get("status") or ""
                )
                if st_code in ("11", "new", "0"):
                    status_name = "🆕 Новое"
                elif st_code in ("12", "sent", "1"):
                    status_name = "📤 Отправлено"
                elif st_code in ("13", "graded", "2"):
                    status_name = "✅ Оценено"
                else:
                    status_name = "📌 Активно"
            else:
                status_name = translator.translate_status(status_name)

            files_list = self._extract_files_from_json_row(row, title)

            raw_desc = (
                row.get("comment")
                or row.get("description")
                or row.get("content")
                or ""
            )
            ru_desc = (
                await translator.translate_text(str(raw_desc))
                if raw_desc
                else ""
            )

            unique_key = t_id or f"{subj_name}_{title}_{deadline_str}"
            if unique_key in seen_ids:
                return None
            seen_ids.add(unique_key)

            return {
                "id": t_id or str(len(collected) + 1),
                "subject": subj_name,
                "title": title,
                "training_type": training_type,
                "description": ru_desc,
                "deadline": deadline_str,
                "max_ball": row.get("max_ball") or row.get("maxBall") or "—",
                "status": status_name,
                "files": files_list,
            }

        for ep in ["/rest/v1/education/task-list", "/rest/v1/education/tasks"]:
            for params in [
                {"semester": sem_code, "limit": 100},
                {"semester": sem_code},
                {},
            ]:
                payload = await self._request_json(ep, params=params)
                items = payload.get("data")
                if isinstance(items, dict):
                    items = items.get("items") or items.get("tasks") or []
                if isinstance(items, list) and items:
                    for r in items:
                        parsed_item = await _parse_task_row(r)
                        if parsed_item:
                            collected.append(parsed_item)
                    if collected:
                        break

        subjects = await self.get_semester_subjects(user_id=user_id)
        for subj in subjects:
            s_id = subj.get("id")
            s_name = subj.get("name", "Дисциплина")
            if s_id and str(s_id).isdigit():
                for ep in [
                    "/rest/v1/education/task-list",
                    "/rest/v1/education/tasks",
                    "/rest/v1/education/subject-detail",
                ]:
                    data = await self._request_json(
                        ep, params={"subject": s_id, "semester": sem_code}
                    )
                    raw_data = data.get("data")
                    rows = []
                    if isinstance(raw_data, list):
                        rows = raw_data
                    elif isinstance(raw_data, dict):
                        rows = (
                            raw_data.get("tasks")
                            or raw_data.get("taskList")
                            or raw_data.get("items")
                            or []
                        )
                    for r in rows:
                        parsed_item = await _parse_task_row(
                            r, default_subject=s_name
                        )
                        if parsed_item:
                            collected.append(parsed_item)

        if not collected:
            for subj in subjects:
                s_id = subj.get("id")
                s_name = subj.get("name", "Дисциплина")
                tasks_url = subj.get("tasks_url") or ""
                paths_to_check = (
                    [tasks_url]
                    if tasks_url
                    else ["/education/tasks", "/education/subject-tasks"]
                )
                for path in paths_to_check:
                    params = (
                        {}
                        if path.startswith("http")
                        else (
                            {"semester": sem_code, "subject": s_id}
                            if s_id and str(s_id).isdigit()
                            else {"semester": sem_code}
                        )
                    )
                    html_text = await self._request_html(path, params=params)
                    if not html_text:
                        continue
                    soup = BeautifulSoup(html_text, "html.parser")
                    for box in soup.select(".box, .card, .panel, table tbody tr"):
                        text_content = box.get_text(" ", strip=True)
                        if not any(
                            w in text_content
                            for w in (
                                "Muddati",
                                "Дедлайн",
                                "Topshiriq",
                                "Срок",
                                "Задани",
                            )
                        ):
                            continue
                        links = box.find_all("a", href=True)
                        files_found = []
                        for a in links:
                            href = a["href"]
                            if self._is_valid_html_resource_link(href, a):
                                full_u = urljoin(
                                    f"{self.base_url}/", href.lstrip("/")
                                )
                                raw_flabel = (
                                    a.get_text(" ", strip=True)
                                    or "Файл_задания.pdf"
                                )
                                files_found.append(
                                    {
                                        "name": translator.apply_dictionary_rules(
                                            raw_flabel
                                        ),
                                        "url": full_u,
                                    }
                                )
                        title_el = box.select_one(
                            "h3, h4, .box-title, strong, b"
                        )
                        raw_t_title = (
                            title_el.get_text(" ", strip=True)
                            if title_el
                            else "Учебное задание"
                        )
                        t_title = await translator.translate_text(raw_t_title)
                        ru_desc = await translator.translate_text(
                            text_content[:350]
                        )
                        key = f"{s_name}_{t_title}"
                        if key not in seen_ids:
                            seen_ids.add(key)
                            collected.append(
                                {
                                    "id": str(len(collected) + 1),
                                    "subject": s_name,
                                    "title": t_title,
                                    "training_type": "",
                                    "description": ru_desc,
                                    "deadline": "См. в описании",
                                    "max_ball": "—",
                                    "status": "📌 Активно",
                                    "files": files_found,
                                }
                            )

        return collected

    # -------------------------------------------------------------------------
    # УЧЕБНЫЕ РЕСУРСЫ ПО ПРЕДМЕТУ (С РЕАЛЬНЫМИ ССЫЛКАМИ И ПАРСИНГОМ HTML + REST)
    # -------------------------------------------------------------------------
    @staticmethod
    def classify_resource_type(training_type_name: str, title: str = "") -> str:
        combined = f"{training_type_name} {title}".lower()
        if any(
            w in combined
            for w in [
                "ma'ruza",
                "maruza",
                "ma’ruza",
                "маъруза",
                "лекц",
                "lecture",
            ]
        ):
            return "lecture"
        if any(
            w in combined
            for w in [
                "amaliy",
                "амалий",
                "практ",
                "семинар",
                "seminar",
                "practical",
            ]
        ):
            return "practical"
        if any(w in combined for w in ["laboratoriya", "лаборатор", "lab"]):
            return "lab"
        if any(
            w in combined
            for w in [
                "mustaqil",
                "мустақил",
                "самостоят",
                "срс",
                "independent",
            ]
        ):
            return "independent"
        return "other"

    async def get_subject_resources(
        self, subject_id: str, subject_name: str, user_id: int = 0
    ) -> List[Dict[str, Any]]:
        await self.load_user_semester_from_db(user_id)
        sem_code = self.get_user_semester_code(user_id)
        sem_num = self.get_user_semester_number(user_id)
        resources: List[Dict[str, Any]] = []
        seen_urls: set[str] = set()

        # 1. Пробуем получить ресурсы через REST API (/rest/v1/education/resources)
        # ВАЖНО: добавляем ТОЛЬКО те элементы, в которых реально присутствует ссылка на файл!
        for params in [
            {"subject": subject_id, "semester": sem_code, "limit": 200},
            {"subject": subject_id, "limit": 200},
            {"semester": sem_code, "limit": 200},
        ]:
            payload = await self._request_json(
                "/rest/v1/education/resources", params=params
            )
            items = payload.get("data") or []
            if isinstance(items, dict):
                items = items.get("items") or items.get("resources") or []
            if not isinstance(items, list) or not items:
                continue

            for idx, row in enumerate(items, start=1):
                if not isinstance(row, dict):
                    continue
                row_subj = row.get("subject") or row.get("curriculumSubject") or {}
                row_subj_id = str(
                    row_subj.get("id") or row.get("subject_id") or ""
                )
                row_subj_name = str(row_subj.get("name") or "").strip()
                row_subj_ru = (
                    await translator.translate_text(row_subj_name)
                    if row_subj_name
                    else ""
                )

                if (
                    str(subject_id).isdigit()
                    and row_subj_id
                    and row_subj_id != str(subject_id)
                ):
                    continue
                if (
                    row_subj_name
                    and subject_name
                    and row_subj_name.lower() != subject_name.lower()
                    and row_subj_ru.lower() != subject_name.lower()
                ):
                    if not (
                        str(subject_id).isdigit()
                        and row_subj_id == str(subject_id)
                    ):
                        continue
                if not self._matches_semester(row, sem_code, sem_num):
                    continue

                raw_title = str(
                    row.get("name")
                    or row.get("title")
                    or row.get("comment")
                    or f"Тема №{idx}"
                )
                raw_training_type = str(
                    (row.get("trainingType") or {}).get("name")
                    or (row.get("resourceType") or {}).get("name")
                    or "Учебный материал"
                )
                category = self.classify_resource_type(
                    raw_training_type, raw_title
                )
                ru_title = await translator.translate_text(raw_title)
                ru_training_type = translator.translate_training_type(
                    raw_training_type
                )

                files_extracted = self._extract_files_from_json_row(
                    row, ru_title
                )

                for f_obj in files_extracted:
                    if f_obj["url"] in seen_urls:
                        continue
                    seen_urls.add(f_obj["url"])
                    resources.append(
                        {
                            "id": str(row.get("id") or len(resources) + 1),
                            "subject": subject_name,
                            "title": ru_title,
                            "training_type": ru_training_type,
                            "category": category,
                            "file_name": f_obj["name"],
                            "url": f_obj["url"],
                        }
                    )

            if resources:
                break

        # 2. Веб-парсинг страницы /education/resources (основной источник реальных ссылок на скачивание в HEMIS)
        cached_links = self._subject_links_cache.get(
            f"{sem_code}_{subject_id}", {}
        )
        direct_res_url = cached_links.get("resources_url") or ""

        html_targets: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        if direct_res_url:
            html_targets.append((direct_res_url, None))
        if str(subject_id).isdigit():
            html_targets.append(
                (
                    "/education/resources",
                    {"subject": subject_id, "semester": sem_code},
                )
            )
            html_targets.append(
                ("/education/resources", {"subject": subject_id})
            )

        for target_path, target_params in html_targets:
            if resources:
                break
            html_text = await self._request_html(
                target_path, params=target_params
            )
            if not html_text:
                continue

            soup = BeautifulSoup(html_text, "html.parser")
            table_rows = soup.select("table tbody tr")

            for idx, tr in enumerate(table_rows, start=1):
                # Берем ТОЛЬКО прямые ячейки <td>, чтобы <span> внутри ячеек не сдвигал индексы колонок
                tds = tr.find_all("td")
                if not tds:
                    continue
                cols = [td.get_text(" ", strip=True) for td in tds]
                row_text = " ".join(cols)

                # Определяем название ресурса и тип занятия по колонкам таблицы HEMIS
                # Обычно: td[0] = №, td[1] = Название (или Предмет), td[2] = Комментарий / Тип занятия
                raw_t_title = (
                    cols[1]
                    if len(cols) > 1 and cols[1]
                    else (cols[0] if cols else f"Материал №{idx}")
                )
                raw_t_type = "Учебный материал"
                for c_txt in cols[2:]:
                    if any(
                        w in c_txt.lower()
                        for w in (
                            "ma'ruza",
                            "maruza",
                            "amaliy",
                            "laboratoriya",
                            "mustaqil",
                            "seminar",
                            "лекц",
                            "практ",
                            "лаб",
                            "самостоят",
                        )
                    ):
                        raw_t_type = c_txt
                        break
                if raw_t_type == "Учебный материал" and len(cols) > 2 and cols[2]:
                    raw_t_type = cols[2]

                for a in tr.find_all("a", href=True):
                    href = a.get("href", "").strip()
                    if not self._is_valid_html_resource_link(href, a):
                        continue

                    full_u = urljoin(f"{self.base_url}/", href.lstrip("/"))
                    if full_u in seen_urls:
                        continue
                    seen_urls.add(full_u)

                    a_text = a.get_text(" ", strip=True)
                    a_title_attr = (
                        a.get("download") or a.get("title") or ""
                    ).strip()
                    ru_t_title = await translator.translate_text(raw_t_title)
                    ru_t_type = translator.translate_training_type(raw_t_type)
                    cat = self.classify_resource_type(raw_t_type, row_text)

                    # Формируем осмысленное имя файла с сохранением расширения
                    cand_fname = a_title_attr or a_text
                    if not cand_fname or cand_fname.lower() in (
                        "yuklab olish",
                        "скачать",
                        "download",
                        "fayl",
                        "файл",
                        "ochish",
                        "открыть",
                    ):
                        cand_fname = ru_t_title

                    parsed_u_path = unquote(urlparse(full_u).path)
                    ext_found = ""
                    for ext in FILE_EXTENSIONS:
                        if parsed_u_path.lower().endswith(ext):
                            ext_found = ext
                            break
                        if cand_fname.lower().endswith(ext):
                            ext_found = ext
                            break
                    if ext_found and not cand_fname.lower().endswith(ext_found):
                        cand_fname = f"{cand_fname}{ext_found}"

                    resources.append(
                        {
                            "id": str(len(resources) + 1),
                            "subject": subject_name,
                            "title": ru_t_title,
                            "training_type": ru_t_type,
                            "category": cat,
                            "file_name": translator.apply_dictionary_rules(
                                cand_fname
                            ),
                            "url": full_u,
                        }
                    )

        return resources

    # -------------------------------------------------------------------------
    # МОНИТОРИНГ БАЛЛОВ, GPA И ПОСЕЩАЕМОСТИ
    # -------------------------------------------------------------------------
    async def get_performance_and_attendance(
        self, user_id: int = 0
    ) -> Dict[str, Any]:
        await self.load_user_semester_from_db(user_id)
        sem_code = self.get_user_semester_code(user_id)
        sem_num = self.get_user_semester_number(user_id)

        subjects = await self.get_semester_subjects(user_id=user_id)

        gpa_value: Optional[str] = None
        try:
            gpa_data = await self._request_json("/rest/v1/education/gpa-list")
            gpa_rows = gpa_data.get("data", [])
            if isinstance(gpa_rows, list) and gpa_rows:
                raw_gpa = gpa_rows[-1].get("gpa")
                if raw_gpa is not None:
                    gpa_value = str(raw_gpa)
            if not gpa_value:
                me_data = await self._request_json("/rest/v1/account/me")
                me_profile = me_data.get("data") or {}
                if me_profile.get("avg_gpa") is not None:
                    gpa_value = str(me_profile.get("avg_gpa"))
        except Exception as exc:
            logger.debug("GPA check note: %s", exc)

        now_dt = datetime.now(self.tz)
        current_month = now_dt.month
        current_year = now_dt.year

        semester_total = 0
        semester_excused = 0
        month_total = 0
        month_excused = 0

        try:
            att_data = await self._request_json(
                "/rest/v1/education/attendance", params={"semester": sem_code}
            )
            att_rows = att_data.get("data", [])
            if isinstance(att_rows, list) and att_rows:
                for a in att_rows:
                    if not isinstance(a, dict) or not self._matches_semester(
                        a, sem_code, sem_num
                    ):
                        continue

                    raw_off = a.get("absent_off")
                    raw_on = a.get("absent_on")
                    if raw_off is not None or raw_on is not None:
                        hours = int(raw_off or 0) + int(raw_on or 0)
                        if hours == 0:
                            hours = 2
                    else:
                        hours = int(a.get("hours") or 2)

                    is_excused = bool(
                        a.get("explicable")
                        or a.get("is_excused")
                        or (int(raw_on or 0) > 0 and int(raw_off or 0) == 0)
                    )

                    semester_total += hours
                    if is_excused:
                        semester_excused += hours

                    lesson_ts = a.get("lesson_date")
                    lesson_dt: Optional[date] = None
                    if isinstance(lesson_ts, (int, float)) and lesson_ts > 100000:
                        lesson_dt = datetime.fromtimestamp(
                            lesson_ts, self.tz
                        ).date()
                    elif isinstance(lesson_ts, str) and len(lesson_ts) >= 10:
                        try:
                            lesson_dt = datetime.strptime(
                                lesson_ts[:10], "%Y-%m-%d"
                            ).date()
                        except ValueError:
                            pass

                    if (
                        lesson_dt
                        and lesson_dt.year == current_year
                        and lesson_dt.month == current_month
                    ):
                        month_total += hours
                        if is_excused:
                            month_excused += hours
            else:
                html_att = await self._request_html(
                    "/education/attendance", params={"semester": sem_code}
                )
                if html_att:
                    soup = BeautifulSoup(html_att, "html.parser")
                    for tr in soup.select("table tbody tr"):
                        tds = [
                            td.get_text(" ", strip=True)
                            for td in tr.find_all("td")
                        ]
                        if len(tds) < 4:
                            continue
                        row_str = " ".join(tds)
                        m_hr = re.search(
                            r"\b(\d+)\s*(?:soat|ч\.|час)", row_str, re.I
                        )
                        hours = int(m_hr.group(1)) if m_hr else 2
                        is_exc = (
                            "sababli" in row_str.lower()
                            or "уваж" in row_str.lower()
                        ) and "sababsiz" not in row_str.lower()
                        semester_total += hours
                        if is_exc:
                            semester_excused += hours
                        m_dt = re.search(
                            r"(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})", row_str
                        )
                        if m_dt:
                            d_s = m_dt.group(1)
                            try:
                                l_dt = (
                                    datetime.strptime(d_s, "%d.%m.%Y").date()
                                    if "." in d_s
                                    else datetime.strptime(
                                        d_s, "%Y-%m-%d"
                                    ).date()
                                )
                                if (
                                    l_dt.year == current_year
                                    and l_dt.month == current_month
                                ):
                                    month_total += hours
                                    if is_exc:
                                        month_excused += hours
                            except ValueError:
                                pass
        except Exception as exc:
            logger.debug("Attendance calculation note: %s", exc)

        return {
            "semester_num": sem_num,
            "semester_code": sem_code,
            "gpa": gpa_value or "Н/Д",
            "subjects": subjects,
            "attendance": {
                "month_total": month_total,
                "month_excused": month_excused,
                "month_unexcused": max(0, month_total - month_excused),
                "semester_total": semester_total,
                "semester_excused": semester_excused,
                "semester_unexcused": max(0, semester_total - semester_excused),
            },
        }

    # -------------------------------------------------------------------------
    # НАДЁЖНОЕ СКАЧИВАНИЕ ФАЙЛОВ ИЗ HEMIS С ПОДДЕРЖКОЙ ВЕБ-СЕССИИ И ВЛОЖЕННЫХ СТРАНИЦ
    # -------------------------------------------------------------------------
    async def download_file_bytes(self, url: str) -> bytes:
        """
        Скачивает учебный файл из HEMIS:
        1. Поддерживает веб-сессию Yii2 и Referer.
        2. Если ссылка ведёт на промежуточную HTML-страницу просмотра материала в HEMIS,
           автоматически находит внутри неё прямую ссылку на файл и скачивает его.
        """
        await self.authenticate()
        if not self._web_authenticated:
            await self._login_web_session()

        session = await self._get_session()
        full_url = (
            url
            if url.startswith("http")
            else urljoin(f"{self.base_url}/", url.lstrip("/"))
        )

        is_rest_url = "/rest/" in full_url

        for attempt in range(2):
            req_headers = self._build_headers(is_rest=is_rest_url)
            req_headers["Referer"] = f"{self.base_url}/education/resources"

            async with session.get(
                full_url, headers=req_headers, allow_redirects=True
            ) as resp:
                final_url = str(resp.url)
                content_type = resp.headers.get("Content-Type", "").lower()

                if (
                    resp.status in (401, 403)
                    or "/dashboard/login" in final_url
                ) and attempt == 0:
                    await self._login_web_session()
                    continue

                resp.raise_for_status()
                data = await resp.read()

                head_sample = data[:512].lstrip().lower()
                is_html = (
                    "text/html" in content_type
                    or head_sample.startswith(b"<!doctype html")
                    or head_sample.startswith(b"<html")
                )

                if not is_html:
                    return data

                # Если вернулся HTML: проверяем, не форма ли это логина
                html_str = data.decode("utf-8", errors="ignore")
                if "FormStudentLogin" in html_str and attempt == 0:
                    await self._login_web_session()
                    continue

                # Проверяем, нет ли внутри этой HTML-страницы прямой ссылки на прикреплённый файл
                soup = BeautifulSoup(html_str, "html.parser")
                inner_file_url = ""
                for tag in soup.select(
                    "a[href], iframe[src], embed[src], object[data]"
                ):
                    cand = (
                        tag.get("href")
                        or tag.get("src")
                        or tag.get("data")
                        or ""
                    ).strip()
                    if not cand or cand.startswith(("#", "javascript:")):
                        continue
                    cand_low = cand.lower()
                    if any(ext in cand_low for ext in FILE_EXTENSIONS) or any(
                        k in cand_low
                        for k in ("/storage/", "/upload", "/static/", "download=")
                    ):
                        candidate_full = urljoin(final_url, cand)
                        if candidate_full != full_url:
                            inner_file_url = candidate_full
                            break

                if inner_file_url:
                    full_url = inner_file_url
                    continue

                if attempt == 0:
                    await self._login_web_session()
                    continue

                raise ValueError(
                    "Материал размещён преподавателем как веб-страница или внешняя ссылка (без бинарного файла)."
                )

        raise ValueError("Не удалось скачать файл из HEMIS.")
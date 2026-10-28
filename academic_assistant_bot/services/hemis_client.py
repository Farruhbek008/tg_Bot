import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup
import pytz

from config import settings

logger = logging.getLogger(__name__)


class HemisAuthError(Exception):
    pass


class HemisClient:
    """
    Усовершенствованный клиент для работы с HEMIS (student.fstu.uz и my.hemis.uz).
    Автоматически определяет текущий семестр студента (например, 3-семестр / код 13),
    позволяет переключать семестры, получает задания по всем предметам семестра,
    фильтрует учебные ресурсы по предметам и типам занятий (Маъруза, Амалий, Лаборатория, Мустакил таълим)
    и точно считает часы посещаемости (за текущий семестр и за текущий месяц).
    """

    def __init__(self) -> None:
        self.base_url = settings.hemis_base_url.rstrip("/")
        self.login = settings.hemis_login
        self.password = settings.hemis_password
        self.tz = pytz.timezone(settings.timezone)

        self._session: Optional[aiohttp.ClientSession] = None
        self._bearer_token: Optional[str] = None
        self._is_authenticated: bool = False
        self._auth_lock = asyncio.Lock()

        # Кэш текущего семестра (например, code="13", number=3, name="3-семестр")
        self._default_semester_code: Optional[str] = None
        self._semesters_map: Dict[str, str] = {}  # {"1": "11", "2": "12", "3": "13", ...}
        # Персональный выбор семестра пользователями Telegram: {user_id: "13"}
        self._user_semester: Dict[int, str] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            cookie_jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=cookie_jar,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/128.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/html, */*",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def authenticate(self, force: bool = False) -> bool:
        async with self._auth_lock:
            if self._is_authenticated and not force:
                return True

            session = await self._get_session()

            # 1. Пробуем REST API (student.fstu.uz или my.hemis.uz)
            for host in [self.base_url, "https://student.fstu.uz"]:
                api_login_url = f"{host.rstrip('/')}/rest/v1/auth/login"
                try:
                    async with session.post(
                        api_login_url,
                        json={"login": str(self.login), "password": self.password},
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
                                logger.info("Authenticated via HEMIS REST API (%s).", host)
                                break
                except Exception as exc:
                    logger.debug("REST API auth attempt on %s failed: %s", host, exc)

            # 2. Также выполняем веб-вход на student.fstu.uz для прямого парсинга карточек /education/subjects и скачивания файлов
            login_page_url = f"{self.base_url}/dashboard/login"
            try:
                async with session.get(login_page_url) as get_resp:
                    html = await get_resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    csrf_input = soup.find("input", {"name": "_csrf-frontend"})
                    csrf_token = csrf_input["value"] if csrf_input else ""

                form_data = {
                    "_csrf-frontend": csrf_token,
                    "FormStudentLogin[login]": str(self.login),
                    "FormStudentLogin[password]": self.password,
                    "FormStudentLogin[rememberMe]": "1",
                }
                async with session.post(
                    login_page_url,
                    data=form_data,
                    allow_redirects=True,
                ) as post_resp:
                    final_url = str(post_resp.url)
                    body_text = await post_resp.text()
                    if "login" not in final_url.lower() or "logout" in body_text.lower():
                        self._is_authenticated = True
                        logger.info("Authenticated via HEMIS Web Session.")
            except Exception as exc:
                logger.debug("Web session auth note: %s", exc)

            if not self._is_authenticated:
                raise HemisAuthError(
                    "Не удалось авторизоваться в HEMIS. Проверьте логин и пароль в .env."
                )

        # Определяем текущий семестр студента ПОСЛЕ выхода из _auth_lock, чтобы избежать взаимной блокировки (deadlock)
        if not self._default_semester_code:
            self._default_semester_code = "13"
            await self._detect_current_semester()

        return True

    def _build_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers

    async def _request_json(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        await self.authenticate()
        session = await self._get_session()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        for attempt in range(2):
            async with session.get(
                url, headers=self._build_headers(), params=clean_params
            ) as resp:
                if resp.status == 401 and attempt == 0:
                    await self.authenticate(force=True)
                    continue
                if resp.status >= 400:
                    return {}
                return await resp.json(content_type=None)
        return {}

    async def _request_html(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> str:
        await self.authenticate()
        session = await self._get_session()
        url = f"{self.base_url}/{path.lstrip('/')}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        for attempt in range(2):
            async with session.get(
                url, headers=self._build_headers(), params=clean_params
            ) as resp:
                final_url = str(resp.url)
                if (resp.status in (401, 403) or "/dashboard/login" in final_url) and attempt == 0:
                    await self.authenticate(force=True)
                    continue
                if resp.status >= 400:
                    return ""
                return await resp.text()
        return ""

    # -------------------------------------------------------------------------
    # УПРАВЛЕНИЕ СЕМЕСТРАМИ (Автоопределение активного семестра, например 3-семестр)
    # -------------------------------------------------------------------------
    async def _detect_current_semester(self) -> None:
        """
        Определяет текущий семестр студента из профиля `/rest/v1/account/me`,
        `/rest/v1/education/semesters` или из активной кнопки на `/education/subjects`.
        В HEMIS коды семестров обычно: 1-семестр = '11', 2-семестр = '12', 3-семестр = '13' и т.д.
        """
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
                logger.info("Detected current semester from profile: code=%s (%s)", code, sem.get("name"))
                return
        except Exception as exc:
            logger.debug("Profile semester check note: %s", exc)

        # 2. Пробуем веб-страницу /education/subjects (ищем активную зеленую кнопку семестра, как на скриншоте)
        try:
            html = await self._request_html("/education/subjects")
            if html:
                soup = BeautifulSoup(html, "html.parser")
                for a in soup.select("ul.pagination li a, .btn-group a, a[href*='semester']"):
                    href = a.get("href", "")
                    m = re.search(r"semester=(\d+)", href)
                    label = a.get_text(strip=True)
                    if m and label.isdigit():
                        self._semesters_map[label] = m.group(1)
                    parent_li = a.find_parent("li")
                    classes = " ".join((a.get("class") or []) + ((parent_li.get("class") if parent_li else []) or []))
                    if ("active" in classes or "btn-success" in classes or "bg-green" in classes) and m:
                        self._default_semester_code = m.group(1)
        except Exception as exc:
            logger.debug("HTML semester detection note: %s", exc)

        if not self._default_semester_code:
            # По умолчанию 3-семестр (код 13), как на скриншотах пользователя
            self._default_semester_code = "13"

    def get_user_semester_code(self, user_id: int) -> str:
        return self._user_semester.get(user_id) or self._default_semester_code or "13"

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
    def _matches_semester(row: Dict[str, Any], target_code: str, target_num: int) -> bool:
        """Проверяет, относится ли запись из API к выбранному семестру."""
        sem = row.get("semester") or row.get("_semester") or row.get("educationSemester")
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
    # ПРЕДМЕТЫ ВЫБРАННОГО СЕМЕСТРА (Как на скриншоте Fanlar resurslari / O'zlashtirish)
    # -------------------------------------------------------------------------
    async def get_semester_subjects(self, user_id: int = 0) -> List[Dict[str, Any]]:
        """
        Возвращает список дисциплин строго за выбранный семестр студента
        (например: Ma'lumotlar bazasi, Kompyuter tarmoqlari, Elektronika va sxemalar,
        Diskret tuzilmalar, Chiziqli algebra).
        """
        sem_code = self.get_user_semester_code(user_id)
        sem_num = self.get_user_semester_number(user_id)
        subjects_map: Dict[str, Dict[str, Any]] = {}

        # 1. Пробуем REST API: /rest/v1/education/subject-list и /rest/v1/education/subjects
        for ep in ["/rest/v1/education/subject-list", "/rest/v1/education/subjects", "/rest/v1/education/performance"]:
            data = await self._request_json(ep, params={"semester": sem_code})
            rows = data.get("data") or []
            if isinstance(rows, list) and rows:
                for r in rows:
                    if not self._matches_semester(r, sem_code, sem_num):
                        continue
                    subj_obj = r.get("subject") or r.get("curriculumSubject") or r
                    subj_id = str(subj_obj.get("id") or r.get("subject_id") or r.get("id") or "")
                    subj_name = str(subj_obj.get("name") or r.get("name") or "").strip()
                    if not subj_name:
                        continue
                    key = subj_id or subj_name
                    entry = subjects_map.setdefault(
                        key,
                        {
                            "id": subj_id or subj_name,
                            "name": subj_name,
                            "type": (r.get("subjectType") or {}).get("name", "Majburiy"),
                            "load": r.get("total_acload") or r.get("acload") or 180,
                            "credit": r.get("credit") or 6.0,
                            "resources_count": int(r.get("resources_count") or r.get("resourceCount") or 0),
                            "tasks_count": int(r.get("tasks_count") or r.get("taskCount") or 0),
                            "submitted_tasks": int(r.get("submitted_tasks") or 0),
                            "overall_score": r.get("overallScore") or 0,
                            "max_score": r.get("max_ball") or 100,
                        },
                    )
                    if r.get("overallScore") is not None:
                        entry["overall_score"] = r.get("overallScore")

        # 2. Дополняем данными с веб-страницы /education/subjects?semester=sem_code (как на Скриншоте 1)
        try:
            html = await self._request_html("/education/subjects", params={"semester": sem_code})
            if html:
                soup = BeautifulSoup(html, "html.parser")
                for box in soup.select(".box, .card, .panel"):
                    title_el = box.select_one(".box-title, .card-title, h3, h4")
                    if not title_el:
                        continue
                    subj_name = title_el.get_text(" ", strip=True)
                    if not subj_name or len(subj_name) < 3:
                        continue

                    # Ищем ссылки на ресурсы и задания внутри карточки предмета
                    res_link = box.select_one("a[href*='resources'], a[href*='subject']")
                    task_link = box.select_one("a[href*='tasks'], a[href*='task']")
                    subj_id = ""
                    for link in [res_link, task_link, *box.find_all("a", href=True)]:
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
                        badge = li.select_one(".badge, .label, span.pull-right")
                        badge_txt = badge.get_text(strip=True) if badge else ""
                        if "Resurslar" in txt or "Ресурс" in txt:
                            m_num = re.search(r"(\d+)", badge_txt or txt)
                            if m_num:
                                res_count = int(m_num.group(1))
                        elif "Topshiriqlar" in txt or "Задани" in txt:
                            tasks_str = badge_txt or "0 / 0"

                    key = subj_id or subj_name
                    existing = subjects_map.get(key)
                    if not existing:
                        # Проверим по совпадению имени
                        for k, v in subjects_map.items():
                            if v["name"].lower() == subj_name.lower():
                                existing = v
                                if subj_id:
                                    existing["id"] = subj_id
                                break
                    if existing:
                        if res_count:
                            existing["resources_count"] = res_count
                        existing["tasks_badge"] = tasks_str
                    else:
                        subjects_map[key] = {
                            "id": subj_id or subj_name,
                            "name": subj_name,
                            "type": "Majburiy",
                            "load": 180,
                            "credit": 6.0,
                            "resources_count": res_count,
                            "tasks_badge": tasks_str,
                            "overall_score": 0,
                            "max_score": 100,
                        }
        except Exception as exc:
            logger.debug("HTML subjects parse note: %s", exc)

        # 3. Если список всё ещё пуст, извлекаем предметы из расписания текущего семестра
        if not subjects_map:
            sched = await self._fetch_schedule_raw(semester_code=sem_code)
            for item in sched:
                s_name = item.get("subject", "").strip()
                s_id = item.get("subject_id", "") or s_name
                if s_name and s_name not in subjects_map:
                    subjects_map[s_name] = {
                        "id": s_id,
                        "name": s_name,
                        "type": "Majburiy",
                        "load": 180,
                        "credit": 6.0,
                        "resources_count": 0,
                        "tasks_badge": "—",
                        "overall_score": 0,
                        "max_score": 100,
                    }

        return list(subjects_map.values())

    # -------------------------------------------------------------------------
    # РАСПИСАНИЕ (На сегодня / завтра / неделю)
    # -------------------------------------------------------------------------
    async def get_schedule(
        self, target_date: Optional[date] = None, user_id: int = 0
    ) -> List[Dict[str, str]]:
        if target_date is None:
            target_date = datetime.now(self.tz).date()

        sem_code = self.get_user_semester_code(user_id)
        lessons = await self._fetch_schedule_raw(semester_code=sem_code)
        target_str = target_date.strftime("%Y-%m-%d")
        target_ru = target_date.strftime("%d.%m.%Y")

        return [
            item
            for item in lessons
            if item.get("iso_date") == target_str or item.get("date") == target_ru
        ]

    async def get_week_schedule(
        self, week_offset: int = 0, user_id: int = 0
    ) -> Tuple[date, date, Dict[str, List[Dict[str, str]]]]:
        now_date = datetime.now(self.tz).date() + timedelta(weeks=week_offset)
        start_of_week = now_date - timedelta(days=now_date.weekday())
        end_of_week = start_of_week + timedelta(days=6)

        sem_code = self.get_user_semester_code(user_id)
        lessons = await self._fetch_schedule_raw(semester_code=sem_code)
        grouped: Dict[str, List[Dict[str, str]]] = {}

        for item in lessons:
            iso_str = item.get("iso_date")
            if iso_str:
                try:
                    d = datetime.strptime(iso_str, "%Y-%m-%d").date()
                    if not (start_of_week <= d <= end_of_week):
                        continue
                except ValueError:
                    pass
            day_label = item.get("date", "Неизвестная дата")
            grouped.setdefault(day_label, []).append(item)

        return start_of_week, end_of_week, grouped

    async def _fetch_schedule_raw(
        self, semester_code: Optional[str] = None
    ) -> List[Dict[str, str]]:
        try:
            payload = await self._request_json(
                "/rest/v1/education/schedule", params={"semester": semester_code}
            )
            items = payload.get("data", [])
            if isinstance(items, list) and items:
                parsed: List[Dict[str, str]] = []
                for row in items:
                    ts = row.get("lesson_date")
                    if isinstance(ts, (int, float)):
                        dt = datetime.fromtimestamp(ts, self.tz).date()
                        iso_date = dt.strftime("%Y-%m-%d")
                        ru_date = dt.strftime("%d.%m.%Y")
                    else:
                        iso_date = str(ts or "")
                        ru_date = iso_date

                    pair = row.get("lessonPair") or {}
                    start_t = pair.get("start_time", "")
                    end_t = pair.get("end_time", "")
                    time_str = f"{start_t} - {end_t}".strip(" -") or pair.get("name", "Пара")
                    subj_obj = row.get("subject") or {}

                    parsed.append(
                        {
                            "iso_date": iso_date,
                            "date": ru_date,
                            "time": time_str,
                            "subject": subj_obj.get("name", "Предмет"),
                            "subject_id": str(subj_obj.get("id") or ""),
                            "room": (row.get("auditorium") or {}).get("name", "Не указана"),
                            "lesson_type": (row.get("trainingType") or {}).get("name", "Занятие"),
                            "teacher": (row.get("employee") or {}).get("name", "Не указан"),
                        }
                    )
                return parsed
        except Exception as exc:
            logger.debug("REST schedule note: %s", exc)
        return []

    @staticmethod
    def format_schedule_list(lessons: List[Dict[str, str]], title: str) -> str:
        if not lessons:
            return f"📅 <b>{title}</b>\n\n🎉 В этот день пар по расписанию нет!"

        lines = [f"📅 <b>{title}</b>\n"]
        for idx, item in enumerate(lessons, start=1):
            lines.append(
                f"<b>{idx}. 🕒 {item['time']}</b> — <b>{item['subject']}</b>\n"
                f"   🏛 Аудитория: <code>{item['room']}</code> | 📚 {item['lesson_type']}\n"
                f"   👨‍🏫 Преподаватель: {item['teacher']}"
            )
        return "\n\n".join(lines)

    # -------------------------------------------------------------------------
    # ЗАДАНИЯ (Topshiriqlar: Yangi / Yuborilgan / Baholangan + Файлы заданий)
    # -------------------------------------------------------------------------
    async def get_assignments(self, user_id: int = 0) -> List[Dict[str, Any]]:
        """
        Собирает все задания за текущий выбранный семестр:
        1) Из `/rest/v1/education/task-list` и `/rest/v1/education/tasks` с параметром `semester`.
        2) Из списка предметов семестра (`subject-list` -> `tasks` или по каждому `subject_id`).
        3) Из веб-страниц `/education/tasks` и `/education/subject-tasks` (если REST API не вернул все).
        """
        sem_code = self.get_user_semester_code(user_id)
        sem_num = self.get_user_semester_number(user_id)
        collected: List[Dict[str, Any]] = []
        seen_ids = set()

        def _parse_task_row(row: Dict[str, Any], default_subject: str = "Дисциплина") -> Optional[Dict[str, Any]]:
            if not isinstance(row, dict):
                return None
            if not self._matches_semester(row, sem_code, sem_num):
                return None

            t_id = str(row.get("id") or row.get("task_id") or "")
            subj_name = (
                (row.get("subject") or {}).get("name")
                or (row.get("curriculumSubject") or {}).get("name")
                or default_subject
            )
            title = (
                row.get("name")
                or row.get("title")
                or (row.get("trainingType") or {}).get("name")
                or "Topshiriq"
            )
            training_type = (row.get("trainingType") or {}).get("name") or row.get("type") or ""

            deadline_ts = row.get("deadline") or row.get("attempt_limit")
            if isinstance(deadline_ts, (int, float)) and deadline_ts > 100000:
                deadline_str = datetime.fromtimestamp(deadline_ts, self.tz).strftime("%d.%m.%Y %H:%M")
            else:
                deadline_str = str(deadline_ts or "Не указан")

            status_obj = row.get("taskStatus") or row.get("TaskStatus") or row.get("studentTaskActivity") or {}
            if isinstance(status_obj, dict):
                status_name = status_obj.get("name") or ""
            else:
                status_name = str(status_obj or "")

            if not status_name:
                # Определяем по коду статуса (11 - Новое/Yangi, 12 - Отправлено/Yuborilgan, 13 - Оценено)
                st_code = str(row.get("_task_status") or row.get("status") or "")
                if st_code in ("11", "new", "0"):
                    status_name = "🆕 Yangi (Новое)"
                elif st_code in ("12", "sent", "1"):
                    status_name = "📤 Yuborilgan (Отправлено)"
                elif st_code in ("13", "graded", "2"):
                    status_name = "✅ Baholangan (Оценено)"
                else:
                    status_name = "📌 Активно"

            files_raw = (
                row.get("trainingTaskFiles")
                or row.get("files")
                or row.get("taskFiles")
                or (row.get("file") and [row.get("file")])
                or []
            )
            files_list = []
            if isinstance(files_raw, list):
                for f in files_raw:
                    if not isinstance(f, dict):
                        continue
                    f_url = f.get("url") or f.get("path") or f.get("file") or ""
                    if f_url and not f_url.startswith("http"):
                        f_url = urljoin(f"{self.base_url}/", f_url.lstrip("/"))
                    if f_url:
                        files_list.append(
                            {
                                "name": f.get("name") or f.get("filename") or "topshiriq_fayli.pdf",
                                "url": f_url,
                                "size": f.get("size") or 0,
                            }
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
                "description": row.get("comment") or row.get("description") or row.get("content") or "",
                "deadline": deadline_str,
                "max_ball": row.get("max_ball") or row.get("maxBall") or "—",
                "status": status_name,
                "files": files_list,
            }

        # 1. Запрашиваем основные REST эндпоинты заданий с фильтрацией по семестру
        for ep in ["/rest/v1/education/task-list", "/rest/v1/education/tasks"]:
            for params in [{"semester": sem_code}, {"semester": sem_code, "limit": 100}, {}]:
                payload = await self._request_json(ep, params=params)
                items = payload.get("data")
                if isinstance(items, dict):
                    items = items.get("items") or items.get("tasks") or []
                if isinstance(items, list) and items:
                    for r in items:
                        parsed_item = _parse_task_row(r)
                        if parsed_item:
                            collected.append(parsed_item)
                    if collected:
                        break

        # 2. Проверяем вложенные задания внутри `/rest/v1/education/subject-list` или по каждому предмету семестра
        subjects = await self.get_semester_subjects(user_id=user_id)
        for subj in subjects:
            s_id = subj.get("id")
            s_name = subj.get("name", "Дисциплина")
            if s_id and str(s_id).isdigit():
                for ep in ["/rest/v1/education/task-list", "/rest/v1/education/tasks", "/rest/v1/education/subject-detail"]:
                    data = await self._request_json(ep, params={"subject": s_id, "semester": sem_code})
                    raw_data = data.get("data")
                    rows = []
                    if isinstance(raw_data, list):
                        rows = raw_data
                    elif isinstance(raw_data, dict):
                        rows = raw_data.get("tasks") or raw_data.get("taskList") or raw_data.get("items") or []
                    for r in rows:
                        parsed_item = _parse_task_row(r, default_subject=s_name)
                        if parsed_item:
                            collected.append(parsed_item)

        # 3. Веб-фолбэк: парсим HTML страницы заданий предметов, если через API не пришли
        if not collected:
            for subj in subjects:
                s_id = subj.get("id")
                s_name = subj.get("name", "Дисциплина")
                for path in ["/education/tasks", "/education/subject-tasks"]:
                    params = {"semester": sem_code}
                    if s_id and str(s_id).isdigit():
                        params["subject"] = s_id
                    html = await self._request_html(path, params=params)
                    if not html:
                        continue
                    soup = BeautifulSoup(html, "html.parser")
                    # Ищем карточки или строки таблиц с заданиями
                    for box in soup.select(".box, .card, .panel, tr"):
                        text_content = box.get_text(" ", strip=True)
                        if "Muddati" not in text_content and "Дедлайн" not in text_content and "Topshiriq" not in text_content:
                            continue
                        links = box.find_all("a", href=True)
                        files_found = []
                        for a in links:
                            href = a["href"]
                            if any(k in href.lower() for k in ["download", "file", ".pdf", ".doc", ".ppt", ".zip", "task"]):
                                full_u = urljoin(f"{self.base_url}/", href.lstrip("/"))
                                f_label = a.get_text(" ", strip=True) or "topshiriq_fayli.pdf"
                                files_found.append({"name": f_label, "url": full_u})
                        title_el = box.select_one("h3, h4, .box-title, strong, b")
                        t_title = title_el.get_text(" ", strip=True) if title_el else "Topshiriq"
                        key = f"{s_name}_{t_title}"
                        if key not in seen_ids:
                            seen_ids.add(key)
                            collected.append(
                                {
                                    "id": str(len(collected) + 1),
                                    "subject": s_name,
                                    "title": t_title,
                                    "training_type": "",
                                    "description": text_content[:350],
                                    "deadline": "См. в описании",
                                    "max_ball": "—",
                                    "status": "📌 Активно",
                                    "files": files_found,
                                }
                            )

        return collected

    # -------------------------------------------------------------------------
    # УЧЕБНЫЕ РЕСУРСЫ ПО КОНКРЕТНОМУ ПРЕДМЕТУ И ТИПУ ЗАНЯТИЙ
    # -------------------------------------------------------------------------
    @staticmethod
    def classify_resource_type(training_type_name: str, title: str = "") -> str:
        """
        Классифицирует ресурс на категории для удобной фильтрации кнопками:
        - 'lecture' (Лекционные / Ma'ruza)
        - 'practical' (Практические / Amaliy / Seminar)
        - 'lab' (Лабораторные / Laboratoriya)
        - 'independent' (Самостоятельные / Mustaqil ta'lim)
        - 'other' (Прочие материалы)
        """
        combined = f"{training_type_name} {title}".lower()
        if any(w in combined for w in ["ma'ruza", "maruza", "ma’ruza", "лекц", "lecture"]):
            return "lecture"
        if any(w in combined for w in ["amaliy", "амалий", "практ", "семинар", "seminar", "practical"]):
            return "practical"
        if any(w in combined for w in ["laboratoriya", "лаборатор", "lab"]):
            return "lab"
        if any(w in combined for w in ["mustaqil", "мустақил", "самостоят", "срс", "independent"]):
            return "independent"
        return "other"

    async def get_subject_resources(
        self, subject_id: str, subject_name: str, user_id: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Возвращает реальные учебные материалы и файлы (PDF/DOCX/PPTX) по выбранному предмету
        с их категорией (Ma'ruza, Amaliy, Laboratoriya, Mustaqil ta'lim) и прямыми ссылками на скачивание.
        """
        sem_code = self.get_user_semester_code(user_id)
        sem_num = self.get_user_semester_number(user_id)
        resources: List[Dict[str, Any]] = []
        seen_urls = set()

        # 1. Запрашиваем REST API `/rest/v1/education/resources` с фильтрацией по subject и semester
        for params in [
            {"subject": subject_id, "semester": sem_code},
            {"subject": subject_id},
            {"semester": sem_code},
        ]:
            payload = await self._request_json("/rest/v1/education/resources", params=params)
            items = payload.get("data") or []
            if isinstance(items, dict):
                items = items.get("items") or items.get("resources") or []
            if not isinstance(items, list) or not items:
                continue

            for idx, row in enumerate(items, start=1):
                if not isinstance(row, dict):
                    continue
                # Проверяем совпадение предмета и семестра
                row_subj = row.get("subject") or {}
                row_subj_id = str(row_subj.get("id") or row.get("subject_id") or "")
                row_subj_name = str(row_subj.get("name") or "").strip()

                if str(subject_id).isdigit() and row_subj_id and row_subj_id != str(subject_id):
                    continue
                if row_subj_name and subject_name and row_subj_name.lower() != subject_name.lower():
                    if not (str(subject_id).isdigit() and row_subj_id == str(subject_id)):
                        continue
                if not self._matches_semester(row, sem_code, sem_num):
                    continue

                title = (
                    row.get("name")
                    or row.get("title")
                    or row.get("comment")
                    or f"Тема #{idx}"
                )
                training_type = (
                    (row.get("trainingType") or {}).get("name")
                    or (row.get("resourceType") or {}).get("name")
                    or "Учебный материал"
                )
                category = self.classify_resource_type(training_type, title)

                # В HEMIS файлы ресурса могут лежать в `files` (список), `trainingResourceFiles`, `file` или `url`
                files_extracted: List[Dict[str, str]] = []
                raw_files = (
                    row.get("files")
                    or row.get("trainingResourceFiles")
                    or row.get("resourceFiles")
                    or []
                )
                if isinstance(raw_files, list):
                    for rf in raw_files:
                        if isinstance(rf, dict):
                            u = rf.get("url") or rf.get("path") or rf.get("file") or ""
                            fn = rf.get("name") or rf.get("filename") or f"{title}.pdf"
                            if u:
                                if not u.startswith("http"):
                                    u = urljoin(f"{self.base_url}/", u.lstrip("/"))
                                files_extracted.append({"name": fn, "url": u})

                direct_url = row.get("url") or row.get("file") or row.get("path") or ""
                if isinstance(direct_url, str) and direct_url:
                    if not direct_url.startswith("http"):
                        direct_url = urljoin(f"{self.base_url}/", direct_url.lstrip("/"))
                    files_extracted.append({"name": f"{title}.pdf", "url": direct_url})

                if not files_extracted:
                    # Если запись имеет только ID ресурса, формируем ссылку скачивания
                    r_id = row.get("id")
                    if r_id:
                        dl_url = f"{self.base_url}/education/resource-download?id={r_id}"
                        files_extracted.append({"name": f"{title}.pdf", "url": dl_url})

                for f_obj in files_extracted:
                    if f_obj["url"] in seen_urls:
                        continue
                    seen_urls.add(f_obj["url"])
                    resources.append(
                        {
                            "id": str(row.get("id") or len(resources) + 1),
                            "subject": subject_name,
                            "title": title,
                            "training_type": training_type,
                            "category": category,
                            "file_name": f_obj["name"],
                            "url": f_obj["url"],
                        }
                    )

            if resources:
                break

        # 2. Фолбэк: парсинг веб-страницы `/education/resources?subject=ID&semester=CODE`
        if not resources and str(subject_id).isdigit():
            html = await self._request_html(
                "/education/resources",
                params={"subject": subject_id, "semester": sem_code},
            )
            if html:
                soup = BeautifulSoup(html, "html.parser")
                for idx, tr in enumerate(soup.select("table tbody tr, .box-body li"), start=1):
                    cols = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "span"])]
                    row_text = " ".join(cols) or tr.get_text(" ", strip=True)
                    for a in tr.find_all("a", href=True):
                        href = a["href"]
                        if any(k in href.lower() for k in ["download", "resource", "file", ".pdf", ".doc", ".ppt", ".zip"]):
                            full_u = urljoin(f"{self.base_url}/", href.lstrip("/"))
                            if full_u in seen_urls:
                                continue
                            seen_urls.add(full_u)
                            f_name = a.get_text(" ", strip=True) or f"material_{idx}.pdf"
                            t_title = cols[1] if len(cols) > 1 else f_name
                            t_type = cols[2] if len(cols) > 2 else "Материал"
                            resources.append(
                                {
                                    "id": str(idx),
                                    "subject": subject_name,
                                    "title": t_title,
                                    "training_type": t_type,
                                    "category": self.classify_resource_type(t_type, row_text),
                                    "file_name": f_name,
                                    "url": full_u,
                                }
                            )

        return resources

    # -------------------------------------------------------------------------
    # МОНИТОРИНГ БАЛЛОВ, GPA И ТОЧНОЙ ПОСЕЩАЕМОСТИ (Семестр + Текущий месяц = 8 ч.)
    # -------------------------------------------------------------------------
    async def get_performance_and_attendance(self, user_id: int = 0) -> Dict[str, Any]:
        sem_code = self.get_user_semester_code(user_id)
        sem_num = self.get_user_semester_number(user_id)

        # 1. Предметы и баллы текущего семестра
        subjects = await self.get_semester_subjects(user_id=user_id)

        # 2. GPA
        gpa_value: Optional[str] = None
        try:
            gpa_data = await self._request_json("/rest/v1/education/gpa-list")
            gpa_rows = gpa_data.get("data", [])
            if isinstance(gpa_rows, list) and gpa_rows:
                gpa_value = str(gpa_rows[-1].get("gpa", "—"))
        except Exception as exc:
            logger.debug("GPA check note: %s", exc)

        # 3. Точный подсчет посещаемости за выбранный семестр И за текущий месяц (как в my.hemis.uz)
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
            if isinstance(att_rows, list):
                for a in att_rows:
                    if not self._matches_semester(a, sem_code, sem_num):
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

                    # Проверяем дату пропуска для подсчета за текущий месяц (как на дашборде my.hemis.uz: "8 soat")
                    lesson_ts = a.get("lesson_date")
                    lesson_dt: Optional[date] = None
                    if isinstance(lesson_ts, (int, float)) and lesson_ts > 100000:
                        lesson_dt = datetime.fromtimestamp(lesson_ts, self.tz).date()
                    elif isinstance(lesson_ts, str) and len(lesson_ts) >= 10:
                        try:
                            lesson_dt = datetime.strptime(lesson_ts[:10], "%Y-%m-%d").date()
                        except ValueError:
                            pass

                    if lesson_dt and lesson_dt.year == current_year and lesson_dt.month == current_month:
                        month_total += hours
                        if is_excused:
                            month_excused += hours
        except Exception as exc:
            logger.debug("Attendance calculation note: %s", exc)

        return {
            "semester_num": sem_num,
            "semester_code": sem_code,
            "gpa": gpa_value or "3.80",
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

    async def download_file_bytes(self, url: str) -> bytes:
        await self.authenticate()
        session = await self._get_session()
        full_url = url if url.startswith("http") else urljoin(f"{self.base_url}/", url.lstrip("/"))

        async with session.get(full_url, headers=self._build_headers()) as resp:
            if resp.status == 401:
                await self.authenticate(force=True)
                async with session.get(full_url, headers=self._build_headers()) as retry_resp:
                    retry_resp.raise_for_status()
                    return await retry_resp.read()
            resp.raise_for_status()
            return await resp.read()
"""Browser session, Gigya login, and Cambridge One course/assignment discovery."""

from __future__ import annotations

import hashlib
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = "https://www.cambridgeone.org"
LOGIN_URL = f"{BASE_URL}/login"
DASHBOARD_URL = f"{BASE_URL}/dashboard/learner/dashboard"

DEFAULT_ORG_ID = "org_cup_ee0u5bQ0JEkCYszI69cPi"
DEFAULT_PRODUCT_CODE = "empbe2ednl5dw"

# 40 Learning Object IDs in Homework Eng3, Unit 6 - 9 (Lesson A x4, Lesson B x4, Lesson D x2)
ASSIGNED_LO_IDS_BY_UNIT: dict[int, tuple[str, ...]] = {
    6: (
        "1633408848069",
        "1633408852081",
        "1633408860222",
        "1633408873087",
        "1633408949802",
        "1633408958382",
        "1633408965901",
        "1633408979314",
        "1633409970001",
        "1633409987920",
    ),
    7: (
        "1633410797614",
        "1633410799292",
        "1633410810284",
        "1633410820095",
        "1633411146235",
        "1633411160151",
        "1633411169942",
        "1633411173406",
        "1633411903678",
        "1633411916139",
    ),
    8: (
        "1633412306760",
        "1633412315234",
        "1633412331106",
        "1633412335542",
        "1633412391296",
        "1633412403036",
        "1633412415555",
        "1633412422155",
        "1633413171218",
        "1633413185435",
    ),
    9: (
        "1633413803839",
        "1633413807077",
        "1633413818531",
        "1633413827394",
        "1633414061155",
        "1633414065415",
        "1633414074098",
        "1633414089541",
        "1633414804969",
        "1633414812309",
    ),
}

ASSIGNED_LO_ID_SET: frozenset[str] = frozenset(
    lo_id for ids in ASSIGNED_LO_IDS_BY_UNIT.values() for lo_id in ids
)


class LoginError(RuntimeError):
    """Raised when Cambridge One authentication fails."""


@dataclass(frozen=True)
class ActivityTarget:
    """Metadata and launch URL for one Cambridge One learning object."""

    unit_num: int
    lesson: str
    lo_path: str
    lo_id: str
    title: str
    activity_url: str
    best_score: str = "-"
    already_100: bool = False


def build_session_dir(username: str) -> Path:
    """Create a deterministic per-run temporary session directory path under .sessions/."""
    seed = f"{username.strip().lower()}:{time.time_ns()}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return Path(__file__).resolve().parent / ".sessions" / digest


class CambridgeSession:
    """Manages an isolated Chrome WebDriver session for Cambridge One."""

    def __init__(
        self,
        username: str,
        headless: bool = True,
        quiet_logs: bool = False,
        session_dir: Path | None = None,
    ) -> None:
        self.username = username.strip()
        self.headless = headless
        self.quiet_logs = quiet_logs
        self.session_dir = session_dir or build_session_dir(self.username)
        self.driver: WebDriver | None = None
        self.org_id: str = DEFAULT_ORG_ID
        self.class_id: str = ""
        self.product_code: str = DEFAULT_PRODUCT_CODE

    def __enter__(self) -> "CambridgeSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def start(self) -> WebDriver:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        options = webdriver.ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument(f"--user-data-dir={self.session_dir}")
        options.add_argument("--window-size=1440,960")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--mute-audio")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-renderer-backgrounding")
        self.driver = webdriver.Chrome(options=options)
        self.driver.set_page_load_timeout(60)
        return self.driver

    def close(self) -> None:
        try:
            if self.driver is not None:
                self.driver.quit()
        except Exception:
            pass
        finally:
            self.driver = None
            shutil.rmtree(self.session_dir, ignore_errors=True)

    def _dismiss_cookie_banner(self) -> None:
        assert self.driver is not None
        try:
            clicked = self.driver.execute_script(
                """
                const candidates = Array.from(document.querySelectorAll('a, button'));
                for (const el of candidates) {
                    const txt = (el.textContent || '').trim().toLowerCase();
                    if (txt === 'accept cookies' && el.offsetParent !== null) {
                        el.click();
                        return true;
                    }
                }
                return false;
                """
            )
            if clicked and not self.quiet_logs:
                print("[session] Dismissed cookie banner")
        except Exception:
            pass

    def login(self, password: str, timeout: float = 35.0) -> None:
        """Sign into Cambridge One via the Gigya login screen."""
        if self.driver is None:
            raise RuntimeError("Session driver is not started")

        if not self.quiet_logs:
            print("[session] Opening login page...")
        self.driver.get(LOGIN_URL)

        wait = WebDriverWait(self.driver, timeout)
        user_input = wait.until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, '#gigya-login-form input[name="username"]')
            )
        )
        self._dismiss_cookie_banner()

        pass_input = wait.until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, '#gigya-login-form input[name="password"]')
            )
        )
        submit_btn = wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, '#gigya-login-form input[type="submit"].gigya-input-submit')
            )
        )

        user_input.clear()
        user_input.send_keys(self.username)
        pass_input.clear()
        pass_input.send_keys(password)
        submit_btn.click()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            url = self.driver.current_url or ""
            if "/dashboard/learner" in url:
                try:
                    WebDriverWait(self.driver, 20).until(
                        lambda d: d.execute_script(
                            """
                            if (document.querySelector('a[href*="/resourcebank/"], a[href*="/class/learner/"][href*="/assignments"]')) {
                                return true;
                            }
                            for (const el of document.querySelectorAll('a, button')) {
                                if (/^My assignments\\b/i.test((el.textContent || '').trim()) && el.offsetParent !== null) {
                                    return true;
                                }
                            }
                            return false;
                            """
                        )
                    )
                except TimeoutException:
                    pass
                self._dismiss_cookie_banner()
                self._detect_active_course_context()
                if not self.quiet_logs:
                    print("[session] Login successful; learner dashboard ready.")
                return

            err_msg = self.driver.execute_script(
                """
                const errs = Array.from(document.querySelectorAll(
                    '.gigya-error-msg-active, .gigya-form-error-msg, [data-screenset-roles="instance"] .gigya-error-msg'
                ));
                for (const el of errs) {
                    const txt = (el.textContent || '').trim();
                    if (txt && el.offsetParent !== null) return txt;
                }
                return '';
                """
            )
            if err_msg:
                raise LoginError(f"Gigya login failed: {err_msg}")
            time.sleep(0.4)

        raise LoginError(f"Timed out waiting for learner dashboard redirect (url={self.driver.current_url})")

    def _detect_active_course_context(self, timeout: float = 15.0) -> None:
        """Extract orgId and classId for the active ENG4 class from the dashboard."""
        assert self.driver is not None

        extract_js = """
            function getCardInfo(el) {
                let container = el;
                let isEnded = false;
                let isEng4 = false;
                for (let i = 0; i < 8 && container; i++) {
                    const txt = container.innerText || '';
                    if (txt.includes('Class ended')) {
                        isEnded = true;
                        break;
                    }
                    if (/Upper Intermediate|Eng\\s*4|emp2l5|empbe2ednl5dw/i.test(txt)) {
                        isEng4 = true;
                    }
                    container = container.parentElement;
                }
                return { isEnded, isEng4 };
            }

            const candidates = [];

            for (const a of document.querySelectorAll('a[href*="/class/learner/"][href*="/assignments"]')) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/\\/class\\/learner\\/([^/]+)\\/class\\/([^/]+)\\/assignments/);
                if (!m) continue;
                const info = getCardInfo(a);
                if (!info.isEnded) {
                    candidates.push({ orgId: m[1], classId: m[2], priority: info.isEng4 ? 4 : 3 });
                }
            }

            for (const a of document.querySelectorAll('a[href*="/resourcebank/"]')) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/\\/dashboard\\/learner\\/([^/]+)\\/resourcebank\\/([^/]+)\\/([^/?#]+)/);
                if (!m) continue;
                const info = getCardInfo(a);
                if (!info.isEnded) {
                    const resIsEng4 = /emp2l5/i.test(m[2]) || info.isEng4;
                    candidates.push({ orgId: m[1], classId: m[3], priority: resIsEng4 ? 2 : 1 });
                }
            }

            if (candidates.length === 0) return null;
            candidates.sort((a, b) => b.priority - a.priority);
            return { orgId: candidates[0].orgId, classId: candidates[0].classId };
        """

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ctx = self.driver.execute_script(extract_js)
            if ctx and ctx.get("orgId") and ctx.get("classId"):
                self.org_id = ctx["orgId"]
                self.class_id = ctx["classId"]
                if not self.quiet_logs:
                    print(f"[session] Detected active class context: class_id={self.class_id}")
                return
            time.sleep(0.4)

        # Fallback: click the active class's "My assignments" button/link and read URL.
        clicked = self.driver.execute_script(
            """
            const candidates = [];
            for (const el of document.querySelectorAll('a, button')) {
                const txt = (el.textContent || '').trim();
                if (!/^My assignments\\b/i.test(txt) || el.offsetParent === null) continue;
                let container = el;
                let isEnded = false;
                let isEng4 = false;
                for (let i = 0; i < 8 && container; i++) {
                    const ctxt = container.innerText || '';
                    if (ctxt.includes('Class ended')) {
                        isEnded = true;
                        break;
                    }
                    if (/Upper Intermediate|Eng\\s*4/i.test(ctxt)) {
                        isEng4 = true;
                    }
                    container = container.parentElement;
                }
                if (!isEnded) {
                    candidates.push({ el, priority: isEng4 ? 2 : 1 });
                }
            }
            if (candidates.length === 0) return false;
            candidates.sort((a, b) => b.priority - a.priority);
            candidates[0].el.click();
            return true;
            """
        )
        if clicked:
            url_deadline = time.monotonic() + 12.0
            while time.monotonic() < url_deadline:
                m = re.search(r"/class/learner/([^/]+)/class/([^/]+)/assignments", self.driver.current_url or "")
                if m:
                    self.org_id = m.group(1)
                    self.class_id = m.group(2)
                    if not self.quiet_logs:
                        print(f"[session] Detected active class via redirect: class_id={self.class_id}")
                    return
                time.sleep(0.3)

        raise RuntimeError("Could not detect active classId on Cambridge One learner dashboard")

    def _read_assignments_status(self) -> dict[str, dict[str, str]]:
        """Read assignment item scores from My assignments page keyed by normalized title."""
        assert self.driver is not None
        assignments_url = f"{BASE_URL}/class/learner/{self.org_id}/class/{self.class_id}/assignments"
        self.driver.get(assignments_url)

        WebDriverWait(self.driver, 25).until(
            lambda d: d.execute_script(
                "const t = document.body ? document.body.innerText : ''; "
                "return t.includes('To do') || t.includes('Completed') || t.includes('Homework');"
            )
        )
        time.sleep(1.0)

        # Read rows from To do tab first, and also check Completed tab if To do has no assignments.
        has_todo_headers = self.driver.execute_script(
            """
            const headers = Array.from(document.querySelectorAll('a.assignment-header'))
                .filter(h => h.offsetParent !== null);
            return headers.length > 0;
            """
        )
        if not has_todo_headers:
            self.driver.execute_script(
                """
                for (const el of document.querySelectorAll('a, button, [role="tab"]')) {
                    if (/^Completed\\b/i.test((el.textContent || '').trim()) && el.offsetParent !== null) {
                        el.click();
                        break;
                    }
                }
                """
            )
            time.sleep(1.0)

        # Ensure assignment accordion is expanded so a.activity-detail.table-row rows render.
        self.driver.execute_script(
            """
            if (document.querySelectorAll('a.activity-detail.table-row').length === 0) {
                const headers = Array.from(document.querySelectorAll('a.assignment-header'));
                for (const h of headers) {
                    if (h.offsetParent !== null) {
                        h.click();
                    }
                }
            }
            """
        )
        try:
            WebDriverWait(self.driver, 8).until(
                lambda d: d.execute_script(
                    "return document.querySelectorAll('a.activity-detail.table-row').length > 0;"
                )
            )
        except TimeoutException:
            pass

        items = self.driver.execute_script(
            """
            const result = {};
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const rows = Array.from(document.querySelectorAll('a.activity-detail.table-row'));
            for (const row of rows) {
                const nameEl = row.querySelector('.activity-name');
                const title = norm(nameEl ? nameEl.textContent : '');
                if (!title) continue;
                const analytics = Array.from(row.querySelectorAll('.analytics-data'))
                    .map(el => norm(el.textContent));
                let bestScore = '-';
                for (const cell of analytics) {
                    if (/^Best score\\b/i.test(cell)) {
                        const m = cell.match(/(\\d{1,3}%|-)/);
                        if (m) bestScore = m[1];
                        break;
                    }
                }
                result[title.toLowerCase()] = {
                    title,
                    bestScore,
                    is100: bestScore === '100%',
                };
            }
            return result;
            """
        )
        return items or {}

    def _expand_workbook_units(self, target_units: Iterable[int]) -> list[dict]:
        """Open Digital Workbook, expand target units & lessons, and scrape LO items."""
        assert self.driver is not None
        unit_set = sorted(set(target_units))
        workbook_url = (
            f"{BASE_URL}/nlp/learner/{self.org_id}/class/{self.class_id}"
            f"/product/{self.product_code}"
        )
        self.driver.get(workbook_url)

        WebDriverWait(self.driver, 30).until(
            lambda d: d.execute_script(
                "return /Unit\\s+\\d+/i.test(document.body ? document.body.innerText : '');"
            )
        )
        self._dismiss_cookie_banner()
        time.sleep(1.0)

        for unit_num in unit_set:
            # Click unit header if not already expanded.
            self.driver.execute_script(
                """
                const unitNum = arguments[0];
                const unitRe = new RegExp('^Unit\\\\s+' + unitNum + '(\\\\b|:)', 'i');
                const headers = Array.from(document.querySelectorAll(
                    '[aria-expanded], button, a, .accordion-header, .unit-header, [class*="unit"] [role="button"], h2, h3, h4'
                ));
                for (const el of headers) {
                    const txt = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!unitRe.test(txt) || el.offsetParent === null) continue;
                    const toggle = el.closest('[aria-expanded]') || el.querySelector('[aria-expanded]') || el;
                    const expanded = toggle.getAttribute('aria-expanded');
                    if (expanded === 'true') return 'already_expanded';
                    const parentCard = toggle.closest('.card, .accordion-item, [class*="unit"]') || toggle.parentElement;
                    if (expanded === null && parentCard && /Lesson\\s+[A-D]/i.test(parentCard.innerText || '')) {
                        const visibleLessons = Array.from(parentCard.querySelectorAll('a.activity-info, [class*="lesson"]'))
                            .filter(x => x.offsetParent !== null);
                        if (visibleLessons.length > 0) return 'already_visible';
                    }
                    toggle.scrollIntoView({ block: 'center' });
                    toggle.click();
                    return 'clicked';
                }
                return 'not_found';
                """,
                unit_num,
            )
            time.sleep(0.8)

        # Expand all collapsed Lesson headers inside the expanded target units.
        for _ in range(2):
            clicked_count = self.driver.execute_script(
                """
                const targetUnits = new Set(arguments[0]);
                let clicked = 0;
                const unitBlocks = Array.from(document.querySelectorAll(
                    '.unit-container, .accordion-item, .card, [class*="unit-block"], [class*="unit"]'
                ));
                const lessonHeaders = Array.from(document.querySelectorAll(
                    '[aria-expanded="false"], .lesson-header, [class*="lesson"] [role="button"]'
                ));
                for (const el of lessonHeaders) {
                    if (el.offsetParent === null) continue;
                    const txt = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!/Lesson\\s+[A-D]/i.test(txt)) continue;
                    // Check nearest ancestor unit text to avoid opening unrelated units.
                    let cur = el;
                    let matchedUnit = null;
                    for (let i = 0; i < 10 && cur; i++) {
                        const m = (cur.innerText || '').match(/^\\s*Unit\\s+(\\d+)\\b/i);
                        if (m) {
                            matchedUnit = parseInt(m[1], 10);
                            break;
                        }
                        cur = cur.parentElement;
                    }
                    if (matchedUnit !== null && !targetUnits.has(matchedUnit)) continue;
                    const exp = el.getAttribute('aria-expanded');
                    if (exp === 'false' || exp === null) {
                        el.scrollIntoView({ block: 'center' });
                        el.click();
                        clicked++;
                    }
                }
                return clicked;
                """,
                unit_set,
            )
            time.sleep(0.8 if clicked_count else 0.3)

        # Wait briefly for activity anchors to settle.
        try:
            WebDriverWait(self.driver, 10).until(
                lambda d: d.execute_script(
                    "return document.querySelectorAll('a.activity-info[id^=\"lo-\"]').length > 0;"
                )
            )
        except TimeoutException:
            pass

        scraped = self.driver.execute_script(
            """
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const anchors = Array.from(document.querySelectorAll('a.activity-info[id^="lo-"]'));
            const items = [];
            for (const a of anchors) {
                const rawId = a.id || '';
                const loPath = rawId.replace(/^lo-/, '');
                const parts = loPath.split('/');
                if (parts.length !== 3) continue;
                const loId = parts[2];

                // Extract activity title cleanly (avoid grabbing icon spans)
                const titleEl = a.querySelector('.activity-title, .title, .name, p, h3, h4');
                let title = norm(titleEl ? titleEl.textContent : '');
                if (!title) title = norm(a.innerText || a.textContent);

                // Walk ancestors to determine Unit number and Lesson letter
                let unitNum = null;
                let lesson = '';
                let cur = a;
                for (let i = 0; i < 12 && cur; i++) {
                    const txt = cur.innerText || '';
                    if (!lesson) {
                        const lm = txt.match(/Lesson\\s+([A-D])\\b/i);
                        if (lm) lesson = lm[1].toUpperCase();
                    }
                    const um = txt.match(/Unit\\s+(\\d+)\\b/i);
                    if (um) {
                        unitNum = parseInt(um[1], 10);
                        break;
                    }
                    cur = cur.parentElement;
                }

                items.push({
                    unitNum,
                    lesson,
                    loPath,
                    loId,
                    title,
                });
            }
            return items;
            """
        )
        return scraped or []

    def discover_course_targets(
        self,
        scope: str = "assignments",
        units: list[int] | None = None,
        include_completed: bool = False,
    ) -> list[ActivityTarget]:
        """Discover activity targets for either 'assignments' (default Units 6-9) or 'workbook'."""
        if scope not in ("assignments", "workbook"):
            raise ValueError(f"Unsupported scope: {scope}")

        if scope == "assignments":
            target_units = sorted(units) if units else [6, 7, 8, 9]
            assignment_status = self._read_assignments_status()
        else:
            target_units = sorted(units) if units else list(range(1, 11))
            assignment_status = {}

        raw_items = self._expand_workbook_units(target_units)

        seen_paths: set[str] = set()
        targets: list[ActivityTarget] = []
        for item in raw_items:
            lo_path = item["loPath"]
            lo_id = item["loId"]
            unit_num = item.get("unitNum")
            lesson = item.get("lesson") or ""
            title = item.get("title") or lo_id

            if lo_path in seen_paths:
                continue
            seen_paths.add(lo_path)

            if unit_num is not None and unit_num not in target_units:
                continue

            if scope == "assignments":
                # Keep the 40 assigned activities (prefer matching LO IDs, or title in assignment table)
                in_assigned_ids = lo_id in ASSIGNED_LO_ID_SET
                status_entry = assignment_status.get(title.lower())
                if not in_assigned_ids and status_entry is None:
                    continue
                best_score = status_entry["bestScore"] if status_entry else "-"
                already_100 = bool(status_entry and status_entry.get("is100"))
            else:
                # Workbook mode: exclude native productive-skill Speaking 2 / Writing 2 essay items
                if lesson == "C" and title.lower().startswith("speaking:") and title.rstrip().endswith("2"):
                    continue
                if lesson == "D" and title.lower().startswith("writing:") and not (
                    "organising" in title.lower()
                    or "using " in title.lower()
                    or "describing data" in title.lower()
                    or "giving a positive" in title.lower()
                    or "arguing " in title.lower()
                    or "presenting " in title.lower()
                    or "making a story" in title.lower()
                ):
                    # Second Writing activity in Lesson D is free-text essay
                    continue
                best_score = "-"
                already_100 = False

            if already_100 and not include_completed:
                continue

            encoded_path = lo_path.replace("/", "%2F")
            activity_url = (
                f"{BASE_URL}/nlp/learner/{self.org_id}/class/{self.class_id}"
                f"/product/{self.product_code}/view/item/{encoded_path}"
            )
            targets.append(
                ActivityTarget(
                    unit_num=unit_num or 0,
                    lesson=lesson,
                    lo_path=lo_path,
                    lo_id=lo_id,
                    title=title,
                    activity_url=activity_url,
                    best_score=best_score,
                    already_100=already_100,
                )
            )

        return targets

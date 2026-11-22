"""Pre-check, post-check, and final-score verification guards for Cambridge One."""

from __future__ import annotations

import re
from typing import Any

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


class AnswerGuardError(RuntimeError):
    """Raised when an exercise step or final score fails 100% verification."""


_SCORE_RE = re.compile(r"You\s+scored\s+(\d+)\s+out\s+of\s+(\d+)", re.IGNORECASE)

_INCORRECT_SELECTORS = (
    ".checked-incorrect",
    ".is-incorrect",
    ".incorrect",
    ".wrong",
    ".checked-partially-correct",
    ".partially-correct",
    ".not-correct",
)


def _switch_to_activity_iframe(driver: WebDriver, timeout: float = 15.0) -> None:
    driver.switch_to.default_content()
    iframe = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, 'iframe[id^="iframe_"]'))
    )
    driver.switch_to.frame(iframe)


def verify_step_ready_before_check(
    driver: WebDriver,
    step_idx: int,
    step_spec: dict[str, Any],
    timeout: float = 15.0,
) -> None:
    """Verify all expected step inputs are filled and the Check button is visible."""
    _switch_to_activity_iframe(driver, timeout=timeout)
    try:
        status = driver.execute_script(
            """
            const stepIdx = arguments[0];
            const spec = arguments[1];
            const wrap = document.getElementById('content_wrap_' + stepIdx);
            if (!wrap) return { ok: false, reason: 'Missing #content_wrap_' + stepIdx };

            const textInputs = Array.from(wrap.querySelectorAll('input[type="text"]'))
                .filter(el => el.offsetParent !== null);
            const expectedTextCount = (spec.textEntries || []).length;
            if (textInputs.length < expectedTextCount) {
                return { ok: false, reason: `Expected ${expectedTextCount} text inputs, found ${textInputs.length}` };
            }
            const normText = s => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
            for (let i = 0; i < expectedTextCount; i++) {
                const expectedAns = normText((spec.textEntries[i] || {}).answer);
                const actualVal = normText(textInputs[i].value);
                if (!actualVal || actualVal !== expectedAns) {
                    return {
                        ok: false,
                        reason: `Text input #${i} mismatch: got '${actualVal}', expected '${expectedAns}'`,
                    };
                }
            }

            const expectedGapMatches = spec.gapMatches || [];
            for (const gm of expectedGapMatches) {
                const expectedCount = (gm.pairs || []).length;
                const gaps = Array.from(wrap.querySelectorAll('.drop_area.gap_match_gap_view'))
                    .filter(el => el.offsetParent !== null);
                const filledGaps = gaps.filter(g => g.querySelector('.drag_element.gap_match_drag') !== null);
                if (filledGaps.length < expectedCount) {
                    return {
                        ok: false,
                        reason: `GapMatch filled ${filledGaps.length}/${expectedCount} gaps`,
                    };
                }
            }

            const expectedInlineCount = (spec.inlineChoices || []).length;
            if (expectedInlineCount > 0) {
                const filledDropdowns = Array.from(
                    wrap.querySelectorAll('.wrapper-dropdown.listbox.filled, .wrapper-dropdown.listbox.listbox--filled')
                ).filter(el => el.offsetParent !== null);
                if (filledDropdowns.length < expectedInlineCount) {
                    return {
                        ok: false,
                        reason: `InlineChoice filled ${filledDropdowns.length}/${expectedInlineCount} dropdowns`,
                    };
                }
            }

            const expectedChoices = spec.choices || [];
            if (expectedChoices.length > 0) {
                const groups = Array.from(wrap.querySelectorAll('.choice_interaction'))
                    .filter(el => el.offsetParent !== null);
                for (let i = 0; i < expectedChoices.length; i++) {
                    const grp = groups[i] || wrap;
                    const selected = grp.querySelectorAll(
                        '[aria-checked="true"], .selected, input:checked'
                    );
                    if (selected.length === 0) {
                        return { ok: false, reason: `Choice group #${i} has no selection` };
                    }
                }
            }

            return { ok: true };
            """,
            step_idx,
            step_spec,
        )
        if not status or not status.get("ok"):
            reason = (status or {}).get("reason", "Unknown pre-check failure")
            raise AnswerGuardError(f"Step {step_idx + 1} pre-check failed: {reason}")
    finally:
        driver.switch_to.default_content()

    try:
        check_btn = WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, 'a.btn.green-btn[title="Check"]'))
        )
    except Exception as exc:
        raise AnswerGuardError(
            f"Step {step_idx + 1} Check button did not appear after filling answers"
        ) from exc

    if not check_btn.is_displayed():
        raise AnswerGuardError(f"Step {step_idx + 1} Check button is not displayed")


def verify_step_100_after_check(
    driver: WebDriver,
    step_idx: int,
    timeout: float = 15.0,
) -> None:
    """Verify zero incorrect markers appear in the active step after clicking Check."""
    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, 'a.btn.green-btn[title="Next"]'))
        )
    except Exception as exc:
        raise AnswerGuardError(
            f"Step {step_idx + 1} Next button did not appear after clicking Check"
        ) from exc

    _switch_to_activity_iframe(driver, timeout=timeout)
    try:
        result = driver.execute_script(
            """
            const stepIdx = arguments[0];
            const selectors = arguments[1];
            const wrap = document.getElementById('content_wrap_' + stepIdx);
            if (!wrap) return { ok: false, reason: 'Missing #content_wrap_' + stepIdx };

            const bad = [];
            for (const sel of selectors) {
                const hits = Array.from(wrap.querySelectorAll(sel));
                if (hits.length > 0) {
                    bad.push(`${sel} (${hits.length})`);
                }
            }
            if (bad.length > 0) {
                return { ok: false, reason: 'Incorrect markers found: ' + bad.join(', ') };
            }
            return { ok: true };
            """,
            step_idx,
            list(_INCORRECT_SELECTORS),
        )
        if not result or not result.get("ok"):
            reason = (result or {}).get("reason", "Unknown post-check failure")
            raise AnswerGuardError(f"Step {step_idx + 1} post-check failed: {reason}")
    finally:
        driver.switch_to.default_content()


def verify_final_score_100(driver: WebDriver, timeout: float = 20.0) -> tuple[int, int]:
    """Wait for the Angular completion screen and assert 100% score (X out of X)."""
    driver.switch_to.default_content()

    def _find_score(drv: WebDriver) -> tuple[int, int] | None:
        text = drv.execute_script(
            "return document.body ? (document.body.innerText || '') : '';"
        ) or ""
        m = _SCORE_RE.search(text)
        if not m:
            return None
        return int(m.group(1)), int(m.group(2))

    try:
        scored, total = WebDriverWait(driver, timeout).until(_find_score)
    except Exception as exc:
        raise AnswerGuardError("Completion score screen ('You scored X out of Y') did not appear") from exc

    if total <= 0 or scored != total:
        raise AnswerGuardError(f"Non-100% score detected: {scored} out of {total}")

    return scored, total

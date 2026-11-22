"""Avallain Buckminster QTI XML solver for Cambridge One exercises."""

from __future__ import annotations

import random
import time
from typing import Any

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from answer_guard import (
    AnswerGuardError,
    verify_final_score_100,
    verify_step_100_after_check,
    verify_step_ready_before_check,
)

_PARSE_AJAX_DATA_JS = """
const ajaxData = window.ajaxData || {};
const loXmlText = ajaxData['LearningObjectInfo.xml'];
if (!loXmlText) return null;

const parser = new DOMParser();
const norm = s => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();

const loDoc = parser.parseFromString(loXmlText, 'text/xml');
const screens = Array.from(loDoc.querySelectorAll('screens > screen'));
const result = [];

for (let idx = 0; idx < screens.length; idx++) {
    const scr = screens[idx];
    const nameEl = scr.querySelector('name');
    const typeEl = scr.querySelector('activityTypeIdentifier');
    const maxScoreEl = scr.querySelector('maximumscore');
    const screenName = nameEl ? norm(nameEl.textContent) : '';
    const catXmlText = ajaxData[screenName];

    const stepSpec = {
        index: idx,
        screenName,
        activityType: typeEl ? norm(typeEl.textContent) : '',
        maxScore: maxScoreEl ? parseInt(norm(maxScoreEl.textContent) || '0', 10) : 0,
        isPresentation: false,
        gapMatches: [],
        inlineChoices: [],
        textEntries: [],
        choices: [],
    };

    if (!catXmlText) {
        stepSpec.isPresentation = true;
        result.push(stepSpec);
        continue;
    }

    const catDoc = parser.parseFromString(catXmlText, 'text/xml');
    const assessEl = catDoc.querySelector('assessmentItem');
    if (!stepSpec.activityType && assessEl) {
        stepSpec.activityType = norm(assessEl.getAttribute('identifier') || '');
    }

    const responses = {};
    for (const rd of Array.from(catDoc.querySelectorAll('responseDeclaration'))) {
        const id = rd.getAttribute('identifier') || '';
        const vals = Array.from(rd.querySelectorAll('correctResponse > value'))
            .map(v => v.textContent || '');
        responses[id] = vals;
    }

    const itemBody = catDoc.querySelector('itemBody') || catDoc;

    // 1. gapMatchInteraction
    for (const gm of Array.from(itemBody.querySelectorAll('gapMatchInteraction'))) {
        const respId = gm.getAttribute('responseIdentifier') || 'RESPONSE';
        const rawPairs = responses[respId] || [];
        const gapTextMap = {};
        for (const gt of Array.from(gm.querySelectorAll('gapText'))) {
            const gtId = gt.getAttribute('identifier') || '';
            gapTextMap[gtId] = norm(gt.textContent);
        }
        const gapOrder = Array.from(gm.querySelectorAll('gap'))
            .map(g => g.getAttribute('identifier') || '');

        const pairsByGap = {};
        for (const pairStr of rawPairs) {
            const parts = norm(pairStr).split(' ');
            if (parts.length < 2) continue;
            const gtId = parts[0];
            const gapId = parts[1];
            if (!pairsByGap[gapId]) pairsByGap[gapId] = [];
            pairsByGap[gapId].push(gtId);
        }

        const usedGtIds = new Set();
        const parsedPairs = [];
        for (let gIdx = 0; gIdx < gapOrder.length; gIdx++) {
            const gapId = gapOrder[gIdx];
            const candidates = pairsByGap[gapId] || [];
            if (candidates.length === 0) continue;
            const canonicalId = 'GT' + gapId + '_0';
            let chosenGtId = candidates.find(id => id === canonicalId && !usedGtIds.has(id));
            if (!chosenGtId) chosenGtId = candidates.find(id => id === canonicalId);
            if (!chosenGtId) chosenGtId = candidates.find(id => !usedGtIds.has(id));
            if (!chosenGtId) chosenGtId = candidates[0];
            usedGtIds.add(chosenGtId);
            parsedPairs.push({
                gtId: chosenGtId,
                gapId,
                gapOrderIdx: gIdx,
                text: gapTextMap[chosenGtId] || '',
            });
        }

        stepSpec.gapMatches.push({
            respId,
            pairs: parsedPairs,
        });
    }

    // 2. inlineChoiceInteraction
    for (const ic of Array.from(itemBody.querySelectorAll('inlineChoiceInteraction'))) {
        const respId = ic.getAttribute('responseIdentifier') || '';
        const vals = responses[respId] || [];
        const targetId = vals.length > 0 ? norm(vals[0]) : '';
        let targetText = '';
        for (const opt of Array.from(ic.querySelectorAll('inlineChoice'))) {
            if (norm(opt.getAttribute('identifier')) === targetId) {
                targetText = norm(opt.textContent);
                break;
            }
        }
        stepSpec.inlineChoices.push({
            respId,
            targetId,
            targetText,
        });
    }

    // 3. textEntryInteraction
    for (const te of Array.from(itemBody.querySelectorAll('textEntryInteraction'))) {
        const respId = te.getAttribute('responseIdentifier') || '';
        const vals = responses[respId] || [];
        const raw = vals.length > 0 ? vals[0] : '';
        // Strip "|cue_word" suffix and "{x}" hint braces, preserving \u00a0 for characters-in-gap masks
        const answer = raw
            .split('|')[0]
            .replace(/\\{([^}]*)\\}/g, '$1')
            .trim();
        stepSpec.textEntries.push({
            respId,
            answer,
        });
    }

    // 4. choiceInteraction (Radiobutton / Checkbox)
    for (const ci of Array.from(itemBody.querySelectorAll('choiceInteraction'))) {
        const respId = ci.getAttribute('responseIdentifier') || '';
        const vals = (responses[respId] || []).map(v => norm(v));
        const allOptions = Array.from(ci.querySelectorAll('simpleChoice')).map((sc, idxOpt) => ({
            id: norm(sc.getAttribute('identifier')),
            text: norm(sc.textContent),
            idx: idxOpt,
        }));
        const targets = allOptions.filter(o => vals.includes(o.id));
        stepSpec.choices.push({
            respId,
            maxChoices: parseInt(ci.getAttribute('maxChoices') || '1', 10),
            targets,
        });
    }

    const totalInteractions =
        stepSpec.gapMatches.length +
        stepSpec.inlineChoices.length +
        stepSpec.textEntries.length +
        stepSpec.choices.length;
    if (totalInteractions === 0 || stepSpec.activityType === 'Present:Present:Present') {
        stepSpec.isPresentation = true;
    }

    result.push(stepSpec);
}
return result;
"""


def _switch_to_iframe(driver: WebDriver, timeout: float = 20.0) -> None:
    driver.switch_to.default_content()
    iframe = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, 'iframe[id^="iframe_"]'))
    )
    driver.switch_to.frame(iframe)


def _get_active_step_index(driver: WebDriver) -> int | None:
    """Detect the currently active 0-based step index inside the activity iframe."""
    return driver.execute_script(
        """
        const wraps = Array.from(document.querySelectorAll('.content-wrap[id^="content_wrap_"]'));
        for (const w of wraps) {
            if (w.offsetParent !== null) {
                const m = (w.id || '').match(/^content_wrap_(\\d+)$/);
                if (m) return parseInt(m[1], 10);
            }
        }
        const steps = Array.from(document.querySelectorAll('header#activity-header ul.progress-bar > li.step'));
        for (let i = 0; i < steps.length; i++) {
            if (steps[i].classList.contains('current')) return i;
        }
        return null;
        """
    )


def _native_click(driver: WebDriver, element: Any) -> None:
    """Scroll an element into view and click with native WebDriver mouse events."""
    driver.execute_script("arguments[0].scrollIntoView({ block: 'center' });", element)
    try:
        element.click()
    except Exception:
        from selenium.webdriver.common.action_chains import ActionChains

        ActionChains(driver).move_to_element(element).click().perform()


def _solve_gap_matches(driver: WebDriver, step_idx: int, gap_matches: list[dict[str, Any]]) -> None:
    """Place wordpool chips into gaps by clicking pool buttons in target gap order."""
    # Reset any previously placed chips (e.g. from an interrupted attempt) back to the pool.
    for _ in range(30):
        placed_btn = driver.execute_script(
            """
            const stepIdx = arguments[0];
            const wrap = document.getElementById('content_wrap_' + stepIdx);
            if (!wrap) return null;
            const gaps = Array.from(wrap.querySelectorAll('.drop_area.gap_match_gap_view'))
                .filter(el => el.offsetParent !== null);
            for (const g of gaps) {
                const btn = g.querySelector('.drag_element.gap_match_drag > button');
                if (btn) return btn;
            }
            return null;
            """,
            step_idx,
        )
        if not placed_btn:
            break
        _native_click(driver, placed_btn)
        time.sleep(0.1)

    for gm in gap_matches:
        for pair in gm.get("pairs", []):
            target_text = pair.get("text", "")
            res = driver.execute_script(
                """
                const stepIdx = arguments[0];
                const targetText = arguments[1];
                const wrap = document.getElementById('content_wrap_' + stepIdx);
                if (!wrap) return { ok: false, reason: 'Missing wrap' };

                const norm = s => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
                const chips = Array.from(
                    wrap.querySelectorAll(
                        'ul.pool li.drop_area.gap_match_gap_text_view.has_drag > .droppable.drag_holder > .drag_element.gap_match_drag'
                    )
                );
                for (const chip of chips) {
                    if (chip.offsetParent === null) continue;
                    const btn = chip.querySelector('button');
                    if (!btn || btn.disabled) continue;
                    const contentEl = chip.querySelector('.draggable__content') ||
                        Array.from(chip.children).find(el => el.tagName !== 'BUTTON') || chip;
                    const txt = norm(contentEl.textContent);
                    if (txt === targetText) {
                        return { ok: true, btn };
                    }
                }
                const targetLower = targetText.toLowerCase();
                for (const chip of chips) {
                    if (chip.offsetParent === null) continue;
                    const btn = chip.querySelector('button');
                    if (!btn || btn.disabled) continue;
                    const contentEl = chip.querySelector('.draggable__content') ||
                        Array.from(chip.children).find(el => el.tagName !== 'BUTTON') || chip;
                    const txt = norm(contentEl.textContent).toLowerCase();
                    if (txt === targetLower) {
                        return { ok: true, btn };
                    }
                }
                return { ok: false, reason: 'No matching chip found in pool for: ' + targetText };
                """,
                step_idx,
                target_text,
            )
            if not res or not res.get("ok") or not res.get("btn"):
                raise AnswerGuardError(
                    f"Step {step_idx + 1} gapMatch failed: {(res or {}).get('reason')}"
                )
            _native_click(driver, res["btn"])
            time.sleep(0.12)


def _solve_inline_choices(
    driver: WebDriver, step_idx: int, inline_choices: list[dict[str, Any]]
) -> None:
    """Select each inline dropdown option inside #content_wrap_<step_idx>."""
    for cb_idx, ic in enumerate(inline_choices):
        target_text = ic.get("targetText", "")
        selected_ok = False
        last_reason = "Dropdown selection did not commit .filled"

        for _ in range(3):
            res = driver.execute_script(
                """
                const stepIdx = arguments[0];
                const cbIdx = arguments[1];
                const targetText = arguments[2];
                const wrap = document.getElementById('content_wrap_' + stepIdx);
                if (!wrap) return { ok: false, reason: 'Missing wrap' };
                const norm = s => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
                const wrappers = Array.from(wrap.querySelectorAll('.wrapper-dropdown.listbox'))
                    .filter(el => el.offsetParent !== null);
                const wrapper = wrappers[cbIdx];
                if (!wrapper) {
                    return { ok: false, reason: `Dropdown wrapper #${cbIdx} not found (total=${wrappers.length})` };
                }
                const btn = wrapper.querySelector('button[role="combobox"]');
                if (!btn) return { ok: false, reason: `Combobox button #${cbIdx} not found` };

                const isFilled = () =>
                    wrapper.classList.contains('filled') || wrapper.classList.contains('listbox--filled');
                const matchesTarget = () => {
                    const cur = norm(btn.textContent);
                    return cur === targetText || cur.toLowerCase() === targetText.toLowerCase();
                };

                if (isFilled() && matchesTarget()) {
                    return { ok: true, alreadyMatched: true };
                }

                btn.scrollIntoView({ block: 'center' });
                if (btn.getAttribute('aria-expanded') !== 'true') {
                    btn.click();
                }

                const opts = Array.from(
                    wrapper.querySelectorAll('ul.listbox__choices > li.listbox__choice[role="option"]')
                );
                let opt = opts.find(o => norm(o.textContent) === targetText);
                if (!opt) {
                    const tLower = targetText.toLowerCase();
                    opt = opts.find(o => norm(o.textContent).toLowerCase() === tLower);
                }
                if (!opt) {
                    return {
                        ok: false,
                        reason: 'Option not found: ' + targetText + ' in [' + opts.map(o => norm(o.textContent)).join(' | ') + ']',
                    };
                }

                opt.click();
                return {
                    ok: isFilled() && matchesTarget(),
                    btn,
                    opt,
                    reason: `After JS click: cls='${wrapper.className}', text='${norm(btn.textContent)}'`,
                };
                """,
                step_idx,
                cb_idx,
                target_text,
            )
            if res and res.get("ok"):
                selected_ok = True
                break

            # Fallback: native click on btn then JS click on opt
            if res and res.get("btn") and res.get("opt"):
                try:
                    _native_click(driver, res["btn"])
                    time.sleep(0.15)
                    driver.execute_script("arguments[0].click();", res["opt"])
                    time.sleep(0.15)
                    chk = driver.execute_script(
                        """
                        const stepIdx = arguments[0];
                        const cbIdx = arguments[1];
                        const wrap = document.getElementById('content_wrap_' + stepIdx);
                        const wrappers = Array.from(wrap.querySelectorAll('.wrapper-dropdown.listbox'))
                            .filter(el => el.offsetParent !== null);
                        const w = wrappers[cbIdx];
                        return Boolean(w && (w.classList.contains('filled') || w.classList.contains('listbox--filled')));
                        """,
                        step_idx,
                        cb_idx,
                    )
                    if chk:
                        selected_ok = True
                        break
                except Exception:
                    pass

            last_reason = (res or {}).get("reason", last_reason)
            time.sleep(0.15)

        if not selected_ok:
            raise AnswerGuardError(f"Step {step_idx + 1} inlineChoice select failed: {last_reason}")
        time.sleep(0.08)


def _norm_nbsp(text: str) -> str:
    return " ".join((text or "").replace("\u00a0", " ").split())


def _solve_text_entries(
    driver: WebDriver, step_idx: int, text_entries: list[dict[str, Any]]
) -> None:
    """Fill visible text input gaps in DOM order, handling characters-in-gap masks."""
    input_infos = driver.execute_script(
        """
        const stepIdx = arguments[0];
        const wrap = document.getElementById('content_wrap_' + stepIdx);
        if (!wrap) return null;
        const inputs = Array.from(wrap.querySelectorAll('input[type="text"]'))
            .filter(el => el.offsetParent !== null);
        return inputs.map(el => ({
            el,
            placeholder: el.placeholder || '',
            value: el.value || '',
        }));
        """,
        step_idx,
    )
    if input_infos is None:
        raise AnswerGuardError(f"Step {step_idx + 1} missing #content_wrap_{step_idx}")
    if len(input_infos) < len(text_entries):
        raise AnswerGuardError(
            f"Step {step_idx + 1} expected {len(text_entries)} text inputs, found {len(input_infos)}"
        )

    for idx_in, entry in enumerate(text_entries):
        answer = entry.get("answer", "")
        info = input_infos[idx_in]
        inp = info["el"]
        placeholder = info.get("placeholder", "")
        cur_value = info.get("value", "")

        if _norm_nbsp(cur_value) == _norm_nbsp(answer):
            continue

        if "_" in placeholder:
            if len(placeholder) == len(answer):
                chars_to_type = "".join(a for p, a in zip(placeholder, answer) if p == "_")
            else:
                ph_compact = placeholder.replace("\u00a0", " ")
                ans_compact = answer.replace("\u00a0", " ")
                if len(ph_compact) == len(ans_compact):
                    chars_to_type = "".join(
                        a for p, a in zip(ph_compact, ans_compact) if p == "_"
                    )
                else:
                    # Alignment fallback: strip hint prefixes that appear in placeholder
                    chars_to_type = answer.replace("\u00a0", "").replace(" ", "")
        else:
            chars_to_type = ""

        for attempt in range(4):
            actual = inp.get_attribute("value") or ""
            if _norm_nbsp(actual) == _norm_nbsp(answer):
                break

            if "_" in placeholder:
                if attempt == 0:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({ block: 'center' });",
                        inp,
                    )
                    _native_click(driver, inp)
                    time.sleep(0.08)
                else:
                    time.sleep(0.25)
                    driver.execute_script(
                        """
                        const el = arguments[0];
                        const ph = el.placeholder || '';
                        el.blur();
                        const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                        if (desc && desc.set) desc.set.call(el, ph);
                        else el.value = ph;
                        """,
                        inp,
                    )
                    _native_click(driver, inp)
                    time.sleep(0.1)

                driver.execute_script(
                    """
                    const el = arguments[0];
                    const ph = el.placeholder || '';
                    if ((el.value || '') === ph && el.setSelectionRange) {
                        const firstIdx = ph.indexOf('_');
                        if (firstIdx >= 0) el.setSelectionRange(firstIdx, firstIdx);
                    }
                    """,
                    inp,
                )
                inp.send_keys(chars_to_type)
            else:
                driver.execute_script(
                    """
                    const el = arguments[0];
                    const ans = arguments[1];
                    el.scrollIntoView({ block: 'center' });
                    el.focus();
                    const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                    if (desc && desc.set) desc.set.call(el, ans);
                    else el.value = ans;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'a', keyCode: 65 }));
                    el.blur();
                    """,
                    inp,
                    answer,
                )

            time.sleep(0.08)
            if _norm_nbsp(inp.get_attribute("value") or "") == _norm_nbsp(answer):
                break


def _solve_choices(driver: WebDriver, step_idx: int, choices: list[dict[str, Any]]) -> None:
    """Select radio or checkbox choices matching XML <correctResponse> targets."""
    for grp_idx, grp in enumerate(choices):
        targets = grp.get("targets", [])
        res = driver.execute_script(
            """
            const stepIdx = arguments[0];
            const grpIdx = arguments[1];
            const targets = arguments[2];
            const wrap = document.getElementById('content_wrap_' + stepIdx);
            if (!wrap) return { ok: false, reason: 'Missing wrap' };

            const norm = s => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
            const groups = Array.from(wrap.querySelectorAll('.choice_interaction'))
                .filter(el => el.offsetParent !== null);
            const container = groups[grpIdx] || wrap;

            let optionEls = Array.from(container.querySelectorAll('[role="radio"], [role="checkbox"]'))
                .filter(el => el.offsetParent !== null);
            if (optionEls.length === 0) {
                optionEls = Array.from(container.querySelectorAll('.input-radio, .input-checkbox'))
                    .filter(el => el.offsetParent !== null);
            }
            if (optionEls.length === 0) {
                return { ok: false, reason: 'No visible radio/checkbox options found' };
            }

            const optionInfo = optionEls.map((el, idx) => {
                let text = norm(el.innerText || el.textContent);
                if (!text && el.parentElement) {
                    text = norm(el.parentElement.innerText || el.parentElement.textContent);
                }
                return { el, idx, text };
            });

            const toClick = [];
            for (const t of targets) {
                const tText = norm(t.text);
                let match = optionInfo.find(o => o.text === tText);
                if (!match) {
                    match = optionInfo.find(o => o.text.toLowerCase() === tText.toLowerCase());
                }
                if (!match && t.idx < optionInfo.length) {
                    match = optionInfo[t.idx];
                }
                if (!match) {
                    return { ok: false, reason: 'Choice option not found: ' + tText };
                }
                if (match.el.getAttribute('aria-checked') !== 'true') {
                    toClick.push(match.el);
                }
            }
            return { ok: true, toClick };
            """,
            step_idx,
            grp_idx,
            targets,
        )
        if not res or not res.get("ok"):
            raise AnswerGuardError(
                f"Step {step_idx + 1} choice failed: {(res or {}).get('reason')}"
            )
        for el in res.get("toClick", []):
            _native_click(driver, el)
            time.sleep(0.08)


def _click_outer_button(driver: WebDriver, css_selector: str, timeout: float = 15.0) -> None:
    driver.switch_to.default_content()
    btn = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, css_selector))
    )
    driver.execute_script("arguments[0].scrollIntoView({ block: 'center' });", btn)
    btn.click()


def solve_activity(
    driver: WebDriver,
    activity_url: str,
    title: str = "",
    quiet_logs: bool = False,
    activity_budget_sec: float = 0.0,
) -> tuple[int, int] | None:
    """Open one Cambridge One activity, solve every step at 100%, and verify completion.

    Returns (scored, total) on 100% completion, or None if skipped (e.g. productive-skill).
    """
    label = title or activity_url.rsplit("/", 1)[-1]
    if not quiet_logs:
        print(f"[solver] Opening: {label}")

    driver.switch_to.default_content()
    driver.get(activity_url)

    # Wait for either the Avallain activity iframe OR a native <productive-skill> component.
    deadline = time.monotonic() + 35.0
    has_iframe = False
    while time.monotonic() < deadline:
        state = driver.execute_script(
            """
            if (document.querySelector('iframe[id^="iframe_"]')) return 'iframe';
            if (document.querySelector('productive-skill')) return 'productive';
            return '';
            """
        )
        if state == "iframe":
            has_iframe = True
            break
        if state == "productive":
            if not quiet_logs:
                print(f"[solver] Skipping native productive-skill activity: {label}")
            return None
        time.sleep(0.3)

    if not has_iframe:
        raise TimeoutException(f"Timed out waiting for activity iframe on: {label}")

    _switch_to_iframe(driver, timeout=20.0)
    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script(
            "return Boolean(window.ajaxData && window.ajaxData['LearningObjectInfo.xml'] && document.getElementById('activity'));"
        )
    )

    steps_spec: list[dict[str, Any]] = driver.execute_script(_PARSE_AJAX_DATA_JS)
    if not steps_spec:
        raise AnswerGuardError(f"Failed to parse LearningObjectInfo.xml for: {label}")

    # Handle purely non-scorable presentation activities (e.g., Listening 2)
    if all(s.get("isPresentation") for s in steps_spec):
        driver.switch_to.default_content()
        clicked_next = driver.execute_script(
            """
            const candidates = Array.from(document.querySelectorAll('a, button'));
            for (const el of candidates) {
                const txt = (el.textContent || '').trim().toUpperCase();
                if (txt === 'NEXT ACTIVITY' && el.offsetParent !== null) {
                    el.click();
                    return true;
                }
            }
            return false;
            """
        )
        time.sleep(1.5)
        if not quiet_logs:
            print(f"[solver] Completed presentation item ({'clicked NEXT ACTIVITY' if clicked_next else 'viewed'})")
        return (0, 0)

    scorable_count = sum(1 for s in steps_spec if not s.get("isPresentation"))
    max_iterations = len(steps_spec) + 5
    scored = 0
    total = 0

    for attempt_pass in range(2):
        needs_restart = False
        if attempt_pass > 0:
            _switch_to_iframe(driver, timeout=20.0)
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script(
                    "return Boolean(window.ajaxData && window.ajaxData['LearningObjectInfo.xml'] && document.getElementById('activity'));"
                )
            )
            steps_spec = driver.execute_script(_PARSE_AJAX_DATA_JS)
            if not steps_spec:
                raise AnswerGuardError(f"Failed to re-parse LearningObjectInfo.xml on restart: {label}")

        for _ in range(max_iterations):
            # Check if outer Angular completion screen has already appeared.
            driver.switch_to.default_content()
            score_ready = driver.execute_script(
                "return /You\\s+scored\\s+\\d+\\s+out\\s+of\\s+\\d+/i.test(document.body ? document.body.innerText : '');"
            )
            if score_ready:
                break

            # Check if iframe is still present.
            iframe_exists = driver.execute_script(
                'return Boolean(document.querySelector(\'iframe[id^="iframe_"]\'));'
            )
            if not iframe_exists:
                break

            _switch_to_iframe(driver, timeout=15.0)
            step_idx = _get_active_step_index(driver)
            if step_idx is None or step_idx >= len(steps_spec):
                raise AnswerGuardError(
                    f"Invalid active step index {step_idx} (total screens={len(steps_spec)})"
                )

            spec = steps_spec[step_idx]
            if spec.get("isPresentation"):
                driver.switch_to.default_content()
                next_btns = driver.find_elements(By.CSS_SELECTOR, 'a.btn.green-btn[title="Next"]')
                if next_btns and next_btns[0].is_displayed():
                    next_btns[0].click()
                else:
                    driver.execute_script(
                        """
                        const els = Array.from(document.querySelectorAll('a, button'));
                        for (const el of els) {
                            const t = (el.textContent || '').trim().toUpperCase();
                            if ((t === 'NEXT' || t === 'NEXT ACTIVITY') && el.offsetParent !== null) {
                                el.click();
                                return;
                            }
                        }
                        """
                    )
                time.sleep(0.7)
                continue

            # Check if this step was already checked previously (Next visible without Check)
            driver.switch_to.default_content()
            already_checked = driver.execute_script(
                """
                const nextBtn = document.querySelector('a.btn.green-btn[title="Next"]');
                const checkBtn = document.querySelector('a.btn.green-btn[title="Check"]');
                return Boolean(nextBtn && nextBtn.offsetParent !== null && (!checkBtn || checkBtn.offsetParent === null));
                """
            )
            if already_checked:
                try:
                    verify_step_100_after_check(driver, step_idx)
                except AnswerGuardError:
                    if attempt_pass == 0:
                        needs_restart = True
                        if not quiet_logs:
                            print(
                                f"[solver] Pre-checked step {step_idx + 1} has prior errors; "
                                "finishing pass to restart at 100%"
                            )
                    else:
                        raise
                _click_outer_button(driver, 'a.btn.green-btn[title="Next"]')
                time.sleep(0.6)
                continue

            _switch_to_iframe(driver, timeout=15.0)
            if spec.get("gapMatches"):
                _solve_gap_matches(driver, step_idx, spec["gapMatches"])
            if spec.get("inlineChoices"):
                _solve_inline_choices(driver, step_idx, spec["inlineChoices"])
            if spec.get("textEntries"):
                _solve_text_entries(driver, step_idx, spec["textEntries"])
            if spec.get("choices"):
                _solve_choices(driver, step_idx, spec["choices"])

            verify_step_ready_before_check(driver, step_idx, spec)
            if activity_budget_sec > 0 and scorable_count > 0:
                step_base = activity_budget_sec / scorable_count
                pre_check_wait = random.uniform(step_base * 0.45, step_base * 0.95)
                post_check_wait = random.uniform(step_base * 0.15, step_base * 0.45)
                time.sleep(max(0.2, pre_check_wait))
            else:
                post_check_wait = 0.0

            _click_outer_button(driver, 'a.btn.green-btn[title="Check"]')
            verify_step_100_after_check(driver, step_idx)
            if post_check_wait > 0:
                time.sleep(max(0.15, post_check_wait))
            _click_outer_button(driver, 'a.btn.green-btn[title="Next"]')

            # Wait briefly for transition to next step or completion view.
            transition_deadline = time.monotonic() + 8.0
            while time.monotonic() < transition_deadline:
                driver.switch_to.default_content()
                done_or_gone = driver.execute_script(
                    """
                    const txt = document.body ? document.body.innerText : '';
                    if (/You\\s+scored\\s+\\d+\\s+out\\s+of\\s+\\d+/i.test(txt)) return true;
                    if (!document.querySelector('iframe[id^="iframe_"]')) return true;
                    return false;
                    """
                )
                if done_or_gone:
                    break
                _switch_to_iframe(driver, timeout=5.0)
                new_idx = _get_active_step_index(driver)
                if new_idx is not None and new_idx != step_idx:
                    break
                time.sleep(0.15)

        if needs_restart and attempt_pass == 0:
            driver.switch_to.default_content()
            WebDriverWait(driver, 20.0).until(
                lambda d: d.execute_script(
                    "return /You\\s+scored\\s+\\d+\\s+out\\s+of\\s+\\d+/i.test(document.body ? document.body.innerText : '');"
                )
            )
            _click_outer_button(driver, "a.tryAgain-btn", timeout=15.0)
            time.sleep(1.0)
            continue

        scored, total = verify_final_score_100(driver, timeout=20.0)
        break

    # Allow xAPI and progress state POST requests to flush cleanly before leaving page.
    time.sleep(1.5)
    if not quiet_logs:
        print(
            f"[solver] Verified 100% ({scored}/{total}, steps={scorable_count}): {label}"
        )
    return scored, total

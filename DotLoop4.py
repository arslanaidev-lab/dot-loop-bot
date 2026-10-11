import copy
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from difflib import SequenceMatcher

import requests
from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright
from selenium.webdriver.common.by import By
from seleniumbase import BaseCase, SB

LOGIN_URL = "https://www.dotloop.com/signin"
EMAIL = ""
PASSWORD = ""
RECOVERY_CODE_GET_URL = ""
RECOVERY_CODE_POST_URL = ""
FINAL_FAILURE_WEBHOOK_URL = ""
ATTEMPT_TIMEOUT_SECONDS = None
ELEMENT_FIND_ATTEMPTS = 3
ELEMENT_RETRY_DELAY_SECONDS = 5
PORT = int(os.getenv("DOTLOOP_PORT", "9125"))
app = Flask(__name__)
browser_lock = threading.Lock()
browser_state = {
    "manager": None,
    "sb": None,
    "playwright_manager": None,
    "playwright_browser": None,
    "page": None,
    "signed_in": False,
    "attempt_cancel": None,
}


def _page(sb):
    page = browser_state.get("page")
    if page is None:
        raise RuntimeError("The Playwright page is not connected.")
    return page


def _selector(by, value):
    if by == By.ID:
        return f"#{value}"
    if by == By.XPATH:
        return f"xpath={value}"
    return value


def log_step(sb, message):
    caller = sys._getframe(1)
    source = f"{os.path.basename(caller.f_code.co_filename)}:{caller.f_lineno} in {caller.f_code.co_name}"
    try:
        current_url = sb.get_current_url()
    except Exception as exc:
        current_url = f"<could not read URL: {exc}>"
    print(f"[STEP] {source}: {message} | current page: {current_url}")


def sleep_log(seconds, reason=""):
    caller = sys._getframe(1)
    source = f"{os.path.basename(caller.f_code.co_filename)}:{caller.f_lineno} in {caller.f_code.co_name}"
    label = f" ({reason})" if reason else ""
    print(f"[WAIT] {source}: sleeping for {seconds:.1f}s{label}...")
    time.sleep(seconds)


def pause_after_action(reason):
    sleep_log(random.uniform(2, 10), reason)


def already_logged_in(sb):
    current_url = sb.get_current_url().lower()
    if "/login" in current_url or "mfa" in current_url:
        return False
    return "my.dotloop.com" in current_url and "/loops" in current_url


def human_type(sb, selector, text, min_delay=0.05, max_delay=0.15):
    log_step(sb, f"Searching for input area {selector}")
    element = _page(sb).locator(selector).first
    log_step(sb, f"Entering text in input area {selector}")
    print(
        f"Typing with a randomized {min_delay:.2f}-{max_delay:.2f}s delay "
        "between characters."
    )
    element.click()
    element.fill("")
    for char in str(text):
        element.press_sequentially(
            char, delay=random.uniform(min_delay, max_delay) * 1000
        )
    pause_after_action("after typing text")


def human_click(sb, selector):
    log_step(sb, f"Searching for control {selector}")
    element = _page(sb).locator(selector).first
    _click_element(sb, element)


def enter_recovery_code(sb, selector, code):
    human_type(sb, selector, code)
    element = _page(sb).locator(selector).first
    if element.input_value() == code:
        return

    element.evaluate(
        """
        (input, value) => {
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
        ).set;
        setter.call(input, value);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        }
        """,
        code,
    )
    if element.input_value() != code:
        raise RuntimeError("Recovery code could not be entered into the form.")


def get_latest_recovery_code():
    print("Fetching latest recovery code from webhook...")
    response = requests.get(RECOVERY_CODE_GET_URL, timeout=20)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        code = response.text.strip()
        if code:
            return code
        raise RuntimeError("Webhook returned an empty recovery code.")

    candidates = data if isinstance(data, list) else [data]
    for candidate in reversed(candidates):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            for key in ("token_code", "Token/Codes", "code", "token", "recovery_code", "value"):
                if candidate.get(key):
                    return str(candidate[key]).strip()
    raise RuntimeError("Webhook responded successfully, but no recovery code was found.")


def save_new_recovery_code(code):
    print("Saving the newly generated recovery code to webhook...")
    response = requests.post(
        RECOVERY_CODE_POST_URL,
        json={"code": code},
        timeout=20,
    )
    response.raise_for_status()
    print(f"New recovery code saved successfully. HTTP {response.status_code}")


def get_new_code_from_page(sb):
    selector = "div.input.multiline"
    try:
        sb.wait_for_element_visible(selector, timeout=30)
        page_text = sb.get_text(selector).strip()
    except Exception:
        page_text = sb.get_page_source()

    match = re.search(r"\b[A-Z0-9]{24}\b", page_text)
    if not match:
        match = re.search(r"\b[A-Z0-9]{10,40}\b", page_text)
    if not match:
        raise RuntimeError("Could not find the newly generated recovery code.")
    return match.group(0)


def select_recovery_code_method(sb):
    another_method = "button[type='submit'][name='action'][value='pick-authenticator']"
    recovery_method = "button[type='submit'][name='action'][value='recovery-code::1']"

    log_step(sb, "Selecting Try another method")
    sb.wait_for_element_visible(another_method, timeout=20)
    human_click(sb, another_method)

    log_step(sb, "Selecting Recovery code")
    sb.wait_for_element_visible(recovery_method, timeout=20)
    human_click(sb, recovery_method)


def enter_code_and_rotate(sb, held_code=None):
    log_step(sb, "Getting recovery code from webhook")
    try:
        code = get_latest_recovery_code()
    except Exception as exc:
        if not held_code:
            raise
        print(f"Could not fetch a fresh recovery code; using the earlier code: {exc}")
        code = held_code

    code_selector = "input#code"
    log_step(sb, "Entering recovery code")
    sb.wait_for_element_visible(code_selector, timeout=30)
    enter_recovery_code(sb, code_selector, code)

    continue_selector = "button[type='submit'][name='action'][data-action-button-primary='true']"
    log_step(sb, "Submitting recovery code")
    sb.wait_for_element_visible(continue_selector, timeout=15)
    human_click(sb, continue_selector)

    new_code_selector = "div.input.multiline"
    for _ in range(20):
        if "new-code" in sb.get_current_url().lower() or sb.is_element_visible(new_code_selector):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError(
            "Recovery code was rejected or the new-code page did not load. "
            f"Current page: {sb.get_current_url()}"
        )

    log_step(sb, "Reading the new recovery code")
    new_code = get_new_code_from_page(sb)

    log_step(sb, "Confirming the new code was recorded")
    for selector in ("input#saved", "input[name='saved']", "label[for='saved']"):
        element = _page(sb).locator(selector).first
        if element.count() == 0:
            continue
        try:
            if (
                element.evaluate("el => el.tagName.toLowerCase()") == "input"
                and element.is_checked()
            ):
                break
            _click_element(sb, element)
            break
        except Exception:
            continue

    log_step(sb, "Saving the new recovery code to webhook")
    save_new_recovery_code(new_code)

    final_continue = "button[type='submit'][name='action']"
    log_step(sb, "Continuing after saving the new code")
    sb.wait_for_element_visible(final_continue, timeout=15)
    sleep_log(60, "before continuing after recording the new recovery code")
    human_click(sb, final_continue)

    log_step(sb, "Waiting for sign-in to complete")
    for _ in range(180):
        if already_logged_in(sb):
            print("Sign-in completed successfully.")
            return True
        time.sleep(0.5)
    print(f"Sign-in did not complete. Current page: {sb.get_current_url()}")
    return False


def sign_in(sb: BaseCase, email, password):
    log_step(sb, "Opening sign-in page")
    sb.goto(LOGIN_URL)
    _wait_for_page_load(sb)
    sleep_log(random.uniform(4, 8), "before interacting with sign-in form")
    if already_logged_in(sb):
        print("Already signed in.")
        return True

    try:
        held_code = get_latest_recovery_code()
    except Exception as exc:
        print(f"Could not pre-fetch recovery code: {exc}")
        held_code = None

    if "otp" in sb.get_current_url().lower():
        select_recovery_code_method(sb)
        return enter_code_and_rotate(sb, held_code)

    if sb.is_element_visible("iframe[src*='perimeterx']"):
        sb.gui_click_captcha()

    email_selector = "input#username"
    try:
        sb.wait_for_element_visible(email_selector, timeout=30)
    except Exception:
        log_step(sb, "Reopening sign-in page and retrying email field")
        sb.goto(LOGIN_URL)
        _wait_for_page_load(sb)
        sb.wait_for_element_visible(email_selector, timeout=30)

    log_step(sb, "Entering email")
    human_type(sb, email_selector, email)
    continue_selector = "button[type='submit'][name='action'][data-action-button-primary='true']"
    sb.wait_for_element_visible(continue_selector, timeout=15)
    human_click(sb, continue_selector)

    password_selector = "input#password"
    sb.wait_for_element_visible(password_selector, timeout=30)
    log_step(sb, "Entering password")
    human_type(sb, password_selector, password)
    sb.wait_for_element_visible(continue_selector, timeout=15)
    human_click(sb, continue_selector)

    otp_selector = "input#code"
    another_method = "button[type='submit'][name='action'][value='pick-authenticator']"
    for _ in range(20):
        current_url = sb.get_current_url().lower()
        if (
            "mfa-otp-challenge" in current_url
            or "enter your one-time code" in sb.get_page_source().lower()
            or sb.is_element_visible(otp_selector)
            or sb.is_element_visible(another_method)
        ):
            break
        time.sleep(0.5)

    select_recovery_code_method(sb)
    return enter_code_and_rotate(sb, held_code)


def _press_and_hold_visible(sb):
    try:
        page_text = _page(sb).locator("body").inner_text().casefold()
    except Exception:
        return False
    return "press and hold" in page_text or "press & hold" in page_text


def _wait_for_page_load(sb):
    page = _page(sb)
    while True:
        try:
            page.wait_for_load_state("load", timeout=60_000)
        except Exception:
            if page.is_closed():
                raise RuntimeError("The current page closed before it finished loading.")
            time.sleep(1)
            continue
        try:
            ready = page.evaluate("document.readyState") == "complete"
        except Exception:
            ready = False
        if not ready:
            time.sleep(1)
            continue
        if not _press_and_hold_visible(sb):
            return

        current_url = sb.get_current_url()
        log_step(sb, f"Press and Hold page detected; reloading {current_url} in the same tab")
        try:
            page.goto(current_url, wait_until="load", timeout=60_000)
        except Exception as exc:
            log_step(sb, f"Same-tab reload is still pending ({exc}); waiting before retrying")
            time.sleep(1)
        browser_state["page"] = page


def _find_with_retries(sb, description, finder):
    last_error = None
    for attempt in range(1, ELEMENT_FIND_ATTEMPTS + 1):
        _wait_for_page_load(sb)
        log_step(sb, f"Searching for {description}, attempt {attempt}/{ELEMENT_FIND_ATTEMPTS}")
        try:
            result = finder()
            if result:
                log_step(sb, f"Found {description}")
                return result
        except Exception as exc:
            last_error = exc

        log_step(
            sb,
            f"Unable to find {description} on attempt {attempt}/{ELEMENT_FIND_ATTEMPTS}; "
            "continuing with the next retry.",
        )
        if attempt < ELEMENT_FIND_ATTEMPTS:
            sleep_log(
                ELEMENT_RETRY_DELAY_SECONDS,
                f"retrying search for {description} ({attempt + 1}/{ELEMENT_FIND_ATTEMPTS})",
            )

    log_step(sb, f"Unable to find {description} after {ELEMENT_FIND_ATTEMPTS} attempts")
    raise RuntimeError(
        f"Can't find {description} after {ELEMENT_FIND_ATTEMPTS} attempts."
    ) from last_error


def _wait_for_element(sb, by, value, timeout=20):
    description = f"element {by}={value} (wait up to {timeout}s per attempt)"
    return _find_with_retries(
        sb,
        description,
        lambda: _wait_visible(_page(sb).locator(_selector(by, value)).first, timeout),
    )


def _wait_visible(element, timeout):
    element.wait_for(state="visible", timeout=timeout * 1000)
    return element


def _wait_for_locator(locator, timeout):
    locator.first.wait_for(state="visible", timeout=timeout * 1000)
    return locator.all()


def _click_element(sb, element):
    _wait_for_page_load(sb)
    log_step(sb, "Clicking the selected element")
    element.click()
    browser = browser_state.get("playwright_browser")
    if browser and browser.contexts:
        pages = browser.contexts[0].pages
        if pages:
            browser_state["page"] = pages[-1]
    _wait_for_page_load(sb)
    _page(sb).wait_for_timeout(2000)


def _close_modal_if_open(sb):
    _wait_for_page_load(sb)
    log_step(sb, "Searching for document modal header div.modal-header")
    for modal_header in _page(sb).locator("div.modal-header").all():
        if not modal_header.is_visible():
            log_step(sb, "Found a modal-header div, but it is not visible; skipping it")
            continue
        log_step(sb, "Found a visible document modal-header div")
        for close_container in modal_header.locator("div.close-container").all():
            if not close_container.is_visible():
                log_step(sb, "Found div.close-container, but it is not visible; skipping it")
                continue
            log_step(sb, "Clicking div.close-container inside div.modal-header")
            try:
                _click_element(sb, close_container)
            except Exception as exc:
                log_step(sb, f"Click on div.close-container failed: {exc}")
            if not modal_header.is_visible():
                log_step(sb, "The modal closed after clicking div.close-container")
                return
            log_step(
                sb,
                "Modal remains open; searching inside div.close-container for "
                "a[class*='close'] or a[class*='cancel']",
            )
            close_links = close_container.locator(
                "a[class*='close'], a[class*='cancel']"
            ).all()
            for close_link in close_links:
                if close_link.is_visible() and close_link.is_enabled():
                    log_step(sb, "Clicking the visible close/cancel anchor in div.close-container")
                    _click_element(sb, close_link)
                    if not modal_header.is_visible():
                        log_step(sb, "The modal closed after clicking the close/cancel anchor")
                        return
                    log_step(sb, "Modal is still visible after clicking the close/cancel anchor")
        if modal_header.is_visible():
            log_step(sb, "Could not close the visible document modal-header div")
            raise RuntimeError("Could not close the document modal.")


def _click_selector(sb, selectors, timeout=20):
    last_error = None
    description = f"clickable control {selectors}"
    for attempt in range(1, ELEMENT_FIND_ATTEMPTS + 1):
        _wait_for_page_load(sb)
        log_step(sb, f"Searching for {description}, attempt {attempt}/{ELEMENT_FIND_ATTEMPTS}")
        end_time = time.time() + timeout
        while time.time() < end_time:
            for by, value in selectors:
                try:
                    element = _page(sb).locator(_selector(by, value)).first
                    if element.is_visible() and element.is_enabled():
                        log_step(sb, f"Found {description}; clicking it")
                        _click_element(sb, element)
                        return element
                except Exception as exc:
                    last_error = exc
            time.sleep(0.25)
        log_step(
            sb,
            f"Can't find {description} on attempt {attempt}/{ELEMENT_FIND_ATTEMPTS}; "
            "continuing with the next retry.",
        )
        if attempt < ELEMENT_FIND_ATTEMPTS:
            sleep_log(
                ELEMENT_RETRY_DELAY_SECONDS,
                f"retrying search for {description} ({attempt + 1}/{ELEMENT_FIND_ATTEMPTS})",
            )
    log_step(sb, f"Unable to find {description} after {ELEMENT_FIND_ATTEMPTS} attempts")
    raise RuntimeError(
        f"Can't find {description} after {ELEMENT_FIND_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def _set_input(element, value, sb=None, context=""):
    value = "" if value is None else str(value)
    field_name = (
        element.get_attribute("aria-label")
        or element.get_attribute("name")
        or element.get_attribute("id")
        or element.evaluate("el => el.tagName.toLowerCase()")
    )
    detail = f" ({context})" if context else ""
    if sb is not None:
        log_step(
            sb,
            f"Preparing to write {len(value)} character(s) to input '{field_name}'{detail}",
        )
    else:
        print(f"Searching for input area {field_name}; entering {len(value)} characters.")
    try:
        element.click()
        element.press("Control+A")
        element.press("Backspace")
        if value:
            typing_duration = random.uniform(3, 5)
            character_delay_ms = typing_duration * 1000 / max(len(value) - 1, 1)
            element.press_sequentially(value, delay=character_delay_ms)
        element.evaluate("el => el.blur()")
    except Exception as exc:
        if sb is not None:
            log_step(sb, f"Typing failed for input '{field_name}'{detail}: {exc}")
        raise
    if element.input_value() == value:
        if sb is not None:
            log_step(sb, f"Successfully typed text in input '{field_name}'{detail}")
        sleep_log(random.uniform(10, 12), "between input fields")
        return
    raise RuntimeError(f"Could not type the requested value into field {field_name}.")


def _activate_field_and_find_controls(sb, field, selector, description):
    controls = field.locator(selector)
    log_step(sb, f"Activating {description} to reveal its editable control")
    field.scroll_into_view_if_needed(timeout=10_000)
    field.click(timeout=10_000)
    found = _wait_for_locator(controls, 10)
    _page(sb).wait_for_timeout(2000)
    return found


def _payload_value(payload, *keys, default=""):
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return default


def _normalize_match_text(value):
    text = "" if value is None else str(value)
    text = text.replace("_", "").casefold()
    return re.sub(r"\s+", "", text)


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


def _ensure_checked(sb, element, context="document checkbox"):
    field = element.locator(
        "xpath=ancestor::div[contains(@class, 'data-type-')][1]"
    )
    if field.count():
        log_step(sb, f"Activating the {context} field container before checking it")
        field.first.scroll_into_view_if_needed(timeout=10_000)
        field.first.click(timeout=10_000)
        element.wait_for(state="visible", timeout=10_000)
        _page(sb).wait_for_timeout(2000)
    try:
        element.evaluate(
            "el => { const field = el.closest(\"div[class*='data-type-']\"); "
            "if (field) field.classList.add('highlight'); }"
        )
    except Exception as exc:
        log_step(sb, f"Could not highlight {context}: {exc}")
        raise
    if element.is_checked():
        log_step(sb, f"{context} is already checked; leaving it unchanged")
        return
    log_step(sb, f"Clicking/checking {context}")
    try:
        element.check()
    except Exception as exc:
        log_step(sb, f"Could not check {context}: {exc}")
        raise
    sleep_log(10, "between selected document items")


DOCUMENT_PAGES_SELECTOR = (
    "section#main-page-content > div#document-list > "
    "div.document-container[id^='document-'] > ul.page-list > "
    "li.document-page.ui-droppable"
)
DATA_TYPE_FIELD_SELECTOR = "div[class*='data-type-']"
EDITABLE_TEXT_FIELDS_SELECTOR = (
    "div.data-item.data-type-TEXTBOX"
)


def _page_elements(sb):
    return _page(sb).locator(DOCUMENT_PAGES_SELECTOR).all()


def _click_control_at_coordinates(sb, element, context):
    page = browser_state.get("page")
    if page is not None:
        try:
            bounds = element.bounding_box()
            if bounds:
                page.mouse.click(
                    bounds["x"] + bounds["width"] / 2,
                    bounds["y"] + bounds["height"] / 2,
                )
                return
        except Exception as exc:
            if sb is not None:
                log_step(
                    sb,
                    f"Coordinate click failed for {context} ({exc}); "
                    "dispatching mouse events on the control",
                )
    try:
        element.evaluate(
            """el => {
            const bounds = el.getBoundingClientRect();
            const init = {
                bubbles: true,
                cancelable: true,
                clientX: bounds.left + bounds.width / 2,
                clientY: bounds.top + bounds.height / 2,
                buttons: 1,
            };
            el.dispatchEvent(new MouseEvent('mousedown', init));
            el.dispatchEvent(new MouseEvent('mouseup', init));
            el.dispatchEvent(new MouseEvent('click', init));
            el.focus();
            }"""
        )
    except Exception as fallback_exc:
        if sb is not None:
            log_step(
                sb,
                f"Control-targeted mouse events failed for {context}: {fallback_exc}",
            )


def _document_page_field_inventory(page):
    return page.evaluate(
        """page => {
        const scopes = [
            ['selected page li', page],
            ['document container', page.closest('div.document-container')],
            ['main document section', page.closest('section#main-page-content')],
        ].filter(([, element]) => element);
        return scopes.map(([name, element]) => ({
            scope: name,
            pageDataCount: element.querySelectorAll('.document-page-data').length,
            fieldContainers: Array.from(
                element.querySelectorAll("div[class*='data-type-']")
            ).slice(0, 30).map(field => ({
                tag: field.tagName.toLowerCase(),
                id: field.id,
                className: field.className,
                controlCount: field.querySelectorAll('input, textarea').length,
            })),
            controls: Array.from(element.querySelectorAll('input, textarea'))
                .slice(0, 30).map(control => ({
                    tag: control.tagName.toLowerCase(),
                    id: control.id,
                    name: control.getAttribute('name'),
                    type: control.getAttribute('type'),
                    className: control.className,
                    disabled: control.disabled,
                    readOnly: control.readOnly,
                })),
        }));
        }"""
    )


def _log_missing_document_fields(sb, page, description):
    try:
        inventory = _document_page_field_inventory(page)
    except Exception as exc:
        log_step(sb, f"Could not inspect document field markup for {description}: {exc}")
        return
    log_step(
        sb,
        f"Unable to find controls for {description}; DOM inventory by document scope "
        f"(field values omitted): {inventory}",
    )


def _scroll_and_click_document_page(sb, page, index, page_count):
    log_step(
        sb,
        f"Scrolling the document/page scrollbar to ul.page-list > "
        f"li.document-page.ui-droppable item {index}/{page_count}; "
        "the scroll target is the list item, not the page image",
    )
    page.scroll_into_view_if_needed(timeout=10_000)
    log_step(
        sb,
        f"Clicking ul.page-list > li.document-page.ui-droppable item "
        f"{index}/{page_count}",
    )
    _click_element(sb, page)
    page_handle = page.element_handle()
    _page(sb).wait_for_function(
        "element => element.classList.contains('focused')",
        arg=page_handle,
        timeout=10_000,
    )
    page_state = page.evaluate(
        """page => {
        const bounds = page.getBoundingClientRect();
        return {
            visible: bounds.width > 0 && bounds.height > 0
                && bounds.top < window.innerHeight && bounds.bottom > 0
                && bounds.left < window.innerWidth && bounds.right > 0,
            focused: page.classList.contains('focused'),
        };
        }"""
    )
    log_step(
        sb,
        f"Document page {index} activation state: "
        f"visible={page_state['visible']}, focused={page_state['focused']}",
    )
    if not page_state["visible"] or not page_state["focused"]:
        raise RuntimeError(
            f"Document page {index} is not visible and focused after activation."
        )


def _log_document_inventory(sb):
    page = _page(sb)
    inventory = page.evaluate(
        """() => {
        const documentSelector = "div.document-container[id^='document-']";
        const scope = document.querySelector(documentSelector);
        if (!scope) return null;
        return {
            pageItems: scope.querySelectorAll(
                "ul.page-list > li.document-page.ui-droppable"
            ).length,
            textboxes: scope.querySelectorAll("div.data-item.data-type-TEXTBOX").length,
            dates: scope.querySelectorAll("div.data-item.data-type-DATE").length,
            checkboxes: scope.querySelectorAll("div.data-item.data-type-CHECKBOX").length,
        };
        }"""
    )
    if inventory is None:
        raise RuntimeError("Could not find the active document container for field counts.")
    log_step(
        sb,
        "Document inventory: "
        f"{inventory['pageItems']} page li item(s), "
        f"{inventory['textboxes']} TEXTBOX div(s), "
        f"{inventory['dates']} DATE div(s), "
        f"{inventory['checkboxes']} CHECKBOX div(s)",
    )


def _scroll_document_page_list_to_bottom(sb, last_page):
    log_step(sb, "Scrolling the document page-list scrollbar to its bottom")
    last_page.scroll_into_view_if_needed(timeout=10_000)


def _wait_for_document_page_render(sb, page, description):
    page_data = page.locator(".document-page-data").first
    page_data.wait_for(state="visible", timeout=10_000)
    page_data_handle = page_data.element_handle()
    _page(sb).wait_for_function(
        "element => Array.from(element.querySelectorAll('img')).every(image => image.complete)",
        arg=page_data_handle,
        timeout=10_000,
    )
    log_step(sb, f"Document image/data is loaded for {description}")
    _page(sb).wait_for_timeout(2000)
    return page_data


def _find_document_field_controls(
    sb, page, field_selector, control_selector, description
):
    fields = _find_with_retries(
        sb,
        f"{description} field container",
        lambda: _wait_for_locator(page.locator(field_selector), 10) or False,
    )
    return _find_with_retries(
        sb,
        description,
        lambda: _activate_field_and_find_controls(
            sb, fields[0], control_selector, description
        ) or False,
    )


def _check_addenda_by_sequence(sb, page, addenda):
    if not addenda:
        return

    checkbox_selector = "div.data-item.data-type-CHECKBOX input[type='checkbox']"
    checkboxes = page.locator(checkbox_selector).all()
    log_step(
        sb,
        f"Found {len(checkboxes)} page 11 addendum checkbox(es) in document order",
    )
    for addendum in addenda:
        sequence_value = (
            addendum.get("sequence") if isinstance(addendum, dict) else addendum
        )
        try:
            sequence = int(str(sequence_value).strip())
        except (TypeError, ValueError):
            log_step(
                sb,
                f"Unable to select addendum with invalid sequence {sequence_value!r}; "
                "use a one-based sequence number",
            )
            continue

        if sequence < 1 or sequence > len(checkboxes):
            log_step(
                sb,
                f"Unable to find page 11 addendum checkbox sequence {sequence}; "
                f"found {len(checkboxes)} checkbox(es)",
            )
            continue

        checkbox = checkboxes[sequence - 1]
        log_step(sb, f"Checking page 11 addendum checkbox sequence {sequence}")
        _ensure_checked(sb, checkbox, f"page 11 addendum checkbox sequence {sequence}")


def _activate_document_pages(sb, page_handler=None):
    log_step(
        sb,
        "Searching for document page items with selector "
        f"{DOCUMENT_PAGES_SELECTOR} (ul.page-list > li.document-page.ui-droppable)",
    )
    pages = _find_with_retries(
        sb,
        f"document page items: {DOCUMENT_PAGES_SELECTOR}",
        lambda: _wait_for_locator(_page(sb).locator(DOCUMENT_PAGES_SELECTOR), 10),
    )

    page_count = len(pages)
    log_step(sb, f"Found {page_count} document page list item(s); returning to page 1, then processing forward")
    if page_count:
        _scroll_document_page_list_to_bottom(sb, pages[-1])
    for page_index in range(page_count):
        pages = _page_elements(sb)
        if page_index >= len(pages):
            log_step(
                sb,
                f"Could not find document page list item {page_index + 1}; "
                "the page list changed during processing",
            )
            raise RuntimeError(
                f"Can't find document page item {page_index + 1} after the page list changed."
            )
        index = page_index + 1
        page = pages[page_index]
        log_step(sb, f"Starting document page {index}/{page_count}")
        _scroll_and_click_document_page(sb, page, index, page_count)
        _find_with_retries(
            sb,
            f"document image/data for page {index}/{page_count}",
            lambda: _wait_for_document_page_render(
                sb, page, f"page {index}/{page_count}"
            )
            or False,
        )
        _close_modal_if_open(sb)
        if page_handler is not None:
            log_step(sb, f"Searching and processing configured fields on page {index}/{page_count}")
            page_handler(page, page_index, page_count)
            log_step(sb, f"Finished configured field processing on page {index}/{page_count}")


def _open_document_for_edit(sb, document_name):
    name = str(document_name).strip()
    if name:
        expected_label = f"Preview item named {name}"
        log_step(
            sb,
            f"Searching for the exact document button '{expected_label}'",
        )

        def find_document():
            return [
                button
                for button in _page(sb).locator(
                    "button[aria-label^='Preview item named ']"
                ).all()
                if button.is_visible()
                and button.get_attribute("aria-label") == expected_label
            ]

        candidates = _find_with_retries(sb, f"document '{name}'", find_document)
        log_step(
            sb,
            f"Found {len(candidates)} exact document button(s) for '{name}'; clicking one",
        )
        _click_element(sb, candidates[0])
    else:
        log_step(sb, "No document name was supplied; skipping the document-name search")

    log_step(sb, "Searching for and clicking #document-more-options")
    _click_selector(
        sb,
        [(By.ID, "document-more-options")],
        timeout=10,
    )
    log_step(sb, "Searching for and clicking #edit-document or its containing li item")
    sleep_log(random.uniform(62, 70), "before entering the document editor")
    _click_selector(
        sb,
        [
            (By.ID, "edit-document"),
            (By.XPATH, "//li[.//*[@id='edit-document'] or @id='edit-document']"),
        ],
        timeout=10,
    )


def _xpath_literal(value):
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def _fill_document_fields(sb, payload):
    log_step(sb, "Scanning the document without entering contract payload values")

    def click_first_text_field(page, page_index, page_count):
        if page_index != 0:
            return

        _log_document_inventory(sb)
        fields = _find_with_retries(
            sb,
            "first editable text field container on page 1",
            lambda: _wait_for_locator(
                page.locator(EDITABLE_TEXT_FIELDS_SELECTOR), 10
            )
            or False,
        )
        inputs = _find_with_retries(
            sb,
            "first textarea in document page 1",
            lambda: _activate_field_and_find_controls(
                sb, fields[0], "textarea", "first text field on page 1"
            )
            or False,
        )
        log_step(sb, "Clicking the first text input without typing a value")
        inputs[0].click(timeout=10_000)

    _activate_document_pages(sb, click_first_text_field)


def _prepare_document_for_edit(sb, document_name, payload, document_list_url):
    while True:
        try:
            _wait_for_page_load(sb)
            _open_document_for_edit(sb, document_name)
            _close_modal_if_open(sb)
            _fill_document_fields(sb, payload)
            return
        except Exception as exc:
            message = str(exc).casefold()
            if "can't find" not in message and "could not close the document modal" not in message:
                raise
            log_step(
                sb,
                    f"Unable to find the requested document or required controls ({exc}); "
                    "reopening the requested document",
            )
            sleep_log(ELEMENT_RETRY_DELAY_SECONDS, "waiting before reopening the document")
            sb.goto(document_list_url)
            _wait_for_page_load(sb)


def _share_document(sb):
    log_step(sb, "Starting document sharing workflow")
    _close_modal_if_open(sb)

    share_selector = (
        "button[class*='share'], a[class*='share'], *[class*='button'][class*='share']"
    )
    log_step(sb, f"Searching for document share control: {share_selector}")
    share_controls = _page(sb).locator(
        share_selector,
    ).all()
    if share_controls:
        log_step(sb, "Found document share control; clicking the first match")
        _click_element(sb, share_controls[0])
    else:
        log_step(sb, "Could not find a document share control; continuing to inspect share dialog")

    log_step(sb, "Searching for share permission checkbox by CSS class")
    share_checkbox = _wait_for_element(
        sb,
        By.CSS_SELECTOR,
        "input[type='checkbox'][class*='sc-cvvRIS'], input[type='checkbox'].hHmAcQ",
        timeout=15,
    )
    _ensure_checked(sb, share_checkbox, "share permission checkbox")
    log_step(sb, "Searching for and clicking share dialog button #next-share-step")
    _click_selector(
        sb,
        [(By.ID, "next-share-step")],
        timeout=15,
    )
    log_step(sb, "Searching for recipient input #add-participant-dropdown-input")
    email_input = _wait_for_element(sb, By.ID, "add-participant-dropdown-input", timeout=15)
    _set_input(email_input, "arslan.ai.dev@gmail.com", sb, "document share recipient input")
    log_step(sb, "Searching for participant checkbox #1195")
    participant_checkbox = _wait_for_element(sb, By.ID, "1195", timeout=10)
    _ensure_checked(sb, participant_checkbox, "document share participant checkbox #1195")


def _save_document(sb):
    save_selector = (
        "a.toolbar-link.save-document, button#save-document, button[aria-label='Save'], "
        "button[aria-label='Save changes'], button[title='Save'], "
        "button[data-testid*='save']"
    )
    log_step(sb, f"Searching for document save control: {save_selector}")
    save_controls = _page(sb).locator(save_selector).all()
    if not save_controls:
        log_step(sb, "No save control matched the primary selector; searching visible button/link text")
        save_controls = [
            element
            for element in _page(sb).locator("button, a").all()
            if element.is_visible()
            and _normalize_match_text(element.inner_text()) in {"save", "savechanges"}
        ]
    if save_controls:
        log_step(sb, "Found document save control; clicking it")
        _click_element(sb, save_controls[0])
    else:
        log_step(sb, "Could not find a document save control")

    saved_selectors = (
        "[aria-label*='saved' i], [class*='save-status' i], "
        "[data-testid*='save-status' i], [role='status'], [aria-live], "
        "[class*='save']"
    )

    def save_completed():
        for status in _page(sb).locator(saved_selectors).all():
            text = " ".join(
                (status.inner_text() or "", status.get_attribute("aria-label") or "")
            )
            normalized = text.casefold()
            if "saved" in normalized and "saving" not in normalized:
                return True
        return False

    log_step(sb, f"Waiting up to 30 seconds for document save status matching {saved_selectors}")
    end_time = time.monotonic() + 30
    while time.monotonic() < end_time:
        if save_completed():
            log_step(sb, "Document save status confirms the document is saved")
            return
        time.sleep(0.25)
    log_step(sb, "Could not find a saved confirmation after 30 seconds")
    raise RuntimeError("DotLoop did not confirm that the document was saved.")


def _ensure_add_loop_button(sb):
    loops_url = "https://my.dotloop.com/loops/"
    for attempt in range(2):
        if attempt or not sb.get_current_url().rstrip("/").endswith("/loops"):
            log_step(sb, "Opening the DotLoop loops page")
            sb.open(loops_url)
        try:
            return _wait_for_element(sb, By.ID, "add-loop-button", timeout=20)
        except Exception as exc:
            if attempt:
                if "/login" in sb.get_current_url().lower():
                    raise RuntimeError(
                        "DotLoop redirected to sign-in; the session is not authenticated."
                    ) from exc
                raise RuntimeError(
                    "Could not find the Add Loop button on the DotLoop loops page."
                ) from exc


def run_contract_workflow(sb, payload):
    if not isinstance(payload, dict):
        raise TypeError("The DotLoop API payload must be a JSON object.")

    log_step(sb, "Starting contract workflow and searching for the Add Loop button")
    _ensure_add_loop_button(sb)
    _click_selector(sb, [(By.ID, "add-loop-button")])
    log_step(sb, "Searching for the new loop form and address input area")
    _wait_for_element(sb, By.CSS_SELECTOR, "form[name='loop-name']", timeout=20)
    address_input = _wait_for_element(
        sb, By.CSS_SELECTOR, 'input[name="loop-address"]', timeout=15
    )
    address = str(_payload_value(payload, "loop_address", "propertyAddress", "property_address"))
    _set_input(address_input, address)

    log_step(sb, "Searching for address suggestions")
    suggestion_lists = _page(sb).locator("ul[class*='sc-gotrZO']").all()
    if suggestion_lists:
        suggestions = suggestion_lists[0].locator("li").all()
        if suggestions:
            suggestion = suggestions[0]
            similarity = SequenceMatcher(
                None,
                _normalize_match_text(address).replace(" ", ""),
                _normalize_match_text(suggestion.inner_text()).replace(" ", ""),
            ).ratio()
            if similarity >= 0.65:
                log_step(sb, "Found a matching address suggestion; selecting it")
                _click_element(sb, suggestion)
            else:
                print("Can't find a close address suggestion; continuing with the entered address.")
    else:
        print("Can't find address suggestions; continuing with the entered address.")

    log_step(sb, "Continuing to the contract template selection page")
    _click_selector(
        sb,
        [
            (By.ID, "create-wizard-next-button"),
            (By.XPATH, "//button[normalize-space()='Continue']"),
        ],
    )
    template_button = _wait_for_element(
        sb,
        By.CSS_SELECTOR,
        "button#create-loop-template-button, button[aria-label='Loop Template (Required)']",
        timeout=20,
    )
    _click_element(sb, template_button)
    template_name = str(
        _payload_value(payload, "Loop_Template", "contract_template", "contractTemplate")
    ).strip()
    log_step(sb, f"Searching for contract template '{template_name}'")
    normalized_template_name = _normalize_match_text(template_name)
    template_options = _find_with_retries(
        sb,
        f"options in the Loop_Template listbox for '{template_name}'",
        lambda: _page(sb).locator(
            "[role='listbox'] [role='option'], "
            "ul#create-loop-template-menu li[role='option']"
        ).all() or False,
    )
    template_matches = [
        (
            option,
            _normalize_match_text(option.inner_text()),
        )
        for option in template_options
    ]
    selected_template = None
    if normalized_template_name:
        selected_template, selected_template_name = max(
            template_matches,
            key=lambda item: (
                normalized_template_name in item[1],
                SequenceMatcher(None, normalized_template_name, item[1]).ratio(),
            ),
            default=(None, ""),
        )
        similarity = SequenceMatcher(
            None,
            normalized_template_name,
            selected_template_name,
        ).ratio()
        if (
            not selected_template_name
            or normalized_template_name not in selected_template_name
            and similarity < 0.65
        ):
            selected_template = None
    if selected_template is None:
        raise RuntimeError(f"Can't find DotLoop contract template: {template_name}")
    log_step(sb, f"Found contract template '{template_name}'; selecting it")
    _click_element(sb, selected_template)
    _click_selector(sb, [(By.ID, "create-wizard-next-button")])

    _click_selector(
        sb,
        [
            (By.ID, "create-wizard-done-button"),
            (By.XPATH, "//button[normalize-space()='Done']"),
        ],
    )
    view_loop = _click_selector(
        sb,
        [
            (By.ID, "create-wizard-view-loop"),
            (By.CSS_SELECTOR, "a[href*='/loops/'][href*='/documents']"),
        ],
        timeout=30,
    )

    requested_documents = _payload_value(payload, "documents", default=[])
    if not requested_documents:
        raise RuntimeError("The API payload must include at least one document in 'documents'.")
    log_step(sb, f"Found {len(requested_documents)} requested document(s)")
    document_names = []
    for document in requested_documents:
        if isinstance(document, dict):
            document_names.append(
                _payload_value(document, "name", "title", "document_name", default="")
            )
        else:
            document_names.append(document)

    document_list_url = sb.get_current_url()
    for document_index, (document, document_name) in enumerate(
        zip(requested_documents, document_names), start=1
    ):
        log_step(
            sb,
            f"Starting document {document_index}/{len(requested_documents)}: {document_name}",
        )
        document_payload = dict(payload)
        if isinstance(document, dict):
            document_payload.update(document)
        _prepare_document_for_edit(
            sb,
            document_name,
            document_payload,
            document_list_url,
        )
        log_step(sb, f"Searching for sharing controls for '{document_name}'")
        _share_document(sb)
        log_step(sb, f"Searching for the final Save control for '{document_name}'")
        _save_document(sb)
        log_step(sb, f"Completed document '{document_name}'")


def _browser_options():
    if not EMAIL or not PASSWORD:
        raise RuntimeError("Set the DotLoop EMAIL and PASSWORD constants before starting the API.")
    sb_options = {"uc": True}
    chrome_binary = os.getenv("CHROME_BINARY")
    default_chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if chrome_binary:
        sb_options["binary_location"] = chrome_binary
    elif os.path.exists(default_chrome):
        sb_options["binary_location"] = default_chrome
    return sb_options


def _get_browser() -> BaseCase:
    cancel_event = browser_state.get("attempt_cancel")
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("The DotLoop browser attempt was cancelled.")
    sb = browser_state["sb"]
    if sb is not None and not _browser_session_is_alive(sb):
        app.logger.warning("Discarding an expired DotLoop browser session")
        _close_browser()

    if browser_state["sb"] is None:
        manager = SB(**_browser_options())
        browser_state["manager"] = manager
        sb = manager.__enter__()
        if cancel_event is not None and cancel_event.is_set():
            manager.__exit__(None, None, None)
            raise RuntimeError("The DotLoop browser attempt was cancelled during startup.")
        sb.activate_cdp_mode()
        playwright_manager = sync_playwright().start()
        browser_state["playwright_manager"] = playwright_manager
        playwright_browser = playwright_manager.chromium.connect_over_cdp(
            sb.get_endpoint_url()
        )
        browser_state["playwright_browser"] = playwright_browser
        browser_state["page"] = playwright_browser.contexts[0].pages[0]
        browser_state["manager"] = manager
        browser_state["sb"] = sb
    return browser_state["sb"]


def _browser_session_is_alive(sb):
    try:
        page = browser_state.get("page")
        browser = browser_state.get("playwright_browser")
        return bool(browser and browser.is_connected() and page and not page.is_closed())
    except Exception:
        return False


def _is_invalid_session_error(error):
    return "invalid session id" in str(error).lower()


def _get_authenticated_browser():
    sb = _get_browser()
    if browser_state["signed_in"]:
        return sb
    if not sign_in(sb, EMAIL, PASSWORD):
        raise RuntimeError("DotLoop sign-in failed or timed out.")
    browser_state["signed_in"] = True
    return sb


def _close_browser():
    cancel_event = browser_state.get("attempt_cancel")
    if cancel_event is not None:
        cancel_event.set()
    manager = browser_state.get("manager")
    try:
        playwright_browser = browser_state.get("playwright_browser")
        if playwright_browser is not None:
            playwright_browser.close()
        playwright_manager = browser_state.get("playwright_manager")
        if playwright_manager is not None:
            playwright_manager.stop()
        if manager is not None:
            manager.__exit__(None, None, None)
    except Exception:
        app.logger.debug("Ignoring an error while closing a stale browser", exc_info=True)
    finally:
        browser_state.update(
            manager=None,
            sb=None,
            playwright_manager=None,
            playwright_browser=None,
            page=None,
            signed_in=False,
            attempt_cancel=None,
        )


def _run_contract_attempt(payload, timeout=ATTEMPT_TIMEOUT_SECONDS):
    result = {}
    completed = threading.Event()
    cancel_event = threading.Event()
    browser_state["attempt_cancel"] = cancel_event

    def run():
        try:
            sb = _get_authenticated_browser()
            run_contract_workflow(sb, payload)
        except BaseException as exc:
            result["error"] = exc
        else:
            result["success"] = True
        finally:
            completed.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    if timeout is None:
        completed.wait()
    elif not completed.wait(timeout):
        cancel_event.set()
        _close_browser()
        raise FutureTimeoutError(f"Attempt exceeded its {timeout}-second time limit.")
    if "error" in result:
        raise result["error"]
    return result.get("success", False)


def _failure_details(attempt, error):
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_match = re.search(r"\b(?:HTTP\s*)?(5\d\d)\b", str(error), re.IGNORECASE)
        if status_match:
            status_code = int(status_match.group(1))
    return {
        "attempt": attempt,
        "status": status_code,
        "error": str(error),
        "error_type": type(error).__name__,
    }


def _report_final_failure(payload, failures):
    try:
        response = requests.post(
            FINAL_FAILURE_WEBHOOK_URL,
            json={"payload": payload, "failures": failures},
            timeout=20,
        )
        response.raise_for_status()
        return {"sent": True, "status_code": response.status_code}
    except Exception as exc:
        app.logger.exception("Could not send the final DotLoop failure webhook")
        return {"sent": False, "error": str(exc)}


@app.get("/health")
def health():
    return jsonify({"status": "ok", "browser_started": browser_state["sb"] is not None})


@app.post("/process")
def process_contract():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    original_payload = copy.deepcopy(payload)
    failures = []
    with browser_lock:
        try:
            if not _run_contract_attempt(copy.deepcopy(original_payload)):
                raise RuntimeError("The document workflow did not confirm completion.")
            return jsonify({"status": "completed", "attempt": 1}), 200
        except Exception as exc:
            app.logger.exception("DotLoop contract workflow failed")
            failures.append(_failure_details(1, exc))

    webhook_result = _report_final_failure(original_payload, failures)
    return jsonify({
        "error": "DotLoop document processing failed.",
        "failures": failures,
        "failure_webhook": webhook_result,
    }), 500


def main():
    try:
        app.run(
            host=os.getenv("DOTLOOP_HOST", "127.0.0.1"),
            port=PORT,
            threaded=True,
            use_reloader=False,
        )
    finally:
        with browser_lock:
            _close_browser()


if __name__ == "__main__":
    main()

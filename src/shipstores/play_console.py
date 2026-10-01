"""Automation for the Play Console "App content" forms.

Exists because none of these declarations has an endpoint in androidpublisher v3 —
they are console forms, and without them a draft app cannot be published.

BEFORE AUTOMATING ANY FIELD, check whether the API already covers it. Contact
details (contactEmail/Website/Phone), for example, are `edits.details` — one PATCH
does what would take several rounds of clicking. See play.patch_details().

The console is driven in pt-BR; UI labels quoted below are given in English with
the literal pt-BR text the code matches in parentheses where relevant.

## How to click in Play Console

Learned the hard way. Three things make a click work, and missing any one of them
makes the click look "ignored" by Angular Material:

  a. **The tab must be in the foreground.** In the background CDP raises an
     intermittent TimeoutError on dispatchMouseEvent and the event never reaches
     the page. This was the cause of most failures that looked like something else.
  b. **`mousePressed` needs `buttons=1`** — the held-button bitmask. The harness's
     `click_at_xy` does not pass this parameter.
  c. **There must be a delay between press and release** (~120ms). An instant
     click is ignored.

With all three, radios, checkboxes, text fields, dropdowns and dialog confirmation
buttons work — including the ones that previously seemed impossible. Use
`real_click()`, which also validates the point with `elementFromPoint` and so
removes the need to guess offsets.

## Other gotchas

1. **URL slugs do not follow the form name** — see FORMS. "Financial features"
   lives at /finance, "App access" (login details) at /testing-credentials.
   "Target audience" and "Advertising ID" have no URL of their own; only via the index.
2. **Order matters in App access**: the "Yes" radio resets on every reload, so
   selecting it and opening the modal must happen in the same session.
3. After saving, the console opens a "Go to Publishing overview?" dialog that has
   to be dismissed with "Not now".
4. **The IARC questionnaire flow is answer -> Save -> Next.** With open questions,
   "Next" ("Avançar") is disabled and "Save" ("Salvar") is enabled; after Save the two
   swap state and "Next" leads to the Summary, where another "Save" submits. Saving
   *incomplete* is what sends the wizard back to step 1 and unchecks the IARC Terms.
   Sequence: Terms -> Next -> answer -> Save -> Next -> Save.
5. **The IARC questionnaire opens via the "Edit" button** at /content-rating-overview.
6. **Targets under the sticky footer** (below ~830px) need extra scrolling:
   `cdp("DOM.scrollIntoViewIfNeeded", backendNodeId=bid,
        rect={"x":0,"y":0,"width":40,"height":420})`
7. **When pairing questions with radios, filter by "?"** — innerText mixes section
   headers ("Violence", "Language") and "Completed" markers in with the questions.
8. **To clear a field, use Backspace.** Neither Ctrl+A (modifiers=4) nor Cmd+A
   (modifiers=8) selects in these inputs; the following insertText concatenates.
9. **Data safety accepts CSV** and that is the path to use — 5 wizard screens become
   one file. The file input exists in the DOM, so the native picker can be avoided:
       doc  = cdp("DOM.getDocument")["root"]["nodeId"]
       node = cdp("DOM.querySelector", nodeId=doc, selector="input[type=file]")["nodeId"]
       cdp("DOM.setFileInputFiles", files=[csv], nodeId=node)
   There are two dialogs in sequence, each with an "Import" button — take the last
   one. To export the template, set the destination with
   **Browser**.setDownloadBehavior (Page. does not work); the file arrives with a GUID name.
10. **Confirm which tab the harness is on** before long sequences. If the user opens
    tabs midway, the harness may migrate and clicks "fail" silently because they
    hit another site. Find the target by URL in `cdp("Target.getTargets")`.
11. **Always confirm the result by re-reading the state** — preferably via the API. A
    closed dialog and text on screen do not prove that it was saved.
12. **There are transparent modals that swallow clicks without errors.** The "Send N
    changes for review" button ("Enviar N mudanças para revisão") sits under a
    `DIV.pane modal visible`: the click hits the overlay, the overlay disappears, and
    nothing happens — no exception, no log. `elementFromPoint` at the center returns
    the overlay instead of the button; shifting ~8px up got past it. That is why
    `real_click` probes several points: when the center is covered, one of the
    edges usually escapes.
13. **"Create new release" is DISABLED when a draft already exists** — not an
    automation failure. The tooltip says "To create a release, roll out or discard
    the current draft release". A release created via API (play_promote_release with
    status draft) is exactly that draft. To edit it: **Releases** tab -> "Edit release".
14. **Managed publishing only exists for apps that are ALREADY published.** On a new
    app Google refuses with "Managed publishing can't be turned on right now" — and it
    is not needed: on a new app, "Send for review" (step 4) and "Publish" (step 5) are
    already separate checklist steps. Sending for review does NOT publish.

## Full flow: sending a new app to production review

Tested end to end on a real app. The API covers only the first step; the rest is
console.

    1. play_promote_release(track="production", status="draft")   <- via API
    2. /tracks/production?tab=releases -> "Edit release"
    3. .../releases/1/prepare  -> "Next"
    4. .../releases/1/review   -> "Save"        (completes "Preview and confirm")
    5. /publishing -> "Send N changes for review" -> confirm in the dialog

Between 4 and 5 Google runs "quick checks for common issues" (up to ~13 min). The
send button already shows up during them; the changes proceed when the checks
finish. After sending, the page shows "Changes in review" ("Alterações em análise")
and replaces the button with "Remove changes" — that is the confirmation it worked.

Warnings do not block sending. "This App Bundle contains native code, and you've
not uploaded debug symbols" is only a recommendation: it affects the readability of
crash reports, not the review.
"""

from __future__ import annotations

import json
from typing import Any

from .browser import run_harness

CONSOLE = "https://play.google.com/console/u/0"

# Slug of each declaration. None = no URL of its own; reachable only by clicking in the index.
FORMS = {
    "privacy_policy": "privacy-policy",
    "ads": "ads-declaration",
    "app_access": "testing-credentials",   # NOT "app-access"
    "government": "government-apps",
    "financial": "finance",                # NOT "financial-features"
    "health": "health",
    "data_safety": "data-privacy-security",
    "content_rating": "content-rating-overview",
    "target_audience": None,               # index only
    "advertising_id": None,                # index only
}

# Pages outside /app-content that are useful in the publishing flow.
PAGES = {
    "publishing": "publishing",                          # send for review
    "dashboard": "app-dashboard",                        # launch checklists
    "store_settings": "store-settings",                  # category and contact
    "signing": "keymanagement",                          # signing certificates
    "production_releases": "tracks/production?tab=releases",
    "internal_testers": "tracks/internal-testing?tab=testers",
}

# Delay between mousePressed and mouseReleased. Any shorter and Material ignores the click.
CLICK_HOLD_SECONDS = 0.12

# Prelude injected into every harness script. Uses __PLACEHOLDER__ instead of
# %-formatting because the body has JS full of % signs and braces.
PRELUDE = """
from browser_harness.helpers import activate_tab, cdp, click_at_xy, current_tab
import json, re, time

HOLD = __HOLD__


def ensure_foreground():
    \"\"\"Bring the tab to the front. In the background CDP times out and the click is lost.\"\"\"
    activate_tab(current_tab())
    time.sleep(1.5)


def hit_points(finder):
    \"\"\"Probe the target with elementFromPoint and return only the points that hit it.\"\"\"
    raw = js('''
    (() => {
      const target = ''' + finder + ''';
      if (!target) return JSON.stringify({error: 'target not found'});
      target.scrollIntoView({block: 'center', inline: 'center'});
      const r = target.getBoundingClientRect();
      const out = [];
      const fractions = [[.5,.5],[.5,.75],[.5,.25],[.25,.5],[.75,.5],[.1,.5],[.9,.5]];
      for (const pair of fractions) {
        const x = Math.round(r.x + r.width*pair[0]);
        const y = Math.round(r.y + r.height*pair[1]);
        if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
        const hit = document.elementFromPoint(x, y);
        if (hit && (hit === target || target.contains(hit) || hit.contains(target)))
          out.push({x: x, y: y});
      }
      return JSON.stringify({points: out});
    })()''')
    return json.loads(raw)


def real_click(finder, pause=5):
    \"\"\"A click Angular Material accepts: buttons=1 and a delay before the release.\"\"\"
    info = hit_points(finder)
    if 'error' in info or not info.get('points'):
        return 'FAILED: ' + str(info.get('error', 'no point hits the target'))
    p = info['points'][0]
    cdp('Input.dispatchMouseEvent', type='mousePressed', x=p['x'], y=p['y'],
        button='left', buttons=1, clickCount=1)
    time.sleep(HOLD)
    cdp('Input.dispatchMouseEvent', type='mouseReleased', x=p['x'], y=p['y'],
        button='left', buttons=0, clickCount=1)
    time.sleep(pause)
    return 'clicked (' + str(p['x']) + ',' + str(p['y']) + ')'


def set_field(finder, text):
    \"\"\"Clear and retype a field. Backspace, because Cmd+A selects nothing here.\"\"\"
    r = real_click(finder, pause=1)
    if r.startswith('FAILED'):
        return r
    current = js('(' + finder + ').value || ""')
    for _ in range(len(current) + 10):
        cdp('Input.dispatchKeyEvent', type='rawKeyDown', key='Backspace',
            code='Backspace', windowsVirtualKeyCode=8, nativeVirtualKeyCode=8)
        cdp('Input.dispatchKeyEvent', type='keyUp', key='Backspace',
            code='Backspace', windowsVirtualKeyCode=8, nativeVirtualKeyCode=8)
    time.sleep(0.4)
    cdp('Input.insertText', text=text)
    time.sleep(0.4)
    return 'ok' if js('(' + finder + ').value') == text else 'value did not stick'


def ax_controls():
    out = []
    for node in cdp('Accessibility.getFullAXTree')['nodes']:
        if (node.get('role') or {}).get('value') not in ('radio', 'checkbox'):
            continue
        props = {p['name']: p.get('value', {}).get('value')
                 for p in (node.get('properties') or [])}
        out.append({'name': ((node.get('name') or {}).get('value') or '').strip(),
                    'checked': props.get('checked'),
                    'bid': node.get('backendDOMNodeId')})
    return out


def _ax_name(node):
    return ((node.get('name') or {}).get('value') or '').strip()


def _center(backend_id):
    # Viewport center of a node, or None when it has no box (hidden or detached)
    try:
        quad = cdp('DOM.getBoxModel', backendNodeId=backend_id)['model']['content']
    except Exception:
        return None
    x, y = sum(quad[0::2]) / 4, sum(quad[1::2]) / 4
    return (x, y) if x > 0 and y > 0 else None


CLICKABLE_ROLES = {'button', 'link', 'menuitem', 'tab', 'checkbox', 'radio', 'combobox', 'option'}


def ax_click(pattern, pause=2, role=None, index=0):
    # Click the element whose accessible name matches `pattern` (regex). The console
    # rebuilds the DOM constantly; the accessible name is what survives. Never raises.
    rx = re.compile(pattern)
    matches = []
    for node in cdp('Accessibility.getFullAXTree').get('nodes', []):
        if node.get('ignored'):
            continue
        name = _ax_name(node)
        node_role = (node.get('role') or {}).get('value') or ''
        if not name or not rx.search(name):
            continue
        if (role and node_role != role) or (not role and node_role not in CLICKABLE_ROLES):
            continue
        point = _center(node.get('backendDOMNodeId')) if node.get('backendDOMNodeId') else None
        if point:
            matches.append((name, point))
    if not matches:
        return 'ax_click: no visible match for ' + repr(pattern)
    if index >= len(matches):
        return 'ax_click: only ' + str(len(matches)) + ' match(es) for ' + repr(pattern)
    name, (x, y) = matches[index]
    click_at_xy(x, y)
    time.sleep(pause)
    return 'ax_click: clicked ' + repr(name)


# Console UI text, matched literally (pt-BR and en-US)
DIALOG_BUTTONS = r'^(OK|Ok|Got it|Entendi|Fechar|Close|Dispensar|Dismiss|Continuar|Continue)$'


def dismiss_dialog(pattern=DIALOG_BUTTONS, pause=1):
    # Close a tip or confirmation covering the form; otherwise later clicks hit the overlay
    for node in cdp('Accessibility.getFullAXTree').get('nodes', []):
        if node.get('ignored') or ((node.get('role') or {}).get('value') or '') not in ('button', 'link'):
            continue
        if not re.match(pattern, _ax_name(node)):
            continue
        point = _center(node.get('backendDOMNodeId')) if node.get('backendDOMNodeId') else None
        if point:
            click_at_xy(*point)
            time.sleep(pause)
            return 'dismiss_dialog: closed via ' + repr(_ax_name(node))
    return 'dismiss_dialog: nothing to close'


def pick(option, nth=0):
    # Select the radio/checkbox labeled `option` (nth occurrence). Material's real
    # <input> sits ~15px above the visible circle, so try several vertical offsets and
    # only report success when the accessibility tree says checked.
    found = [c for c in ax_controls() if c['name'] == option.strip()]
    if nth >= len(found):
        names = sorted({c['name'] for c in ax_controls() if c['name']})
        return 'FAILED: ' + str(len(found)) + ' control(s) named ' + repr(option) + '; available: ' + repr(names[:30])
    bid = found[nth]['bid']
    if found[nth]['checked'] in (True, 'true'):
        return 'already selected: ' + repr(option)
    try:
        cdp('DOM.scrollIntoViewIfNeeded', backendNodeId=bid)
        time.sleep(0.5)
    except Exception:
        pass
    center = _center(bid)
    if not center:
        return 'FAILED: ' + repr(option) + ' has no visible box'
    x, y = center
    for dy in (0, -15, 15, -8, 8, -22):
        cdp('Input.dispatchMouseEvent', type='mousePressed', x=x, y=y + dy, button='left', buttons=1, clickCount=1)
        time.sleep(HOLD)
        cdp('Input.dispatchMouseEvent', type='mouseReleased', x=x, y=y + dy, button='left', buttons=0, clickCount=1)
        time.sleep(1)
        state = next((c['checked'] for c in ax_controls() if c['bid'] == bid), None)
        if state in (True, 'true'):
            return 'selected: ' + repr(option) + ' (offset ' + str(dy) + 'px)'
    return 'FAILED: clicked ' + repr(option) + ' but the accessibility tree never reported it checked'


def body():
    return js('document.body.innerText')
""".replace("__HOLD__", repr(CLICK_HOLD_SECONDS))


def script(body: str) -> str:
    return PRELUDE + "\n" + body


def form_url(dev_id: str, app_id: str, key: str) -> str | None:
    slug = FORMS.get(key)
    if not slug:
        return None
    return f"{CONSOLE}/developers/{dev_id}/app/{app_id}/app-content/{slug}"


def run(dev_id: str, app_id: str, body: str, timeout: int = 300) -> str:
    """Run a script in the harness, always with the tab in the foreground."""
    return run_harness(script("ensure_foreground()\n" + body), timeout=timeout)


def open_form(dev_id: str, app_id: str, key: str, timeout: int = 240) -> str:
    url = form_url(dev_id, app_id, key)
    if not url:
        raise ValueError(
            f"'{key}' has no URL of its own — open it from the App content index "
            f"({CONSOLE}/developers/{dev_id}/app/{app_id}/app-content/overview)"
        )
    return run(
        dev_id,
        app_id,
        f'goto_url({url!r})\nwait_for_load()\ntime.sleep(8)\n'
        'print(page_info()["url"])\nprint(body()[:2000])\n',
        timeout=timeout,
    )


def parse_json_tail(output: str) -> Any:
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None

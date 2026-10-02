"""App Store Resolution Center: read the rejection and reply to App Review.

Apple's public API exposes neither the review messages nor replies to them. The
web console uses the internal /iris API, which accepts the dedicated Chrome's
cookie session. Reading is done with fetch inside the page; replying goes through
the console's own "Reply to App Review" box, because the attachment goes through
an upload that only the UI knows how to assemble.

Details learned by trial and error:
- The harness js() gives up after 5s; the fetch calls run in an IIFE that writes
  to window, and the script polls for the result.
- The reply textarea only enables the "Reply" button after real typing: setting
  .value is not enough, so the script types and deletes a space via CDP.
- Attachments go through DOM.setFileInputFiles and show "Processing..." before
  appearing in the list; sending before that drops the file.
- iPhone video is recorded in HEVC and weighs ~100 MB; converted to H.264 at
  1920px it is ~8 MB and opens in any player.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import browser

ASC = "https://appstoreconnect.apple.com"

# Printed by the harness script right after the Reply button is clicked. Without it
# in the output, the failure happened before anything could reach Apple.
CLICKED = "__SP__clicked"


class ReplyNotSent(browser.BrowserError):
    """The reply failed before the Reply button was clicked: nothing reached Apple."""


# Console UI labels, matched literally against the page text (pt-BR and en-US)
REPLY_OPEN = ("Responda à equipe de revisão de apps", "Reply to App Review")
REPLY_SEND = ("Responder", "Reply")
PROCESSING = ("Processando", "Processing")


def read_messages(app_id: str) -> list[dict]:
    """The app's Resolution Center messages, oldest first."""
    script = f"""
import time, json
new_tab({f"{ASC}/apps/{app_id}/distribution"!r})
wait_for_load()
time.sleep(3)
js(r\"\"\"window.__sp='running';(async()=>{{
 const g=async p=>{{const r=await fetch(p,{{credentials:'include'}}); return r.ok? r.json(): null}};
 const th=await g('/iris/v1/apps/{app_id}/resolutionCenterThreads');
 const out=[];
 for (const t of ((th&&th.data)||[])) {{
   const m=await g('/iris/v1/resolutionCenterThreads/'+t.id+'/resolutionCenterMessages?include=rejections&limit=50');
   const rej=((m&&m.included)||[]).filter(i=>i.type&&i.type.indexOf('ejection')>=0).map(i=>i.attributes.reasons||[]).flat();
   for (const x of ((m&&m.data)||[])) out.push({{thread:t.id, date:x.attributes.createdDate, sender:x.attributes.fromActor||null,
     text:(x.attributes.messageBody||'').replace(/<br\\s*\\/?>/g,' ').replace(/<[^>]+>/g,'').trim(), reasons:rej}});
 }}
 window.__sp=JSON.stringify(out.sort((a,b)=>a.date<b.date?-1:1));
}})().catch(e=>window.__sp='ERROR '+e); 'ok'\"\"\")
for _ in range(40):
    v = js("String(window.__sp)")
    if v != 'running': break
    time.sleep(1.5)
print('__SP__' + v)
"""
    out = browser.run_harness(script, timeout=180)
    payload = out.split("__SP__", 1)[-1].strip()
    if payload.startswith("ERROR") or not payload.startswith("["):
        raise browser.BrowserError(f"Could not read the Resolution Center: {payload[:300]}")
    return json.loads(payload)


def prepare_attachment(path: Path) -> Path:
    """Convert HEVC video to H.264 at 1920px (smaller and universal); anything else passes through unchanged."""
    if path.suffix.lower() not in (".mp4", ".mov", ".m4v"):
        return path
    if not (shutil.which("ffprobe") and shutil.which("ffmpeg")):
        return path
    codec = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    if codec != "hevc":
        return path
    out = Path(tempfile.mkdtemp(prefix="sp-review-")) / f"{path.stem}.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vf", "scale=-2:1920", "-c:v", "libx264",
         "-preset", "medium", "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
         "-movflags", "+faststart", str(out)],
        check=True,
    )
    return out


def reply(app_id: str, submission_id: str, text: str, attachments: list[Path]) -> str:
    """Reply to App Review through the console box and verify the message landed in the thread.

    Raises ReplyNotSent only when the failure provably happened before the click.
    Any other error means the message may have reached Apple.
    """
    script = f"""
import time, json
TEXT = {json.dumps(text)}
FILES = {json.dumps([str(p) for p in attachments])}
OPEN = {json.dumps(list(REPLY_OPEN))}
SEND = {json.dumps(list(REPLY_SEND))}
BUSY = {json.dumps(list(PROCESSING))}
new_tab({f"{ASC}/apps/{app_id}/distribution/reviewsubmissions/details/{submission_id}"!r})
wait_for_load()
for _ in range(15):
    time.sleep(2)
    if js("[...document.querySelectorAll('button')].some(b=>%s.includes(b.innerText.trim()))" % json.dumps(OPEN)): break
else:
    raise SystemExit('ERROR: the reply button did not appear (does the submission have an open issue?)')
js("[...document.querySelectorAll('button')].find(b=>%s.includes(b.innerText.trim())).click()" % json.dumps(OPEN))
time.sleep(3)
ok = js(\"\"\"(()=>{{const t=document.querySelector('[role=dialog] textarea'); if(!t) return false;
 Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(t, %s);
 t.dispatchEvent(new Event('input',{{bubbles:true}})); t.focus(); t.setSelectionRange(t.value.length,t.value.length); return true}})()\"\"\" % json.dumps(TEXT))
if not ok:
    raise SystemExit('ERROR: the reply box did not open (the console may not have rendered in headless mode; try SHIPSTORES_HEADLESS=0)')
cdp("Input.insertText", text=" ")
cdp("Input.dispatchKeyEvent", type="keyDown", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
cdp("Input.dispatchKeyEvent", type="keyUp", key="Backspace", code="Backspace", windowsVirtualKeyCode=8)
for f in FILES:
    doc = cdp("DOM.getDocument", depth=-1, pierce=True)
    ids = cdp("DOM.querySelectorAll", nodeId=doc["root"]["nodeId"], selector="[role=dialog] input[type=file]")["nodeIds"]
    if not ids:
        raise SystemExit('ERROR: attachment input not found')
    cdp("DOM.setFileInputFiles", nodeId=ids[-1], files=[f])
    name = f.rsplit('/', 1)[-1]
    for _ in range(90):
        time.sleep(2)
        d = js("(document.querySelector('[role=dialog]')||{{}}).innerText||''")
        if name in d and not any(b in d for b in BUSY): break
    else:
        raise SystemExit('ERROR: attachment did not finish processing: ' + name)
# "Mensagens"/"Messages" is console UI text, matched literally (pt-BR / en-US)
COUNT = r"(()=>{{const m=document.body.innerText.match(/(?:Mensagens|Messages) \\((\\d+)\\)/); return m? Number(m[1]): -1}})()"
before = js(COUNT)
sent = js("(()=>{{const b=[...document.querySelectorAll('[role=dialog] button')].filter(b=>%s.includes(b.innerText.trim())).pop(); if(!b) return 'no button'; if(b.disabled) return 'disabled'; b.click(); return 'sent'}})()" % json.dumps(SEND))
if sent != 'sent':
    raise SystemExit('ERROR: not sent (' + sent + ')')
print({CLICKED!r}, flush=True)
for _ in range(15):
    time.sleep(2)
    if js(COUNT) > before: break
else:
    raise SystemExit('ERROR: the message did not appear in the thread after sending')
print('__SP__ok')
"""
    try:
        out = browser.run_harness(script, timeout=600)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        if partial and CLICKED not in partial:
            raise ReplyNotSent("Timed out before clicking Reply; nothing was sent.") from exc
        raise browser.BrowserError("Timed out after clicking Reply; the message may have been sent.") from exc
    except browser.BrowserError as exc:
        # Only a failed script run carries its output; anything else may come after the click.
        if str(exc).startswith(("browser-harness failed", "browser-harness not found")) and CLICKED not in str(exc):
            erro = next((l for l in str(exc).splitlines() if l.startswith("ERROR")), str(exc)[-500:])
            raise ReplyNotSent(erro) from exc
        raise
    if "__SP__ok" not in out:
        erro = next((l for l in out.splitlines() if l.startswith("ERROR")), out[-500:])
        raise (browser.BrowserError if CLICKED in out else ReplyNotSent)(erro)
    return "sent"

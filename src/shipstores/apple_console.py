"""App settings that only exist in the console's internal API (/iris).

Territory availability and "App Privacy" have no usable public endpoint: setting
availability through the public API answers 409 even with every territory in the
payload, and privacy does not exist there at all. The web console uses /iris with
the cookie session, so these operations run as fetch calls inside an App Store
Connect page in the dedicated Chrome.

Rules Apple does not document (validated against a production app):
- appAvailabilities requires ALL territories in `included`, each with `available`.
- appDataUsages: category, purpose and protection go in the SAME record. Separate
  records (purpose only, protection only) are accepted one by one, but publishing
  fails with "An app data usage is missing a category/purpose or data protection type".
"""

from __future__ import annotations

import json

from . import browser

ASC = "https://appstoreconnect.apple.com"

DATA_CATEGORIES = (
    "PAYMENT_INFORMATION", "CREDIT_AND_FRAUD", "OTHER_FINANCIAL_INFO", "PRECISE_LOCATION",
    "COARSE_LOCATION", "SENSITIVE_INFO", "PHYSICAL_ADDRESS", "EMAIL_ADDRESS", "NAME",
    "PHONE_NUMBER", "OTHER_CONTACT_INFO", "CONTACTS", "EMAILS_OR_TEXT_MESSAGES",
    "PHOTOS_OR_VIDEOS", "AUDIO", "GAMEPLAY_CONTENT", "CUSTOMER_SUPPORT", "OTHER_USER_CONTENT",
    "BROWSING_HISTORY", "SEARCH_HISTORY", "USER_ID", "DEVICE_ID", "PURCHASE_HISTORY",
    "PRODUCT_INTERACTION", "ADVERTISING_DATA", "OTHER_USAGE_DATA", "CRASH_DATA",
    "PERFORMANCE_DATA", "OTHER_DIAGNOSTIC_DATA", "OTHER_DATA", "HEALTH", "FITNESS",
    "ENVIRONMENTAL_SCANNING", "HANDS", "HEAD_MOVEMENT",
)
DATA_PURPOSES = (
    "THIRD_PARTY_ADVERTISING", "DEVELOPERS_ADVERTISING", "ANALYTICS",
    "PRODUCT_PERSONALIZATION", "APP_FUNCTIONALITY", "OTHER_PURPOSES",
)


def _run_iris(app_id: str, body_js: str, timeout: int = 300) -> dict:
    """Run `body_js` (the body of an async function that returns an object) in a console page."""
    script = f"""
import time
new_tab({f"{ASC}/apps/{app_id}/distribution"!r})
wait_for_load()
time.sleep(3)
js(r\"\"\"window.__sp='running';(async()=>{{
 const H={{'Content-Type':'application/json'}};
 const req=async (method,path,body)=>{{const r=await fetch(path,{{method,credentials:'include',headers:H,body:body?JSON.stringify(body):undefined}});
   const t=await r.text(); return {{status:r.status, json:(()=>{{try{{return JSON.parse(t)}}catch(e){{return null}}}})(), text:t.slice(0,600)}}}};
 const all=async (path)=>{{let out=[],p=path; while(p){{const r=await req('GET',p); if(r.status!==200) throw new Error(path+' -> '+r.status+' '+r.text);
   out=out.concat(r.json.data); p=r.json.links&&r.json.links.next? r.json.links.next.replace(/^https:\\/\\/[^/]+/,''): null;}} return out}};
 const result=await (async()=>{{ {body_js} }})();
 window.__sp=JSON.stringify(result);
}})().catch(e=>window.__sp=JSON.stringify({{error:String(e)}})); 'ok'\"\"\")
for _ in range({timeout // 2}):
    v = js("String(window.__sp)")
    if v != 'running': break
    time.sleep(2)
print('__SP__' + v)
"""
    out = browser.run_harness(script, timeout=timeout + 60)
    payload = out.split("__SP__", 1)[-1].strip()
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise browser.BrowserError(f"Unexpected response from the console: {payload[:300]}") from exc
    if isinstance(result, dict) and result.get("error"):
        raise browser.BrowserError(result["error"])
    return result


def set_availability(app_id: str, territories: list[str], available_in_new: bool) -> dict:
    """Make the app available only in the given territories (3-letter ISO codes)."""
    wanted = json.dumps(sorted(set(t.upper() for t in territories)))
    body = f"""
 const want={wanted};
 const terr=(await all('/iris/v1/territories?limit=200')).map(t=>t.id);
 const missing=want.filter(t=>!terr.includes(t)); if(missing.length) throw new Error('unknown territory: '+missing.join(','));
 const rel=terr.map(t=>({{type:'territoryAvailabilities',id:'${{'+t+'}}'}}));
 const inc=terr.map(t=>({{type:'territoryAvailabilities',id:'${{'+t+'}}',attributes:{{available:want.includes(t)}},relationships:{{territory:{{data:{{type:'territories',id:t}}}}}}}}));
 const r=await req('POST','/iris/v2/appAvailabilities',{{data:{{type:'appAvailabilities',attributes:{{availableInNewTerritories:{json.dumps(available_in_new)}}},
   relationships:{{app:{{data:{{type:'apps',id:'{app_id}'}}}},territoryAvailabilities:{{data:rel}}}}}},included:inc}});
 if(r.status>=300) throw new Error('appAvailabilities -> '+r.status+' '+r.text);
 const now=(await all('/iris/v2/appAvailabilities/{app_id}/territoryAvailabilities?limit=200&include=territory'))
   .filter(d=>d.attributes.available).map(d=>(d.relationships.territory.data||{{}}).id);
 return {{total_territories:terr.length, available:now}};
"""
    return _run_iris(app_id, body)


def set_privacy(app_id: str, usages: list[dict]) -> dict:
    """Replace the whole privacy declaration and publish it.

    usages: [{"category": "NAME", "purposes": ["APP_FUNCTIONALITY"], "linked": true,
    "tracking": false}, ...]. An empty list declares "we do not collect data".
    """
    records = []
    for u in usages:
        cat = u["category"].upper()
        if cat not in DATA_CATEGORIES:
            raise ValueError(f"Invalid category: {cat}. Options: {', '.join(DATA_CATEGORIES)}")
        purposes = [p.upper() for p in u.get("purposes") or []]
        bad = [p for p in purposes if p not in DATA_PURPOSES]
        if not purposes or bad:
            raise ValueError(f"{cat}: invalid purposes {bad or '(none)'}. Options: {', '.join(DATA_PURPOSES)}")
        protections = ["DATA_LINKED_TO_YOU" if u.get("linked", True) else "DATA_NOT_LINKED_TO_YOU"]
        if u.get("tracking"):
            protections.append("DATA_USED_TO_TRACK_YOU")
        for purpose in purposes:
            for protection in protections:
                records.append({"category": cat, "purpose": purpose, "protection": protection})
    plan = json.dumps(records)
    body = f"""
 const plan={plan};
 const old=await all('/iris/v1/apps/{app_id}/dataUsages?limit=200');
 for (const d of old) {{ const r=await req('DELETE','/iris/v1/appDataUsages/'+d.id); if(r.status>=300) throw new Error('DELETE '+d.id+' -> '+r.status); }}
 const created=[];
 const recs = plan.length? plan : [{{protection:'DATA_NOT_COLLECTED'}}];
 for (const x of recs) {{
   const rel={{app:{{data:{{type:'apps',id:'{app_id}'}}}},dataProtection:{{data:{{type:'appDataUsageDataProtections',id:x.protection}}}}}};
   if (x.category) rel.category={{data:{{type:'appDataUsageCategories',id:x.category}}}};
   if (x.purpose) rel.purpose={{data:{{type:'appDataUsagePurposes',id:x.purpose}}}};
   const r=await req('POST','/iris/v1/appDataUsages',{{data:{{type:'appDataUsages',relationships:rel}}}});
   if (r.status!==201) throw new Error('appDataUsages '+(x.category||'')+' -> '+r.status+' '+r.text);
   created.push((x.category||'NOT_COLLECTED')+':'+(x.purpose||'')+':'+x.protection);
 }}
 const p=await req('PATCH','/iris/v1/appDataUsagesPublishState/{app_id}',{{data:{{type:'appDataUsagesPublishState',id:'{app_id}',attributes:{{published:true}}}}}});
 if (p.status>=300) throw new Error('publish -> '+p.status+' '+p.text);
 return {{removed:old.length, created:created, published:true}};
"""
    return _run_iris(app_id, body)


# UI labels in pt-BR and en-US
ADD_FOR_REVIEW = ("Adicionar para revisão", "Add for Review")
# Banner the page shows when the item is already in the draft ("Este item foi adicionado
# para revisão, mas você ainda pode removê-lo."). The English form is a guess: only the
# pt-BR one has been seen.
ADDED_FOR_REVIEW_RE = r"adicionado para revis|added (for|to) review"


def add_to_review_draft(app_id: str, kind: str, object_id: str) -> str:
    """Put a subscription or a subscription group in the review draft with "Add for Review".

    After a canceled submission, the public API refuses POST /v1/subscriptionSubmissions
    with "has no pending version for submission", but the console accepts it: the button
    creates the subscription's new version and attaches it to the open draft.
    `kind` is "subscriptions" or "subscription-groups". The draft holding the app version
    must exist first, otherwise the menu only offers to create a new submission.
    """
    if kind not in ("subscriptions", "subscription-groups"):
        raise ValueError("kind must be 'subscriptions' or 'subscription-groups'")
    url = f"{ASC}/apps/{app_id}/distribution/{kind}/{object_id}"
    # The button is born disabled while the page loads (~6 s). Disabled does NOT prove the
    # item is already in the draft (trusting it once sent a submission with 1 item instead
    # of 4): only the "added for review" banner proves it. So wait for the button to enable
    # or the banner to appear.
    script = f"""
import time, json
ADD = {json.dumps(list(ADD_FOR_REVIEW))}
ADDED = {ADDED_FOR_REVIEW_RE!r}
BUTTON = "(()=>{{const b=[...document.querySelectorAll('button')].find(b=>%s.includes(b.innerText.trim())); return b? (b.disabled?'off':'on') : null}})()" % json.dumps(ADD)
BANNER = "new RegExp(%s,'i').test(document.body.innerText)" % json.dumps(ADDED)
new_tab({url!r})
wait_for_load()
state = None
for _ in range(30):
    time.sleep(1.5)
    if js(BANNER):
        state = 'added'
        break
    state = js(BUTTON)
    if state == 'on':
        break
print('URL=' + str(page_info().get('url','')))
if state == 'added':
    print('__SP__ok already in the draft')
elif state != 'on':
    print('__SP__ERROR Add for Review button ' + ('did not appear' if state is None else 'stayed disabled for 45 s'))
else:
    js("[...document.querySelectorAll('button')].find(b=>%s.includes(b.innerText.trim())).click()" % json.dumps(ADD))
    time.sleep(3)
    ok = js(r\"\"\"(()=>{{const el=[...document.querySelectorAll('button,li,a,[role=menuitem],div')]
      .filter(e=>e.innerText&&/^(Rascunho|Draft)/.test(e.innerText.trim())&&e.getBoundingClientRect().height>0&&e.getBoundingClientRect().height<80);
      const t=el[el.length-1]; if(!t) return false; t.click(); return true}})()\"\"\")
    if not ok:
        print('__SP__ERROR the menu has no open draft submission')
    else:
        confirmed = False
        for _ in range(10):
            time.sleep(1.5)
            if js(BANNER):
                confirmed = True
                break
        print('__SP__ok' if confirmed else '__SP__ERROR clicked the draft, but the page did not confirm')
"""
    out = browser.run_harness(script, timeout=180)
    payload = out.split("__SP__", 1)[-1].strip()
    if not payload.startswith("ok"):
        raise browser.BrowserError(f"Could not add {kind}/{object_id} to the review draft: {payload[:300]}")
    return payload

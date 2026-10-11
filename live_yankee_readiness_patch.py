from __future__ import annotations

from datetime import datetime, timezone
from html import escape

import live_yankee_feed as feed
import web as web_module

_INSTALLED=False
_ORIGINAL_SCOREBOARD=None
_ORIGINAL_PAGE=None


def _readiness(db):
    now=datetime.now(timezone.utc)
    rows=db.fetchall("SELECT * FROM live_yankee_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id")
    details=[]; events=set(); ready_events=set(); ready_candidates=0
    for raw in rows:
        c=dict(raw); eid=str(c.get('event_id') or '')
        if eid: events.add(eid)
        quotes=feed._valid_quotes(db,c,now) if feed._actionable(c.get('commence_time'),now) else {}
        best=max((float(q.get('price') or 0) for q in quotes.values()),default=None)
        ready=bool(quotes)
        if ready:
            ready_candidates+=1
            if eid: ready_events.add(eid)
        details.append({
            'id':int(c.get('id') or 0),'source_lane':str(c.get('source_lane') or ''),
            'selection':str(c.get('selection') or ''),'event_id':eid,
            'min_odds':float(c.get('min_odds') or 0),'valid_books':len(quotes),
            'best_valid_price':best,'ready':ready,'commence_time':c.get('commence_time'),
        })
    return {
        'queued_candidates':len(rows),'queued_distinct_events':len(events),
        'price_ready_candidates':ready_candidates,'price_ready_distinct_events':len(ready_events),
        'events_needed_for_card':max(0,4-len(ready_events)),'details':details[:30],
    }


def _scoreboard(db):
    out=dict(_ORIGINAL_SCOREBOARD(db)); out['readiness']=_readiness(db); return out


def _page_html():
    html=_ORIGINAL_PAGE(); r=_readiness(web_module.db); parts=[]
    for x in r['details']:
        price='—' if x['best_valid_price'] is None else f"{x['best_valid_price']:.2f}"
        css='ok' if x['ready'] else 'warn'; label='READY' if x['ready'] else 'WAITING PRICE'
        parts.append(
            f"<tr><td>{escape(x['source_lane'])}</td><td>{escape(x['selection'])}</td>"
            f"<td>{x['valid_books']}</td><td>{price}</td><td class='{css}'>{label}</td></tr>"
        )
    rows=''.join(parts) or "<tr><td colspan='5'>No queued candidates.</td></tr>"
    panel=(
        "<div class='panel'><h2>Queue readiness</h2>"
        f"<div class='muted'>{r['queued_candidates']} queued candidates · {r['queued_distinct_events']} distinct events · "
        f"{r['price_ready_distinct_events']} price-ready events · {r['events_needed_for_card']} more ready event(s) needed for a Yankee.</div>"
        "<div class='table-wrap'><table><thead><tr><th>Source</th><th>Selection</th><th>Valid books</th><th>Best valid</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div></div>"
    )
    return html.replace('</body>',panel+'</body>',1)


def install():
    global _INSTALLED,_ORIGINAL_SCOREBOARD,_ORIGINAL_PAGE
    if _INSTALLED: return {'installed':True,'already_installed':True}
    _ORIGINAL_SCOREBOARD=feed.scoreboard; _ORIGINAL_PAGE=feed._page_html
    feed.scoreboard=_scoreboard; feed._page_html=_page_html
    web_module.VERSION='0.19.44'; _INSTALLED=True
    return {'installed':True,'already_installed':False,'dashboard_version':web_module.VERSION}

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
    book_events={}; book_titles={}
    for raw in rows:
        c=dict(raw); eid=str(c.get('event_id') or '')
        if eid: events.add(eid)
        quotes=feed._valid_quotes(db,c,now) if feed._actionable(c.get('commence_time'),now) else {}
        best=max((float(q.get('price') or 0) for q in quotes.values()),default=None)
        ready=bool(quotes)
        if ready:
            ready_candidates+=1
            if eid: ready_events.add(eid)
            for book_key, quote in quotes.items():
                key=str(book_key or '')
                if not key or not eid:
                    continue
                book_events.setdefault(key,set()).add(eid)
                book_titles[key]=str(quote.get('bookmaker_title') or quote.get('bookmaker_key') or key)
        details.append({
            'id':int(c.get('id') or 0),'source_lane':str(c.get('source_lane') or ''),
            'selection':str(c.get('selection') or ''),'event_id':eid,
            'min_odds':float(c.get('min_odds') or 0),'valid_books':len(quotes),
            'best_valid_price':best,'ready':ready,'commence_time':c.get('commence_time'),
        })
    coverage=sorted(
        ((len(event_ids),book_titles.get(book_key,book_key),book_key) for book_key,event_ids in book_events.items()),
        key=lambda x:(-x[0],x[1],x[2]),
    )
    best_coverage,best_book_title,best_book_key=coverage[0] if coverage else (0,None,None)
    return {
        'queued_candidates':len(rows),'queued_distinct_events':len(events),
        'price_ready_candidates':ready_candidates,'price_ready_distinct_events':len(ready_events),
        'events_needed_for_card':max(0,4-len(ready_events)),'details':details[:30],
        'best_book_event_coverage':best_coverage,'best_book_title':best_book_title,
        'best_book_key':best_book_key,
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

    # The headline must reflect the final card-assembly authority, not merely
    # whether four events have a price somewhere.  By render time the same-book
    # policy patch has replaced feed._pick_four, so this includes both verified
    # same-book cards and the tightly bounded same-book consolidation route.
    try:
        picked=feed._pick_four(web_module.db,datetime.now(timezone.utc))
    except Exception:
        picked=None

    if picked:
        quotes=picked[1] if len(picked)>1 else []
        mode=str(picked[2] if len(picked)>2 else 'SAME_BOOK').replace('_',' ')
        book=''
        if quotes:
            book=str(quotes[0].get('bookmaker_title') or quotes[0].get('bookmaker_key') or '')
        placement=(
            f"<span class='ok'><strong>PLACEABLE SAME-BOOK YANKEE READY"
            f"{(' at '+escape(book)) if book else ''}</strong></span> · {escape(mode)}."
        )
    elif r['price_ready_distinct_events']>=4:
        best=''
        if r['best_book_event_coverage'] and r['best_book_title']:
            best=(
                f" Best current bookmaker coverage: {r['best_book_event_coverage']}/4 distinct events "
                f"at {escape(str(r['best_book_title']))}."
            )
        placement=(
            "<span class='warn'><strong>NO SAME-BOOK YANKEE CURRENTLY AVAILABLE.</strong></span>"
            +best
        )
    else:
        needed=max(0,4-r['price_ready_distinct_events'])
        placement=(
            f"<span class='warn'><strong>{needed} more distinct price-ready event(s) needed before "
            "same-book Yankee assembly is possible.</strong></span>"
        )

    panel=(
        "<div class='panel'><h2>Queue readiness</h2>"
        f"<div class='muted'>{r['queued_candidates']} queued candidates · {r['queued_distinct_events']} distinct events · "
        f"{r['price_ready_candidates']} price-ready candidates across {r['price_ready_distinct_events']} distinct events. "
        f"{placement}</div>"
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
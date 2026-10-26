"""Owner-selected earlier history: the plan and the provider identity it would carry.

Recording old messages into canonical conversation history needs a Host ingress that
does not wake, reply, notify or create tasks. No current Host advertises one
(``presence_history_import_version``), so a saved plan reads nothing from Telegram and
sends nothing anywhere. It never falls back to ``/presence/turn`` or ``/chat/inject``:
those would treat old messages as new arrivals. ``history_event`` shows the exact facts
an import would carry; they match live capture so the Host can deduplicate the two.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import secrets
import time

from .normalize import actor_facts, canonical_text, content_revision, iso, media_descriptor
from .participant_state import receiving_refusal
from .presence import build_event

MAX_PER_CHAT = 5000
MAX_WINDOW = timedelta(days=3660)


def host_supports_import(capabilities):
    return type(capabilities) is dict and capabilities.get('presence_history_import_version') == 1


def _when(value, name):
    if type(value) is not str or len(value) > 40:
        raise ValueError(name + ' must be an ISO 8601 time with a time zone')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(name + ' must be an ISO 8601 time with a time zone') from None
    if parsed.utcoffset() is None:
        raise ValueError(name + ' must be an ISO 8601 time with a time zone')
    return parsed.astimezone(timezone.utc)


def _peers(value, name):
    if type(value) is not list or len(value) > 1000:
        raise ValueError(name + ' must be a list of numeric chat IDs')
    peers = []
    for item in value:
        peer = int(item) if type(item) is str and item.lstrip('-').isdigit() else item
        if type(peer) is not int or peer == 0:
            raise ValueError(name + ' must be a list of numeric chat IDs')
        peers.append(peer)
    return sorted(set(peers))


def validate_plan(body, row, account, now=None):
    """Normalize one owner request. Receiving-scope exclusions always apply."""
    if type(body) is not dict or set(body) - {'chats', 'exclude', 'from', 'to', 'max_per_chat'}:
        raise ValueError('import plan fields are chats, exclude, from, to and max_per_chat')
    now = now or datetime.now(timezone.utc)
    start, end = _when(body.get('from'), 'from'), _when(body.get('to'), 'to')
    if not start < end or end > now + timedelta(days=1) or end - start > MAX_WINDOW:
        raise ValueError('from must be before to, within ten years, and not in the future')
    cap = body.get('max_per_chat', 500)
    if type(cap) is not int or not 1 <= cap <= MAX_PER_CHAT:
        raise ValueError('max_per_chat must be 1..%d' % MAX_PER_CHAT)
    exclude = _peers(body.get('exclude', []), 'exclude')
    chats = body.get('chats', 'receiving')
    if chats != 'receiving':
        chats = _peers(chats, 'chats')
        if not chats:
            raise ValueError('choose at least one chat or all receiving chats')
        outside = [peer for peer in chats if receiving_refusal(row, peer, account)]
        if outside:
            raise ValueError('chats outside the receiving scope cannot be imported: ' + ','.join(map(str, outside)))
    return {'chats': chats, 'exclude': exclude, 'from': start.isoformat(), 'to': end.isoformat(), 'max_per_chat': cap}


def save_plan(journal, account, spec):
    plan_id = 'imp_' + secrets.token_hex(8)
    now = time.time()
    with journal.db:
        journal.db.execute("INSERT INTO imports(id,account,spec,state,progress,created,updated) VALUES(?,?,?,?,?,?,?)",
                           (plan_id, str(account), json.dumps(spec, sort_keys=True), 'saved', '{}', now, now))
    return plan_id


def plans(journal, account, limit=20):
    rows = journal.db.execute("SELECT id,spec,state,progress,created,updated FROM imports WHERE account=? ORDER BY created DESC LIMIT ?",
                              (str(account), limit)).fetchall()
    return [{'id': r['id'], 'spec': json.loads(r['spec']), 'state': r['state'], 'progress': json.loads(r['progress']),
             'created_unix': r['created'], 'updated_unix': r['updated']} for r in rows]


def cancel_plan(journal, account, plan_id):
    with journal.db:
        return journal.db.execute("UPDATE imports SET state='cancelled', updated=? WHERE account=? AND id=? AND state='saved'",
                                  (time.time(), str(account), str(plan_id))).rowcount


def history_event(account, peer, topic, message, sender=None):
    """One earlier message in the shape proposed for a history-only Host ingress.

    Identity facts are built by the same functions as live capture: an unedited
    message keeps its plain ID, an edited one is its current revision (its original
    text is unknown and not invented). The account's own past messages are
    authored by that platform account, never by the owner or current Ouroboros.
    """
    own = bool(getattr(message, 'out', False))
    actor_id = int(account) if own else int(getattr(message, 'sender_id', None) or peer)
    edited = getattr(message, 'edit_date', None) is not None
    media = media_descriptor(message)
    if media is not None:
        media['status'] = 'not_downloaded'
    facts = {'sent_at': iso(getattr(message, 'date', None)),
             'reply_to_message_id': getattr(getattr(message, 'reply_to', None), 'reply_to_msg_id', None)}
    if edited:
        facts['edited_at'] = iso(message.edit_date)
    if media is not None:
        facts['media'] = media
    event = build_event(int(account), int(peer), message.id, actor_id, canonical_text(message), topic_id=topic,
                        event_kind='edit' if edited else 'new',
                        revision=content_revision(message) if edited else '',
                        actor=actor_facts(sender), message=facts)
    return {'source_event_id': event['source_event_id'], 'actor': event['actor'],
            'authorship': 'platform_account' if own else 'participant',
            'text': event['text'], 'message': event['message']}

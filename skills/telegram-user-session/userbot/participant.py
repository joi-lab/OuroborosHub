"""Account-wide Telegram participant using current Places, not legacy allowlist profiles."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode, quote
from .config import load_config, _private_dir
from .participant_state import Journal, policy, digest
from .participant_ops import Operations, SPECS, validate
from .presence import _urllib_transport, PresenceToken, build_event

@contextmanager
def session_lock(root):
    """Kernel-released lifetime lock. A stale word/file never denies a new owner."""
    root = _private_dir(root)
    path = root / 'participant-session.lock'
    if path.is_symlink():
        raise ValueError('session lock redirected')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_BINARY', 0), 0o600)
    locked = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'0')
        os.lseek(fd, 0, 0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        if locked:
            if os.name == 'nt':
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

class Host:
    def __init__(self, url, token, transport=None):
        self.transport = transport or _urllib_transport(url, timeout=30)
        self.token = token
    def call(self, method, path, body=None):
        return self.transport(method, path, body, {'x-skill-token': self.token.use_in_request()})
    def identity(self):
        status, body = self.call('GET', '/identity')
        if status != 200 or body.get('ok') is not True or any(body.get(k) != 1 for k in ('places_version','presence_work_output_version','presence_continuation_version','presence_delivery_version')):
            raise RuntimeError('current authenticated Host Places unavailable')
        return body
    def place(self, account, peer='', topic=''):
        status, body = self.call('GET', '/presence/place?' + urlencode({'provider':'telegram', 'account_id':str(account), 'conversation_id':str(peer), 'thread_id':str(topic or '')}))
        if status != 200 or body.get('state') not in {'active','silent','paused'}:
            raise RuntimeError('place not connected or unreadable')
        return body

class CurrentAuthorization:
    """One current local/Host authorization owner for both transport modes."""
    def __init__(self, root, host, account, mode):
        self.root, self.host, self.account, self.mode = Path(root), host, str(account), mode
        config = load_config(self.root)
        self.credentials = (config.api_id, config.api_hash, config.phone) if config else None
    def current(self, peer='', write=False, *, topic=None, expected_binding=None):
        p = policy(self.root)
        c = load_config(self.root)
        if p['mode'] != self.mode or c is None or str(c.account_id) != self.account:
            raise ValueError('account or mode changed')
        if self.credentials != (c.api_id, c.api_hash, c.phone):
            raise ValueError('session configuration changed')
        if p['expected_account_id'] and p['expected_account_id'] != self.account:
            raise ValueError('authenticated account does not match expected identity')
        if peer and str(peer).lstrip('-').isdigit() and int(peer) in p['blocked_peers']:
            raise ValueError('peer blocked by current configuration')
        if self.mode == 'legacy' and (int(peer) not in c.allowed_peer_ids or not expected_binding
                or c.binding_for(int(peer), topic) != expected_binding):
            raise ValueError('legacy enrollment changed')
        if write and not p['outgoing']:
            raise ValueError('outgoing actions paused locally')
        return p
    async def authorize(self, peer='', topic='', write=False, *, expected_binding=None):
        self.current(peer, write, topic=topic or None, expected_binding=expected_binding)
        place = await asyncio.to_thread(self.host.place, self.account, peer, topic)
        self.current(peer, write, topic=topic or None, expected_binding=expected_binding)
        if write and place['state'] != 'active':
            raise ValueError('destination is Silent or Paused')
        return place

class TopicRoutingUnavailable(ValueError):
    """Captured Telegram source lacks a currently provable forum destination."""


class Participant:
    def __init__(self, root, adapter, host, journal, account):
        self.root, self.adapter, self.host, self.journal, self.account = Path(root), adapter, host, journal, str(account)
        self.ops = Operations(adapter, root)
        self.ops.guard = self.authorize
        self.capabilities = {}
        self.authorization = CurrentAuthorization(root, host, account, 'participant')
        self.next_send = 0.0
    def current(self, peer='', write=False):
        return self.authorization.current(peer, write)
    async def authorize(self, peer='', topic='', write=False):
        return await self.authorization.authorize(peer, topic, write)
    async def dispatch_one(self):
        from telethon import errors
        row = self.journal.claim(self.account)
        if not row:
            return False
        args = json.loads(row['args'])
        effect = validate(row['op'], args)
        write = effect != 'read'
        try:
            await self.authorize(args.get('peer',''), args.get('topic_id',''), write)
            if await self.adapter.get_me() != int(self.account):
                raise ValueError('authenticated account changed')
            prepared = await asyncio.wait_for(self.ops.prepare(row['op'], args), 60)
            # Resolve aliases BEFORE the final destination check; a @name must
            # not evade a numeric muted/blocked place. Source reads are checked too.
            from telethon import utils
            if prepared.get('source_peer') is not None:
                await self.authorize(prepared['resolved_source_peer'], '', False)
            if prepared.get('user') is not None:
                await self.authorize(prepared['resolved_user'], '', write)
            await self.authorize(prepared.get('resolved_peer',''), prepared.get('topic_id',''), write)
            self.current(prepared.get('resolved_peer',''), write)
        except errors.FloodWaitError as exc:
            self.journal.set(row['seq'], 'queued', {'code':'flood_wait','reason':type(exc).__name__,'retry_after_seconds':exc.seconds}, max(1, exc.seconds))
            self.journal.flood(self.account, exc.seconds, type(exc).__name__)
            return True
        except (TimeoutError, OSError, RuntimeError):
            self.journal.set(row['seq'], 'queued', {'code':'preflight_unavailable'}, 30)
            return True
        except Exception as exc:
            # Only our fixed validation sentences become public reason codes;
            # arbitrary provider text may contain private request details.
            reasons = {'outgoing actions paused locally':'outgoing_paused',
                'destination is Silent or Paused':'destination_inactive',
                'account or mode changed':'account_changed',
                'authenticated account changed':'account_changed',
                'unknown topic':'unknown_topic',
                'selected document is not a Telegram sticker; select its canonical sticker-pack document':'not_a_sticker'}
            reason = reasons.get(str(exc)) if type(exc) is ValueError else None
            self.journal.set(row['seq'], 'failed', {'code':'preflight_refused', 'type':type(exc).__name__, 'reason':reason})
            return True
        remaining = self.journal.flood_remaining(self.account)
        if remaining:
            self.journal.set(row['seq'], 'queued', {'code':'flood_wait','retry_after_seconds':remaining}, remaining)
            return True
        self.journal.set(row['seq'], 'attempted')
        try:
            result = await asyncio.wait_for(self.ops.execute(row['op'], prepared, row['random_id']), 90)
        except asyncio.CancelledError:
            self.journal.set(row['seq'], 'unknown' if write else 'queued', {'code':'interrupted'})
            raise
        except (errors.FloodWaitError, errors.SlowModeWaitError) as exc:
            # A typed Telegram refusal proves no accepted operation; preserve ID
            # and pace the account, never bypass the provider's flood controls.
            self.journal.set(row['seq'], 'queued', {'code':'flood_wait','reason':type(exc).__name__,'retry_after_seconds':exc.seconds}, max(1, exc.seconds))
            self.journal.flood(self.account, exc.seconds, type(exc).__name__)
            self.next_send = asyncio.get_running_loop().time() + max(1, exc.seconds)
            return True
        except (errors.ServerError, errors.TimeoutError, errors.TimedOutError, errors.RandomIdDuplicateError, TimeoutError, ConnectionError, OSError, RuntimeError) as exc:
            self.journal.set(row['seq'], 'unknown' if write else 'queued', {'code':'outcome_unknown', 'type':type(exc).__name__}, 30)
            return True
        except errors.RPCError as exc:
            self.journal.set(row['seq'], 'failed', {'code':'telegram_refused', 'type':type(exc).__name__,
                'rpc_code':exc.code, 'rpc_message':exc.message,
                'request_type':type(exc.request).__name__ if exc.request is not None else None})
            return True
        except Exception as exc:
            self.journal.set(row['seq'], 'unknown' if write else 'failed', {'code':'execution_error', 'type':type(exc).__name__})
            return True
        if row['op'] in {'send_text','send_media','send_sticker','forward'}:
            report = self.delivery_body(row, {**args,'topic_id':prepared.get('topic_id')}, result, prepared['resolved_peer'])
            self.journal.settle_with_report(row['seq'],result,row['id'],report)
        else:
            self.journal.set(row['seq'], 'sent' if write else 'done', result)
        self.next_send = asyncio.get_running_loop().time() + policy(self.root)['interval']
        return True
    def delivery_body(self, row, args, result, peer):
        origin = json.loads(row.get('origin') or '{"kind":"tool"}')
        body = {'schema_version':1, 'delivery_id': row['id'], 'part_id':'0', 'state':'delivered',
                'provider':'telegram', 'account_id':self.account, 'conversation_id':str(peer),
                'thread_id':str(args.get('topic_id') or ''), 'text':args.get('text',''),
                'format':'text' if row['op']=='send_text' else 'media',
                'message':{'message_id':str(result['message_id'])}, 'origin':origin}
        return body
    def spool(self, event, kind='new'):
        """Capture immutable TL bytes before the first provider/Host await."""
        m = event.message
        if m.out or not event.chat_id or not event.sender_id:
            return
        p = self.current()
        if event.chat_id in p['blocked_peers']:
            return  # Explicit excluded scope, not a capture/storage fault.
        if len((event.raw_text or '').encode('utf-8')) > 60000:
            raise RuntimeError('raw ingress too large')
        payload = bytes(m)
        key = digest([self.account,event.chat_id,m.id,kind,str(getattr(m,'edit_date',None)),hashlib.sha256(payload).hexdigest()])
        self.journal.capture_raw(key,{'account_id':self.account,'peer':event.chat_id,'actor':event.sender_id,'kind':kind,'private':bool(event.is_private)},payload)
    async def hydrate_once(self):
        from telethon.extensions import BinaryReader
        from types import SimpleNamespace
        for row in self.journal.raw_heads(self.account):
            if self.journal.flood_remaining(self.account): return
            if row['due'] > time.time(): continue
            meta=json.loads(row['metadata'])
            if meta['peer'] in self.current()['blocked_peers']:
                self.journal.raw_done(row['id']); continue
            with BinaryReader(row['payload']) as reader:
                m=reader.tgread_object()
            async def get_sender(): return await self.adapter.client.get_entity(meta['actor'])
            async def get_chat(): return await self.adapter.client.get_entity(meta['peer'])
            event=SimpleNamespace(message=m,chat_id=meta['peer'],sender_id=meta['actor'],raw_text=getattr(m,'message','') or '',is_private=meta['private'],get_sender=get_sender,get_chat=get_chat)
            from telethon import errors
            try:
                await asyncio.wait_for(self.capture(event,meta['kind'],raw_id=row['id']),90)
            except errors.FloodWaitError as exc:
                self.journal.raw_defer(row['id'],max(1,exc.seconds),'flood_wait')
                self.journal.flood(self.account,exc.seconds)
                continue
            except TopicRoutingUnavailable:
                # Retain this peer's exact raw head; other peers and tools keep moving.
                self.journal.raw_defer(row['id'],60,'topic_routing_unavailable')
                continue
            except (ConnectionError,TimeoutError,asyncio.TimeoutError,errors.RPCError) as exc:
                import traceback
                frames='/'.join(frame.name for frame in traceback.extract_tb(exc.__traceback__)[-5:])
                self.journal.raw_defer(row['id'],30,'provider_read_retry:'+type(exc).__name__+':'+frames)
                continue
            self.journal.raw_done(row['id'])
    async def capture(self, event, kind='new', raw_id=None):
        from telethon import utils
        m = event.message
        if m.out or not event.chat_id or not event.sender_id:
            return
        p = self.current(event.chat_id)
        text = event.raw_text or ''
        if len(text.encode('utf-8')) > 60000:
            raise RuntimeError('ingress text exceeds capture bound')
        # All supported provider events are reported; host wakes decide attention.
        try:
            sender = await event.get_sender()
        except ValueError:
            # A valid public sender ID need not have an access hash in this
            # session's entity cache. Display-profile enrichment is optional;
            # keep the authenticated numeric actor and leave profile/bot unknown.
            sender = None
        chat = await event.get_chat()
        topic = getattr(getattr(m,'reply_to',None), 'reply_to_top_id', None)
        if getattr(chat,'forum',False):
            topic = await self.forum_topic(event.chat_id, m)
        facts = ['any_message']
        if getattr(event,'is_private',False): facts.append('direct_message')
        if getattr(m,'mentioned',False): facts.append('mention')
        if getattr(sender,'bot',False): facts.append('from_bot')
        reply_id = getattr(getattr(m,'reply_to',None),'reply_to_msg_id',None)
        if reply_id:
            target = await self.adapter.client.get_messages(event.chat_id, ids=reply_id)
            if target is not None and getattr(target,'sender_id',None) == int(self.account):
                facts.append('reply_to_me')
        files = []
        if m.media and getattr(m,'file',None):
            size = getattr(m.file, 'size', 0) or 0
            if size > 32*1024*1024:
                text += '\n[Media not staged: exceeds 32 MiB; available via Telegram message reference.]'
            else:
                directory = self.root / 'incoming-media'
                directory.mkdir(exist_ok=True)
                if directory.is_symlink(): raise ValueError('media directory redirected')
                name = digest([self.account,event.chat_id,m.id,kind,str(getattr(m,'edit_date',None)),getattr(getattr(m,'document',None),'id',None),hashlib.sha256(bytes(m.media)).hexdigest()]) + (m.file.ext or '.bin')
                path = directory / name
                if path.is_symlink(): raise ValueError('media path redirected')
                if not path.exists():
                    temporary=path.with_name('.'+name+'.download')
                    if temporary.is_symlink(): raise ValueError('temporary media path redirected')
                    try:
                        missing = await self.download_snapshot(event.chat_id,m,temporary)
                        if missing:
                            text += '\n[Original Telegram media unavailable: '+missing+'; newer media was not substituted. Original Telegram reference retained.]'
                        elif temporary.is_file(): os.replace(temporary,path)
                    finally: temporary.unlink(missing_ok=True)
                if path.is_file():
                    files.append(str(path))
        if not text and not files:
            text = '[Telegram service/media event: ' + type(m.media or m.action).__name__ + ']'
        revision = (str(m.edit_date) + ':' + hashlib.sha256(bytes(m)).hexdigest()) if kind=='edit' else ''
        e = build_event(int(self.account),event.chat_id,m.id,event.sender_id,text,topic_id=topic,event_kind=kind,revision=revision,
              conversation={'title': getattr(chat,'title',None) or getattr(chat,'first_name','')}, 
              message={'username':getattr(sender,'username',None), 'sender_name':getattr(sender,'first_name',''),
                       'sender_profile_status':'available' if sender is not None else 'unavailable',
                       'mentioned':bool(m.mentioned),
                       'reply_to_message_id':getattr(getattr(m,'reply_to',None),'reply_to_msg_id',None)})
        e['facts'] = facts
        self.journal.capture(e, files, raw_id=raw_id)
    async def download_snapshot(self, peer, message, temporary):
        """Refresh expiring references, never a changed immutable media snapshot."""
        from telethon import errors, utils
        current = message
        for attempt in range(2):
            try:
                await self.adapter.client.download_media(current,file=str(temporary))
                return None
            except (errors.FileReferenceExpiredError,errors.FilerefUpgradeNeededError):
                if attempt: raise  # Refreshed reference still transiently unavailable.
                fresh=await self.adapter.client.get_messages(peer,ids=message.id)
                original=getattr(message,'document',None) or getattr(message,'photo',None)
                media=getattr(fresh,'document',None) or getattr(fresh,'photo',None)
                if (fresh is None or fresh.id!=message.id or original is None
                        or str(utils.get_peer_id(fresh.peer_id))!=str(peer)
                        or type(media) is not type(original) or media.id!=original.id):
                    return 'retained snapshot removed or changed'
                temporary.unlink(missing_ok=True)
                current=fresh
            except (errors.BadRequestError,errors.ForbiddenError) as exc:
                # A provider-declared unsupported/inaccessible download is an
                # explicit missing attachment, not a forever-blocked peer.
                return type(exc).__name__

    async def forum_topic(self, peer, message):
        """Refresh once, preserving an explicit top over any reply ancestry."""
        from .topic import classify_message
        from .runtime import UserSessionBridge
        try:
            catalog = await self.adapter.topic_ids(peer)
            try:
                return classify_message(message, catalog)
            except ValueError:
                catalog = await self.adapter.topic_ids(peer)
                try:
                    return classify_message(message, catalog)
                except ValueError:
                    header = getattr(message, 'reply_to', None)
                    if (getattr(header, 'reply_to_top_id', None) is not None
                            or getattr(message, 'action', None) is not None):
                        raise
                    reply_id = getattr(header, 'reply_to_msg_id', None)
                    if type(reply_id) is not int or reply_id <= 0:
                        raise
                    resolver = object.__new__(UserSessionBridge)
                    resolver.adapter = self.adapter
                    return await resolver._resolve_forum_target(peer, reply_id, catalog)
        except ValueError as exc:
            raise TopicRoutingUnavailable('forum destination unresolved; raw event retained') from exc

    def consume_outputs(self, body, event):
        if str(event.get('account_id','')) != self.account:
            raise ValueError('retained event belongs to another account')
        candidates = list(body.get('outputs') or [])
        if body.get('output_ref'):
            candidates.append(body)
        for out in candidates:
            if out.get('outcome') not in {'message','deferred'} or not out.get('output_ref') or not out.get('text','').strip():
                continue
            # Output identity, not text or producer work id, deduplicates speech.
            text = out['text']
            # Split UTF-8 safely into independently recorded physical parts.
            chunks, current = [], ''
            for char in text:
                if len((current+char).encode()) > 4096:
                    chunks.append(current); current=''
                current += char
            if current: chunks.append(current)
            for n, chunk in enumerate(chunks):
                args = {'peer':event['conversation_id'], 'text':chunk, 'reply_id':int(event['message']['message_id'])}
                if event['thread_id']: args['topic_id']=int(event['thread_id'])
                self.journal.enqueue(self.account, 'auto:'+digest(out['output_ref'])+':'+str(n), 'send_text', args,
                    origin={'kind':'automatic','source_event_id':event['source_event_id'], 'task_id':str(body.get('task_id') or body.get('turn_ref') or '')})
    async def incoming_once(self):
        blocked = set()
        for row in self.journal.heads(self.account):
            e = json.loads(row['event']); room=e['conversation_key']
            if str(e.get('account_id','')) != self.account:
                self.journal.inbox_update(row['seq'],'held',json.loads(row['refs']))
                continue
            if room in blocked: continue
            refs = json.loads(row['refs'])
            try:
                self.current(e['conversation_id'])
                if not refs:
                    code, body = await asyncio.to_thread(self.host.call,'POST','/presence/turn',{'event':e, 'staged_files':json.loads(row['files']), 'delivery_reporting_version':1, 'continuation_version':1})
                    self.journal.observe_inbox(row['seq'],code,body)
                    if body.get('disposition') == 'retry':
                        after=body.get('retry_after',30)
                        if isinstance(after,str):
                            try: after=max(1,datetime.fromisoformat(after.replace('Z','+00:00')).timestamp()-time.time())
                            except ValueError: after=30
                        delay=after if type(after) in (int,float) and after>0 else 30
                        self.journal.inbox_update(row['seq'],'pending',[],delay)
                        blocked.add(room)
                        continue  # Same immutable POST after the Host's certified reset.
                    if body.get('disposition') == 'retained' and body.get('turn_ref'):
                        refs.append(body['turn_ref'])
                    elif body.get('ok') is not True:
                        if body.get('disposition') in {'blocked','rejected'}:
                            self.journal.inbox_update(row['seq'],'held',refs); continue
                        raise RuntimeError('host intake retry')
                    self.consume_outputs(body,e)
                    for key in ('continuation_ref','work_ref','child_work_ref'):
                        if body.get(key) and body[key] not in refs: refs.append(body[key])
                    if body.get('status') not in {'completed','failed','cancelled'} and body.get('turn_ref') and body['turn_ref'] not in refs:
                        refs.append(body['turn_ref'])
                else:
                    live = []
                    for ref in list(refs):
                        code, body = await asyncio.to_thread(self.host.call,'GET','/presence/work/'+quote(ref,safe=''))
                        self.journal.observe_inbox(row['seq'],code,body)
                        if body.get('ok') is not True:
                            if body.get('disposition') in {'blocked','rejected'}:
                                raise ValueError('host work reference refused; exact response retained')
                            raise RuntimeError('host work unavailable')
                        self.consume_outputs(body,e)
                        if body.get('child_work_ref') and body['child_work_ref'] not in refs+live: live.append(body['child_work_ref'])
                        if body.get('status') not in {'completed','failed','cancelled','interrupted'}: live.append(ref)
                    refs = list(dict.fromkeys(live))
                self.journal.inbox_update(row['seq'],'pending' if refs else 'done',refs,3)
            except (RuntimeError, OSError, TimeoutError):
                self.journal.inbox_update(row['seq'],'pending',refs,30)
            except ValueError:
                self.journal.inbox_update(row['seq'],'held',refs)
            blocked.add(room)
    async def reports_once(self):
        for row in self.journal.pending_reports():
            try:
                code, body = await asyncio.to_thread(self.host.call, 'POST','/presence/delivery',json.loads(row['body']))
                if code==200 and body.get('recorded') is True:
                    self.journal.report_done(row['id'])
                else:
                    self.journal.report_defer(row['id'], 'host_'+str(code), permanent=code in (400,409))
            except (OSError,ValueError,TimeoutError):
                self.journal.report_defer(row['id'],'transport_unavailable')

async def serve(root, host_url, token, adapter_factory, login, verify, health):
    from telethon import events
    cfg = load_config(root)
    while cfg is None:
        await asyncio.sleep(2); cfg=load_config(root)
    host = Host(host_url, token)
    await asyncio.to_thread(host.identity)
    with session_lock(root):
        adapter=adapter_factory(Path(root)/'mtproto.session',cfg.api_id,cfg.api_hash)
        await adapter.connect()
        try:
            while not await login(root,cfg,adapter):
                health(root,'login_required'); await asyncio.sleep(2)
            account = await adapter.get_me()
            cfg=verify(root,cfg,account)
            with Journal(root,recover=True) as journal:
                engine=Participant(root,adapter,host,journal,account)
                engine.current()
                faults=[]
                async def capture(e,kind):
                    try: engine.spool(e,kind)
                    except asyncio.CancelledError: raise
                    except Exception as exc:
                        faults.append(type(exc).__name__)
                        health(root,'ingress_failed')
                async def new(e): await capture(e,'new')
                async def edit(e): await capture(e,'edit')
                adapter.client.add_event_handler(new,events.NewMessage(incoming=True))
                adapter.client.add_event_handler(edit,events.MessageEdited(incoming=True))
                from telethon import errors
                while True:
                    try:
                        remaining=journal.flood_remaining(account)
                        if remaining: await asyncio.sleep(remaining)
                        await adapter.client.catch_up()
                        break
                    except errors.FloodWaitError as exc:
                        journal.flood(account,exc.seconds)
                        health(root,'provider_retry')
                        await asyncio.sleep(max(1,exc.seconds))
                    except (ConnectionError,TimeoutError,errors.RPCError):
                        health(root,'provider_retry');await asyncio.sleep(30)
                intake = None
                hydration = None
                try:
                    while True:
                        if faults: raise RuntimeError('ingress unavailable; reconcile before restart')
                        if intake is not None and intake.done():
                            intake.result()  # Surface unexpected intake/storage failures.
                            intake = None
                        if hydration is not None and hydration.done():
                            hydration.result()
                            hydration = None
                        engine.current()
                        if (Path(root)/'login-request.json').exists() or (Path(root)/'auth-handoff.json').exists():
                            await login(root,cfg,adapter)
                        health(root,'running')
                        if hydration is None:
                            hydration = asyncio.create_task(engine.hydrate_once(), name='telegram-media-hydration')
                        # A Host turn can await a model that needs our outbound tools.
                        # Keep one owned intake worker, never block those tools on it.
                        if intake is None:
                            intake = asyncio.create_task(engine.incoming_once(), name='telegram-host-intake')
                        if asyncio.get_running_loop().time() >= engine.next_send:
                            await engine.dispatch_one()
                        await engine.reports_once()
                        await asyncio.sleep(1)
                finally:
                    workers = [task for task in (intake, hydration) if task is not None]
                    for task in workers: task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
        finally:
            await adapter.disconnect()

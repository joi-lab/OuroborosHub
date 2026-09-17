from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .store import InboxItem

_OUTCOMES = frozenset({"message", "silent", "tool_delivered", "deferred"})

class HostContractError(RuntimeError): pass
class HostBindingTerminalError(HostContractError): pass
class HostAdapterUnavailable(RuntimeError): pass

@dataclass(frozen=True)
class HostTurnStatus:
    state: str
    error: str = ""
    texts: tuple[str, ...] = ()

@dataclass(frozen=True)
class HostDelivery:
    texts: tuple[str, ...] = ()

def normalize_binding_id(value: Any) -> str:
    value = str(value or "").strip()
    if value and not re.fullmatch(r"[0-9a-f]{32}", value):
        raise HostContractError("binding_id must be 32 lowercase hexadecimal characters")
    return value

def email_presence_event(item: InboxItem, *, account_id: str) -> dict[str, Any]:
    return {
        "source_event_id": item.provider_event_key,
        "provider": "email",
        "account_id": str(account_id or "").strip(),
        "conversation_id": item.thread_key,
        "thread_id": item.thread_key,
        "conversation_key": f"email:{item.thread_key}",
        "actor": {"platform": "email", "platform_actor_id": item.sender},
        "conversation": {"platform": "email", "mailbox": item.recipients[0] if item.recipients else "", "thread_id": item.thread_key},
        "message": {"message_id": item.message_id, "in_reply_to": item.in_reply_to, "references": list(item.references), "imap_uid": item.uid, "folder": item.folder, "uidvalidity": item.uidvalidity, "subject": item.subject},
        "text": f"Subject: {item.subject}\n\n{item.body}",
    }

def _ref(kind: str, payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False).encode()
    return f"{kind}:{base64.urlsafe_b64encode(raw).decode().rstrip('=')}"

def _decode(reference: str, kind: str) -> dict[str, Any]:
    if not reference.startswith(kind + ":"): raise HostContractError("Unknown presence reference type")
    encoded = reference.split(":", 1)[1] + "=" * (-len(reference.split(":", 1)[1]) % 4)
    try: value = json.loads(base64.urlsafe_b64decode(encoded).decode())
    except Exception as exc: raise HostContractError("Invalid persisted presence receipt") from exc
    if not isinstance(value, dict): raise HostContractError("Invalid persisted presence receipt")
    return value

def _loopback(url: str) -> bool:
    p = urlsplit(url)
    if p.scheme != "http" or p.username or p.password or not p.hostname: return False
    if p.hostname == "localhost": return True
    try: return ipaddress.ip_address(p.hostname).is_loopback
    except ValueError: return False

class _SkillToken:
    def __init__(self, value: str): self._value = str(value or "")
    def use_in_request(self) -> str:
        if not self._value: raise HostContractError("Presence Host token is unavailable")
        return self._value
    def __repr__(self) -> str: return "<SkillToken:redacted>"


class LoopbackPresenceHostAdapter:
    def __init__(self, binding_id: str, *, host_service_url: str, skill_token: str, http_client: httpx.AsyncClient | None = None):
        self.binding_id = normalize_binding_id(binding_id) if str(binding_id or "").strip() else ""
        self.host_service_url = host_service_url.rstrip("/")
        if not _loopback(self.host_service_url): raise HostContractError("HOST_SERVICE_URL must be an HTTP loopback URL")
        self._token = _SkillToken(skill_token); self.available = bool(self.binding_id and skill_token)
        self._http = http_client or httpx.AsyncClient(timeout=35, trust_env=False); self._owns = http_client is None; self._terminal: dict[str, dict[str, Any]] = {}
    def _headers(self): return {"X-Skill-Token": self._token.use_in_request(), "Content-Type": "application/json"}
    async def _json(self, response):
        if response.status_code in (403,404): raise HostBindingTerminalError(f"Presence binding rejected (HTTP {response.status_code})")
        if response.status_code < 200 or response.status_code >= 300: raise HostContractError(f"Presence Host returned HTTP {response.status_code}")
        try: payload=response.json()
        except ValueError as exc: raise HostContractError("Presence Host returned invalid JSON") from exc
        if not isinstance(payload,dict): raise HostContractError("Presence Host response must be an object")
        return payload
    @staticmethod
    def _outcome(payload):
        outcome=str(payload.get("outcome") or "").lower()
        if outcome not in _OUTCOMES: raise HostContractError(f"Unknown presence outcome: {outcome or '<empty>'}")
        return outcome
    async def submit(self, item: InboxItem) -> str:
        if not self.available: raise HostAdapterUnavailable("Presence binding or Host Service token is not configured")
        response=await self._http.post(f"{self.host_service_url}/presence/turn",headers=self._headers(),json={"binding_id":self.binding_id,"event":email_presence_event(item, account_id=os.environ.get("EMAIL_USER", ""))},timeout=1800)
        payload=await self._json(response)
        if str(payload.get("status") or "") != "completed": raise HostContractError("Presence Host did not complete the turn request")
        outcome=self._outcome(payload)
        data={"status":"deferred" if outcome=="deferred" else "completed","text":str(payload.get("text") or "") if outcome in {"message", "deferred"} else "","turn_ref":str(payload.get("turn_ref") or ""),"work_ref":str(payload.get("work_ref") or "")}
        if outcome=="deferred" and not data["work_ref"]: raise HostContractError("Deferred presence turn omitted work_ref")
        return _ref(outcome, data) if outcome=="deferred" else _ref("completed", data)
    async def _poll(self, work_ref):
        response=await self._http.get(f"{self.host_service_url}/presence/work/{quote(work_ref,safe='')}",headers=self._headers(),params={"binding_id":self.binding_id},timeout=35)
        payload=await self._json(response); state=str(payload.get("status") or "").lower()
        if state=="pending": return payload
        if state not in {"completed","failed","cancelled"}: raise HostContractError(f"Unknown presence work status: {state or '<empty>'}")
        self._outcome(payload); self._terminal[work_ref]=payload; return payload
    async def status(self, reference: str) -> HostTurnStatus:
        if reference.startswith("completed:"): return HostTurnStatus("ready")
        receipt=_decode(reference,"deferred"); work_ref=str(receipt.get("work_ref") or ""); immediate=(str(receipt.get("text") or ""),) if receipt.get("text") else ()
        payload=self._terminal.get(work_ref) or await self._poll(work_ref); state=str(payload.get("status") or "").lower()
        return HostTurnStatus("ready",texts=immediate) if state=="completed" else HostTurnStatus("failed",str(payload.get("error") or state),immediate) if state in {"failed","cancelled"} else HostTurnStatus("pending",texts=immediate)
    async def deliver(self, reference: str) -> HostDelivery:
        if reference.startswith("completed:"): payload=_decode(reference,"completed")
        else:
            work_ref=str(_decode(reference,"deferred").get("work_ref") or ""); payload=self._terminal.get(work_ref) or await self._poll(work_ref)
            if str(payload.get("status") or "").lower()!="completed": raise HostContractError("Presence work is not completed")
        text=str(payload.get("text") or "") if payload.get("outcome", "message") == "message" else ""; return HostDelivery((text,)) if text.strip() else HostDelivery()
    async def aclose(self):
        if self._owns: await self._http.aclose()

def create_host_adapter(binding_id: str, *, http_client=None):
    return LoopbackPresenceHostAdapter(binding_id,host_service_url=os.environ.get("HOST_SERVICE_URL","http://127.0.0.1:8767"),skill_token=os.environ.get("HOST_SERVICE_TOKEN",""),http_client=http_client)

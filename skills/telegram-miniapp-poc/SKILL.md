---
name: telegram-miniapp-poc
description: Automatic owner-only Telegram Mini App gateway for the unchanged Ouroboros SPA, using a temporary Cloudflare Quick Tunnel that terminates at a skill-owned Telegram-auth sidecar.
version: 0.3.0
type: extension
entry: plugin.py
runtime: python3
os: any
permissions: [net, widget, route, subprocess, companion_process]
env_from_settings: [TELEGRAM_BOT_TOKEN]
when_to_use: The owner wants to open and fully control the current Ouroboros web interface inside the existing private Telegram bot without a domain, Cloudflare account, app-store installation, or manually copied URL.
timeout_sec: 30
companion_processes:
  - name: miniapp_gateway
    command: [python3, scripts/companion.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 5
---

# Telegram Mini App PoC

This local data-plane skill exposes the **existing Ouroboros SPA unchanged** in
the private Telegram bot already configured by `telegram-bridge`. It does not
ship a second frontend and makes no change to the Ouroboros repository or core
server.

## Automatic lifecycle

Enabling the skill is the only setup action:

1. Read the positive private owner chat already pinned by `telegram-bridge` and
   verify it through Telegram `getChat(type=private)`.
2. Start a host-supervised loopback auth/reverse-proxy sidecar on a random port.
3. If the text bridge is enabled, transactionally switch its mirror mode to
   `telegram_only` through its own settings route so replies are not duplicated;
   if it is disabled there is no mirror to coordinate. Durable ownership state
   restores the prior mode without overwriting an external change.
4. Download the pinned official `cloudflared` 2026.7.2 asset for the current
   supported host to private skill state if absent, verify its exact reviewed
   size and SHA-256, safely extract archives where applicable, and re-verify the
   cached executable on every start.
5. Start a Cloudflare Quick Tunnel **to the sidecar only**. The local Ouroboros
   port is never supplied to the tunnel process.
6. Verify the public bootstrap through TLS, snapshot the existing private chat
   menu button, and install the temporary Mini App button. The normal system
   resolver is tried first; only a confirmed resolver failure uses Cloudflare
   DNS-over-HTTPS through `1.1.1.1`, then probes the returned IP while preserving
   the original `trycloudflare.com` Host and TLS SNI.
7. While enabled, publish a heartbeat every ten seconds and continuously check
   local core, owner binding, bridge mode, public marker, and Telegram menu
   ownership. Recoverable tunnel/download/network failures retry indefinitely
   with bounded jittered backoff inside the same companion. DNS/observer or
   Telegram transport outages keep the last verified URL; three confirmed bad
   marker responses rotate it.
8. On a normal disable, stop the tunnel and best-effort restore the exact prior
   Telegram button and mirror mode. Crash and URL-rotation state is durable and
   conflict-safe; a value changed elsewhere is never overwritten. A singleton
   lease, server parent lifeline, and POSIX pipe watchdog or Windows Job Object
   also kill cloudflared if the companion dies too hard to run normal cleanup.

No Cloudflare login, DNS zone, public inbound port, Telegram app-store flow, or
manual URL is involved. Cloudflare Quick Tunnels are public development/test
transport with a random `trycloudflare.com` hostname, no SLA, a 200 in-flight
request limit, and no Server-Sent Events support. The current main SPA uses
WebSocket and is supported; a niche extension widget that requires SSE may not
stream through this PoC. Enabling the skill downloads and runs Cloudflare's
official binary and uses Cloudflare's tunnel service under its published terms
and privacy policy.

The current PoC targets native Telegram clients (iOS, Android, and desktop
WebViews). Telegram WebA/WebK may embed the Mini App cross-site and reject its
`SameSite=Strict` owner cookie; those browser clients are intentionally not
claimed until they have a separately tested partitioned-cookie design.

The same payload targets Ouroboros v6.40 and newer on macOS arm64/x86_64,
Linux arm64/x86_64, and Windows x86_64. Windows arm64 has no pinned upstream
asset and fails cleanly before registration. Actual client and packaged-host
coverage is tracked in the manual QA matrix; a code path being supported does
not substitute for running that matrix on each release artifact.

## Authentication boundary

The random URL is not treated as a secret. Before any Ouroboros byte is served,
the sidecar validates raw `Telegram.WebApp.initData` with the bot-token HMAC,
requires a fresh timestamp, and binds the signed Telegram user exactly to the
independently pinned private owner chat. It then issues a short-lived,
process-memory, `Secure; HttpOnly; SameSite=Strict` host-only session.
The bootstrap immediately verifies that the WebView stored the cookie; clients
that reject it show an explicit native-client message instead of reloading in a
loop. Authentication bodies, concurrency, global attempts, and per-client
attempts are bounded; status exports aggregate counters only.

Every SPA path is protected: `/`, `/static/*`, all `/api/*`, file upload and
download, and `/ws`. Exact public Host and Origin are enforced; authorization,
cookies, forwarding and Cloudflare headers are removed before the fixed
loopback upstream. The tunnel can never target Ouroboros directly because core
loopback requests are owner-trusted and the Files surface may expose the local
home directory.

The authenticated Mini App intentionally has the same authority as the local
owner UI, including Main, Projects, Files, Skills, Widgets, Dashboard, Settings,
task control, and chat. Treat the phone and private Telegram account as owner
credentials.

Bot-token rotation is deliberately fail-closed: disable this skill, rotate the
token in the shared Ouroboros setting, then re-enable both Telegram skills as
needed. Disabling destroys process-memory sessions. Do not rotate a token while
the public companion is still running.

## Scope

This remains a beta transport: it pins one reviewed Cloudflare release rather
than following `latest`, has no fallback provider, and Quick Tunnels have a
random URL and no SLA. A future stable named tunnel or private transport can
replace it without changing the SPA or sidecar auth contract.

Ouroboros currently stops companion processes gracefully on POSIX but uses a
hard process stop on Windows. Therefore a Windows disable always stops public
exposure, but cannot guarantee that the companion's final rollback code ran;
the durable ledgers reconcile on the next enable, and an old dead menu button
may remain in the interval. This is the known v6.40+ host-lifecycle limitation
behind the word “best-effort”, not a loss of authentication isolation.

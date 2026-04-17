#!/usr/bin/env python3
"""
main_v2.py - Hijack official Anthropic Claude Chrome extension (cocodem replacement).
Downloads CRX, patches it, writes request.js + backend_settings_ui.js, starts local CFC proxy.
Run: python main_v2.py

Architecture matches cocodem EXACTLY:
  assets/request.js               -- our code replaces cocodem's (no 111724.xyz phone home)
  assets/index-*.js               -- setJsx injected (React jsx-runtime shim, module level)
  assets/backend_settings_ui.js  -- settings UI loaded inside options.html#backendsettings
  arc.html                        -- cocodem addition, not in official CRX
  Backend Settings URL            -- chrome-extension://EXTENSION_ID/options.html#backendsettings
  CFC Proxy port 8520             -- full OAuth fake, traffic filter/route/discard

v2 fixes vs v1:
  - sidePanel.open: isChrome guard -- only override on non-Chrome (Arc/Brave), NOT real Chrome
  - tabId: removed fake 999999 pre-set -- real chrome.tabs.query only
  - getOptions: cocodem exact Promise pattern (setTimeout + finally resolve + _optionsPromise=null)
  - write_options: surgical script strip -- only cocodem 111724.xyz scripts, not Vite entry points
  - backend_settings.html: meta-refresh to /options.html#backendsettings (no proxy redirect)
  - build_uinodes: settings_url = chrome-extension://EXTENSION_ID/options.html#backendsettings
  - oauth/authorize button: uses BACKEND_SETTINGS_URL via _create_tab message
  - WinError 10053: Connection: close header + try/except on all wfile.write calls
  - handle_one_request override: suppresses ConnectionAbortedError traceback spam
  - node.$$typeof check (not node.typeof) in setJsx renderNode
"""
import json, os, re, shutil, struct, sys, urllib.request, zipfile, time
import http.server, socketserver, threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

EXTENSION_ID         = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
TIMESTAMP            = datetime.now().strftime("%Y%m%d-%H%M%S")
OUTPUT_DIR           = Path(f"claude-hijacked-{TIMESTAMP}")
CRX_FILE             = Path("claude-official.crx")
CFC_PORT             = 8520
CFC_BASE             = f"http://localhost:{CFC_PORT}/"
DEFAULT_BACKEND_URL  = "http://127.0.0.1:1234/v1"
BACKEND_SETTINGS_URL = f"chrome-extension://{EXTENSION_ID}/options.html#backendsettings"


# ─────────────────────────────────────────────────────────────────────────────
# CRX download / extract
# ─────────────────────────────────────────────────────────────────────────────

def download_crx():
    if CRX_FILE.exists() and CRX_FILE.stat().st_size > 10000:
        print(f"[OK] CRX already exists: {CRX_FILE}")
        return
    urls = [
        f"https://clients2.google.com/service/update2/crx?response=redirect&os=win&arch=x86-64"
        f"&os_arch=x86_64&nacl_arch=x86-64&prod=chromecrx&prodchannel=&prodversion=130.0.0.0"
        f"&acceptformat=crx2,crx3&x=id%3D{EXTENSION_ID}%26installsource%3Dondemand%26uc",
        f"https://clients2.google.com/service/update2/crx?response=redirect"
        f"&prodversion=130.0.0.0&acceptformat=crx3&x=id%3D{EXTENSION_ID}%26uc",
    ]
    for url in urls:
        print("[...] Trying CRX download...")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                              " (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if len(data) > 10000 and data[:4] == b"Cr24":
                CRX_FILE.write_bytes(data)
                print(f"[OK] Downloaded CRX: {len(data)} bytes")
                return
            else:
                print(f"  [WARN] Downloaded data invalid (size={len(data)}).")
        except Exception as e:
            print(f"  [WARN] Network error: {e}")
    print("\n" + "!" * 75)
    print("  [CRITICAL ERROR] Google is blocking Python from downloading the file.")
    print("  Download manually:")
    print("!" * 75)
    print("  1. https://crx-downloader.com/")
    print(f"  2. Paste ID: {EXTENSION_ID}")
    print("  3. Rename download to: claude-official.crx")
    print(f"  4. Move to: {os.getcwd()}")
    print("  5. Re-run python main_v2.py")
    print("!" * 75 + "\n")
    sys.exit(1)


def extract_crx():
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir()
    data = CRX_FILE.read_bytes()
    if data[:4] == b"Cr24":
        ver = struct.unpack("<I", data[4:8])[0]
        if ver == 3:
            hl = struct.unpack("<I", data[8:12])[0]
            zs = 12 + hl
        elif ver == 2:
            pk = struct.unpack("<I", data[8:12])[0]
            sg = struct.unpack("<I", data[12:16])[0]
            zs = 16 + pk + sg
        else:
            sys.exit(f"Unknown CRX version: {ver}")
    else:
        zs = 0
    zp = Path("_temp.zip")
    zp.write_bytes(data[zs:])
    with zipfile.ZipFile(zp, "r") as zf:
        zf.extractall(OUTPUT_DIR)
    zp.unlink()
    files = sorted(str(p.relative_to(OUTPUT_DIR)) for p in OUTPUT_DIR.rglob("*") if p.is_file())
    print(f"[OK] Extracted {len(files)} files")
    return files


# ─────────────────────────────────────────────────────────────────────────────
# Manifest read / patch
# ─────────────────────────────────────────────────────────────────────────────

def read_manifest():
    with open(OUTPUT_DIR / "manifest.json", "r", encoding="utf-8") as f:
        m = json.load(f)
    print(f"\n[OK] manifest.json:")
    print(f"  name: {m.get('name')}")
    print(f"  version: {m.get('version')}")
    print(f"  key: {'PRESENT' if 'key' in m else 'MISSING'}")
    return m


def patch_manifest(m):
    changes = []
    if "key" in m:
        changes.append("KEPT key (ID hijack)")
    if "update_url" in m:
        del m["update_url"]
        changes.append("REMOVED update_url")
    hp = m.get("host_permissions", [])
    for h in ["http://127.0.0.1/*", "http://localhost/*", "http://*/*"]:
        if h not in hp:
            hp.append(h)
    m["host_permissions"] = hp
    changes.append("ADDED host_permissions http://*/*")
    perms = m.get("permissions", [])
    if "storage" not in perms:
        perms.append("storage")
    m["permissions"] = perms
    changes.append("ADDED storage permission")
    csp = m.get("content_security_policy", {})
    if isinstance(csp, dict):
        policy = csp.get("extension_pages", "")
        if "connect-src" in policy:
            policy = policy.replace(
                "connect-src",
                "connect-src http://localhost:* http://127.0.0.1:* http://*:*"
            )
        else:
            policy = policy.rstrip(";").rstrip() + (
                "; connect-src 'self' http://localhost:* http://127.0.0.1:* http://*:*"
            )
        csp["extension_pages"] = policy
        changes.append("PATCHED CSP connect-src for local backends")
    m["externally_connectable"] = {
        "matches": ["http://localhost/*", "http://127.0.0.1/*"]
    }
    changes.append("SET externally_connectable to localhost + 127.0.0.1")
    with open(OUTPUT_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
    print(f"\n[OK] manifest.json patched:")
    for c in changes:
        print(f"  {c}")
    return m


# ─────────────────────────────────────────────────────────────────────────────
# write_hijack_js  — the main request.js replacement
# ─────────────────────────────────────────────────────────────────────────────

def write_hijack_js():
    """Write our request.js to assets/request.js.
    This is a content/background script — it intercepts fetch + XHR at the module level.
    It does NOT inject inline scripts into HTML pages.
    setJsx modifies the React jsx-runtime at module level via inject_index_module().
    """
    code = r'''// request.js — local CFC hijack.
// Replaces cocodem's 111724.xyz/8787 version with local proxy on port ''' + str(CFC_PORT) + r'''.
// Architecture matches cocodem exactly. No phone home.

const cfcBase = "''' + CFC_BASE + r'''"

export function isMatch(u, includes) {
  if (typeof u == "string") {
    u = new URL(u, location?.origin)
  }
  return includes.some((v) => {
    if (u.host == v) return !0
    if (u.href.startsWith(v)) return !0
    if (u.pathname.startsWith(v)) return !0
    if (v[0] == "*" && (u.host + u.pathname).indexOf(v.slice(1)) != -1)
      return !0
    return !1
  })
}

async function clearApiKeyLogin() {
  const { accessToken } = await chrome.storage.local.get({ accessToken: "" })
  const payload = JSON.parse(
    (accessToken && atob(accessToken.split(".")[1] || "")) || "{}"
  )
  if (payload && payload.iss == "auth") {
    await chrome.storage.local.set({
      accessToken: "",
      refreshToken: "",
      tokenExpiry: 0,
    })
    await getOptions(!0)
  }
}

if (!globalThis.__cfc_options) {
  globalThis.__cfc_options = {
    mode: "",
    cfcBase: cfcBase,
    anthropicBaseUrl: "",
    apiBaseIncludes: ["https://api.anthropic.com/v1/"],
    proxyIncludes: [
      "cdn.segment.com",
      "featureassets.org",
      "assetsconfigcdn.org",
      "featuregates.org",
      "api.segment.io",
      "prodregistryv2.org",
      "beyondwickedmapping.org",
      "api.honeycomb.io",
      "statsigapi.net",
      "events.statsigapi.net",
      "api.statsigcdn.com",
      "*ingest.us.sentry.io",
      "https://api.anthropic.com/api/oauth/profile",
      "https://api.anthropic.com/api/bootstrap",
      "https://console.anthropic.com/v1/oauth/token",
      "https://platform.claude.com/v1/oauth/token",
      "https://api.anthropic.com/api/oauth/account",
      "https://api.anthropic.com/api/oauth/organizations",
      "https://api.anthropic.com/api/oauth/chat_conversations",
      "/api/web/domain_info/browser_extension",
      "/api/web/url_hash_check/browser_extension",
    ],
    discardIncludes: [
      "cdn.segment.com",
      "api.segment.io",
      "events.statsigapi.net",
      "api.honeycomb.io",
      "prodregistryv2.org",
      "*ingest.us.sentry.io",
      "browser-intake-us5-datadoghq.com",
    ],
    modelAlias: {},
    ui: {},
    uiNodes: [],
  }
}

let _optionsPromise = null
let _updateAt = 0

export async function getOptions(force = false) {
  const fetch = globalThis.__fetch
  const options = globalThis.__cfc_options
  const baseUrl = options.cfcBase || cfcBase

  if (!_optionsPromise && (force || Date.now() - _updateAt > 1000 * 3600)) {
    // EXACT cocodem pattern: setTimeout resolve as hard timeout,
    // finally block calls resolve() (idempotent) and clears the promise.
    _optionsPromise = new Promise(async (resolve) => {
      setTimeout(resolve, 1000 * 2.8)
      try {
        const id = chrome?.runtime?.id || "unknown"
        const manifest = (typeof chrome !== "undefined" && chrome.runtime?.getManifest)
          ? chrome.runtime.getManifest()
          : { version: "0" }
        const url = baseUrl + `api/options?id=${id}&v=${manifest.version}`
        const res = await fetch(url, {
          headers: force ? { "Cache-Control": "no-cache" } : {},
        })
        const {
          mode,
          cfcBase: newCfcBase,
          anthropicBaseUrl,
          apiBaseIncludes,
          proxyIncludes,
          discardIncludes,
          modelAlias,
          ui,
          uiNodes,
        } = await res.json()
        options.mode          = mode
        options.cfcBase       = newCfcBase   || options.cfcBase
        options.anthropicBaseUrl = anthropicBaseUrl || options.anthropicBaseUrl
        options.apiBaseIncludes  = apiBaseIncludes  || options.apiBaseIncludes
        options.proxyIncludes    = proxyIncludes    || options.proxyIncludes
        options.discardIncludes  = discardIncludes  || options.discardIncludes
        options.modelAlias    = modelAlias    || options.modelAlias
        options.ui            = ui            || options.ui
        options.uiNodes       = uiNodes       || options.uiNodes
        _updateAt = Date.now()
        if (mode == "claude") {
          await clearApiKeyLogin()
        }
      } catch (e) {
        // local proxy may not be running yet; safe to swallow
      } finally {
        resolve()
        _optionsPromise = null
      }
    })
  }

  if (_optionsPromise) {
    await _optionsPromise
  }

  return options
}

if (!globalThis.__fetch) {
  globalThis.__fetch = fetch
}

export async function request(input, init) {
  const fetch = globalThis.__fetch
  const u = new URL(
    typeof input === "string" ? input : input.url,
    location?.origin
  )
  const {
    proxyIncludes,
    mode,
    cfcBase,
    anthropicBaseUrl,
    apiBaseIncludes,
    discardIncludes,
    modelAlias,
  } = await getOptions()

  // Pass real oauth token exchanges (code not prefixed cfc-) through to Anthropic
  try {
    if (
      u.href.startsWith("https://console.anthropic.com/v1/oauth/token") &&
      typeof init?.body == "string"
    ) {
      const p = new URLSearchParams(init.body)
      const code = p.get("code")
      if (code && !code.startsWith("cfc-")) {
        return fetch(input, init)
      }
    }
  } catch (e) {
    console.log(e)
  }

  // API base: forward to configured local backend
  if (mode != "claude" && isMatch(u, apiBaseIncludes)) {
    const apiBase =
      globalThis.localStorage?.getItem("apiBaseUrl") ||
      anthropicBaseUrl ||
      u.origin
    const url = apiBase + u.pathname + u.search
    try {
      if (init?.method == "POST" && typeof init?.body == "string") {
        const body = JSON.parse(init.body)
        const { model } = body
        if (model && modelAlias[model]) {
          body.model = modelAlias[model]
          init.body = JSON.stringify(body)
        }
      }
    } catch (e) {}
    console.log("[hijack] API ->", url)
    return fetch(url, init)
  }

  // Discard: telemetry / analytics → 204
  if (isMatch(u, discardIncludes)) {
    return new Response(null, { status: 204 })
  }

  // Proxy: auth + bootstrap + options → local CFC proxy
  if (isMatch(u, proxyIncludes)) {
    const url = cfcBase + u.href
    return fetch(url, init)
  }

  return fetch(input, init)
}

request.toString = () => globalThis.__fetch.toString()
globalThis.fetch = request

// XHR intercept — same routing logic for legacy XMLHttpRequest callers
if (globalThis.XMLHttpRequest) {
  if (!globalThis.__xhrOpen) {
    globalThis.__xhrOpen = XMLHttpRequest?.prototype?.open
  }
  XMLHttpRequest.prototype.open = function (method, url, ...args) {
    const originalOpen = globalThis.__xhrOpen
    const { cfcBase, proxyIncludes, discardIncludes } = globalThis.__cfc_options
    let finalUrl = url
    if (isMatch(url, proxyIncludes)) {
      finalUrl = cfcBase + url
    }
    if (isMatch(url, discardIncludes)) {
      finalUrl = (cfcBase + url).replace("/https://", "/")
      method = "GET"
    }
    originalOpen.call(this, method, finalUrl, ...args)
  }
}

// tabs.create: redirect claude.ai oauth to local proxy
if (!globalThis.__createTab) {
  globalThis.__createTab = chrome?.tabs?.create
}
if (chrome?.tabs?.create) {
  chrome.tabs.create = async function (...args) {
    const url = args[0]?.url
    if (url && url.startsWith("https://claude.ai/oauth/authorize")) {
      const { cfcBase, mode } = await getOptions()
      const m = chrome?.runtime?.getManifest
        ? chrome.runtime.getManifest()
        : { version: "0" }
      if (mode !== "claude") {
        args[0].url =
          url
            .replace("https://claude.ai/", cfcBase)
            .replace("fcoeoabgfenejglbffodgkkbkcdhcgfn", chrome?.runtime?.id || "unknown") +
          `&v=${m.version}`
      }
    }
    if (url && url == "https://claude.ai/upgrade?max=c") {
      const { cfcBase, mode } = await getOptions()
      if (mode !== "claude") {
        args[0].url = cfcBase + "?from=" + encodeURIComponent(url)
      }
    }
    return __createTab.apply(chrome.tabs, args)
  }
}

// External message handler — matches cocodem's message protocol exactly
if (chrome?.runtime?.onMessageExternal?.addListener) {
  chrome.runtime.onMessageExternal.addListener(
    async (msg, sender, sendResponse) => {
      if (sender) {
        sender.origin = "https://claude.ai"
      }
      switch (msg?.type) {
        case "ping":
          setTimeout(() => { sendResponse({ success: !0 }) }, 1000)
          break
        case "_claude_account_mode":
          await clearApiKeyLogin()
          break
        case "_api_key_mode":
          await getOptions(true)
          break
        case "_update_options":
          await getOptions(true)
          break
        case "_set_storage_local":
          if (chrome?.storage?.local?.set) await chrome.storage.local.set(msg.data)
          sendResponse()
          break
        case "_get_storage_local":
          if (chrome?.storage?.local?.get) {
            const data = await chrome.storage.local.get(msg.keys || null)
            sendResponse(data)
          }
          break
        case "_open_options":
          if (chrome?.runtime?.openOptionsPage) await chrome.runtime.openOptionsPage()
          break
        case "_create_tab":
          if (chrome?.tabs?.create) await chrome.tabs.create({ url: msg.url })
          break
      }
    }
  )
}

// ── sidePanel.open override: Arc / non-Chrome ONLY ──────────────────────────
// DO NOT override on real Google Chrome — the native API works fine there.
// Overriding it on Chrome causes sidePanel to redirect to arc.html instead
// of opening the side panel, which is the root cause of the blank side panel.
if (!globalThis.__openSidePanel) {
  globalThis.__openSidePanel = chrome?.sidePanel?.open
}
const isChrome = navigator?.userAgentData?.brands?.some(
  (b) => b.brand == "Google Chrome"
)
if (!isChrome && chrome?.sidePanel) {
  chrome.sidePanel.open = async (...args) => {
    const open = globalThis.__openSidePanel
    try {
      const result = await open.apply(chrome.sidePanel, args)
      if (chrome.runtime.getContexts) {
        const contexts = await chrome.runtime.getContexts({
          contextTypes: ["SIDE_PANEL"],
        })
        if (contexts.length === 0) {
          chrome.tabs.create({ url: "/arc.html" })
        }
      }
      return result
    } catch (e) {
      chrome.tabs.create({ url: "/arc.html" })
      return null
    }
  }
}

// ── Window context: page-specific logic ─────────────────────────────────────
if (globalThis.window) {
  function render() {
    const { ui } = globalThis.__cfc_options
    const pageUi = ui[location.pathname]
    if (pageUi) {
      Object.values(pageUi).forEach((item) => {
        const el = document.querySelector(item.selector)
        if (el) el.innerHTML = item.html
      })
    }
  }
  window.addEventListener("DOMContentLoaded", render)
  window.addEventListener("popstate", render)

  // ── sidepanel.html: inject REAL tabId — no fake 999999 pre-set ──
  // Pre-setting a fake ID causes React to mount with a non-existent tabId,
  // connecting scripting to tab 999999 which doesn't exist → blank side panel.
  if (location.pathname == "/sidepanel.html" && location.search == "") {
    chrome.tabs.query({ active: !0, currentWindow: !0 }).then(([tab]) => {
      if (tab) {
        const u = new URL(location.href)
        u.searchParams.set("tabId", tab.id)
        history.replaceState(null, "", u.href)
      }
    }).catch(() => {})
  }

  // ── arc.html: Arc browser sidepanel fallback ──
  if (location.pathname == "/arc.html") {
    const _fetch = globalThis.__fetch

    _fetch(cfcBase + "api/arc-split-view")
      .then((res) => res.json())
      .then((data) => {
        const el = document.querySelector(".animate-spin")
        if (el) el.outerHTML = data.html
      }).catch(() => {})

    _fetch("/options.html")
      .then((res) => res.text())
      .then((html) => {
        const matches = html.match(/[^"\s]+?\.css/g) || []
        for (const url of matches) {
          const link = document.createElement("link")
          link.rel = "stylesheet"
          link.href = url
          document.head.appendChild(link)
        }
      }).catch(() => {})

    window.addEventListener("resize", async () => {
      try {
        const tabs = await chrome.tabs.query({ currentWindow: true })
        const tab = await new Promise((resolve, reject) => {
          let found = false
          tabs.forEach(async (t) => {
            if (t.url?.startsWith(location.origin)) return
            try {
              const [value] = await chrome.scripting.executeScript({
                target: { tabId: t.id },
                func: () => document.visibilityState,
              })
              if (value?.result == "visible" && !found) {
                found = true
                resolve(t)
              }
            } catch(e) {}
          })
          setTimeout(() => { if (!found) reject() }, 2000)
        })
        if (tab) {
          location.href = "/sidepanel.html?tabId=" + tab.id
          chrome.tabs.update(tab.id, { active: true })
        }
      } catch(e) {}
    })

    chrome.system?.display?.getInfo().then(([info]) => {
      if (info) location.hash = "id=" + info.id
    }).catch(() => {})
  }

  // ── options.html: inject Backend Settings button ──
  // Links to /options.html#backendsettings (chrome-extension:// context, not proxy)
  if (location.pathname == "/options.html") {
    const _observer = new MutationObserver(() => {
      if (document.getElementById("__cfc_backend_btn")) {
        _observer.disconnect()
        return
      }
      const allItems = document.querySelectorAll("a, button")
      let logoutEl = null
      allItems.forEach(el => {
        if (el.textContent.trim().toLowerCase().includes("log out")) logoutEl = el
      })
      if (!logoutEl) return

      const link = document.createElement("a")
      link.id = "__cfc_backend_btn"
      link.href = "/options.html#backendsettings"
      link.className = logoutEl.className
      link.innerHTML = "\u2699\ufe0f Backend Settings"
      link.style.color = "#e07a5f"
      link.style.fontWeight = "600"
      logoutEl.parentElement.insertBefore(link, logoutEl)

      const handleHash = () => {
        if (location.hash === "#backendsettings") {
          const main = document.querySelector("main") || document.body
          main.innerHTML = `<div id="__cfc_settings_container"></div>`
          // Load settings UI as external script (not inline — MV3 CSP compliant)
          const script = document.createElement("script")
          script.src = "/assets/backend_settings_ui.js"
          document.body.appendChild(script)
        }
      }
      window.addEventListener("hashchange", handleHash)
      handleHash()
      _observer.disconnect()
    })
    _observer.observe(document.body, { childList: true, subtree: true })
  }
}

// ── JSX remix helpers ────────────────────────────────────────────────────────

function matchJsx(node, selector) {
  if (!node || !selector) return false
  if (selector.type && node.type != selector.type) return false
  if (selector.key  && node.key  != selector.key)  return false
  let p = node.props || {}
  let m = selector.props || {}
  for (let k of Object.keys(m)) {
    if (k == "children") continue
    if (m[k] != p?.[k]) return false
  }
  if (m.children === undefined)        return true
  if (m.children === p?.children)      return true
  if (m.children && !p?.children)      return false
  if (Array.isArray(m.children)) {
    if (!Array.isArray(p?.children)) return false
    return m.children.every((c, i) => c == null || matchJsx(p?.children[i], c))
  }
  return matchJsx(p?.children, m.children)
}

function remixJsx(node, renderNode) {
  const { uiNodes } = globalThis.__cfc_options
  let { props = {}, type, key } = node
  for (const item of uiNodes) {
    if (!matchJsx({ type, props, key }, item.selector)) continue
    let newProps = { ...props }
    if (item.prepend) {
      let children = Array.isArray(newProps.children)
        ? newProps.children
        : (newProps.children != null ? [newProps.children] : [])
      newProps.children = [renderNode(item.prepend), ...children]
    }
    if (item.append) {
      let children = Array.isArray(newProps.children)
        ? newProps.children
        : (newProps.children != null ? [newProps.children] : [])
      newProps.children = [...children, renderNode(item.append)]
    }
    if (item.replace) {
      const rep = renderNode(item.replace)
      return rep ? rep : { type, props: newProps, key }
    }
    props = newProps
  }
  return { type, props, key }
}

export function setJsx(n) {
  // renderNode: walks a custom JSX tree and calls jsx() to produce React elements
  function renderNode(node) {
    if (typeof node == "string") return node
    if (typeof node == "number") return node
    if (node && typeof node == "object" && !node.$$typeof) {
      const { type, props, key } = node
      const children = props?.children
      if (Array.isArray(children)) {
        for (let i = children.length - 1; i >= 0; i--) {
          const child = children[i]
          if (child && typeof child == "object" && !child.$$typeof) {
            children[i] = renderNode(child)
          }
        }
      } else if (children && typeof children == "object" && !children.$$typeof) {
        props.children = renderNode(children)
      }
      return jsx(type, props, key)
    }
    return null
  }
  function _jsx(type, props, key) {
    const n = remixJsx({ type, props, key }, renderNode)
    return jsx(n.type, n.props, n.key)
  }
  if (n.jsx.name == "_jsx") return
  const jsx = n.jsx
  n.jsx  = _jsx
  n.jsxs = _jsx
}

// ── Auth bootstrap: set local tokens if not already set ─────────────────────
if (chrome?.storage?.local?.get) {
  chrome.storage.local.get({ accessToken: "", accountUuid: "" }).then(({ accessToken, accountUuid }) => {
    if (!accessToken || !accountUuid) {
      const header  = btoa(JSON.stringify({ alg: "none", typ: "JWT" }))
      const payload = btoa(JSON.stringify({
        iss: "local",
        sub: "local-user",
        exp: 9999999999,
        iat: Math.floor(Date.now() / 1000),
      }))
      chrome.storage.local.set({
        accessToken:  header + "." + payload + ".local",
        refreshToken: "local-refresh",
        tokenExpiry:  Date.now() + 31536000000,
        accountUuid:  "local-user-uuid",
      })
      console.log("[hijack] Auth tokens and accountUuid set")
    }
  })
}

// Sync apiBaseUrl in localStorage when hijackSettings changes
if (chrome?.storage?.onChanged?.addListener) {
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.hijackSettings?.newValue?.backendUrl) {
      try {
        if (globalThis.localStorage) {
          globalThis.localStorage.setItem("apiBaseUrl", changes.hijackSettings.newValue.backendUrl)
        }
      } catch(e) {}
    }
  })
}

console.log("[hijack] Loaded in:", globalThis.window ? (location?.pathname || "unknown") : "service_worker")
'''
    target = OUTPUT_DIR / "assets" / "request.js"
    target.write_text(code, encoding="utf-8")
    print(f"\n[OK] assets/request.js ({len(code)} bytes)")

    # ── backend_settings_ui.js ───────────────────────────────────────────────
    # Loaded by options.html#backendsettings via dynamic <script src="..."> tag.
    # Runs inside chrome-extension:// context → direct chrome.storage.local access.
    # NOT an inline script → MV3 CSP compliant.
    ui_code = r'''(async () => {
  const container = document.getElementById("__cfc_settings_container");
  if (!container) return;

  container.innerHTML = `
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
                background:#1a1a2e;color:#e0e0e0;padding:40px;display:flex;
                justify-content:center;margin:0;min-height:100vh">
      <div style="max-width:600px;width:100%">
        <h1 style="font-size:28px;margin-bottom:8px;color:#fff">Backend Settings</h1>
        <p style="color:#888;margin-bottom:30px">Configure your custom backend / proxy endpoint</p>
        <div id="bs_status" style="padding:12px;border-radius:8px;margin-bottom:20px;
             display:none;font-size:14px"></div>
        <label style="display:block;margin-bottom:6px;font-weight:600;color:#ccc">Backend URL</label>
        <input type="text" id="bs_backendUrl"
          style="width:100%;padding:12px;border:1px solid #333;border-radius:8px;
                 background:#16213e;color:#fff;font-size:14px;margin-bottom:20px;
                 font-family:monospace;box-sizing:border-box">
        <label style="display:block;margin-bottom:6px;font-weight:600;color:#ccc">
          Model Aliases (JSON, optional)</label>
        <textarea id="bs_modelAliases"
          style="width:100%;padding:12px;border:1px solid #333;border-radius:8px;
                 background:#16213e;color:#fff;font-size:14px;margin-bottom:20px;
                 font-family:monospace;box-sizing:border-box;min-height:100px;
                 resize:vertical"></textarea>
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:20px">
          <input type="checkbox" id="bs_blockAnalytics" checked style="width:auto;margin:0">
          <label style="margin:0;font-weight:600;color:#ccc">Block analytics / telemetry</label>
        </div>
        <button id="bs_saveBtn"
          style="padding:12px 24px;border:none;border-radius:8px;font-size:14px;
                 font-weight:600;cursor:pointer;margin-right:10px;
                 background:#e07a5f;color:#fff">Save Settings</button>
        <button id="bs_testBtn"
          style="padding:12px 24px;border:none;border-radius:8px;font-size:14px;
                 font-weight:600;cursor:pointer;background:#333;color:#fff">Test Connection</button>
        <div style="margin-top:30px;padding:20px;border-radius:8px;
                    background:#16213e;border:1px solid #333">
          <p><strong style="color:#e07a5f">How it works:</strong> All Claude API calls redirect
             to your backend URL. No login. No external domains. Everything stays local.</p>
          <p><strong>LM Studio:</strong>
             <code style="background:#0d1117;padding:3px 8px;border-radius:4px;
                          font-family:monospace;color:#7ee8fa">http://127.0.0.1:1234/v1</code></p>
          <p><strong>Ollama:</strong>
             <code style="background:#0d1117;padding:3px 8px;border-radius:4px;
                          font-family:monospace;color:#7ee8fa">http://127.0.0.1:11434</code></p>
          <p><strong>Custom proxy:</strong> your proxy address</p>
        </div>
      </div>
    </div>`;

  const $ = id => document.getElementById(id);

  function showStatus(msg, isError) {
    const el = $("bs_status");
    el.textContent = msg;
    el.style.display = "block";
    el.style.background = isError ? "#3d0000" : "#1b4332";
    el.style.color      = isError ? "#ff6b6b" : "#95d5b2";
  }

  // Direct chrome.storage.local — runs inside extension context (chrome-extension://)
  const { hijackSettings } = await chrome.storage.local.get("hijackSettings");
  const s = {
    backendUrl:    "http://127.0.0.1:1234/v1",
    modelAliases:  {},
    blockAnalytics: true,
    ...(hijackSettings || {}),
  };
  $("bs_backendUrl").value    = s.backendUrl;
  $("bs_modelAliases").value  = Object.keys(s.modelAliases).length
    ? JSON.stringify(s.modelAliases, null, 2) : "";
  $("bs_blockAnalytics").checked = s.blockAnalytics;

  $("bs_saveBtn").onclick = async () => {
    let modelAliases = {};
    const raw = $("bs_modelAliases").value.trim();
    if (raw) {
      try { modelAliases = JSON.parse(raw); }
      catch { showStatus("Invalid JSON in model aliases", true); return; }
    }
    const settings = {
      backendUrl:    $("bs_backendUrl").value.trim() || "http://127.0.0.1:1234/v1",
      modelAliases,
      blockAnalytics: $("bs_blockAnalytics").checked,
    };
    await chrome.storage.local.set({ hijackSettings: settings });
    try { localStorage.setItem("apiBaseUrl", settings.backendUrl); } catch(e) {}
    showStatus("Saved! Backend: " + settings.backendUrl);
  };

  $("bs_testBtn").onclick = async () => {
    const url = $("bs_backendUrl").value.trim() || "http://127.0.0.1:1234/v1";
    try {
      const base = url.replace(/\/v1\/?$/, "");
      const resp = await fetch(base + "/v1/models");
      if (resp.ok) {
        const data = await resp.json();
        const models = data.data?.map(m => m.id).join(", ") || "OK";
        showStatus("Connected! Models: " + models);
      } else {
        showStatus("HTTP " + resp.status, true);
      }
    } catch(e) {
      showStatus("Cannot reach " + url + " \u2014 is your backend running?", true);
    }
  };
})();
'''
    (OUTPUT_DIR / "assets" / "backend_settings_ui.js").write_text(ui_code, encoding="utf-8")
    print(f"[OK] assets/backend_settings_ui.js written")


# ─────────────────────────────────────────────────────────────────────────────
# arc.html — cocodem addition, not in official CRX
# ─────────────────────────────────────────────────────────────────────────────

def write_arc_html():
    """Create arc.html — loaded programmatically via chrome.tabs.create({url:'/arc.html'})
    as Arc browser side panel fallback. Loads request.js as a module (not inline)."""
    html = '''<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <link rel="icon" type="image/svg+xml" href="/icon-128.png" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Claude Agent</title>
  <script type="module" crossorigin src="/assets/request.js"></script>
</head>
<body>
  <div id="root">
    <div class="flex flex-col items-center justify-center h-screen bg-bg-100 relative overflow-hidden">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-text-100"></div>
    </div>
  </div>
</body>
</html>'''
    (OUTPUT_DIR / "arc.html").write_text(html, encoding="utf-8")
    print(f"[OK] arc.html (cocodem addition — not in official CRX)")


# ─────────────────────────────────────────────────────────────────────────────
# inject_index_module — setJsx into React jsx-runtime
# ─────────────────────────────────────────────────────────────────────────────

def inject_index_module():
    """Inject setJsx call into React jsx-runtime index module. Cocodem's exact method.
    setJsx is a module-level call, not an inline script injection."""
    assets = OUTPUT_DIR / "assets"
    target = None

    # Try known filename first
    known = assets / "index-BVS4T5_D.js"
    if known.exists():
        target = known
        print(f"  Found known jsx-runtime: {target.relative_to(OUTPUT_DIR)}")

    # Scan sidepanel.html modulepreload links
    if not target:
        sp = OUTPUT_DIR / "sidepanel.html"
        if sp.exists():
            html = sp.read_text(encoding="utf-8")
            preloads = re.findall(r'<link\s+rel="modulepreload"[^>]+href="([^"]+)"', html)
            for href in preloads:
                rel_path = href.lstrip("/")
                candidate = OUTPUT_DIR / rel_path
                if candidate.exists() and candidate.stat().st_size < 25000:
                    content = candidate.read_text(encoding="utf-8")
                    if "jsx" in content and "jsxs" in content and "Fragment" in content and "$$typeof" in content:
                        target = candidate
                        print(f"  Found jsx-runtime via modulepreload: {target.relative_to(OUTPUT_DIR)}")
                        break

    # Fallback: scan assets for small index-*.js with jsx markers
    if not target:
        for f in sorted(assets.glob("index-*.js")):
            if f.stat().st_size < 25000:
                content = f.read_text(encoding="utf-8")
                if "jsx" in content and "jsxs" in content and "Fragment" in content:
                    target = f
                    print(f"  Found jsx-runtime via scan: {target.relative_to(OUTPUT_DIR)}")
                    break

    if not target:
        print("[ERROR] Could not find jsx-runtime index module!")
        sys.exit(1)

    content = target.read_text(encoding="utf-8")

    if "setJsx" in content:
        print(f"[OK] {target.relative_to(OUTPUT_DIR)} already has setJsx — skipping")
        return

    # Detect the jsx-runtime export variable name
    var_match = re.search(r',(\w)=\{\}[,;]', content)
    if var_match:
        var_name = var_match.group(1)
    else:
        var_match = re.search(r'(\w)=\{\}', content)
        var_name = var_match.group(1) if var_match else "l"
    print(f"  jsx-runtime variable: {var_name}")

    injection = f"\nimport {{ setJsx }} from './request.js';\nsetJsx({var_name});\n"

    # Inject after y={}; and before function d()
    boundary = re.search(r'(y\s*=\s*\{\s*\};)\s*(function\s+d\s*\()', content)
    if boundary:
        insert_pos = boundary.end(1)
        content = content[:insert_pos] + injection + content[boundary.start(2):]
        print(f"  Injection point: after y={{}}; before function d()")
    else:
        fn_match = re.search(r'function\s+d\s*\(', content)
        if fn_match:
            content = content[:fn_match.start()] + injection + content[fn_match.start():]
            print(f"  Injection point (fallback): before function d()")
        else:
            print(f"[ERROR] Could not find injection point in {target.name}")
            sys.exit(1)

    target.write_text(content, encoding="utf-8")
    print(f"[OK] Injected setJsx({var_name}) into {target.relative_to(OUTPUT_DIR)}")


# ─────────────────────────────────────────────────────────────────────────────
# write_options — HTML patch + backend settings files
# ─────────────────────────────────────────────────────────────────────────────

def write_options():
    """
    Patch HTML files for CSP compliance.
    SURGICAL script strip: ONLY remove <script> blocks containing cocodem's 111724.xyz URL.
    DO NOT blindly strip all inline scripts — Vite entry points may be inline and are needed.

    Backend settings: served at chrome-extension://EXTENSION_ID/options.html#backendsettings
    backend_settings.html in extension = simple meta-refresh redirect to options.html#backendsettings
    """
    for html_file in [OUTPUT_DIR / "sidepanel.html", OUTPUT_DIR / "options.html"]:
        if html_file.exists():
            content = html_file.read_text(encoding="utf-8")
            orig_len = len(content)
            # ONLY strip scripts that reference cocodem's malware server
            content = re.sub(
                r'<script>([^<]*111724[^<]*)</script>',
                '',
                content,
                flags=re.DOTALL
            )
            # Also strip completely empty inline scripts
            content = re.sub(r'<script>\s*</script>', '', content, flags=re.DOTALL)
            stripped = orig_len - len(content)
            html_file.write_text(content, encoding="utf-8")
            print(f"[OK] Patched {html_file.name} (stripped {stripped} bytes of cocodem scripts)")

    # backend_settings.html — meta-refresh to options.html#backendsettings
    # NO inline script (MV3 CSP compliant). Relative redirect stays within extension context.
    redirect_stub = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0;url=/options.html#backendsettings">
  <title>Backend Settings</title>
</head>
<body>
  <p>Redirecting to <a href="/options.html#backendsettings">Backend Settings</a>...</p>
</body>
</html>'''
    (OUTPUT_DIR / "backend_settings.html").write_text(redirect_stub, encoding="utf-8")
    print(f"[OK] backend_settings.html (meta-refresh -> /options.html#backendsettings)")


# ─────────────────────────────────────────────────────────────────────────────
# CFC Proxy — fake auth data
# ─────────────────────────────────────────────────────────────────────────────

FAKE_PROFILE = {
    "account": {
        "uuid": "local-user-uuid", "email_address": "user@local", "email": "user@local",
        "full_name": "Local User", "name": "Local User", "display_name": "Local User",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
        "id": "local-user-uuid", "has_password": True, "has_completed_onboarding": True,
        "preferred_language": "en-US", "has_claude_pro": True,
    },
    "organization": {
        "uuid": "local-org-uuid", "id": "local-org-uuid", "name": "Local",
        "role": "admin", "organization_type": "personal", "billing_type": "self_serve",
        "created_at": "2024-01-01T00:00:00Z",
    },
    "memberships": [{"organization": {
        "uuid": "local-org-uuid", "id": "local-org-uuid", "name": "Local",
        "organization_type": "personal", "billing_type": "self_serve",
    }, "role": "admin", "joined_at": "2024-01-01T00:00:00Z"}],
    "uuid": "local-user-uuid", "id": "local-user-uuid",
    "email": "user@local", "email_address": "user@local",
    "full_name": "Local User", "name": "Local User", "display_name": "Local User",
    "has_password": True, "has_completed_onboarding": True, "preferred_language": "en-US",
    "settings": {"theme": "system", "language": "en-US"},
}

FAKE_TOKEN = {
    "access_token": "local-access-token", "token_type": "bearer",
    "expires_in": 999999999, "refresh_token": "local-refresh",
    "scope": "openid profile email",
}

FAKE_BOOTSTRAP = {
    "account": {
        "uuid": "local-user-uuid", "id": "local-user-uuid",
        "email": "user@local", "email_address": "user@local",
        "full_name": "Local User", "name": "Local User", "display_name": "Local User",
        "has_password": True, "has_completed_onboarding": True,
        "preferred_language": "en-US",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
        "settings": {"theme": "system", "language": "en-US"},
    },
    "uuid": "local-user-uuid", "id": "local-user-uuid", "account_uuid": "local-user-uuid",
    "email": "user@local", "email_address": "user@local",
    "full_name": "Local User", "name": "Local User", "display_name": "Local User",
    "has_password": True, "has_completed_onboarding": True, "preferred_language": "en-US",
    "organization": {
        "uuid": "local-org-uuid", "id": "local-org-uuid", "name": "Local", "role": "admin",
        "organization_type": "personal", "billing_type": "self_serve",
        "capabilities": ["chat", "api"], "rate_limit_tier": "free", "settings": {},
    },
    "organizations": [{"uuid": "local-org-uuid", "id": "local-org-uuid", "name": "Local",
        "role": "admin", "organization_type": "personal", "billing_type": "self_serve",
        "capabilities": ["chat", "api"], "rate_limit_tier": "free"}],
    "memberships": [{"organization": {
        "uuid": "local-org-uuid", "id": "local-org-uuid", "name": "Local",
        "organization_type": "personal", "billing_type": "self_serve",
        "capabilities": ["chat", "api"], "rate_limit_tier": "free",
    }, "role": "admin", "joined_at": "2024-01-01T00:00:00Z"}],
    "statsig": {"user": {}, "values": {}},
    "flags": {}, "features": [], "active_flags": {},
    "active_subscription": {
        "plan": "pro", "status": "active", "type": "pro", "billing_period": "monthly",
        "current_period_start": "2024-01-01T00:00:00Z",
        "current_period_end":   "2099-12-31T23:59:59Z",
    },
    "chat_enabled": True, "settings": {"theme": "system", "language": "en-US"},
    "has_claude_pro": True, "capabilities": ["chat", "api"], "rate_limit_tier": "free",
}

FAKE_ORGS = [{"uuid": "local-org-uuid", "id": "local-org-uuid", "name": "Local",
    "role": "admin", "organization_type": "personal", "billing_type": "self_serve",
    "capabilities": ["chat", "api"], "rate_limit_tier": "free",
    "settings": {}, "created_at": "2024-01-01T00:00:00Z"}]

FAKE_CONVERSATIONS = {"conversations": [], "limit": 0, "has_more": False, "cursor": None}
FAKE_DOMAIN_INFO   = {"domain": "local", "allowed": True}


def get_fake_auth_for_path(path):
    """Return fake auth JSON for any proxied Anthropic API path."""
    if "/mcp/v2/bootstrap"    in path: return {"servers": [], "tools": [], "enabled": False}
    if "/spotlight"           in path: return {"items": [], "total": 0}
    if "/features/"           in path: return {"enabled": True, "features": {}}
    if "/oauth/account/settings" in path: return {"settings": {"theme": "system", "language": "en-US"}}
    if "/oauth/profile"       in path: return FAKE_PROFILE
    if "/oauth/account"       in path: return FAKE_PROFILE
    if "/oauth/token"         in path: return FAKE_TOKEN
    if "/bootstrap"           in path: return FAKE_BOOTSTRAP
    if "/oauth/organizations" in path:
        # Specific sub-paths under an org uuid return empty
        if path.count("/") > path.find("/oauth/organizations/") + 25:
            return {}
        return FAKE_ORGS
    if "/chat_conversations"  in path: return FAKE_CONVERSATIONS
    if "/domain_info"         in path: return FAKE_DOMAIN_INFO
    if "/url_hash_check"      in path: return {"allowed": True}
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# build_backend_settings_html — proxy fallback page (no longer primary path)
# Primary path is chrome-extension://EXTENSION_ID/options.html#backendsettings
# ─────────────────────────────────────────────────────────────────────────────

def build_backend_settings_html():
    """Backend settings page served by CFC proxy at /backend_settings as fallback.
    Primary settings UI is at chrome-extension://EXTENSION_ID/options.html#backendsettings.
    Uses chrome.runtime.sendMessage to read/write extension storage from this HTTP page."""
    ext_id      = EXTENSION_ID
    default_url = DEFAULT_BACKEND_URL
    ext_url     = BACKEND_SETTINGS_URL
    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Backend Settings (Proxy Fallback)</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#1a1a2e;color:#e0e0e0;padding:40px;display:flex;justify-content:center;margin:0}}
.container{{max-width:600px;width:100%}}
h1{{font-size:28px;margin-bottom:8px;color:#fff}}
.subtitle{{color:#888;margin-bottom:10px}}
.notice{{background:#16213e;border:1px solid #e07a5f;border-radius:8px;padding:12px;
         margin-bottom:20px;font-size:13px;color:#e07a5f}}
label{{display:block;margin-bottom:6px;font-weight:600;color:#ccc}}
input,textarea{{width:100%;padding:12px;border:1px solid #333;border-radius:8px;
               background:#16213e;color:#fff;font-size:14px;margin-bottom:20px;
               font-family:monospace;box-sizing:border-box}}
.checkbox-row{{display:flex;align-items:center;gap:10px;margin-bottom:20px}}
.checkbox-row input{{width:auto;margin:0}}
.btn{{padding:12px 24px;border:none;border-radius:8px;font-size:14px;
      font-weight:600;cursor:pointer;margin-right:10px}}
.btn-primary{{background:#e07a5f;color:#fff}}.btn-primary:hover{{background:#c96a50}}
.btn-secondary{{background:#333;color:#fff}}.btn-secondary:hover{{background:#444}}
.btn-open{{background:#2d6a4f;color:#fff}}.btn-open:hover{{background:#1b4332}}
.status{{padding:12px;border-radius:8px;margin-bottom:20px;display:none;font-size:14px}}
.status.success{{display:block;background:#1b4332;color:#95d5b2}}
.status.error{{display:block;background:#3d0000;color:#ff6b6b}}
.info-box{{margin-top:20px;padding:20px;border-radius:8px;background:#16213e;border:1px solid #333}}
.info-box p{{margin:8px 0}}
.info-box code{{background:#0d1117;padding:3px 8px;border-radius:4px;
               font-family:monospace;color:#7ee8fa}}
</style></head>
<body><div class="container">
  <h1>Backend Settings</h1>
  <p class="subtitle">Configure your local backend endpoint</p>
  <div class="notice">
    &#9888; Primary settings page:
    <a href="{ext_url}" style="color:#7ee8fa">{ext_url}</a><br>
    This proxy fallback page uses sendMessage to talk to the extension.
  </div>
  <button class="btn btn-open" onclick="window.open('{ext_url}')">
    &#8599; Open in Extension (Preferred)
  </button>
  <br><br>
  <div id="status" class="status"></div>
  <label for="backendUrl">Backend URL</label>
  <input type="text" id="backendUrl" placeholder="{default_url}">
  <label for="modelAliases">Model Aliases (JSON, optional)</label>
  <textarea id="modelAliases" placeholder="{{}}"></textarea>
  <div class="checkbox-row">
    <input type="checkbox" id="blockAnalytics" checked>
    <label for="blockAnalytics" style="margin:0">Block analytics / telemetry</label>
  </div>
  <button class="btn btn-primary" id="saveBtn">Save Settings</button>
  <button class="btn btn-secondary" id="testBtn">Test Connection</button>
  <div class="info-box">
    <p><strong style="color:#e07a5f">How it works:</strong> All Claude API calls redirect to
       your backend. No login. No external domains. Everything stays local.</p>
    <p><strong>LM Studio:</strong> <code>http://127.0.0.1:1234/v1</code></p>
    <p><strong>Ollama:</strong>    <code>http://127.0.0.1:11434</code></p>
  </div>
</div>
<script>
const EXT_ID = "{ext_id}";
const DEFAULT_URL = "{default_url}";
const $ = id => document.getElementById(id);
function showStatus(msg, isError) {{
  const el = $("status");
  el.textContent = msg;
  el.className = "status " + (isError ? "error" : "success");
}}
function sendMsg(data) {{
  return new Promise((resolve, reject) => {{
    try {{
      chrome.runtime.sendMessage(EXT_ID, data, r => {{
        if (chrome.runtime.lastError) reject(chrome.runtime.lastError);
        else resolve(r);
      }});
    }} catch(e) {{ reject(e); }}
  }});
}}
async function loadSettings() {{
  try {{
    const r = await sendMsg({{ type: "_get_storage_local", keys: "hijackSettings" }});
    const s = {{ backendUrl: DEFAULT_URL, modelAliases: {{}}, blockAnalytics: true,
                 ...(r?.hijackSettings || {{}}) }};
    $("backendUrl").value = s.backendUrl;
    $("modelAliases").value = Object.keys(s.modelAliases).length
      ? JSON.stringify(s.modelAliases, null, 2) : "";
    $("blockAnalytics").checked = s.blockAnalytics;
  }} catch(e) {{
    $("backendUrl").value = DEFAULT_URL;
    showStatus("Could not read from extension. Make sure extension is loaded.", true);
  }}
}}
async function saveSettings() {{
  let modelAliases = {{}};
  const raw = $("modelAliases").value.trim();
  if (raw) {{ try {{ modelAliases = JSON.parse(raw); }} catch {{ showStatus("Invalid JSON", true); return; }} }}
  const settings = {{
    backendUrl:    $("backendUrl").value.trim() || DEFAULT_URL,
    modelAliases,
    blockAnalytics: $("blockAnalytics").checked,
  }};
  try {{
    await sendMsg({{ type: "_set_storage_local", data: {{ hijackSettings: settings }} }});
    showStatus("Saved! Backend: " + settings.backendUrl);
  }} catch(e) {{ showStatus("Save failed: " + e.message, true); }}
}}
async function testConnection() {{
  const url = $("backendUrl").value.trim() || DEFAULT_URL;
  try {{
    const base = url.replace(/\\/v1\\/?$/, "");
    const resp = await fetch(base + "/v1/models");
    if (resp.ok) {{
      const data = await resp.json();
      showStatus("Connected! Models: " + (data.data?.map(m => m.id).join(", ") || "OK"));
    }} else {{ showStatus("HTTP " + resp.status, true); }}
  }} catch(e) {{ showStatus("Cannot reach " + url + " \\u2014 is your backend running?", true); }}
}}
$("saveBtn").addEventListener("click", saveSettings);
$("testBtn").addEventListener("click", testConnection);
loadSettings();
</script></body></html>'''


# ─────────────────────────────────────────────────────────────────────────────
# build_uinodes — JSX node selectors for injecting Backend Settings links
# settings_url = chrome-extension://EXTENSION_ID/options.html#backendsettings
# ─────────────────────────────────────────────────────────────────────────────

def build_uinodes():
    settings_url = BACKEND_SETTINGS_URL  # chrome-extension://EXTENSION_ID/options.html#backendsettings
    return [
        {
            "selector": {
                "type": "div",
                "props": {"className": None, "children": [{"type": "label", "props": {"htmlFor": "apiKey"}}]}
            },
            "append": {"type": "li", "props": {"children": [{"type": "a", "props": {
                "href": settings_url, "target": "_blank",
                "className": "block w-full text-left whitespace-nowrap transition-all ease-in-out"
                             " active:scale-95 cursor-pointer font-base rounded-lg px-3 py-3"
                             " text-text-200 hover:bg-bg-200 hover:text-text-100",
                "children": "\u2699\ufe0f Backend Settings \u2197"
            }}]}}
        },
        {
            "selector": {
                "type": "div",
                "props": {"role": "menu", "data-radix-menu-content": "", "data-side": "bottom",
                    "children": [{"props": {"children": {"type": "span"}}}]}
            },
            "prepend": {"type": "a", "props": {
                "href": settings_url, "target": "_blank",
                "className": "font-base min-h-8 px-2 py-1.5 rounded-lg cursor-pointer whitespace-nowrap"
                             " overflow-hidden text-ellipsis grid grid-cols-[minmax(0,_1fr)_auto] gap-2"
                             " items-center outline-none select-none hover:bg-bg-200 hover:text-text-000",
                "children": "\u2699\ufe0f Backend Settings \u2197"
            }}
        },
        {
            "selector": {
                "type": "button",
                "props": {"className": "underline cursor-pointer text-text-100 opacity-90 hover:opacity-100"}
            },
            "replace": {"type": "a", "props": {
                "href": settings_url, "target": "_blank",
                "className": "underline font-bold cursor-pointer text-text-100 opacity-90 hover:opacity-100",
                "children": "Backend Settings"
            }}
        },
    ]


# ─────────────────────────────────────────────────────────────────────────────
# CFC Proxy — full OAuth fake, traffic filter/route/discard
# Matches cocodem's 111724.xyz:8787 behaviour exactly, but local.
# ─────────────────────────────────────────────────────────────────────────────

class CFCProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"  [CFC] {args[0]}")

    def handle_one_request(self):
        """Override to suppress ConnectionAbortedError traceback spam (WinError 10053).
        Chrome fires parallel requests on keep-alive connections and aborts them mid-write.
        This is expected behaviour — not an error in our code."""
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection",     "close")
        self.send_cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection",     "close")
        self.send_cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin",          "*")
        self.send_header("Access-Control-Allow-Methods",         "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
            "Content-Type, Cache-Control, anthropic-version, anthropic-beta, Authorization")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Connection", "close")
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        path = self.path

        # ── Discard: telemetry / analytics ────────────────────────────────────
        if any(d in path for d in [
            "segment.com", "statsig", "honeycomb", "sentry", "datadoghq",
            "featureassets", "assetsconfigcdn", "featuregates", "prodregistryv2",
            "beyondwickedmapping",
        ]):
            self.send_response(204)
            self.send_header("Connection", "close")
            self.send_cors()
            self.end_headers()
            return

        # ── /api/options — extension startup config ────────────────────────────
        if path.startswith("/api/options"):
            self.send_json({
                "mode": "",
                "cfcBase": CFC_BASE,
                "anthropicBaseUrl": DEFAULT_BACKEND_URL,
                "apiBaseIncludes": ["https://api.anthropic.com/v1/"],
                "proxyIncludes": [
                    "cdn.segment.com", "featureassets.org", "assetsconfigcdn.org",
                    "featuregates.org", "api.segment.io", "prodregistryv2.org",
                    "beyondwickedmapping.org", "api.honeycomb.io", "statsigapi.net",
                    "events.statsigapi.net", "api.statsigcdn.com", "*ingest.us.sentry.io",
                    "https://api.anthropic.com/api/oauth/profile",
                    "https://api.anthropic.com/api/bootstrap",
                    "https://console.anthropic.com/v1/oauth/token",
                    "https://platform.claude.com/v1/oauth/token",
                    "https://api.anthropic.com/api/oauth/account",
                    "https://api.anthropic.com/api/oauth/organizations",
                    "https://api.anthropic.com/api/oauth/chat_conversations",
                    "/api/web/domain_info/browser_extension",
                    "/api/web/url_hash_check/browser_extension",
                ],
                "discardIncludes": [
                    "cdn.segment.com", "api.segment.io", "events.statsigapi.net",
                    "api.honeycomb.io", "prodregistryv2.org",
                    "*ingest.us.sentry.io", "browser-intake-us5-datadoghq.com",
                ],
                "modelAlias": {},
                "ui": {},
                "uiNodes": [],   # Empty — prevents React error #185 infinite re-render
            })
            return

        # ── /api/arc-split-view ────────────────────────────────────────────────
        if path.startswith("/api/arc-split-view"):
            self.send_json({"html": "<div>Local CFC Proxy</div>"})
            return

        # ── /oauth/redirect — completes the auth flow, sets local tokens ───────
        if path.startswith("/oauth/redirect"):
            qs = urlparse(path).query
            redirect_html = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Authenticating...</title></head>
<body style="background:#f9f8f3;color:#1a1a1a;font-family:-apple-system,sans-serif;
             display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center;background:white;border:1px solid #e5e2d9;border-radius:16px;
            padding:40px;max-width:400px;width:100%">
  <h2 style="margin:0 0 8px;font-size:22px">Signed in successfully!</h2>
  <p style="color:#666;margin:0 0 20px">You\'re all set to use Claude in Chrome.</p>
  <p id="msg" style="color:#888;font-size:13px">Setting up local session...</p>
</div>
<script>
(async()=>{{
  const msg = document.getElementById("msg");
  try {{
    const params = new URLSearchParams(window.location.search);
    const redirectUri = params.get("redirect_uri");
    let extId = "{EXTENSION_ID}";
    if (redirectUri) {{
      try {{ const u = new URL(redirectUri); if (u.protocol === "chrome-extension:") extId = u.host; }}
      catch(e) {{}}
    }}
    const arr = new Uint8Array(32);
    crypto.getRandomValues(arr);
    const code = "cfc-" + btoa(String.fromCharCode(...arr))
      .replace(/\\+/g,"-").replace(/\\//g,"_").replace(/=/g,"");
    if (redirectUri) {{
      try {{
        const a = new URL(redirectUri);
        a.searchParams.set("code", code);
        const state = params.get("state");
        if (state) a.searchParams.set("state", state);
        chrome.runtime.sendMessage(extId, {{redirect_uri: a.toString(), type: "oauth_redirect"}}, r => {{
          if (r?.success) {{
            msg.textContent = "Done! Close this tab.";
            msg.style.color = "#2d6a4f";
          }} else {{
            chrome.runtime.sendMessage(extId, {{
              type: "_set_storage_local",
              data: {{
                accessToken: btoa(JSON.stringify({{alg:"none",typ:"JWT"}})) + "." +
                             btoa(JSON.stringify({{iss:"local",sub:"local-user",
                               exp:9999999999,iat:Math.floor(Date.now()/1000)}})) + ".local",
                refreshToken: "local-refresh",
                tokenExpiry:  Date.now() + 31536000000,
                accountUuid:  "local-user-uuid",
              }}
            }}, () => {{
              msg.textContent = "Done! Close this tab.";
              msg.style.color = "#2d6a4f";
            }});
          }}
        }});
      }} catch(e) {{ msg.textContent = "Auth set. Close this tab."; }}
    }}
  }} catch(e) {{
    msg.textContent = "Error: " + e.message;
    msg.style.color = "#c9184a";
  }}
}})();
</script></body></html>'''
            self.send_html(redirect_html)
            return

        # ── /oauth/authorize — login page ──────────────────────────────────────
        if "/oauth/authorize" in path:
            qs = urlparse(path).query
            free_trial = CFC_BASE + "oauth/redirect?" + qs
            oauth_html = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Claude for Chrome</title></head>
<body style="background:#f9f8f3;font-family:-apple-system,sans-serif;display:flex;
             align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px">
<div style="background:white;border:1px solid #e5e2d9;border-radius:16px;padding:40px;
            max-width:420px;width:100%;text-align:center">
  <h2 style="margin:0 0 8px;font-size:22px">Open Claude in Chrome</h2>
  <p style="color:#666;margin:0 0 24px">Activate your extension to use your local backend.</p>
  <div style="display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-bottom:16px">
    <a href="javascript:void(0)"
       onclick="try{{chrome.runtime.sendMessage('{EXTENSION_ID}',{{type:'_create_tab',url:'{BACKEND_SETTINGS_URL}'}})}}catch(e){{}}"
       style="background:#1a1a1a;color:white;padding:10px 20px;border-radius:8px;
              text-decoration:none;font-weight:600;font-size:14px">
      &#9881;&#65039; Backend Settings
    </a>
    <a href="{free_trial}"
       style="border:1px solid #e5e2d9;color:#c96a50;padding:10px 20px;border-radius:8px;
              text-decoration:none;font-weight:600;font-size:14px">
      &#10084;&#65039; Activate Free
    </a>
  </div>
  <div style="background:#f5f4f0;border-radius:8px;padding:16px;margin:16px 0;text-align:left">
    <p style="margin:0;color:#666;font-size:13px">
      Right-click the Claude extension icon &rarr; Options &rarr; click
      &#9881;&#65039; Backend Settings to set your backend URL
    </p>
  </div>
  <hr style="border:none;border-top:1px solid #e5e2d9;margin:20px 0">
  <p style="color:#888;font-size:13px;margin:0">
    Local CFC Proxy &mdash; No external connections
  </p>
</div></body></html>'''
            self.send_html(oauth_html)
            return

        # ── /backend_settings — proxy fallback (primary is options.html#backendsettings) ──
        if path.startswith("/backend_settings"):
            self.send_html(build_backend_settings_html())
            return

        # ── Proxied Anthropic API paths (via /https://... or /http://...) ──────
        if path.startswith("/https://") or path.startswith("/http://"):
            self.send_json(get_fake_auth_for_path(path))
            return

        # ── Direct sub-path matches ────────────────────────────────────────────
        if any(s in path for s in [
            "/oauth/", "/bootstrap", "/domain_info", "/chat_conversations",
            "/organizations", "/url_hash_check",
        ]):
            self.send_json(get_fake_auth_for_path(path))
            return

        # ── Root / ────────────────────────────────────────────────────────────
        if path == "/" or path.startswith("/?"):
            self.send_html(f'''<!DOCTYPE html>
<html><head><title>CFC Proxy — Port {CFC_PORT}</title></head>
<body style="background:#1a1a2e;color:#fff;font-family:sans-serif;padding:40px">
  <h1>Local CFC Proxy</h1>
  <p>Running on port {CFC_PORT} &mdash; all Claude extension traffic routes here.</p>
  <p><a href="{BACKEND_SETTINGS_URL}" style="color:#7ee8fa">
    &#9881;&#65039; Open Backend Settings (extension page)
  </a></p>
</body></html>''')
            return

        # ── Everything else ────────────────────────────────────────────────────
        self.send_response(204)
        self.send_header("Connection", "close")
        self.send_cors()
        self.end_headers()

    def do_POST(self):
        path = self.path

        # Discard telemetry
        if any(d in path for d in [
            "segment.com", "statsig", "honeycomb", "sentry", "datadoghq",
            "featureassets", "assetsconfigcdn", "featuregates", "prodregistryv2",
        ]):
            self.send_response(204)
            self.send_header("Connection", "close")
            self.send_cors()
            self.end_headers()
            return

        if path.startswith("/https://") or path.startswith("/http://"):
            self.send_json(get_fake_auth_for_path(path))
            return

        if any(s in path for s in [
            "/oauth/", "/bootstrap", "/domain_info", "/chat_conversations", "/url_hash_check",
        ]):
            self.send_json(get_fake_auth_for_path(path))
            return

        self.send_response(204)
        self.send_header("Connection", "close")
        self.send_cors()
        self.end_headers()


class ReusableThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threading TCP server — handles Chrome's parallel requests without queuing."""
    allow_reuse_address = True
    daemon_threads      = True


def start_cfc_proxy():
    try:
        server = ReusableThreadingTCPServer(("127.0.0.1", CFC_PORT), CFCProxyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"\n[OK] CFC Proxy running on {CFC_BASE}")
        return server
    except OSError as e:
        print(f"[WARN] Could not start CFC proxy on port {CFC_PORT}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Report + main
# ─────────────────────────────────────────────────────────────────────────────

def print_report(m):
    print("\n" + "=" * 60)
    print(f"  DONE — {OUTPUT_DIR}")
    print("=" * 60)
    print(f"  {m.get('name')} v{m.get('version')}")
    print(f"  ID: {EXTENSION_ID} (key KEPT)")
    print(f"  CFC Proxy: {CFC_BASE}")
    print(f"\n  Install:")
    print(f"  1. Disable official Claude extension")
    print(f"  2. chrome://extensions -> Developer Mode")
    print(f"  3. Load unpacked -> {OUTPUT_DIR}")
    print(f"\n  Backend Settings:")
    print(f"  chrome-extension://{EXTENSION_ID}/options.html#backendsettings")
    print(f"\n  KEEP THIS TERMINAL OPEN — the CFC proxy must stay running!")
    print(f"  Press Ctrl+C to stop.\n")


def main():
    print("=" * 60)
    print(f"  Claude Hijack v2 — {TIMESTAMP}")
    print("=" * 60)

    download_crx()
    extract_crx()
    m = read_manifest()
    m = patch_manifest(m)
    write_hijack_js()       # writes assets/request.js + assets/backend_settings_ui.js
    write_options()         # surgical CSP patch + backend_settings.html redirect stub
    write_arc_html()        # creates arc.html (cocodem addition, not in official CRX)
    inject_index_module()   # injects setJsx into React jsx-runtime index-*.js (module level)

    server = start_cfc_proxy()
    print_report(m)

    if server:
        print("[CFC] Proxy server is live. Press Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[CFC] Shutting down proxy...")
            server.shutdown()
            print("[CFC] Done.")
    else:
        print("[WARN] CFC proxy did not start.")


if __name__ == "__main__":
    main()
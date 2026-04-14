#!/usr/bin/env python3
"""
main.py - Hijack official Anthropic Claude Chrome extension.
Downloads CRX, patches it, writes hijack.js + backend settings, starts local CFC proxy.
Run: python main.py
"""
import json, os, re, shutil, struct, sys, urllib.request, zipfile, time
import http.server, socketserver, threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
OUTPUT_DIR = Path(f"claude-hijacked-{TIMESTAMP}")
CRX_FILE = Path("claude-official.crx")
CFC_PORT = 8520
CFC_BASE = f"http://127.0.0.1:{CFC_PORT}/"
DEFAULT_BACKEND_URL = "http://127.0.0.1:1234/v1"

def download_crx():
    if CRX_FILE.exists() and CRX_FILE.stat().st_size > 10000:
        print(f"[OK] CRX already exists: {CRX_FILE}")
        return

    # Google's strict URL format for extension downloads
    urls = [
        f"https://clients2.google.com/service/update2/crx?response=redirect&os=win&arch=x86-64&os_arch=x86_64&nacl_arch=x86-64&prod=chromecrx&prodchannel=&prodversion=130.0.0.0&acceptformat=crx2,crx3&x=id%3D{EXTENSION_ID}%26installsource%3Dondemand%26uc",
        f"https://clients2.google.com/service/update2/crx?response=redirect&prodversion=130.0.0.0&acceptformat=crx3&x=id%3D{EXTENSION_ID}%26uc"
    ]

    for url in urls:
        print(f"[...] Trying CRX download...")
        try:
            # Spoof exact Chrome browser headers to bypass bot protection
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1"
            })
            
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                # Ensure it's not a tiny XML error file AND that it starts with the valid 'Cr24' extension header
                if len(data) > 10000 and data[:4] == b"Cr24":
                    CRX_FILE.write_bytes(data)
                    print(f"[OK] Downloaded CRX: {len(data)} bytes")
                    return
                else:
                    print(f"  [WARN] Downloaded data invalid (size={len(data)}). Google blocked it.")
        except Exception as e:
            print(f"  [WARN] Network error: {e}")

    # If Python is still permanently blocked, print explicit manual instructions
    print("\n" + "!" * 75)
    print("  [CRITICAL ERROR] Google is blocking Python from downloading the file.")
    print("  Because you don't have the old file, you MUST do this manually:")
    print("!" * 75)
    print("  1. Open your web browser and go to:")
    print("     https://crx-downloader.com/")
    print(f"  2. Paste this ID into the box: {EXTENSION_ID}")
    print("  3. Click 'Download' and save the file.")
    print("  4. Rename the downloaded file to exactly: claude-official.crx")
    print(f"  5. Move that file into this exact folder:\n     {os.getcwd()}")
    print("  6. Run this script again: python main.py")
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
    print(f"[OK] Extracted {len(files)} files:")
    for f in files:
        print(f"  {f}")
    return files

def read_manifest():
    with open(OUTPUT_DIR / "manifest.json", "r", encoding="utf-8") as f:
        m = json.load(f)
    print(f"\n[OK] manifest.json:")
    print(f"  name: {m.get('name')}")
    print(f"  version: {m.get('version')}")
    print(f"  key: {'PRESENT' if 'key' in m else 'MISSING'}")
    sw = m.get("background", {}).get("service_worker", "")
    print(f"  service_worker: {sw}")
    print(f"  permissions: {m.get('permissions', [])}")
    print(f"  host_permissions: {m.get('host_permissions', [])}")
    csp = m.get("content_security_policy", {}).get("extension_pages", "")
    print(f"  CSP: {csp[:80]}...")
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
        "matches": ["http://127.0.0.1/*", "http://localhost/*"]
    }
    changes.append("ADDED externally_connectable")
    with open(OUTPUT_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
    print(f"\n[OK] manifest.json patched:")
    for c in changes:
        print(f"  {c}")
    return m

def write_hijack_js():
    code = r'''// hijack.js — cocodem request.js verbatim, cfcBase = local proxy.
// Matches cocodem/claude-for-chrome guide/request.js exactly.

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
    _optionsPromise = new Promise((resolve) => {
      let resolved = false
      const finish = () => {
        if (resolved) return
        resolved = true
        resolve()
        _optionsPromise = null
      }
      setTimeout(finish, 2800)
      ;(async () => {
        try {
          const id = chrome?.runtime?.id || "unknown"
          const manifest = (typeof chrome !== "undefined" && chrome.runtime && typeof chrome.runtime.getManifest === "function")
            ? chrome.runtime.getManifest() : { version: "0" }
          const url = baseUrl + `api/options?id=${id}&v=${manifest.version}`
          const res = await fetch(url, {
            headers: force ? { "Cache-Control": "no-cache" } : {},
          })
          const {
            mode, cfcBase, anthropicBaseUrl, apiBaseIncludes,
            proxyIncludes, discardIncludes, modelAlias, ui, uiNodes,
          } = await res.json()
          options.mode = mode
          options.cfcBase = cfcBase || options.cfcBase
          options.anthropicBaseUrl = anthropicBaseUrl || options.anthropicBaseUrl
          options.apiBaseIncludes = apiBaseIncludes || options.apiBaseIncludes
          options.proxyIncludes = proxyIncludes || options.proxyIncludes
          options.discardIncludes = discardIncludes || options.discardIncludes
          options.modelAlias = modelAlias || options.modelAlias
          options.ui = ui || options.ui
          options.uiNodes = uiNodes || options.uiNodes
          _updateAt = Date.now()
          if (mode == "claude") {
            await clearApiKeyLogin()
          }
        } catch (e) {
        } finally {
          finish()
        }
      })()
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
    proxyIncludes, mode, cfcBase, anthropicBaseUrl,
    apiBaseIncludes, discardIncludes, modelAlias,
  } = await getOptions()

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

  if (isMatch(u, discardIncludes)) {
    const url = (cfcBase + u.href).replace("/https://", "/")
    return new Response(null, { status: 204 })
  }

  if (isMatch(u, proxyIncludes)) {
    const url = cfcBase + u.href
    return fetch(url, init)
  }

  return fetch(input, init)
}

request.toString = () => globalThis.__fetch.toString()

globalThis.fetch = request

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

if (!globalThis.__createTab) {
  globalThis.__createTab = chrome?.tabs?.create
}
if (chrome?.tabs?.create) {
  chrome.tabs.create = async function (...args) {
    const url = args[0]?.url
    if (url && url.startsWith("https://claude.ai/oauth/authorize")) {
      const { cfcBase, mode } = await getOptions()
      const m = (typeof chrome !== "undefined" && chrome.runtime && typeof chrome.runtime.getManifest === "function")
        ? chrome.runtime.getManifest() : { version: "0" }
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

if (chrome?.sidePanel?.open) {
  if (!globalThis.__openSidePanel) { globalThis.__openSidePanel = chrome.sidePanel.open }
  chrome.sidePanel.open = async (...args) => {
    const open = globalThis.__openSidePanel
    try {
      const result = await open.apply(chrome.sidePanel, args)
      if (chrome.runtime.getContexts) {
        const contexts = await chrome.runtime.getContexts({ contextTypes: ["SIDE_PANEL"] })
        if (contexts.length === 0) { chrome.tabs.create({ url: "/arc.html" }) }
      }
      return result
    } catch(e) {
      chrome.tabs.create({ url: "/arc.html" })
      return null
    }
  }
}

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

  if (location.pathname == "/sidepanel.html" && location.search == "") {
    const u = new URL(location.href)
    u.searchParams.set("tabId", "999999")
    history.replaceState(null, "", u.href)
    if (chrome?.tabs?.query) {
      chrome.tabs.query({ active: !0, currentWindow: !0 }).then(([tab]) => {
        if (tab) {
          const u2 = new URL(location.href)
          u2.searchParams.set("tabId", tab.id)
          history.replaceState(null, "", u2.href)
        }
      }).catch(() => {})
    }
  }
  if (location.pathname == "/options.html") {}
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
  }
}

if (!globalThis.__openSidePanel) {
  globalThis.__openSidePanel = chrome?.sidePanel?.open
}

function matchJsx(node, selector) {
  if (!node || !selector) return false
  if (selector.type && node.type != selector.type) return false
  if (selector.key && node.key != selector.key) return false
  let p = node.props || {}
  let m = selector.props || {}
  for (let k of Object.keys(m)) {
    if (k == "children") continue
    if (m[k] != p?.[k]) return false
  }
  if (m.children === undefined) return true
  if (m.children === p?.children) return true
  if (m.children && !p?.children) return false
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
      let children = Array.isArray(newProps.children) ? newProps.children : (newProps.children != null ? [newProps.children] : [])
      newProps.children = [renderNode(item.prepend), ...children]
    }
    if (item.append) {
      let children = Array.isArray(newProps.children) ? newProps.children : (newProps.children != null ? [newProps.children] : [])
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
  const t = (l) => l
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
  n.jsx = _jsx
  n.jsxs = _jsx
}

function patchLocales(module, localesVar, localMapVar) {
  if (!globalThis.window) return
  import(module).then((m) => {
    const locales = m[localesVar]
    const localMap = m[localMapVar]
    const more = {
      "ru-RU": "\u0420\u0443\u0441\u0441\u043a\u0438\u0439",
      "zh-CN": "\u7b80\u4f53\u4e2d\u6587",
      "zh-TW": "\u7e41\u9ad4\u4e2d\u6587",
    }
    if (locales && Array.isArray(locales) && locales[0] == "en-US" && localMap) {
      Object.keys(more).forEach((k) => { locales.push(k); localMap[k] = more[k] })
    }
  }).catch(() => {})
}

if (typeof chrome !== "undefined" && chrome.runtime && typeof chrome.runtime.getManifest === "function") {
  const manifest = chrome.runtime.getManifest()
  const { version } = manifest
  if (version.startsWith("1.0.36")) patchLocales("./Main-iyJ1wi9k.js", "H", "J")
  if (version.startsWith("1.0.39")) patchLocales("./Main-tYwvm-WT.js", "a6", "a7")
  if (version.startsWith("1.0.41")) patchLocales("./Main-BlBvQSg-.js", "a7", "a8")
  if (version.startsWith("1.0.47")) patchLocales("./index-D2rCaB8O.js", "A", "L")
  if (version.startsWith("1.0.55")) patchLocales("./index-C56daOBQ.js", "A", "L")
  if (version.startsWith("1.0.56")) patchLocales("./index-DiHrZgA3.js", "A", "L")
  if (version.startsWith("1.0.66")) patchLocales("./index-5uYI7rOK.js", "A", "L")
}

const DEFAULT_SETTINGS = {
    backendUrl: "http://127.0.0.1:1234/v1",
    modelAliases: {},
    blockAnalytics: true,
};
let __hijackSettings = { ...DEFAULT_SETTINGS };

if (chrome?.storage?.local?.get) {
  chrome.storage.local.get({ accessToken: "", accountUuid: "" }).then(({ accessToken, accountUuid }) => {
    if (!accessToken || !accountUuid) {
      const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
      const payload = btoa(JSON.stringify({
        iss: "local",
        sub: "local-user",
        exp: 9999999999,
        iat: Math.floor(Date.now() / 1000),
      }));
      chrome.storage.local.set({
        accessToken: header + "." + payload + ".local",
        refreshToken: "local-refresh",
        tokenExpiry: Date.now() + 31536000000,
        accountUuid: "local-user-uuid"
      });
      console.log("[hijack] Auth tokens and accountUuid set");
    }
  });
}

if (chrome?.storage?.onChanged?.addListener) {
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.hijackSettings) {
      __hijackSettings = { ...DEFAULT_SETTINGS, ...changes.hijackSettings.newValue };
      try {
        if (globalThis.localStorage) {
          globalThis.localStorage.setItem("apiBaseUrl", __hijackSettings.backendUrl);
        }
      } catch(e) {}
    }
  });
}

console.log("[hijack] Loaded in:", globalThis.window ? (location?.pathname || "unknown") : "service_worker")
'''
    (OUTPUT_DIR / "hijack.js").write_text(code, encoding="utf-8")
    print(f"\n[OK] hijack.js ({len(code)} bytes)")


def write_options():
    # Changed files from options.html to backend_settings.html so it doesn't overwrite original options
    html = '''<!DOCTYPE html>
<html> <head> <meta charset="utf-8"> <title>Backend Settings</title> <link rel="stylesheet" href="backend_settings.css"> </head> <body> <div class="container">
<h1>Backend Settings</h1>
<p class="subtitle">Configure your custom backend / proxy endpoint</p>
<div id="status" class="status"></div>
<label for="backendUrl">Backend URL</label>
<input type="text" id="backendUrl" placeholder="http://127.0.0.1:1234/v1">
<label for="modelAliases">Model Aliases (JSON, optional)</label>
<textarea id="modelAliases" placeholder="{}"></textarea>
<div class="checkbox-row">
  <input type="checkbox" id="blockAnalytics" checked>
  <label for="blockAnalytics" style="margin:0">Block analytics / telemetry</label>
</div>
<button class="btn btn-save" id="saveBtn">Save Settings</button>
<button class="btn btn-test" id="testBtn">Test Connection</button>
<div class="info-box">
  <p><strong style="color:#e07a5f">How it works:</strong> All Claude API calls redirect to your backend URL. No login. No external domains. Everything stays local.</p>
  <p><strong>LM Studio:</strong> <code>http://127.0.0.1:1234/v1</code></p>
  <p><strong>Ollama:</strong> <code>http://127.0.0.1:11434</code></p>
  <p><strong>Custom proxy:</strong> your proxy address</p>
</div>
</div> <script src="backend_settings.js"></script> </body> </html>'''

    css = '''body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #1a1a2e; color: #e0e0e0; padding: 40px;
  display: flex; justify-content: center;
}
.container { max-width: 600px; width: 100%; }
h1 { font-size: 28px; margin-bottom: 8px; color: #fff; }
.subtitle { color: #888; margin-bottom: 30px; }
label { display: block; margin-bottom: 6px; font-weight: 600; color: #ccc; }
input, textarea {
  width: 100%; padding: 12px; border: 1px solid #333;
  border-radius: 8px; background: #16213e; color: #fff;
  font-size: 14px; margin-bottom: 20px; font-family: monospace;
}
input:focus, textarea:focus { outline: none; border-color: #e07a5f; }
textarea { min-height: 100px; resize: vertical; }
.checkbox-row { display: flex; align-items: center; gap: 10px; margin-bottom: 20px; }
.checkbox-row input { width: auto; margin: 0; }
.btn {
  padding: 12px 24px; border: none; border-radius: 8px;
  font-size: 14px; font-weight: 600; cursor: pointer; margin-right: 10px;
}
.btn-save { background: #e07a5f; color: #fff; }
.btn-save:hover { background: #c96a50; }
.btn-test { background: #333; color: #fff; }
.btn-test:hover { background: #444; }
.status {
  padding: 12px; border-radius: 8px; margin-bottom: 20px;
  display: none; font-size: 14px;
}
.status.success { display: block; background: #1b4332; color: #95d5b2; }
.status.error { display: block; background: #3d0000; color: #ff6b6b; }
.info-box {
  margin-top: 30px; padding: 20px; border-radius: 8px;
  background: #16213e; border: 1px solid #333;
}
.info-box p { margin: 8px 0; }
.info-box code {
  background: #0d1117; padding: 3px 8px; border-radius: 4px;
  font-family: monospace; color: #7ee8fa;
}'''

    js = r'''const DEFAULT = {
  backendUrl: "http://127.0.0.1:1234/v1",
  modelAliases: {},
  blockAnalytics: true,
};
const $ = (id) => document.getElementById(id);
async function loadSettings() {
  const { hijackSettings } = await chrome.storage.local.get("hijackSettings");
  const s = { ...DEFAULT, ...hijackSettings };
  $("backendUrl").value = s.backendUrl;
  $("modelAliases").value = Object.keys(s.modelAliases).length
    ? JSON.stringify(s.modelAliases, null, 2) : "";
  $("blockAnalytics").checked = s.blockAnalytics;
}
async function saveSettings() {
  let modelAliases = {};
  const raw = $("modelAliases").value.trim();
  if (raw) {
    try { modelAliases = JSON.parse(raw); }
    catch { showStatus("Invalid JSON in model aliases", true); return; }
  }
  const settings = {
    backendUrl: $("backendUrl").value.trim() || DEFAULT.backendUrl,
    modelAliases,
    blockAnalytics: $("blockAnalytics").checked,
  };
  await chrome.storage.local.set({ hijackSettings: settings });
  try { localStorage.setItem("apiBaseUrl", settings.backendUrl); } catch(e) {}
  showStatus("Settings saved! Backend: " + settings.backendUrl);
}
async function testConnection() {
  const url = $("backendUrl").value.trim() || DEFAULT.backendUrl;
  try {
    const base = url.replace(/\/v1\/?$/, "");
    const resp = await fetch(base + "/v1/models");
    if (resp.ok) {
      const data = await resp.json();
      const models = data.data?.map(m => m.id).join(", ") || "Connected OK";
      showStatus("Connected! Models: " + models);
    } else {
      showStatus("HTTP " + resp.status, true);
    }
  } catch (e) {
    showStatus("Cannot reach " + url + " \u2014 is your backend running?", true);
  }
}
function showStatus(msg, isError) {
  const el = $("status");
  el.textContent = msg;
  el.className = "status " + (isError ? "error" : "success");
}
$("saveBtn").addEventListener("click", saveSettings);
$("testBtn").addEventListener("click", testConnection);
loadSettings();
'''
    (OUTPUT_DIR / "backend_settings.html").write_text(html, encoding="utf-8")
    (OUTPUT_DIR / "backend_settings.css").write_text(css, encoding="utf-8")
    (OUTPUT_DIR / "backend_settings.js").write_text(js, encoding="utf-8")
    print(f"\n[OK] backend_settings.html + css + js")


def inject_service_worker(m):
    sw = m.get("background", {}).get("service_worker", "")
    sw_type = m.get("background", {}).get("type", "")
    if not sw:
        sys.exit("[ERROR] No service_worker in manifest")
    p = OUTPUT_DIR / sw
    if not p.exists():
        sys.exit(f"[ERROR] {p} not found")
    content = p.read_text(encoding="utf-8")
    line = 'import "./hijack.js";\n' if sw_type == "module" else 'importScripts("hijack.js");\n'
    if "hijack.js" not in content:
        p.write_text(line + content, encoding="utf-8")
        print(f"[OK] Injected into {sw} (type={sw_type})")


def inject_html_pages():
    skip = {"offscreen.html"}
    injected = []
    for f in OUTPUT_DIR.rglob("*.html"):
        if f.name in skip:
            print(f"  SKIP {f.relative_to(OUTPUT_DIR)} (offscreen — restricted context)")
            continue
        content = f.read_text(encoding="utf-8")
        if "hijack.js" in content:
            continue
        rel = os.path.relpath(OUTPUT_DIR / "hijack.js", f.parent).replace("\\", "/")
        tag = f'<script src="{rel}" type="module"></script>'
        content = re.sub(
            r'<meta\s+http-equiv=["\']Content-Security-Policy["\'][^>]*>',
            '', content, flags=re.IGNORECASE)
        # CRITICAL FIX: Inject at the TOP of <head> so it executes BEFORE React boots!
        if re.search(r'<head[^>]*>', content, flags=re.IGNORECASE):
            content = re.sub(r'(<head[^>]*>)', r'\1\n' + tag, content, count=1, flags=re.IGNORECASE)
        elif "</head>" in content:
            content = content.replace("</head>", tag + "\n</head>")
        else:
            content = tag + "\n" + content
        f.write_text(content, encoding="utf-8")
        injected.append(str(f.relative_to(OUTPUT_DIR)))
    print(f"\n[OK] Injected into {len(injected)} HTML files:")
    for f in injected:
        print(f"  {f}")


def extract_inline_scripts():
    """Extract bare inline <script> blocks (no type/src) from HTML files."""
    sp = OUTPUT_DIR / "sidepanel.html"
    if not sp.exists():
        return
    content = sp.read_text(encoding="utf-8")
    pattern = r'<script>(.*?)</script>'
    matches = list(re.finditer(pattern, content, re.DOTALL))
    if not matches:
        return
    for i in reversed(range(len(matches))):
        match = matches[i]
        script_content = match.group(1).strip()
        if not script_content:
            continue
        fname = f"sidepanel-inline-{i}.js"
        (OUTPUT_DIR / fname).write_text(script_content, encoding="utf-8")
        content = content[:match.start()] + f'<script src="/{fname}"></script>' + content[match.end():]
        print(f"[OK] Extracted inline script from sidepanel.html -> {fname}")
    sp.write_text(content, encoding="utf-8")


# =============================================================================
# LOCAL CFC PROXY — replaces cocodem's openclaude.111724.xyz
# Port 8520. Handles /api/options, proxied auth URLs, oauth pages.
# =============================================================================

FAKE_PROFILE = {
    "account": {
        "uuid": "local-user-uuid", "email_address": "user@local", "email": "user@local",
        "full_name": "Local User", "name": "Local User", "display_name": "Local User",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
        "id": "local-user-uuid", "has_password": True, "has_completed_onboarding": True,
        "preferred_language": "en-US", "has_claude_pro": True
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
        "current_period_end": "2099-12-31T23:59:59Z",
    },
    "chat_enabled": True, "settings": {"theme": "system", "language": "en-US"},
    "has_claude_pro": True, "capabilities": ["chat", "api"], "rate_limit_tier": "free",
}

FAKE_ORGS = [{"uuid": "local-org-uuid", "id": "local-org-uuid", "name": "Local",
    "role": "admin", "organization_type": "personal", "billing_type": "self_serve",
    "capabilities": ["chat", "api"], "rate_limit_tier": "free",
    "settings": {}, "created_at": "2024-01-01T00:00:00Z"}]

FAKE_CONVERSATIONS = {"conversations": [], "limit": 0, "has_more": False, "cursor": None}
FAKE_DOMAIN_INFO = {"domain": "local", "allowed": True}


def get_fake_auth_for_path(path):
    # Specific sub-paths first — order matters
    if "/mcp/v2/bootstrap" in path:
        return {"servers": [], "tools": [], "enabled": False}
    if "/spotlight" in path:
        return {"items": [], "total": 0}
    if "features/claude_in_chrome" in path or "/features/" in path:
        return {"enabled": True, "features": {}}
    if "/oauth/account/settings" in path:
        return {"settings": {"theme": "system", "language": "en-US"}}
    if "/oauth/profile" in path or "/oauth/account" in path:
        return FAKE_PROFILE
    if "/oauth/token" in path:
        return FAKE_TOKEN
    if "/bootstrap" in path:
        return FAKE_BOOTSTRAP
    # Org-specific sub-paths (e.g. /organizations/UUID/something) → empty object
    if "/oauth/organizations/" in path and path.count("/") > path.find("/oauth/organizations/") + 20:
        return {}
    if "/oauth/organizations" in path:
        return FAKE_ORGS
    if "/chat_conversations" in path:
        return FAKE_CONVERSATIONS
    if "/domain_info" in path:
        return FAKE_DOMAIN_INFO
    # Any other proxied URL — return empty object not 204
    return {}


def build_uinodes():
    """Inject Backend Settings where cocodem puts API KEY into original options page."""
    settings_url = "/backend_settings.html"
    return [
        {
            "selector": {
                "type": "div",
                "props": {"className": None, "children": [{"type": "label", "props": {"htmlFor": "apiKey"}}]}
            },
            "append": {"type": "li", "props": {"children": [{"type": "a", "props": {
                "href": settings_url, "target": "_blank",
                "className": "block w-full text-left whitespace-nowrap transition-all ease-in-out active:scale-95 cursor-pointer font-base rounded-lg px-3 py-3 text-text-200 hover:bg-bg-200 hover:text-text-100",
                "children": "\u2699\ufe0f  Backend Settings  \u2197"
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
                "className": "font-base min-h-8 px-2 py-1.5 rounded-lg cursor-pointer whitespace-nowrap overflow-hidden text-ellipsis grid grid-cols-[minmax(0,_1fr)_auto] gap-2 items-center outline-none select-none hover:bg-bg-200 hover:text-text-000",
                "children": "\u2699\ufe0f  Backend Settings  \u2197"
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
        }
    ]


class CFCProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"  [CFC] {args[0]}")

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
            "Content-Type, Cache-Control, anthropic-version, anthropic-beta")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        path = self.path
        if any(d in path for d in ["segment", "statsig", "honeycomb", "sentry", "datadoghq"]):
            self.send_response(204)
            self.send_cors()
            self.end_headers()
            return

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
                ],
                "discardIncludes": [
                    "cdn.segment.com", "api.segment.io", "events.statsigapi.net",
                    "api.honeycomb.io", "prodregistryv2.org",
                    "*ingest.us.sentry.io", "browser-intake-us5-datadoghq.com",
                ],
                "modelAlias": {},
                "ui": {},
                "uiNodes": build_uinodes(),
            })
            return

        if path.startswith("/api/arc-split-view"):
            self.send_json({"html": "<div>Local CFC Proxy</div>"})
            return

        if path.startswith("/oauth/redirect"):
            qs = urlparse(path).query
            redirect_html = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Authenticating...</title></head>
<body style="background:#f9f8f3;color:#1a1a1a;font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center;background:white;border:1px solid #e5e2d9;border-radius:16px;padding:40px;max-width:400px;width:100%">
<h2 style="margin:0 0 8px;font-size:22px">Signed in successfully!</h2>
<p style="color:#666;margin:0 0 20px">You're all set to use Claude in Chrome.</p>
<p id="msg" style="color:#888;font-size:13px">Setting up local session...</p>
<script>
(async()=>{{
  const msg=document.getElementById("msg");
  try{{
    const params=new URLSearchParams(window.location.search);
    const redirectUri=params.get("redirect_uri");
    let extId="{EXTENSION_ID}";
    if(redirectUri){{try{{const u=new URL(redirectUri);if(u.protocol==="chrome-extension:")extId=u.host;}}catch(e){{}}}}
    const arr=new Uint8Array(32);crypto.getRandomValues(arr);
    const code="cfc-"+btoa(String.fromCharCode(...arr)).replace(/\\+/g,"-").replace(/\\//g,"_").replace(/=/g,"");
    if(redirectUri){{
      try{{
        const a=new URL(redirectUri);a.searchParams.set("code",code);
        const state=params.get("state");if(state)a.searchParams.set("state",state);
        chrome.runtime.sendMessage(extId,{{redirect_uri:a.toString(),type:"oauth_redirect"}},r=>{{
          if(r?.success){{msg.textContent="Done! Close this tab.";msg.style.color="#2d6a4f";}}
          else{{
            chrome.runtime.sendMessage(extId,{{type:"_set_storage_local",data:{{
              accessToken: btoa(JSON.stringify({{alg:"none",typ:"JWT"}})) + "." + btoa(JSON.stringify({{iss:"local",sub:"local-user",exp:9999999999,iat:Math.floor(Date.now()/1000)}})) + ".local",
              refreshToken:"local-refresh",
              tokenExpiry:Date.now()+31536000000,
              accountUuid:"local-user-uuid"
            }}}},()=>{{msg.textContent="Done! Close this tab.";msg.style.color="#2d6a4f";}});
          }}
        }});
      }}catch(e){{msg.textContent="Auth set. Close this tab.";}}
    }}
  }}catch(e){{msg.textContent="Error: "+e.message;msg.style.color="#c9184a";}}
}})();
</script></div></body></html>'''
            self.send_html(redirect_html)
            return

        if "/oauth/authorize" in path:
            qs = urlparse(path).query
            free_trial = CFC_BASE + "oauth/redirect?" + qs
            oauth_html = f'''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Claude for Chrome</title></head>
<body style="background:#f9f8f3;font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px">
<div style="background:white;border:1px solid #e5e2d9;border-radius:16px;padding:40px;max-width:420px;width:100%;text-align:center">
<h2 style="margin:0 0 8px;font-size:22px">Open Claude in Chrome</h2>
<p style="color:#666;margin:0 0 24px">Activate your extension to use your local backend.</p>
<div style="display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-bottom:16px">
<a href="javascript:void(0)" onclick="try{{chrome.runtime.sendMessage('{EXTENSION_ID}', {{type: '_create_tab', url: '/backend_settings.html'}})}}catch(e){{}}"
   style="background:#1a1a1a;color:white;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">\u2699\ufe0f Backend Settings</a>
<a href="{free_trial}"
   style="border:1px solid #e5e2d9;color:#c96a50;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">\u2764\ufe0f Activate Free</a>
</div>
<div style="background:#f5f4f0;border-radius:8px;padding:16px;margin:16px 0;text-align:left">
<p style="margin:0;color:#666;font-size:13px">Right-click the Claude extension icon &rarr; Options &rarr; Click 'Backend Settings' to set your backend URL</p>
</div>
<hr style="border:none;border-top:1px solid #e5e2d9;margin:20px 0">
<p style="color:#888;font-size:13px;margin:0">Local CFC Proxy &mdash; No external connections</p>
</div></body></html>'''
            self.send_html(oauth_html)
            return

        if path.startswith("/settings"):
            self.send_html(
                '<!DOCTYPE html><html><head><meta charset="utf-8">'
                '<title>Backend Settings</title></head>'
                '<body style="background:#1a1a2e;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
                '<div style="text-align:center">'
                '<h2>Opening Backend Settings...</h2>'
                '<p style="color:#888">If nothing opens, right-click the Claude extension icon &rarr; Options &rarr; Click Backend Settings</p>'
                '<script>'
                'try{["' + EXTENSION_ID + '"].forEach(id=>{try{chrome.runtime.sendMessage(id,{type:"_create_tab", url:"/backend_settings.html"});}catch(e){}})}'
                'catch(e){}'
                '</script></div></body></html>'
            )
            return

        if path.startswith("/https://") or path.startswith("/http://"):
            self.send_json(get_fake_auth_for_path(path))
            return

        if any(s in path for s in ["/oauth/", "/bootstrap", "/domain_info", "/chat_conversations", "/organizations"]):
            self.send_json(get_fake_auth_for_path(path))
            return

        if path == "/" or path.startswith("/?"):
            self.send_html(f'''<!DOCTYPE html><html><head><title>CFC Proxy</title></head>
<body style="background:#1a1a2e;color:#fff;font-family:sans-serif;padding:40px">
<h1>Local CFC Proxy</h1>
<p>Running on port {CFC_PORT}</p>
<p>Replaces cocodem's openclaude.111724.xyz</p>
<p>No external connections. All auth faked locally.</p>
</body></html>''')
            return

        self.send_response(204)
        self.send_cors()
        self.end_headers()

    def do_POST(self):
        path = self.path
        if any(d in path for d in ["segment", "statsig", "honeycomb", "sentry", "datadoghq"]):
            self.send_response(204)
            self.send_cors()
            self.end_headers()
            return
            
        if path.startswith("/https://") or path.startswith("/http://"):
            self.send_json(get_fake_auth_for_path(path))
            return
        if any(s in path for s in ["/oauth/", "/bootstrap", "/domain_info", "/chat_conversations"]):
            self.send_json(get_fake_auth_for_path(path))
            return
        self.send_response(204)
        self.send_cors()
        self.end_headers()


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def start_cfc_proxy():
    try:
        server = ReusableTCPServer(("127.0.0.1", CFC_PORT), CFCProxyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"\n[OK] CFC Proxy running on {CFC_BASE}")
        return server
    except OSError as e:
        print(f"[WARN] Could not start CFC proxy on port {CFC_PORT}: {e}")
        print(f"  Is something already using port {CFC_PORT}?")
        return None


def print_report(m):
    print("\n" + "=" * 60)
    print(f"  DONE — {OUTPUT_DIR}")
    print("=" * 60)
    print(f"  {m.get('name')} v{m.get('version')}")
    print(f"  ID: {EXTENSION_ID} (key KEPT)")
    print(f"  CFC Proxy: {CFC_BASE}")
    print(f"\n  Install:")
    print(f"    1. Disable official Claude extension")
    print(f"    2. chrome://extensions -> Developer Mode")
    print(f"    3. Load unpacked -> {OUTPUT_DIR}")
    print(f"    4. Right-click icon -> Options -> Click 'Backend Settings' to set backend URL")
    print(f"\n  KEEP THIS TERMINAL OPEN — the CFC proxy must stay running!")
    print(f"  Press Ctrl+C to stop.\n")


def main():
    print("=" * 60)
    print(f"  Claude Hijack — {TIMESTAMP}")
    print("=" * 60)
    
    download_crx()
    extract_crx()
    m = read_manifest()
    m = patch_manifest(m)
    write_hijack_js()
    write_options()
    inject_service_worker(m)
    extract_inline_scripts()
    inject_html_pages()
    
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
        print("[WARN] CFC proxy did not start. Extension may not work fully.")


if __name__ == "__main__":
    main()
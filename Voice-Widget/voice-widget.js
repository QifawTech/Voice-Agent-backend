/**
 * Qifaw Voice Agent v3.0
 * Real-time voice agent widget — a real phone call, over LiveKit WebRTC.
 *
 * v3 replaced the chunked-WebSocket transport with LiveKit. The difference
 * that matters: the microphone is published continuously instead of being
 * recorded, stopped and uploaded, so the caller can interrupt the agent
 * mid-sentence and replies start while the agent is still thinking.
 *
 * Gone with the old transport: MediaRecorder, the silence detector that
 * guessed when a caller had finished, the base64 audio upload and the
 * playback queue. LiveKit streams both directions and the agent worker owns
 * turn-taking.
 *
 * Usage:
 *   <script
 *     src="widget.js"
 *     data-tenant-id="YOUR_TENANT_ID"
 *     data-api-key="sk_live_YOUR_KEY"
 *     data-sub-tenant-id="your-restaurant-slug"  // multi-tenant accounts
 *     data-restaurant-id="your-restaurant-slug"  // legacy name, still works
 *     data-language="en"
 *     data-ws-host="ws://localhost:8001"         // the voice platform API
 *     data-livekit-url="ws://localhost:7880"     // optional override
 *     data-sdk-url="/livekit-client.js"          // optional override
 *     data-title="Voice Assistant"
 *     data-launcher="none"                       // hide the floating button
 *     data-ui="none"                             // hide the call screen too
 *   ></script>
 *
 * Host-driven start. With data-launcher="none" no button is drawn and the page
 * runs the call itself. Add data-ui="none" and the call has no interface at
 * all -- voice only, over whatever the host page is already showing. The host
 * then owns the hang-up control, because the widget no longer draws one:
 *
 *   window.QifawVoice.start();            // begin a call (from a real tap)
 *   window.QifawVoice.stop();             // hang up
 *   window.QifawVoice.isActive();         // true while a call is running
 *   window.QifawVoice.on("end", fn);      // "start" | "connected" | "end"
 *   window.QifawVoice.on("speaking", (agentSpeaking) => {});
 *                                         // every on() returns an unsubscribe
 *
 * window.QifawVoice appears only once the widget is built, so a host should
 * wait for it rather than assume the script tag's load event is enough.
 */

(function () {
  "use strict";

  // ───────────────────────────── Config ─────────────────────────────
  const scriptTag = document.currentScript;

  const attr = (name) => scriptTag?.getAttribute(name) || "";

  const CONFIG = {
    tenantId:     attr("data-tenant-id"),
    apiKey:       attr("data-api-key"),
    // Multi-tenant accounts identify which of their tenants this embed is for.
    // data-sub-tenant-id is the current name; data-restaurant-id is the older
    // one and still honoured so existing embeds keep working.
    subTenantId:  attr("data-sub-tenant-id") || attr("data-restaurant-id"),
    language:     attr("data-language") || "",
    primaryColor: attr("data-primary-color") || "#6C63FF",
    position:     attr("data-position") || "bottom-right",
    wsHost:       attr("data-ws-host")
                  || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`,
    livekitUrl:   attr("data-livekit-url"),
    sdkUrl:       attr("data-sdk-url"),
    title:        attr("data-title") || "Voice Assistant",
    // "fab" (default) draws the floating phone button, so existing embeds are
    // unchanged. "none" draws no launcher at all, for host pages that present
    // their own branded start button and call window.QifawVoice.start().
    launcher:     attr("data-launcher") || "fab",
    // "full" (default) shows the call screen -- avatar, timer, transcript,
    // mute and hang-up. "none" runs the call with no interface at all: the
    // agent is heard, the host page stays on screen, and the host owns both
    // the hang-up control and whatever it wants to show while the call runs.
    ui:           attr("data-ui") || "full",
  };

  // The token endpoint is plain HTTP on the same host as the WebSocket, so the
  // embed only ever has to supply one address.
  const HTTP_BASE = CONFIG.wsHost.replace(/^ws/, "http").replace(/\/+$/, "");
  const TOKEN_URL = `${HTTP_BASE}/api/v1/livekit/token`;
  const SDK_URL = CONFIG.sdkUrl || `${HTTP_BASE}/livekit-client.js`;

  // The token endpoint names languages in full; the embed attribute uses short
  // codes, as the old widget did. Unknown codes pass through untouched.
  const LANG_NAMES = {
    en: "english",
    ta: "tamil",
    te: "telugu",
    kn: "kannada",
    ml: "malayalam",
    hi: "hindi",
  };

  // ───────────────────────────── State ─────────────────────────────
  let LK = null;            // the LiveKit SDK namespace, loaded on first call
  let room = null;
  let currentLanguage = CONFIG.language;

  let isCallActive = false;
  let isMuted = false;

  let callStartTime = null;
  let callTimerInterval = null;

  // Waveform, driven by the published microphone track.
  let audioContext = null;
  let analyserNode = null;
  let animFrameId = null;

  // Transcript segments arrive many times per turn, each re-sending the whole
  // sentence so far under one id. Tracking the id is what stops the sidebar
  // filling with half-finished copies of the same line.
  const renderedSegments = new Map();

  // ───────────────────────────── SDK loader ─────────────────────────────
  // The widget is a plain script tag with no bundler, so the SDK is fetched
  // once, on the first call rather than on page load -- a shop page that
  // nobody speaks to should not pay for 580 KB of WebRTC.
  function loadSdk() {
    if (window.LivekitClient) return Promise.resolve(window.LivekitClient);

    return new Promise((resolve, reject) => {
      const existing = document.getElementById("qifaw-livekit-sdk");
      if (existing) {
        existing.addEventListener("load", () => resolve(window.LivekitClient));
        existing.addEventListener("error", () => reject(new Error("SDK failed to load")));
        return;
      }
      const s = document.createElement("script");
      s.id = "qifaw-livekit-sdk";
      s.src = SDK_URL;
      s.async = true;
      s.onload = () =>
        window.LivekitClient
          ? resolve(window.LivekitClient)
          : reject(new Error("SDK loaded but LivekitClient is missing"));
      s.onerror = () => reject(new Error(`Could not load the LiveKit SDK from ${SDK_URL}`));
      document.head.appendChild(s);
    });
  }

  // ───────────────────────────── CSS ─────────────────────────────
  function injectStyles() {
    const s = document.createElement("style");
    s.id = "qifaw-voice-widget-styles";
    s.textContent = `
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

      /* ── Launcher FAB ──────────────────────────────────────── */
      #qifaw-launcher {
        position: fixed;
        ${CONFIG.position.includes("right") ? "right: 24px;" : "left: 24px;"}
        bottom: 24px;
        z-index: 2147483647;
        width: 64px;
        height: 64px;
        border-radius: 50%;
        background: linear-gradient(135deg, ${CONFIG.primaryColor}, color-mix(in srgb, ${CONFIG.primaryColor} 70%, black));
        border: none;
        cursor: pointer;
        box-shadow: 0 8px 32px rgba(108,99,255,0.5), 0 0 0 0 rgba(108,99,255,0.4);
        display: flex;
        align-items: center;
        justify-content: center;
        transition: transform 0.25s cubic-bezier(0.34,1.56,0.64,1), box-shadow 0.3s;
        outline: none;
        animation: qw-fab-pulse 3s ease-in-out infinite;
      }
      #qifaw-launcher:hover {
        transform: scale(1.1);
        box-shadow: 0 12px 40px rgba(108,99,255,0.65);
      }
      #qifaw-launcher.qw-hidden { display: none; }

      @keyframes qw-fab-pulse {
        0%, 100% { box-shadow: 0 8px 32px rgba(108,99,255,0.5), 0 0 0 0 rgba(108,99,255,0.3); }
        50% { box-shadow: 0 8px 32px rgba(108,99,255,0.5), 0 0 0 12px rgba(108,99,255,0); }
      }

      /* ── Call Overlay ──────────────────────────────────────── */
      #qifaw-call-overlay {
        position: fixed;
        inset: 0;
        z-index: 2147483647;
        background: radial-gradient(ellipse at 30% 30%, #1a1a2e 0%, #0a0a14 100%);
        display: none;
        flex-direction: row;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        color: #f0f0f8;
        padding: 0;
        opacity: 0;
        transition: opacity 0.35s ease;
      }
      #qifaw-call-overlay.qw-active {
        display: flex;
        opacity: 1;
      }

      .qw-call-main {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: space-between;
        min-width: 0;
      }

      /* Conversation Log Sidebar */
      .qw-chat-sidebar {
        width: 380px;
        background: #0d0d18;
        border-left: 1px solid rgba(255,255,255,0.06);
        display: flex;
        flex-direction: column;
        flex-shrink: 0;
      }
      .qw-chat-sidebar-header {
        padding: 18px 20px;
        font-size: 13px;
        font-weight: 600;
        color: #8888aa;
        text-transform: uppercase;
        letter-spacing: 1px;
        border-bottom: 1px solid rgba(255,255,255,0.06);
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .qw-chat-sidebar-header .qw-dot-live {
        width: 6px; height: 6px;
        background: #2ed573;
        border-radius: 50%;
        animation: qw-fab-pulse 2s infinite;
      }
      .qw-chat-log {
        flex: 1;
        overflow-y: auto;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 12px;
        scrollbar-width: thin;
        scrollbar-color: rgba(255,255,255,0.1) transparent;
      }
      .qw-chat-log::-webkit-scrollbar { width: 4px; }
      .qw-chat-log::-webkit-scrollbar-track { background: transparent; }
      .qw-chat-log::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
      .qw-log-msg {
        display: flex;
        flex-direction: column;
        gap: 4px;
        animation: qw-log-fade 0.3s ease;
      }
      @keyframes qw-log-fade {
        from { opacity: 0; transform: translateY(8px); }
        to { opacity: 1; transform: translateY(0); }
      }
      .qw-log-label {
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .qw-log-label.qw-you { color: #ff6b81; }
      .qw-log-label.qw-agent { color: ${CONFIG.primaryColor}; }
      .qw-log-label.qw-system { color: #8888aa; }
      .qw-log-text {
        font-size: 14px;
        line-height: 1.5;
        color: #d0d0e8;
        padding: 10px 14px;
        border-radius: 12px;
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.06);
      }
      .qw-log-time {
        font-size: 10px;
        color: #555;
        align-self: flex-end;
      }

      /* ── Top Bar ────────────────────────────────────────────── */
      .qw-call-topbar {
        width: 100%;
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 20px 28px;
        flex-shrink: 0;
      }
      .qw-call-topbar-left {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .qw-call-secure {
        display: flex;
        align-items: center;
        gap: 5px;
        font-size: 11px;
        color: #2ed573;
        font-weight: 500;
        letter-spacing: 0.5px;
      }
      .qw-call-timer {
        font-size: 13px;
        color: #8888aa;
        font-weight: 500;
        font-variant-numeric: tabular-nums;
      }

      /* ── Center Content ─────────────────────────────────────── */
      .qw-call-center {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 32px;
        padding: 20px;
        width: 100%;
        max-width: 500px;
      }

      /* Agent Avatar with ring */
      .qw-agent-avatar {
        width: 120px;
        height: 120px;
        border-radius: 50%;
        background: linear-gradient(135deg, ${CONFIG.primaryColor}, color-mix(in srgb, ${CONFIG.primaryColor} 60%, #0a0a14));
        display: flex;
        align-items: center;
        justify-content: center;
        position: relative;
        box-shadow: 0 0 40px rgba(108,99,255,0.25);
      }
      .qw-agent-avatar::before {
        content: '';
        position: absolute;
        inset: -6px;
        border-radius: 50%;
        border: 2px solid rgba(108,99,255,0.3);
        animation: qw-ring-idle 3s ease-in-out infinite;
      }
      .qw-agent-avatar.qw-speaking::before {
        border-color: #2ed573;
        animation: qw-ring-speak 0.8s ease-in-out infinite;
      }
      .qw-agent-avatar.qw-thinking::before {
        border-color: #ffa502;
        animation: qw-ring-think 1.2s linear infinite;
      }
      .qw-agent-avatar.qw-listening::before {
        border-color: #ff4757;
        animation: qw-ring-speak 0.6s ease-in-out infinite;
      }

      @keyframes qw-ring-idle {
        0%, 100% { transform: scale(1); opacity: 0.5; }
        50% { transform: scale(1.06); opacity: 0.8; }
      }
      @keyframes qw-ring-speak {
        0%, 100% { transform: scale(1); opacity: 0.6; }
        50% { transform: scale(1.12); opacity: 1; }
      }
      @keyframes qw-ring-think {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
      }

      .qw-agent-name {
        font-size: 22px;
        font-weight: 700;
        text-align: center;
      }

      /* Status line */
      .qw-call-status {
        font-size: 14px;
        color: #8888aa;
        text-align: center;
        min-height: 22px;
        transition: all 0.2s;
      }
      .qw-call-status.qw-listening { color: #ff4757; }
      .qw-call-status.qw-thinking { color: #ffa502; }
      .qw-call-status.qw-speaking { color: #2ed573; }

      /* Waveform canvas */
      .qw-waveform-container {
        width: 100%;
        max-width: 320px;
        height: 64px;
        position: relative;
      }
      #qw-waveform {
        width: 100%;
        height: 100%;
        display: block;
      }

      /* Transcript */
      .qw-transcript {
        font-size: 16px;
        text-align: center;
        line-height: 1.5;
        color: #ccccee;
        min-height: 48px;
        max-height: 120px;
        overflow-y: auto;
        padding: 0 20px;
        scrollbar-width: none;
      }
      .qw-transcript::-webkit-scrollbar { display: none; }

      /* ── Bottom Bar ─────────────────────────────────────────── */
      .qw-call-bottombar {
        width: 100%;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 32px;
        padding: 28px;
        flex-shrink: 0;
      }

      /* Control buttons */
      .qw-ctrl-btn {
        width: 56px;
        height: 56px;
        border-radius: 50%;
        border: none;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: transform 0.15s, background 0.2s;
        outline: none;
      }
      .qw-ctrl-btn:hover { transform: scale(1.08); }
      .qw-ctrl-btn:active { transform: scale(0.94); }

      .qw-btn-mute {
        background: rgba(255,255,255,0.1);
        color: #f0f0f8;
      }
      .qw-btn-mute.qw-muted {
        background: rgba(255,71,87,0.2);
        color: #ff4757;
      }

      .qw-btn-end {
        width: 72px;
        height: 72px;
        background: #ff4757;
        color: #fff;
        box-shadow: 0 6px 24px rgba(255,71,87,0.4);
      }
      .qw-btn-end:hover { background: #ff6b6b; }

      .qw-btn-lang {
        background: rgba(255,255,255,0.1);
        color: #f0f0f8;
        font-size: 12px;
        font-weight: 600;
        font-family: 'Inter', sans-serif;
      }

      /* Language dropdown */
      .qw-lang-dropdown {
        position: absolute;
        bottom: 100px;
        background: #1a1a2e;
        border: 1px solid rgba(255,255,255,0.1);
        border-radius: 14px;
        padding: 8px;
        box-shadow: 0 12px 40px rgba(0,0,0,0.6);
        display: none;
        z-index: 10;
        min-width: 180px;
      }
      .qw-lang-dropdown.qw-visible { display: block; }
      .qw-lang-dropdown-opt {
        padding: 10px 14px;
        border-radius: 10px;
        cursor: pointer;
        font-size: 14px;
        color: #ccccee;
        display: flex;
        align-items: center;
        gap: 10px;
        transition: background 0.1s;
      }
      .qw-lang-dropdown-opt:hover { background: rgba(255,255,255,0.06); }
      .qw-lang-dropdown-opt.qw-active { color: ${CONFIG.primaryColor}; font-weight: 600; }

      /* Powered by */
      .qw-powered {
        font-size: 10px;
        color: #555;
        text-align: center;
        padding-bottom: 12px;
        letter-spacing: 0.3px;
      }
      .qw-powered a { color: ${CONFIG.primaryColor}; text-decoration: none; }

      /* ── Mobile ─────────────────────────────────────────────── */
      @media (max-width: 768px) {
        #qifaw-call-overlay { flex-direction: column; }
        .qw-chat-sidebar {
          width: 100%;
          height: 40vh;
          border-left: none;
          border-top: 1px solid rgba(255,255,255,0.06);
        }
        .qw-agent-avatar { width: 80px; height: 80px; }
        .qw-agent-name { font-size: 16px; }
        .qw-call-center { gap: 20px; }
        .qw-call-bottombar { gap: 24px; padding: 16px; }
      }
    `;
    document.head.appendChild(s);
  }

  // ───────────────────────────── SVG Icons ─────────────────────────────
  const ICON = {
    phone: `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></svg>`,
    phoneOff: `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10.68 13.31a16 16 0 0 0 3.41 2.6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7 2 2 0 0 1 1.72 2v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.42 19.42 0 0 1-3.33-2.67m-2.67-3.34a19.79 19.79 0 0 1-3.07-8.63A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`,
    mic: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>`,
    micOff: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="1" y1="1" x2="23" y2="23"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6"/><path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2c0 .87-.16 1.7-.45 2.47"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>`,
    globe: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
    lock: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`,
    bot: `<svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8V4H8"/><rect x="2" y="8" width="20" height="12" rx="3"/><circle cx="8" cy="14" r="1.5" fill="currentColor"/><circle cx="16" cy="14" r="1.5" fill="currentColor"/></svg>`,
  };

  // ───────────────────────────── Build DOM ─────────────────────────────
  let els = {};

  function buildWidget() {
    // FAB Launcher
    const fab = document.createElement("button");
    fab.id = "qifaw-launcher";
    fab.setAttribute("aria-label", "Start voice call");
    fab.innerHTML = ICON.phone;
    fab.addEventListener("click", startCall);

    // Call overlay
    const overlay = document.createElement("div");
    overlay.id = "qifaw-call-overlay";
    overlay.innerHTML = `
      <div class="qw-call-main">
        <div class="qw-call-topbar">
          <div class="qw-call-topbar-left">
            <div class="qw-call-secure">${ICON.lock} Encrypted</div>
          </div>
          <div class="qw-call-timer" id="qw-call-timer">00:00</div>
        </div>

        <div class="qw-call-center">
          <div class="qw-agent-avatar" id="qw-avatar">${ICON.bot}</div>
          <div class="qw-agent-name">${CONFIG.title}</div>
          <div class="qw-call-status" id="qw-status">Connecting...</div>

          <div class="qw-waveform-container">
            <canvas id="qw-waveform"></canvas>
          </div>

          <div class="qw-transcript" id="qw-transcript"></div>
        </div>

        <div class="qw-call-bottombar">
          <button class="qw-ctrl-btn qw-btn-mute" id="qw-mute-btn" title="Mute">${ICON.mic}</button>
          <button class="qw-ctrl-btn qw-btn-end" id="qw-end-btn" title="End Call">${ICON.phoneOff}</button>
          <button class="qw-ctrl-btn qw-btn-lang" id="qw-lang-btn" title="Language">${ICON.globe}</button>
        </div>

        <div class="qw-lang-dropdown" id="qw-lang-dropdown"></div>
        <div class="qw-powered">Powered by <a href="https://qifaw.com" target="_blank" rel="noopener">Qifaw</a> AI</div>
      </div>

      <div class="qw-chat-sidebar">
        <div class="qw-chat-sidebar-header">
          <div class="qw-dot-live"></div>
          Live Transcript
        </div>
        <div class="qw-chat-log" id="qw-chat-log"></div>
      </div>
    `;

    // With data-launcher="none" the button is still built but never inserted.
    // Keeping the detached node means startCall and endCall can go on toggling
    // its class without either of them having to know whether a launcher is on
    // screen at all.
    if (CONFIG.launcher !== "none") document.body.appendChild(fab);
    // Same for the call screen under data-ui="none". The agent's own audio
    // element is appended to document.body by connectRoom, not to this
    // overlay, so detaching the overlay silences the interface and nothing
    // else -- the call is still heard.
    if (CONFIG.ui !== "none") document.body.appendChild(overlay);

    els = {
      fab,
      overlay,
      timer:     overlay.querySelector("#qw-call-timer"),
      avatar:    overlay.querySelector("#qw-avatar"),
      status:    overlay.querySelector("#qw-status"),
      canvas:    overlay.querySelector("#qw-waveform"),
      transcript:overlay.querySelector("#qw-transcript"),
      chatLog:   overlay.querySelector("#qw-chat-log"),
      muteBtn:   overlay.querySelector("#qw-mute-btn"),
      endBtn:    overlay.querySelector("#qw-end-btn"),
      langBtn:   overlay.querySelector("#qw-lang-btn"),
      langDrop:  overlay.querySelector("#qw-lang-dropdown"),
    };

    els.endBtn.addEventListener("click", endCall);
    els.muteBtn.addEventListener("click", toggleMute);
    els.langBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      els.langDrop.classList.toggle("qw-visible");
    });
    document.addEventListener("click", () => els.langDrop.classList.remove("qw-visible"));

    buildLangOptions();
  }

  // ───────────────────────────── Token ─────────────────────────────
  async function fetchToken(language) {
    const res = await fetch(TOKEN_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Tenant-ID": CONFIG.tenantId,
        "X-API-Key": CONFIG.apiKey,
      },
      body: JSON.stringify({
        participant_name: "web-caller",
        // Only send a language when one was explicitly chosen (data-language or
        // the in-call switcher). When omitted, the backend uses the tenant's
        // dashboard Voice Config language.
        ...(language ? { language: LANG_NAMES[language] || language } : {}),
        // Sent under both names: the platform prefers sub_tenant_id and falls
        // back to restaurant_id, and older builds only know the latter.
        sub_tenant_id: CONFIG.subTenantId || undefined,
        restaurant_id: CONFIG.subTenantId || undefined,
      }),
    });

    if (!res.ok) {
      let detail = "";
      try {
        const body = await res.json();
        detail = body.detail || body.error || body.message || "";
      } catch {
        /* non-JSON error body */
      }
      throw new Error(detail || `Token request failed (${res.status})`);
    }
    return res.json();
  }

  // A token issued by a server that calls itself "localhost" is useless to a
  // phone on the same Wi-Fi -- there, localhost is the phone. When the page is
  // not itself on localhost, borrow the host the embed already told us about.
  function resolveLivekitUrl(urlFromServer) {
    if (CONFIG.livekitUrl) return CONFIG.livekitUrl;
    const pageIsLocal = ["localhost", "127.0.0.1"].includes(location.hostname);
    if (pageIsLocal) return urlFromServer;
    try {
      const fromServer = new URL(urlFromServer);
      if (!["localhost", "127.0.0.1"].includes(fromServer.hostname)) return urlFromServer;
      fromServer.hostname = new URL(CONFIG.wsHost).hostname;
      return fromServer.toString().replace(/\/+$/, "");
    } catch {
      return urlFromServer;
    }
  }

  // ───────────────────────────── Call Lifecycle ─────────────────────────────
  async function startCall() {
    if (isCallActive) return;
    isCallActive = true;
    emit("start");

    els.fab.classList.add("qw-hidden");
    els.overlay.classList.add("qw-active");
    setCallStatus("connecting", "Connecting...");
    renderedSegments.clear();

    callStartTime = Date.now();
    callTimerInterval = setInterval(updateCallTimer, 1000);

    try {
      LK = await loadSdk();
      const tokenRes = await fetchToken(currentLanguage);
      await connectRoom(resolveLivekitUrl(tokenRes.livekit_url), tokenRes.token);
    } catch (err) {
      console.error("[Qifaw Voice Agent]", err);
      setCallStatus("error", err.message || "Connection failed");
      setTimeout(endCall, 3000);
    }
  }

  async function connectRoom(url, token) {
    const r = new LK.Room({ adaptiveStream: true, dynacast: true });
    room = r;

    // The agent's voice. Attaching the track is what plays it -- there is no
    // queue and no decoding on our side any more.
    r.on(LK.RoomEvent.TrackSubscribed, (track) => {
      if (track.kind !== LK.Track.Kind.Audio) return;
      const el = track.attach();
      el.id = "qifaw-agent-audio";
      el.autoplay = true;
      document.body.appendChild(el);
    });

    r.on(LK.RoomEvent.TrackUnsubscribed, (track) => {
      track.detach().forEach((el) => el.remove());
    });

    // Captions. Each segment re-sends the whole sentence so far under a stable
    // id, so these are replaced rather than appended.
    r.on(LK.RoomEvent.TranscriptionReceived, (segments, participant) => {
      const mine = participant?.identity === r.localParticipant?.identity;
      const role = mine ? "user" : "agent";
      for (const seg of segments || []) {
        showTranscript(role, seg.text || "", seg.id);
      }
    });

    // Who is talking drives the status line and the avatar animation.
    r.on(LK.RoomEvent.ActiveSpeakersChanged, (speakers) => {
      if (!isCallActive) return;
      const agentTalking = (speakers || []).some(
        (p) => p.identity !== r.localParticipant?.identity,
      );
      if (agentTalking) setCallStatus("speaking", "Speaking...");
      else if (isMuted) setCallStatus("connected", "Muted");
      else setCallStatus("listening", "Listening...");

      // A host drawing its own avatar needs this to animate in time with the
      // agent's voice -- it is the only turn-taking signal it can see.
      emit("speaking", agentTalking);
    });

    r.on(LK.RoomEvent.Disconnected, () => {
      if (isCallActive) endCall();
    });

    r.on(LK.RoomEvent.ConnectionStateChanged, (state) => {
      if (!isCallActive) return;
      if (state === LK.ConnectionState.Reconnecting) {
        setCallStatus("connecting", "Reconnecting...");
      } else if (state === LK.ConnectionState.Connected && !isMuted) {
        setCallStatus("listening", "Listening...");
      }
    });

    await r.connect(url, token);
    await r.localParticipant.setMicrophoneEnabled(true);

    isMuted = false;
    els.muteBtn.innerHTML = ICON.mic;
    els.muteBtn.classList.remove("qw-muted");
    setCallStatus("listening", "Listening...");
    // A host that drew its own "connecting" state can now correct it. Fired
    // here rather than in startCall, which returns long before the room is up.
    emit("connected");
    startWaveform();
  }

  function endCall() {
    isCallActive = false;

    stopWaveform();

    clearInterval(callTimerInterval);
    callTimerInterval = null;
    callStartTime = null;

    if (room) {
      try { room.disconnect(); } catch { /* already gone */ }
      room = null;
    }

    document.getElementById("qifaw-agent-audio")?.remove();

    if (audioContext) {
      audioContext.close().catch(() => {});
      audioContext = null;
    }
    analyserNode = null;

    els.overlay.classList.remove("qw-active");
    els.fab.classList.remove("qw-hidden");
    els.transcript.innerHTML = "";
    els.timer.textContent = "00:00";
    els.avatar.className = "qw-agent-avatar";
    renderedSegments.clear();
    isMuted = false;
    els.muteBtn.innerHTML = ICON.mic;
    els.muteBtn.classList.remove("qw-muted");

    emit("end");
  }

  function updateCallTimer() {
    if (!callStartTime) return;
    const elapsed = Math.floor((Date.now() - callStartTime) / 1000);
    const m = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const s = String(elapsed % 60).padStart(2, "0");
    els.timer.textContent = `${m}:${s}`;
  }

  // ───────────────────────────── Mute ─────────────────────────────
  async function toggleMute() {
    if (!room) return;
    isMuted = !isMuted;
    els.muteBtn.innerHTML = isMuted ? ICON.micOff : ICON.mic;
    els.muteBtn.classList.toggle("qw-muted", isMuted);

    try {
      await room.localParticipant.setMicrophoneEnabled(!isMuted);
    } catch (err) {
      console.error("[Qifaw Voice Agent] mute failed", err);
    }

    setCallStatus(isMuted ? "connected" : "listening", isMuted ? "Muted" : "Listening...");
  }

  // ───────────────────────────── Status & Transcript ─────────────────────────────
  function setCallStatus(state, text) {
    els.status.textContent = text;
    els.status.className = "qw-call-status";
    els.avatar.className = "qw-agent-avatar";

    switch (state) {
      case "listening":
        els.status.classList.add("qw-listening");
        els.avatar.classList.add("qw-listening");
        break;
      case "thinking":
        els.status.classList.add("qw-thinking");
        els.avatar.classList.add("qw-thinking");
        break;
      case "speaking":
        els.status.classList.add("qw-speaking");
        els.avatar.classList.add("qw-speaking");
        break;
    }
  }

  function showTranscript(role, text, segmentId) {
    const clean = text.replace(/[*#_`]/g, "").replace(/^\s*-\s+/gm, "");
    if (!clean.trim()) return;

    if (role === "user") {
      els.transcript.innerHTML =
        `<div style="opacity:0.6; font-size:12px; margin-bottom:4px;">You</div>` +
        `<div>${escapeHtml(clean)}</div>`;
    } else if (role === "agent") {
      els.transcript.innerHTML = `<div>${escapeHtml(clean)}</div>`;
    } else {
      els.transcript.innerHTML = `<div style="opacity:0.5; font-size:13px;">${escapeHtml(clean)}</div>`;
    }

    if (!els.chatLog) return;

    const labelClass = role === "user" ? "qw-you" : role === "agent" ? "qw-agent" : "qw-system";
    const labelText = role === "user" ? "You" : role === "agent" ? "Voice Agent" : "System";

    // Same segment growing as it is spoken: replace its text in place.
    const existing = segmentId ? renderedSegments.get(segmentId) : null;
    if (existing && existing.isConnected) {
      existing.querySelector(".qw-log-text").textContent = clean;
      els.chatLog.scrollTop = els.chatLog.scrollHeight;
      return;
    }

    const time = new Date().toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    const msgEl = document.createElement("div");
    msgEl.className = "qw-log-msg";
    msgEl.innerHTML = `
      <div class="qw-log-label ${labelClass}">${labelText}</div>
      <div class="qw-log-text">${escapeHtml(clean)}</div>
      <div class="qw-log-time">${time}</div>
    `;
    els.chatLog.appendChild(msgEl);
    els.chatLog.scrollTop = els.chatLog.scrollHeight;
    if (segmentId) renderedSegments.set(segmentId, msgEl);
  }

  // ───────────────────────────── Waveform ─────────────────────────────
  // Driven by the microphone track LiveKit is publishing, so it reflects what
  // the agent is actually hearing.
  function localMicStream() {
    const lp = room?.localParticipant;
    if (!lp) return null;
    const pub =
      lp.getTrackPublication?.(LK.Track.Source.Microphone) ??
      lp.getTrack?.(LK.Track.Source.Microphone);
    const mst = pub?.track?.mediaStreamTrack;
    return mst ? new MediaStream([mst]) : null;
  }

  function startWaveform() {
    // The waveform exists only to be looked at. With no call screen the canvas
    // is detached, so offsetWidth is 0 and the animation loop would redraw an
    // empty bitmap for the whole call.
    if (CONFIG.ui === "none") return;

    const stream = localMicStream();
    if (!stream) return;

    try {
      audioContext = new (window.AudioContext || window.webkitAudioContext)();
      const source = audioContext.createMediaStreamSource(stream);
      analyserNode = audioContext.createAnalyser();
      analyserNode.fftSize = 256;
      source.connect(analyserNode);
    } catch {
      analyserNode = null;
      return;
    }

    const canvas = els.canvas;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    canvas.width = canvas.offsetWidth * dpr;
    canvas.height = canvas.offsetHeight * dpr;
    ctx.scale(dpr, dpr);

    const W = canvas.offsetWidth;
    const H = canvas.offsetHeight;
    const bufLen = analyserNode.frequencyBinCount;
    const dataArray = new Uint8Array(bufLen);

    function draw() {
      if (!isCallActive || !analyserNode) return;
      animFrameId = requestAnimationFrame(draw);

      analyserNode.getByteTimeDomainData(dataArray);
      ctx.clearRect(0, 0, W, H);

      const barCount = 48;
      const barWidth = (W / barCount) * 0.6;
      const gap = W / barCount;

      for (let i = 0; i < barCount; i++) {
        const idx = Math.floor((i / barCount) * bufLen);
        const v = (dataArray[idx] - 128) / 128; // -1 to 1
        const h = Math.max(3, Math.abs(v) * H * 0.9);

        const x = i * gap + (gap - barWidth) / 2;
        const y = (H - h) / 2;

        const intensity = Math.abs(v);
        const r = Math.round(108 + intensity * 147);
        const g = Math.round(99 - intensity * 28);
        const b = 255;
        ctx.fillStyle = `rgba(${r}, ${g}, ${b}, ${0.4 + intensity * 0.6})`;
        ctx.beginPath();
        ctx.roundRect(x, y, barWidth, h, barWidth / 2);
        ctx.fill();
      }
    }

    draw();
  }

  function stopWaveform() {
    if (animFrameId) {
      cancelAnimationFrame(animFrameId);
      animFrameId = null;
    }
    if (els.canvas) {
      const ctx = els.canvas.getContext("2d");
      ctx.clearRect(0, 0, els.canvas.width, els.canvas.height);
    }
  }

  // ───────────────────────────── Language ─────────────────────────────
  // The agent's STT and TTS are chosen from the token, so switching language
  // means a new token and a new room. Mid-call it is a quick reconnect.
  function buildLangOptions() {
    const langMap = {
      en: { name: "English", flag: "🇺🇸" },
      ta: { name: "Tamil", flag: "🇮🇳" },
      te: { name: "Telugu", flag: "🇮🇳" },
      kn: { name: "Kannada", flag: "🇮🇳" },
      ml: { name: "Malayalam", flag: "🇮🇳" },
    };

    els.langDrop.innerHTML = Object.entries(langMap)
      .map(
        ([code, meta]) =>
          `<div class="qw-lang-dropdown-opt ${code === currentLanguage ? "qw-active" : ""}" data-lang="${code}">
            <span>${meta.flag}</span><span>${meta.name}</span>
          </div>`,
      )
      .join("");

    els.langDrop.querySelectorAll(".qw-lang-dropdown-opt").forEach((opt) => {
      opt.addEventListener("click", async () => {
        const lang = opt.dataset.lang;
        els.langDrop.classList.remove("qw-visible");
        if (lang === currentLanguage) return;

        currentLanguage = lang;
        els.langBtn.textContent = lang.toUpperCase();
        buildLangOptions();

        if (!isCallActive) return;

        setCallStatus("connecting", "Switching language...");
        try {
          if (room) {
            try { room.disconnect(); } catch { /* already gone */ }
            room = null;
          }
          document.getElementById("qifaw-agent-audio")?.remove();
          stopWaveform();
          const tokenRes = await fetchToken(currentLanguage);
          await connectRoom(resolveLivekitUrl(tokenRes.livekit_url), tokenRes.token);
        } catch (err) {
          console.error("[Qifaw Voice Agent]", err);
          setCallStatus("error", err.message || "Language switch failed");
          setTimeout(endCall, 3000);
        }
      });
    });

    els.langBtn.textContent = currentLanguage.toUpperCase();
  }

  // ───────────────────────────── Helpers ─────────────────────────────
  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ───────────────────────────── Public API ─────────────────────────────
  // A host page that replaces the floating button with its own branded card
  // needs two things: a way to start the call from its own click handler, and
  // a way to hear that the call ended so it can show that card again.
  const listeners = { start: [], connected: [], speaking: [], end: [] };

  function emit(event, ...args) {
    for (const fn of (listeners[event] || []).slice()) {
      try {
        fn(...args);
      } catch (err) {
        console.error("[Qifaw Voice Agent] listener failed", err);
      }
    }
  }

  // Published at the end of init, never before: its existence is the host's
  // signal that the widget is built and start() is safe to call.
  function publishApi() {
    window.QifawVoice = {
      start: startCall,
      stop: endCall,
      isActive: () => isCallActive,
      on(event, fn) {
        if (!listeners[event]) return () => {};
        listeners[event].push(fn);
        return () => {
          const i = listeners[event].indexOf(fn);
          if (i >= 0) listeners[event].splice(i, 1);
        };
      },
    };
  }

  // ───────────────────────────── Init ─────────────────────────────
  function init() {
    if (!CONFIG.tenantId || !CONFIG.apiKey) {
      console.error("[Qifaw Voice Agent] Missing data-tenant-id or data-api-key.");
      return;
    }
    injectStyles();
    buildWidget();
    publishApi();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

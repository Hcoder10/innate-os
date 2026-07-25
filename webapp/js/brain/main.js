// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Brain page — a live window into the local Gemini agent loop.
//
// Deep telemetry rides /brain/trace (JSON on std_msgs/String, published by
// brain_client's BrainAgent): turn lifecycle with the exact frame the model
// saw, tool calls with args + outcomes, think latencies, the event queue, and
// a 1 Hz snapshot heartbeat. Everything else (mind stream, skill runs, pose,
// live camera fallback) comes from the public topics, so the page degrades
// gracefully on robots without the trace publisher — the deep panels just
// stay quiet.
//
// ?demo drives the whole page from a scripted synthetic brain (demo.js) —
// useful for UI work with no robot.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";

const CAM_TOPIC = "/mars/main_camera/left/image_raw/compressed";
const HIST_MAX = 60; // brain_client default history_max_entries

/** @type {Record<string, string>} */
const PHASE_LABEL = {
  off: "OFFLINE",
  idle: "STANDBY",
  look: "LOOK",
  think: "THINK",
  act: "ACT",
  wait: "NEXT LOOK",
  error: "RETRY IN",
};
/** @type {Record<string, string>} */
const PHASE_INK = { look: "var(--br-aqua)", think: "var(--br-violet)", act: "var(--br-orange)", error: "var(--br-critical)" };
const NODE_ANGLE = { look: -90, think: 30, act: 150 };
const RING_R = 112;
const RING_C = 2 * Math.PI * RING_R;

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "brain-page", buildView);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildView(root) {
  root.innerHTML = template();
  /** @param {string} sel */
  const $ = (sel) => /** @type {HTMLElement} */ (root.querySelector(sel));

  // ---- live state ----------------------------------------------------------
  const S = {
    phase: "off",
    phaseAt: performance.now(),
    turn: 0,
    waitUntil: 0,
    waitTotal: 0,
    streak: 0,
    /** @type {{turn: number, s: number}[]} */
    latencies: [],
    history: 0,
    uptime: 0,
    uptimeAt: 0,
    /** @type {string | null} */
    running: null,
    haveTrace: false,
    cometAngle: -90,
  };

  // ---- the loop ------------------------------------------------------------
  for (const [n, a] of Object.entries(NODE_ANGLE)) {
    const rad = (a * Math.PI) / 180;
    $(`.br-node.${n}`).setAttribute(
      "transform",
      `translate(${150 + RING_R * Math.cos(rad)}, ${150 + RING_R * Math.sin(rad)})`,
    );
  }
  $(".br-ring-count").style.strokeDasharray = String(RING_C);

  /** @param {string} p */
  function setPhase(p) {
    if (S.phase === p) return;
    S.phase = p;
    S.phaseAt = performance.now();
    for (const n of ["look", "think", "act"]) $(`.br-node.${n}`).classList.toggle("active", p === n);
    $(".br-comet").style.opacity = p === "think" ? "1" : "0";
    for (const t of root.querySelectorAll(".br-trail")) /** @type {HTMLElement} */ (t).style.opacity = p === "think" ? "0.45" : "0";
    $(".br-ring-count").style.stroke = p === "error" ? "var(--br-critical)" : p === "wait" ? "var(--br-aqua)" : "transparent";
    $(".br-phase").textContent = PHASE_LABEL[p] ?? p;
    $(".br-phase").style.fill = PHASE_INK[p] || "var(--muted)";
  }

  // ---- handlers (fed by ros subscriptions or the demo driver) --------------
  /** @param {any} d */
  function onTrace(d) {
    S.haveTrace = true;
    if (d.ev === "turn_start") {
      S.turn = d.turn;
      $(".br-tools").textContent = d.tools ? `${d.tools.length} tool${d.tools.length === 1 ? "" : "s"} armed` : "";
      if (d.frame) {
        showFrame("data:image/jpeg;base64," + d.frame, `what the brain saw · turn ${d.turn}`);
        sweep();
      }
      hidePing();
      drainQueue();
      setPhase("look");
      window.setTimeout(() => {
        if (S.phase === "look") setPhase("think");
      }, 550);
    }
    if (d.ev === "turn_end") {
      S.streak = 0;
      renderStreak();
      S.history = d.history;
      S.latencies.push({ turn: d.turn, s: d.latency });
      if (S.latencies.length > 60) S.latencies.shift();
      addTurnRow(d);
      renderVitals();
      const point = (d.calls || []).find((/** @type {any} */ c) => c.name === "go_to_point");
      if (point?.args) showPing(point.args.x, point.args.y);
      const acted = (d.calls || []).some((/** @type {any} */ c) => c.name !== "wait") || d.speech;
      setPhase(acted ? "act" : "wait");
      S.waitUntil = performance.now() + d.next_in * 1000;
      S.waitTotal = d.next_in * 1000;
      if (acted)
        window.setTimeout(() => {
          if (S.phase === "act") setPhase("wait");
        }, 1100);
    }
    if (d.ev === "turn_error") {
      S.streak = d.streak;
      renderStreak();
      setPhase("error");
      S.waitUntil = performance.now() + d.backoff * 1000;
      S.waitTotal = d.backoff * 1000;
      const sub = $(".br-loop-sub");
      sub.textContent = trunc(d.error, 90);
      sub.classList.add("err");
    }
    if (d.ev === "event") {
      addQueueCard(d.kind, d.text);
      if (d.kind === "user") addMsg("user", "USER", d.text.replace(/^The user says: "(.*)"$/s, "$1"));
    }
    if (d.ev === "snapshot") onSnapshot(d);
  }

  /** @param {any} d */
  function onSnapshot(d) {
    S.history = d.history;
    S.streak = d.streak;
    S.uptime = d.uptime;
    S.uptimeAt = performance.now();
    S.running = d.running;
    $(".br-chip-backend b").textContent = d.backend || "—";
    $(".br-chip-model b").textContent = d.model || "—";
    renderQueue(d.queued || []);
    renderStreak();
    renderVitals();
    if (!d.active) {
      setPhase("off");
      return;
    }
    if (d.in_flight) {
      if (S.phase !== "think" && S.phase !== "look") setPhase("think");
    } else if (S.phase === "off" || S.phase === "idle") {
      setPhase("wait");
      S.waitUntil = performance.now() + d.next_in * 1000;
      S.waitTotal = Math.max(d.next_in * 1000, 1);
    }
  }

  /** @param {any} d */
  function onAgentStatus(d) {
    $(".br-chip-active b").textContent = d.brain_active ? "active" : "stopped";
    $(".br-chip-active").className = "br-chip br-chip-active " + (d.brain_active ? "live" : "off");
    $(".br-chip-directive b").textContent = d.current_directive || "—";
    if (!d.brain_active) setPhase("off");
    else if (S.phase === "off") setPhase("idle");
  }

  /** @param {any} d */
  function onBrainStatus(d) {
    if (!S.haveTrace) $(".br-chip-backend b").textContent = d.backend || "—";
  }

  /** @param {any} d */
  function onChat(d) {
    if (d.sender === "robot_thoughts") addMsg("thought", "THINKS", d.text, d.timestamp);
    else if (d.sender === "robot") addMsg("speech", "SAYS", d.text, d.timestamp);
    else if (d.sender === "system") addMsg("system", "SYSTEM", d.text, d.timestamp);
    else if (d.sender === "skill_output") addMsg("system", "SKILL OUTPUT", d.text, d.timestamp);
  }

  /** @param {number} x @param {number} y @param {number} yaw */
  function setPose(x, y, yaw) {
    $(".br-pose-txt").textContent = `x ${x.toFixed(2)}  y ${y.toFixed(2)}  θ ${((yaw * 180) / Math.PI).toFixed(0)}°`;
    $(".br-needle").style.transform = `rotate(${(-yaw * 180) / Math.PI}deg)`;
  }

  /** @param {boolean} on */
  function setSpeaking(on) {
    $(".br-chip-voice").classList.toggle("talk", on);
  }

  // ---- renderers -----------------------------------------------------------
  /** @param {string | undefined} s @param {number} n */
  const trunc = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");
  /** @param {number} [ts] */
  const hhmmss = (ts) => new Date((ts ?? Date.now() / 1000) * 1000).toLocaleTimeString([], { hour12: false });

  /** @param {string} panelSel @param {HTMLElement} el @param {number} [cap] */
  function prepend(panelSel, el, cap = 60) {
    const feed = $(panelSel);
    feed.querySelector(".br-empty")?.remove();
    feed.prepend(el);
    while (feed.children.length > cap) /** @type {Element} */ (feed.lastChild).remove();
  }

  /** @param {string} cls @param {string} who @param {string} text @param {number} [ts] */
  function addMsg(cls, who, text, ts) {
    const div = document.createElement("div");
    div.className = "br-msg " + cls;
    div.innerHTML = `<div class="who">${who}<span class="when">${hhmmss(ts)}</span></div><div class="txt"></div>`;
    /** @type {HTMLElement} */ (div.querySelector(".txt")).textContent = text;
    prepend(".br-mind", div);
  }

  /** @param {any} d */
  function addTurnRow(d) {
    const div = document.createElement("div");
    div.className = "br-turn-row";
    const calls = (d.calls || [])
      .map((/** @type {any} */ c) => {
        const bad = /^(rejected|unknown|could not)/.test(c.outcome || "");
        const quiet = c.name === "wait";
        const args =
          c.args && Object.keys(c.args).length
            ? "(" +
              Object.entries(c.args)
                .map(([k, v]) => `${k}:${JSON.stringify(v)}`)
                .join(" ") +
              ")"
            : "";
        const title = (c.outcome || "").replace(/"/g, "&quot;");
        return `<span class="br-call ${bad ? "bad" : quiet ? "quiet" : "go"}" title="${title}">${c.name}${trunc(args, 46)}</span>`;
      })
      .join("");
    div.innerHTML =
      `<div class="head"><b>turn ${d.turn}</b><span>${(d.calls || []).length ? "" : "observed"}</span>` +
      `<span class="lat">${d.latency.toFixed(1)}s think</span></div>` +
      (calls ? `<div class="calls">${calls}</div>` : "");
    prepend(".br-actions", div);
  }

  /** @type {Map<string, HTMLElement>} */
  const skillRows = new Map();
  /** @param {any} d */
  function onSkill(d) {
    const key = d.primitive_id || d.skill_name;
    let row = skillRows.get(key);
    if (!row) {
      row = document.createElement("div");
      skillRows.set(key, row);
      prepend(".br-actions", row);
      if (skillRows.size > 40) skillRows.delete(/** @type {string} */ (skillRows.keys().next().value));
    }
    row.className = "br-skill-row " + d.status;
    row.innerHTML =
      `<span class="st"></span><span>${d.skill_name || d.primitive_name}</span>` +
      (d.reason ? `<span class="reason">${trunc(d.reason, 60)}</span>` : "") +
      `<span class="status">${d.status}</span>`;
  }

  /** @param {string} kind @param {string} text */
  function addQueueCard(kind, text) {
    const div = document.createElement("div");
    div.className = "br-qcard " + (kind === "user" ? "user" : "");
    div.innerHTML = `<div class="k">${kind}</div><div class="t"></div>`;
    /** @type {HTMLElement} */ (div.querySelector(".t")).textContent = trunc(text, 160);
    prepend(".br-queue", div, 20);
  }

  /** @param {{kind: string, text: string}[]} queued */
  function renderQueue(queued) {
    const feed = $(".br-queue");
    feed.innerHTML = "";
    if (!queued.length) {
      feed.innerHTML = `<div class="br-empty">quiet — heartbeat looks only</div>`;
      return;
    }
    queued.forEach((e) => addQueueCard(e.kind, e.text));
  }

  function drainQueue() {
    for (const c of root.querySelectorAll(".br-qcard")) c.classList.add("drain");
    window.setTimeout(() => renderQueue([]), 420);
  }

  function renderStreak() {
    $(".br-streak").innerHTML = "<i></i>".repeat(Math.min(S.streak, 8));
  }

  function renderVitals() {
    $(".br-tile-turns").textContent = String(S.turn);
    const L = S.latencies;
    $(".br-tile-last").textContent = L.length ? L[L.length - 1].s.toFixed(1) : "—";
    $(".br-tile-avg").textContent = L.length ? (L.reduce((a, b) => a + b.s, 0) / L.length).toFixed(1) : "—";
    $(".br-hist-fill").style.width = Math.min(100, (S.history / HIST_MAX) * 100) + "%";
    $(".br-hist-txt").textContent = `${S.history} / ${HIST_MAX}`;
    drawSpark();
  }

  // Single-series sparkline (the panel title names it); hover for values.
  /** @type {{x: number, y: number, turn: number, s: number}[]} */
  let sparkPts = [];
  function drawSpark() {
    const svg = $(".br-spark");
    const L = S.latencies.slice(-40);
    if (L.length < 2) {
      svg.innerHTML = "";
      sparkPts = [];
      return;
    }
    const W = 400, H = 88, PAD = 6;
    const max = Math.max(...L.map((p) => p.s), 1);
    sparkPts = L.map((p, i) => ({
      x: PAD + (i * (W - 2 * PAD)) / (L.length - 1),
      y: H - PAD - (p.s / max) * (H - 2 * PAD),
      ...p,
    }));
    const line = sparkPts.map((p) => `${p.x},${p.y}`).join(" ");
    svg.innerHTML =
      `<polygon class="area" points="${PAD},${H - PAD} ${line} ${W - PAD},${H - PAD}"/>` +
      `<polyline class="line" points="${line}"/>` +
      `<circle class="dot" r="4" style="display:none"/>`;
  }

  $(".br-spark-wrap").addEventListener("mousemove", (e) => {
    if (!sparkPts.length) return;
    const rect = $(".br-spark").getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * 400;
    const p = sparkPts.reduce((a, b) => (Math.abs(b.x - x) < Math.abs(a.x - x) ? b : a));
    const dot = $(".br-spark .dot");
    if (dot) {
      dot.style.display = "";
      dot.setAttribute("cx", String(p.x));
      dot.setAttribute("cy", String(p.y));
    }
    const tip = $(".br-spark-tip");
    tip.style.display = "block";
    tip.style.left = (p.x / 400) * rect.width + "px";
    tip.style.top = (p.y / 88) * rect.height + "px";
    tip.textContent = `turn ${p.turn} · ${p.s.toFixed(2)}s`;
  });
  $(".br-spark-wrap").addEventListener("mouseleave", () => {
    $(".br-spark-tip").style.display = "none";
    const dot = $(".br-spark .dot");
    if (dot) dot.style.display = "none";
  });

  // ---- vision --------------------------------------------------------------
  /** @param {string} src @param {string} caption */
  function showFrame(src, caption) {
    /** @type {HTMLImageElement} */ ($(".br-frame")).src = src;
    $(".br-stage-idle").style.display = "none";
    $(".br-frame-cap").innerHTML = `<b>${caption}</b>`;
    $(".br-vision-src").textContent = S.haveTrace ? "turn-exact frames · /brain/trace" : "live · " + CAM_TOPIC;
  }

  function sweep() {
    const scan = $(".br-scan");
    scan.classList.remove("run");
    void scan.offsetWidth;
    scan.classList.add("run");
  }

  let pingTimer = 0;
  /** @param {number} x1000 @param {number} y1000 */
  function showPing(x1000, y1000) {
    // Position against the contain-fitted image, not the (letterboxed) stage.
    const img = /** @type {HTMLImageElement} */ ($(".br-frame"));
    const stage = $(".br-stage");
    const iw = img.naturalWidth || 4, ih = img.naturalHeight || 3;
    const scale = Math.min(stage.clientWidth / iw, stage.clientHeight / ih);
    const w = iw * scale, h = ih * scale;
    const ping = $(".br-ping");
    ping.style.left = (stage.clientWidth - w) / 2 + (x1000 / 1000) * w + "px";
    ping.style.top = (stage.clientHeight - h) / 2 + (y1000 / 1000) * h + "px";
    ping.classList.add("on");
    window.clearTimeout(pingTimer);
    pingTimer = window.setTimeout(hidePing, 8000);
  }
  function hidePing() {
    $(".br-ping").classList.remove("on");
  }

  // ---- animation loop ------------------------------------------------------
  let raf = 0;
  function tickUI() {
    const t = performance.now();
    const timer = $(".br-timer");
    const ring = $(".br-ring-count");

    if (S.phase === "think") {
      S.cometAngle += 2.6;
      const rad = (S.cometAngle * Math.PI) / 180;
      const set = (/** @type {string} */ sel, /** @type {number} */ lag) => {
        const lr = ((S.cometAngle - lag) * Math.PI) / 180;
        const el = $(sel);
        el.setAttribute("cx", String(150 + RING_R * Math.cos(lag ? lr : rad)));
        el.setAttribute("cy", String(150 + RING_R * Math.sin(lag ? lr : rad)));
      };
      set(".br-comet", 0);
      set(".br-trail.t1", 9);
      set(".br-trail.t2", 17);
      timer.textContent = ((t - S.phaseAt) / 1000).toFixed(1);
      ring.style.strokeDashoffset = "0";
      ring.style.stroke = "transparent";
    } else if (S.phase === "wait" || S.phase === "error") {
      const left = Math.max(0, S.waitUntil - t);
      timer.textContent = (left / 1000).toFixed(1);
      ring.style.strokeDashoffset = String(RING_C * (1 - left / Math.max(S.waitTotal, 1)));
    } else if (S.phase === "look" || S.phase === "act") {
      timer.textContent = "·";
    } else {
      timer.textContent = "—";
    }

    const sub = $(".br-loop-sub");
    if (S.phase !== "error" && sub.classList.contains("err") && S.streak === 0) {
      sub.textContent = "";
      sub.classList.remove("err");
    }
    if (S.running) {
      sub.textContent = "▶ " + S.running;
      sub.classList.remove("err");
    } else if (S.phase !== "error" && sub.textContent.startsWith("▶")) sub.textContent = "";

    $(".br-turn").textContent = "TURN " + S.turn;
    if (S.uptimeAt) {
      const up = S.uptime + (t - S.uptimeAt) / 1000;
      $(".br-tile-up").textContent =
        up >= 3600
          ? (up / 3600).toFixed(1) + "h"
          : up >= 60
            ? Math.floor(up / 60) + "m" + String(Math.floor(up % 60)).padStart(2, "0")
            : Math.floor(up) + "s";
    }
    raf = requestAnimationFrame(tickUI);
  }
  raf = requestAnimationFrame(tickUI);

  // ---- data in -------------------------------------------------------------
  /** @param {any} msg */
  const asJson = (msg) => {
    try {
      return JSON.parse(msg.data);
    } catch {
      return null;
    }
  };
  /** @param {(d: any) => void} fn */
  const jsonHandler = (fn) => (/** @type {any} */ msg) => {
    const d = asJson(msg);
    if (d) fn(d);
  };

  const handlers = { onTrace, onChat, onSkill, onAgentStatus, onBrainStatus, setPose, setSpeaking };

  /** @type {(() => void)[]} */
  let unsubs = [];
  /** @type {(() => void) | null} */
  let demoStop = null;

  if (new URLSearchParams(location.search).has("demo")) {
    // No robot in the picture: the socket's connect card would just sit over
    // the synthetic show.
    root.parentElement?.querySelector(".connect-layer")?.classList.add("br-hidden");
    void import("./demo.js").then((m) => {
      demoStop = m.startDemo(handlers);
    });
  } else {
    unsubs = [
      ros.subscribe("/brain/trace", jsonHandler(onTrace), 0, "std_msgs/msg/String"),
      ros.subscribe("/brain/agent_status", jsonHandler(onAgentStatus), 0, "std_msgs/msg/String"),
      ros.subscribe("/brain/websocket_status", jsonHandler(onBrainStatus), 0, "std_msgs/msg/String"),
      ros.subscribe("/brain/chat_out", jsonHandler(onChat), 0, "std_msgs/msg/String"),
      ros.subscribe("/brain/skill_status_update", jsonHandler(onSkill), 0, "std_msgs/msg/String"),
      ros.subscribe("/tts/is_playing", (msg) => setSpeaking(msg.data === "true"), 0, "std_msgs/msg/String"),
      ros.subscribe(
        "/odom",
        (msg) => {
          const p = msg.pose.pose.position, q = msg.pose.pose.orientation;
          setPose(p.x, p.y, Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)));
        },
        200,
        "nav_msgs/msg/Odometry",
      ),
      ros.subscribe(
        CAM_TOPIC,
        (msg) => {
          if (!S.haveTrace) showFrame("data:image/jpeg;base64," + msg.data, "live camera");
        },
        500,
        "sensor_msgs/msg/CompressedImage",
      ),
    ];
  }

  return {
    destroy() {
      cancelAnimationFrame(raf);
      window.clearTimeout(pingTimer);
      unsubs.forEach((u) => u());
      demoStop?.();
      root.innerHTML = "";
    },
  };
}

function template() {
  return `
  <div class="br-head">
    <h1 class="page-title">Brain</h1>
    <div class="br-chips">
      <div class="br-chip br-chip-active"><span class="led"></span><span>brain <b>—</b></span></div>
      <div class="br-chip br-chip-directive"><span>directive</span><b>—</b></div>
      <div class="br-chip br-chip-backend"><span>backend</span><b>—</b></div>
      <div class="br-chip br-chip-model"><span>model</span><b>—</b></div>
      <div class="br-chip br-chip-voice"><span class="led"></span><span>voice</span></div>
    </div>
  </div>
  <div class="br-grid">
    <section class="br-panel br-panel-loop">
      <h2>The agent loop <span class="sub br-tools"></span></h2>
      <svg class="br-loop-svg" viewBox="0 0 300 300">
        <circle class="br-ring-base" cx="150" cy="150" r="${RING_R}"/>
        <circle class="br-ring-count" cx="150" cy="150" r="${RING_R}"/>
        <circle class="br-trail t2" r="2.5"/>
        <circle class="br-trail t1" r="3.5"/>
        <circle class="br-comet" r="5"/>
        <g class="br-node look"><circle r="27"/><text dy="4">LOOK</text></g>
        <g class="br-node think"><circle r="27"/><text dy="4">THINK</text></g>
        <g class="br-node act"><circle r="27"/><text dy="4">ACT</text></g>
        <g class="br-center">
          <text class="br-phase" x="150" y="130">OFFLINE</text>
          <text class="br-timer" x="150" y="168">—</text>
          <text class="br-turn"  x="150" y="192">TURN 0</text>
        </g>
      </svg>
      <div class="br-loop-sub"></div>
      <div class="br-streak"></div>
    </section>

    <section class="br-panel br-panel-vision">
      <h2>Robot vision <span class="sub br-vision-src">waiting for frames</span></h2>
      <div class="br-stage">
        <img class="br-frame" alt="">
        <div class="br-stage-idle">NO SIGNAL</div>
        <div class="br-scan"></div>
        <div class="br-ping"><span class="ringA"></span><span class="ringB"></span><span class="cross"></span></div>
      </div>
      <div class="br-vision-cap">
        <span class="br-frame-cap"></span>
        <span class="br-pose-strip">
          <svg class="br-heading" viewBox="0 0 16 16"><polygon class="br-needle" points="8,1.5 11,12 8,9.5 5,12"/></svg>
          <span class="br-pose-txt">pose —</span>
        </span>
      </div>
    </section>

    <section class="br-panel br-panel-mind">
      <h2>Mind stream <span class="sub">thoughts · speech · system</span></h2>
      <div class="br-feed br-mind"></div>
    </section>

    <section class="br-panel br-panel-actions">
      <h2>Turns &amp; actions <span class="sub">tool calls and skill runs</span></h2>
      <div class="br-feed br-actions"></div>
    </section>

    <section class="br-panel br-panel-queue">
      <h2>Event queue <span class="sub">what the next look will carry</span></h2>
      <div class="br-feed br-queue"><div class="br-empty">quiet — heartbeat looks only</div></div>
    </section>

    <section class="br-panel br-panel-vitals">
      <h2>Vitals <span class="sub">think latency · s</span></h2>
      <div class="br-tiles">
        <div class="br-tile"><div class="v br-tile-turns">0</div><div class="l">turns</div></div>
        <div class="br-tile"><div class="v br-tile-last">—</div><div class="l">last think</div></div>
        <div class="br-tile"><div class="v br-tile-avg">—</div><div class="l">avg think</div></div>
        <div class="br-tile"><div class="v br-tile-up">—</div><div class="l">uptime</div></div>
      </div>
      <div class="br-spark-wrap">
        <svg class="br-spark" viewBox="0 0 400 88" preserveAspectRatio="none"></svg>
        <div class="br-spark-tip"></div>
      </div>
      <div class="br-gauge">
        <div class="bar"><div class="fill br-hist-fill"></div></div>
        <div class="lab"><span>conversation history</span><span class="br-hist-txt">0 / ${HIST_MAX}</span></div>
      </div>
    </section>
  </div>`;
}

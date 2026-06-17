// @ts-check
// Episode player — MP4-backed replay with synced telemetry. One <video> per
// camera (main + wrist), served same-origin from the front door with HTTP
// Range, so scrubbing is native and instant (no WebRTC, no robot-side replay).
// The transport bar and the joint graph are driven by the master video's
// currentTime; the other cameras are slaved to it. Joint data comes from
// GET /episode/joints.

import { SET_EPISODE_OUTCOME_SERVICE, DELETE_EPISODE_SERVICE } from "../constants.js";

const SVG_NS = "http://www.w3.org/2000/svg";
// Per-joint hues for the telemetry legend (cycled if there are more joints).
const JOINT_COLORS = ["#6fa8dc", "#d96a5a", "#e8a33d", "#56c28c", "#b48ead", "#c9b458", "#e88fb6"];
const SYNC_TOLERANCE = 0.15; // seconds of drift before nudging a slaved camera
const SPEEDS = [1, 2, 0.5]; // playback-rate cycle

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {Skill} skill
 * @param {EpisodeSummary} episode
 * @param {{ onBack: () => void, onPrev?: (() => void) | null, onNext?: (() => void) | null }} opts
 * @returns {{ destroy: () => void }}
 */
export function createEpisodePlayer(parent, ros, skill, episode, opts) {
  const dir = skill.directory || "";
  const id = numericId(episode); // service returns "episode_0"; the route wants 0
  const q = `dir=${encodeURIComponent(dir)}&id=${id}`;
  const cameras = camerasOf(episode, id);
  let fps = 30; // refined from the joints payload's data_frequency

  const wrap = document.createElement("div");
  wrap.className = "episode-player";

  // --- header: skill title (back link) + episode name + prev/next ----------
  // Mirrors the episode-list header style (.episodes-title / .episodes-sub).
  const header = document.createElement("div");
  header.className = "player-head";

  const headinfo = document.createElement("div");
  headinfo.className = "player-headinfo";
  const titleBtn = document.createElement("button");
  titleBtn.type = "button";
  titleBtn.className = "player-title episodes-title";
  titleBtn.textContent = skill.name;
  titleBtn.title = "Back to episodes";
  titleBtn.addEventListener("click", () => opts.onBack());

  const subrow = document.createElement("div");
  subrow.className = "player-subrow";
  const epName = document.createElement("span");
  epName.className = "episodes-sub";
  epName.textContent = `episode_${id}`;
  /** @param {string} sym @param {string} t @param {(() => void) | null | undefined} handler */
  const navButton = (sym, t, handler) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "player-navbtn";
    b.textContent = sym;
    b.title = t;
    if (handler) b.addEventListener("click", handler);
    else b.disabled = true;
    return b;
  };
  const nav = document.createElement("div");
  nav.className = "player-nav";
  nav.append(
    navButton("‹", "Previous episode", opts.onPrev),
    navButton("›", "Next episode", opts.onNext),
  );
  subrow.append(epName, nav);
  headinfo.append(titleBtn, subrow);

  // Right-side actions: outcome label + delete.
  const actions = document.createElement("div");
  actions.className = "player-actions";
  const label = document.createElement("select");
  const applyLabelClass = (/** @type {boolean} */ fail) =>
    (label.className = `ep-label-select ${fail ? "is-fail" : "is-success"}`);
  applyLabelClass(episode.outcome === "failure");
  for (const [val, text] of [
    ["success", "Successful"],
    ["failure", "Unsuccessful"],
  ]) {
    const o = document.createElement("option");
    o.value = val;
    o.textContent = text;
    if ((val === "failure") === (episode.outcome === "failure")) o.selected = true;
    label.appendChild(o);
  }
  label.addEventListener("change", () => {
    const outcome = /** @type {"success"|"failure"} */ (label.value);
    episode.outcome = outcome;
    applyLabelClass(outcome === "failure");
    ros.callService(SET_EPISODE_OUTCOME_SERVICE, {
      task_directory: dir,
      episode_id: id,
      outcome,
    }).catch((err) => console.error("[datasets] set outcome failed:", err));
  });
  const del = document.createElement("button");
  del.type = "button";
  del.className = "player-delete";
  del.innerHTML =
    '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/></svg><span>Delete</span>';
  del.addEventListener("click", () => {
    if (!window.confirm(`Delete episode_${id}? This permanently removes its video and data.`)) return;
    del.disabled = true;
    ros.callService(DELETE_EPISODE_SERVICE, { task_directory: dir, episode_id: id })
      .then((res) => {
        if (res && res.success === false) throw new Error(res.message || "delete failed");
        opts.onBack(); // returns to the list, which refreshes
      })
      .catch((err) => {
        del.disabled = false;
        window.alert(`Couldn't delete episode: ${err.message}`);
      });
  });
  actions.append(label, del);

  header.append(headinfo, actions);

  // --- video pair ----------------------------------------------------------
  const pair = document.createElement("div");
  pair.className = "video-pair";
  /** @type {HTMLVideoElement[]} */
  const videos = cameras.map((cam) => {
    const cell = document.createElement("div");
    cell.className = "video-cell";

    const label = document.createElement("span");
    label.className = "video-cell-label microlabel";
    const dot = document.createElement("span");
    dot.className = "video-dot";
    const name = document.createElement("span");
    name.textContent = cameraLabel(cam);
    label.append(name, dot);

    const v = document.createElement("video");
    v.src = `/episode?${q}&camera=${encodeURIComponent(cam)}`;
    v.muted = true;
    v.playsInline = true;
    v.preload = "auto";
    v.addEventListener("canplay", () => dot.classList.add("live"));
    // A guessed camera (no video_files) may not exist — hide its cell rather
    // than show a broken element. The first camera stays as the master.
    v.addEventListener("error", () => {
      if (cell !== pair.firstChild) cell.hidden = true;
    });

    cell.append(label, v);
    pair.appendChild(cell);
    return v;
  });
  const master = videos[0];

  // --- transport -----------------------------------------------------------
  const transport = document.createElement("div");
  transport.className = "episode-transport";
  const back10 = tbtn("«10", "Back 10s", () => seekBy(-10));
  const stepB = tbtn("‹1", "Back 1s", () => seekBy(-1));
  const playBtn = tbtn("▶", "Play / pause", togglePlay);
  playBtn.classList.add("transport-play");
  const stepF = tbtn("1›", "Forward 1s", () => seekBy(1));
  const toEnd = tbtn("›|", "Jump to end", () => seekAll(master.duration || 0));
  const speedBtn = tbtn("1×", "Playback speed", cycleSpeed);
  speedBtn.classList.add("transport-speed");
  const time = document.createElement("span");
  time.className = "transport-time mono";
  time.textContent = "0:00 / 0:00";
  transport.append(back10, stepB, playBtn, stepF, toEnd, speedBtn, time);

  // --- scrubber ------------------------------------------------------------
  const scrub = document.createElement("input");
  scrub.type = "range";
  scrub.className = "transport-scrub";
  scrub.min = "0";
  scrub.max = "1000";
  scrub.value = "0";
  scrub.setAttribute("aria-label", "Seek");

  // --- telemetry -----------------------------------------------------------
  const telemetry = document.createElement("div");
  telemetry.className = "telemetry";
  const telHead = document.createElement("div");
  telHead.className = "telemetry-head";
  const telLabel = document.createElement("span");
  telLabel.className = "microlabel";
  telLabel.textContent = "telemetry";
  const legend = document.createElement("div");
  legend.className = "telemetry-legend";
  telHead.append(telLabel, legend);
  const plot = document.createElement("div");
  plot.className = "telemetry-plot";
  telemetry.append(telHead, plot);

  wrap.append(header, pair, transport, scrub, telemetry);
  parent.appendChild(wrap);

  // ---- video group control -----------------------------------------------
  let scrubbing = false;
  let speedIdx = 0;

  /** @param {number} t */
  function seekAll(t) {
    const clamped = Math.max(0, t);
    for (const v of videos) {
      v.currentTime = Number.isFinite(v.duration) ? Math.min(clamped, v.duration) : clamped;
    }
  }
  /** @param {number} delta */
  function seekBy(delta) {
    seekAll((master.currentTime || 0) + delta);
  }
  function playAll() {
    for (const v of videos) v.play().catch(() => {});
  }
  function pauseAll() {
    for (const v of videos) v.pause();
  }
  function togglePlay() {
    if (master.paused) playAll();
    else pauseAll();
  }
  function cycleSpeed() {
    speedIdx = (speedIdx + 1) % SPEEDS.length;
    const rate = SPEEDS[speedIdx];
    for (const v of videos) v.playbackRate = rate;
    speedBtn.textContent = `${rate}×`;
  }

  master.addEventListener("play", () => (playBtn.textContent = "❚❚"));
  master.addEventListener("pause", () => (playBtn.textContent = "▶"));
  master.addEventListener("loadedmetadata", () => {
    time.textContent = `${fmt(0)} / ${fmt(master.duration)}`;
  });
  /** @param {number} v scrubber value 0..1000 */
  function setScrubFill(v) {
    scrub.style.setProperty("--pct", `${v / 10}%`);
  }
  setScrubFill(0);

  master.addEventListener("timeupdate", () => {
    const dur = master.duration || 0;
    if (!scrubbing && dur) {
      scrub.value = String(Math.round((master.currentTime / dur) * 1000));
      setScrubFill(Number(scrub.value));
    }
    time.textContent = `${fmt(master.currentTime)} / ${fmt(dur)}`;
    for (let i = 1; i < videos.length; i++) {
      if (Math.abs(videos[i].currentTime - master.currentTime) > SYNC_TOLERANCE) {
        videos[i].currentTime = master.currentTime;
      }
    }
    updatePlayhead(dur ? master.currentTime / dur : 0);
  });
  master.addEventListener("seeking", () => {
    for (let i = 1; i < videos.length; i++) videos[i].currentTime = master.currentTime;
  });

  scrub.addEventListener("input", () => {
    scrubbing = true;
    setScrubFill(Number(scrub.value));
    const dur = master.duration || 0;
    if (dur) seekAll((Number(scrub.value) / 1000) * dur);
  });
  scrub.addEventListener("change", () => (scrubbing = false));

  // ---- telemetry build + playhead -----------------------------------------
  let playLine = /** @type {SVGLineElement | null} */ (null);
  let headDot = /** @type {HTMLElement | null} */ (null);
  let hoverLine = /** @type {HTMLElement | null} */ (null);
  let tip = /** @type {HTMLElement | null} */ (null);
  let tipTime = /** @type {HTMLElement | null} */ (null);
  /** @type {HTMLElement[]} per-joint value cells in the hover tooltip */
  let tipVals = [];
  /** @type {number[][]} */
  let gQpos = [];
  /** @type {number[]} */
  let gTimes = [];
  let gMin = 0;
  let gSpan = 1;

  /** @param {number} frac 0..1 */
  function updatePlayhead(frac) {
    frac = Math.max(0, Math.min(1, frac));
    if (playLine) {
      const x = frac * 1000;
      playLine.setAttribute("x1", String(x));
      playLine.setAttribute("x2", String(x));
    }
    if (headDot && gQpos.length) {
      const idx = Math.round(frac * (gQpos.length - 1));
      const val = gQpos[idx] && gQpos[idx][0];
      if (typeof val === "number") {
        headDot.style.left = `${frac * 100}%`;
        headDot.style.top = `${(1 - (val - gMin) / gSpan) * 100}%`;
        headDot.hidden = false;
      }
    }
  }

  const jointsAbort = new AbortController();
  fetch(`/episode/joints?${q}`, { signal: jointsAbort.signal })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`joints ${r.status}`))))
    .then((/** @type {EpisodeJoints} */ data) => {
      if (data.data_frequency) fps = data.data_frequency;
      renderGraph(data);
    })
    .catch(() => {
      plot.innerHTML = '<p class="datasets-empty">Joint data unavailable.</p>';
    });

  /** @param {EpisodeJoints} data */
  function renderGraph(data) {
    const qpos = Array.isArray(data.qpos) ? data.qpos : [];
    if (qpos.length < 2 || !Array.isArray(qpos[0])) {
      plot.innerHTML = '<p class="datasets-empty">No joint trace recorded.</p>';
      return;
    }
    gQpos = qpos;
    gTimes = Array.isArray(data.timestamps) ? data.timestamps : [];
    const n = qpos.length;
    const numJoints = qpos[0].length;
    let min = Infinity;
    let max = -Infinity;
    for (const row of qpos) {
      for (const val of row) {
        if (val < min) min = val;
        if (val > max) max = val;
      }
    }
    gMin = min;
    gSpan = max - min || 1;

    // Legend (joint names), matching the colors used for the lines.
    const legendFrag = document.createDocumentFragment();
    for (let j = 0; j < numJoints; j++) {
      const item = document.createElement("span");
      item.className = "legend-item";
      const dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = JOINT_COLORS[j % JOINT_COLORS.length];
      const lbl = document.createElement("span");
      lbl.textContent = `J${j + 1}`;
      item.append(dot, lbl);
      legendFrag.appendChild(item);
    }
    legend.replaceChildren(legendFrag);

    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 1000 200");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("class", "telemetry-svg");

    for (let j = 0; j < numJoints; j++) {
      let d = "";
      for (let i = 0; i < n; i++) {
        const x = (i / (n - 1)) * 1000;
        const y = 200 - ((qpos[i][j] - min) / gSpan) * 200;
        d += `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)} `;
      }
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d.trim());
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", JOINT_COLORS[j % JOINT_COLORS.length]);
      path.setAttribute("stroke-width", "1.5");
      path.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(path);
    }

    playLine = document.createElementNS(SVG_NS, "line");
    playLine.setAttribute("x1", "0");
    playLine.setAttribute("x2", "0");
    playLine.setAttribute("y1", "0");
    playLine.setAttribute("y2", "200");
    playLine.setAttribute("class", "telemetry-playhead");
    playLine.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(playLine);

    // Round playhead marker (HTML overlay so it stays circular despite the
    // non-uniform SVG scaling); tracks the first joint's value.
    headDot = document.createElement("div");
    headDot.className = "telemetry-dot";
    headDot.hidden = true;

    // Hover guide + value tooltip (one color-keyed row per joint).
    hoverLine = document.createElement("div");
    hoverLine.className = "telemetry-hoverline";
    hoverLine.hidden = true;

    tip = document.createElement("div");
    tip.className = "telemetry-tip";
    tip.hidden = true;
    tipTime = document.createElement("div");
    tipTime.className = "tip-time mono";
    tip.appendChild(tipTime);
    tipVals = [];
    for (let j = 0; j < numJoints; j++) {
      const row = document.createElement("div");
      row.className = "tip-row";
      const dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = JOINT_COLORS[j % JOINT_COLORS.length];
      const lbl = document.createElement("span");
      lbl.className = "tip-label";
      lbl.textContent = `J${j + 1}`;
      const val = document.createElement("span");
      val.className = "tip-val mono";
      row.append(dot, lbl, val);
      tip.appendChild(row);
      tipVals.push(val);
    }

    plot.replaceChildren(svg, headDot, hoverLine, tip);
    plot.addEventListener("pointermove", onHover);
    plot.addEventListener("pointerleave", hideHover);
    updatePlayhead(0);
  }

  /** @param {PointerEvent} e */
  function onHover(e) {
    if (!gQpos.length || !tip || !hoverLine) return;
    const rect = plot.getBoundingClientRect();
    if (rect.width === 0) return;
    const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const idx = Math.round(frac * (gQpos.length - 1));
    const row = gQpos[idx] || [];

    hoverLine.style.left = `${frac * 100}%`;
    hoverLine.hidden = false;

    const t = gTimes.length > idx ? gTimes[idx] - gTimes[0] : idx / fps;
    if (tipTime) tipTime.textContent = `${t.toFixed(2)}s · #${idx}`;
    for (let j = 0; j < tipVals.length; j++) {
      tipVals[j].textContent = typeof row[j] === "number" ? row[j].toFixed(3) : "—";
    }
    tip.style.left = `${frac * 100}%`;
    tip.classList.toggle("flip", frac > 0.6);
    tip.hidden = false;
  }

  function hideHover() {
    if (hoverLine) hoverLine.hidden = true;
    if (tip) tip.hidden = true;
  }

  return {
    destroy() {
      jointsAbort.abort();
      for (const v of videos) {
        v.pause();
        v.removeAttribute("src");
        v.load(); // release the network/decoder
      }
      wrap.remove();
    },
  };
}

/** @param {string} text @param {string} title @param {() => void} onClick */
function tbtn(text, title, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "transport-btn";
  b.textContent = text;
  b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

/** camera_1 → "Main camera", camera_2 → "Wrist camera", else prettified.
 * @param {string} cam */
function cameraLabel(cam) {
  if (cam === "camera_1") return "Main camera";
  if (cam === "camera_2") return "Wrist camera";
  return cam.replace(/_/g, " ");
}

/** Numeric episode index from "episode_0" or "episode_0.h5". @param {EpisodeSummary} ep */
function numericId(ep) {
  const m = /(\d+)/.exec(String(ep.episode_id)) || /(\d+)/.exec(ep.file_name || "");
  return m ? Number(m[1]) : 0;
}

/** Camera names from video_files ("episode_3_camera_1.mp4" → "camera_1"), main
 * camera first; when video_files is absent, guess the two usual cameras (cells
 * that 404 hide themselves). @param {EpisodeSummary} ep @param {number} id */
function camerasOf(ep, id) {
  const files = ep.video_files || [];
  const prefix = `episode_${id}_`;
  const cams = files
    .map((f) => (f.startsWith(prefix) && f.endsWith(".mp4") ? f.slice(prefix.length, -4) : null))
    .filter(/** @returns {c is string} */ (c) => !!c)
    .sort();
  return cams.length ? cams : ["camera_1", "camera_2"];
}

/** @param {number} secs @returns {string} */
function fmt(secs) {
  if (!Number.isFinite(secs) || secs < 0) secs = 0;
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

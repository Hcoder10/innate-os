// @ts-check
// Episode list — the recorded episodes of the selected skill as a rich table
// (thumbnail, recorded time, duration, status), from /brain/recorder/get_task_metadata.
// Each episode is in one of three states:
//   ready    — has H.264 MP4s (video_files present) → click the row to replay
//   encoding — the dataset_encoder is converting it now (live status topic)
//   raw      — not yet converted → a "Prepare video" button queues encoding
// Thumbnails are lazily-loaded <video> frames from the same /episode route.

import {
  GET_TASK_METADATA_SERVICE,
  ENCODE_EPISODE_SERVICE,
  ENCODE_STATUS_TOPIC,
  SET_EPISODE_OUTCOME_SERVICE,
  DELETE_EPISODE_SERVICE,
} from "../constants.js";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Line-style icons, same stroke language as the rail (24×24, stroke currentColor).
const ICON_TRASH =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/></svg>';
const ICON_DOWNLOAD =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v10"/><path d="M8 11l4 4 4-4"/><path d="M5 19h14"/></svg>';

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ onOpen: (skill: Skill, episode: EpisodeSummary) => void }} opts
 * @returns {{ show: (skill: Skill) => void, neighbor: (ep: EpisodeSummary, delta: number) => EpisodeSummary | null, destroy: () => void }}
 */
export function createEpisodeList(parent, ros, opts) {
  const wrap = document.createElement("div");
  wrap.className = "episodes";

  // --- header: title + counts + toolbar -----------------------------------
  const head = document.createElement("div");
  head.className = "episodes-head";
  const headText = document.createElement("div");
  const title = document.createElement("h2");
  title.className = "episodes-title";
  const sub = document.createElement("p");
  sub.className = "episodes-sub";
  headText.append(title, sub);

  const tools = document.createElement("div");
  tools.className = "episodes-tools";
  const search = document.createElement("input");
  search.type = "search";
  search.className = "episodes-search";
  search.placeholder = "Search episodes";
  const sortBtn = document.createElement("button");
  sortBtn.type = "button";
  sortBtn.className = "episodes-tool";
  const refreshBtn = document.createElement("button");
  refreshBtn.type = "button";
  refreshBtn.className = "episodes-tool";
  refreshBtn.textContent = "↻";
  refreshBtn.title = "Refresh";
  // Jump to the Collect page with this dataset pre-selected to record more.
  const collectLink = document.createElement("a");
  collectLink.className = "episodes-tool episodes-collect";
  collectLink.textContent = "+ Collect";
  collectLink.title = "Record new episodes for this dataset";
  tools.append(search, sortBtn, refreshBtn, collectLink);
  head.append(headText, tools);

  // --- column header + scrolling rows --------------------------------------
  const colhead = document.createElement("div");
  colhead.className = "episodes-colhead";
  for (const label of ["Episode", "Recorded", "Duration", "Label", ""]) {
    const c = document.createElement("span");
    c.className = "col";
    c.textContent = label;
    colhead.appendChild(c);
  }

  const rowsEl = document.createElement("div");
  rowsEl.className = "episodes-rows";

  const foot = document.createElement("p");
  foot.className = "episodes-foot";

  wrap.append(head, colhead, rowsEl, foot);
  parent.appendChild(wrap);

  let requestSeq = 0;
  /** @type {Skill | null} */
  let current = null;
  /** @type {EpisodeSummary[]} */
  let episodes = [];
  let dataFreq = 0;
  /** @type {EncodeStatusMsg | null} */
  let encodeStatus = null;
  let query = "";
  let sortNewest = true;

  syncSortLabel();
  showIdle();

  const unsub = ros.subscribe(ENCODE_STATUS_TOPIC, (msg) => {
    if (!current || msg.task_directory !== current.directory) return;
    encodeStatus = msg;
    // Re-fetch metadata whenever this skill's encode completes — unconditionally,
    // not only if we witnessed the "encoding" phase (the topic isn't latched, so
    // a "done" can arrive without us having seen the in-progress updates). "done"
    // fires once per completion, so this is at most one extra fetch.
    if (msg.stage === "done") show(current);
    else render();
  });

  search.addEventListener("input", () => {
    query = search.value.trim();
    render();
  });
  sortBtn.addEventListener("click", () => {
    sortNewest = !sortNewest;
    syncSortLabel();
    render();
  });
  refreshBtn.addEventListener("click", () => current && show(current));

  function syncSortLabel() {
    sortBtn.textContent = `Sort: ${sortNewest ? "Newest" : "Oldest"}`;
  }

  /** @param {string} text */
  function showMessage(text) {
    const p = document.createElement("p");
    p.className = "datasets-empty";
    p.textContent = text;
    rowsEl.replaceChildren(p);
  }

  function showIdle() {
    title.textContent = "Datasets";
    sub.textContent = "Select a skill to browse its episodes.";
    tools.hidden = true;
    colhead.hidden = true;
    foot.textContent = "";
    showMessage("Pick a skill on the left to browse and replay its recorded episodes.");
  }

  /** @param {Skill} skill */
  function show(skill) {
    const seq = ++requestSeq;
    current = skill;
    tools.hidden = false;
    collectLink.href = `../collect/index.html?dir=${encodeURIComponent(skill.directory || "")}&name=${encodeURIComponent(skill.name)}`;
    title.textContent = skill.name;
    sub.textContent = "loading…";
    showMessage("Loading episodes…");

    ros.callService(GET_TASK_METADATA_SERVICE, { task_directory: skill.directory })
      .then((res) => {
        if (seq !== requestSeq) return;
        if (!res?.success || !res.json_metadata) throw new Error(res?.message || "No metadata returned");
        /** @type {TaskSummary} */
        const meta = JSON.parse(res.json_metadata);
        episodes = Array.isArray(meta.episodes) ? meta.episodes : [];
        dataFreq = meta.data_frequency || 0;
        render();
      })
      .catch((err) => {
        if (seq !== requestSeq) return;
        episodes = [];
        colhead.hidden = true;
        foot.textContent = "";
        sub.textContent = "error";
        showMessage(`Couldn't load episodes — ${err.message}`);
      });
  }

  /** An episode is playable only when ITS OWN MP4s exist. Do not fall back to
   * the dataset-level `dataset_type === "h264"`: that flips true after the first
   * batch is encoded, which would mark every newly-recorded (un-encoded) episode
   * "ready" before its video exists. The converter writes per-episode
   * video_files for everything it encodes, so this is the reliable signal.
   * @param {EpisodeSummary} ep */
  function isReady(ep) {
    return !!(ep.video_files && ep.video_files.length);
  }

  /** Two states: "ready" (has MP4s, playable) or "preparing" (recorded but not
   * yet encoded — auto-encoding runs in the background). @param {EpisodeSummary} ep
   * @returns {"ready"|"preparing"} */
  function stateOf(ep) {
    return isReady(ep) ? "ready" : "preparing";
  }

  /** Episodes in display order (current sort + search filter). @returns {EpisodeSummary[]} */
  function orderedEpisodes() {
    const sorted = [...episodes].sort((a, b) => (sortNewest ? numericId(b) - numericId(a) : numericId(a) - numericId(b)));
    return query ? sorted.filter((e) => String(numericId(e)).includes(query)) : sorted;
  }

  /** Nearest *ready* (playable) neighbor of *ep* in display order, walking in the
   * delta direction (-1 = up, +1 = below) and skipping still-preparing episodes.
   * Resolved against the live list, so it reflects deletes and encodes that
   * happened since the player opened — never returns a now-missing or un-encoded
   * episode (which would load a 404 video + "joint data unavailable").
   * @param {EpisodeSummary} ep @param {number} delta @returns {EpisodeSummary | null} */
  function neighbor(ep, delta) {
    const list = orderedEpisodes();
    let i = list.findIndex((e) => numericId(e) === numericId(ep));
    if (i < 0) return null; // ep itself was deleted while the player was open
    for (i += delta; i >= 0 && i < list.length; i += delta) {
      if (isReady(list[i])) return list[i];
    }
    return null;
  }

  function render() {
    if (!current) return;
    const readyCount = episodes.filter(isReady).length;
    const preparingCount = episodes.length - readyCount;
    sub.innerHTML = "";
    sub.append(
      document.createTextNode(`${episodes.length} episode${episodes.length === 1 ? "" : "s"} · `),
      spanText(`${readyCount} ready`, "episodes-ready"),
    );
    if (preparingCount > 0) {
      sub.append(document.createTextNode(" · "), spanText(`${preparingCount} preparing`, "episodes-preparing"));
    }

    if (episodes.length === 0) {
      colhead.hidden = true;
      foot.textContent = "";
      showMessage("This skill has no recorded episodes yet.");
      return;
    }
    colhead.hidden = false;

    const shown = orderedEpisodes();

    const frag = document.createDocumentFragment();
    for (const ep of shown) {
      frag.appendChild(buildRow(ep));
    }
    rowsEl.replaceChildren(frag);
    foot.textContent = `Showing ${shown.length} of ${episodes.length} episodes`;
  }

  /** @param {EpisodeSummary} ep @returns {HTMLElement} */
  function buildRow(ep) {
    const state = stateOf(ep);
    const id = numericId(ep);
    const row = document.createElement("div");
    row.className = `episode-row episode-${state}`;

    // col 1: status dot + thumbnail + id
    const epCol = document.createElement("div");
    epCol.className = "ep-cell";
    const dot = document.createElement("span");
    dot.className = "ep-dot";

    const thumb = document.createElement("div");
    thumb.className = "ep-thumb";
    if (state === "ready") {
      const img = document.createElement("img");
      img.loading = "lazy"; // browser fetches only as rows scroll into view
      img.decoding = "async";
      img.alt = "";
      img.src = `/episode/thumb?dir=${encodeURIComponent(current?.directory || "")}&id=${id}&camera=camera_1`;
      img.addEventListener("error", () => thumb.classList.add("empty"));
      thumb.appendChild(img);
    }
    const idEl = document.createElement("span");
    idEl.className = "ep-id mono";
    idEl.textContent = `#${id}`;
    epCol.append(dot, thumb, idEl);

    // col 2: recorded
    const recCol = document.createElement("span");
    recCol.className = "ep-recorded";
    recCol.textContent = formatRecorded(ep.start_time);

    // col 3: duration
    const durCol = document.createElement("span");
    durCol.className = "ep-duration mono";
    durCol.textContent = formatDuration(ep, dataFreq);

    // readiness is conveyed by the dot (Status column removed); replay on click
    const pct = encodeStatus ? Math.round((encodeStatus.progress || 0) * 100) : 0;
    dot.title = state === "ready" ? "ready" : encodeStatus?.stage === "encoding" ? `encoding ${pct}%` : "preparing";
    if (state === "ready") {
      row.classList.add("clickable");
      row.title = "Replay episode";
      row.addEventListener("click", () => opts.onOpen(/** @type {Skill} */ (current), ep));
    }

    // col 4: label dropdown (default Successful)
    const labelCol = buildLabelSelect(ep);

    // col 5: actions — manual re-encode kick (preparing only) + delete
    const actions = document.createElement("div");
    actions.className = "ep-actions";
    if (state === "preparing") {
      const prep = actionBtn(ICON_DOWNLOAD, "Prepare video", (e) => {
        e.stopPropagation();
        prep.disabled = true;
        ros.callService(ENCODE_EPISODE_SERVICE, { task_directory: current?.directory, episode_id: id }).catch(
          () => (prep.disabled = false),
        );
      });
      actions.appendChild(prep);
    }
    actions.appendChild(
      actionBtn(ICON_TRASH, "Delete episode", (e) => {
        e.stopPropagation();
        deleteEpisode(ep, id);
      }),
    );

    row.append(epCol, recCol, durCol, labelCol, actions);
    return row;
  }

  /** Label dropdown: Successful (default) / Unsuccessful, calls SetEpisodeOutcome.
   * @param {EpisodeSummary} ep @returns {HTMLElement} */
  function buildLabelSelect(ep) {
    const cell = document.createElement("div");
    cell.className = "ep-label";
    const sel = document.createElement("select");
    const applyClass = (/** @type {boolean} */ fail) =>
      (sel.className = `ep-label-select ${fail ? "is-fail" : "is-success"}`);
    applyClass(ep.outcome === "failure");
    for (const [val, text] of [
      ["success", "Successful"],
      ["failure", "Unsuccessful"],
    ]) {
      const o = document.createElement("option");
      o.value = val;
      o.textContent = text;
      if ((val === "failure") === (ep.outcome === "failure")) o.selected = true;
      sel.appendChild(o);
    }
    sel.addEventListener("click", (e) => e.stopPropagation());
    sel.addEventListener("change", (e) => {
      e.stopPropagation();
      const outcome = /** @type {"success"|"failure"} */ (sel.value);
      ep.outcome = outcome;
      applyClass(outcome === "failure");
      ros.callService(SET_EPISODE_OUTCOME_SERVICE, {
        task_directory: current?.directory,
        episode_id: numericId(ep),
        outcome,
      }).catch((err) => console.error("[datasets] set outcome failed:", err));
    });
    cell.appendChild(sel);
    return cell;
  }

  /** @param {EpisodeSummary} ep @param {number} id */
  function deleteEpisode(ep, id) {
    if (!current) return;
    if (!window.confirm(`Delete episode #${id}? This permanently removes its video and data.`)) return;
    ros.callService(DELETE_EPISODE_SERVICE, { task_directory: current.directory, episode_id: id })
      .then((res) => {
        if (res && res.success === false) throw new Error(res.message || "delete failed");
        episodes = episodes.filter((e) => e !== ep);
        render();
      })
      .catch((err) => window.alert(`Couldn't delete episode: ${err.message}`));
  }

  return {
    show,
    neighbor,
    destroy() {
      requestSeq++;
      unsub();
      wrap.remove();
    },
  };
}

/** @param {string} iconSvg @param {string} title @param {(e: Event) => void} onClick @returns {HTMLButtonElement} */
function actionBtn(iconSvg, title, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "ep-action-btn" + (title === "Delete episode" ? " ep-delete" : "");
  b.title = title;
  b.innerHTML = iconSvg;
  b.addEventListener("click", onClick);
  return b;
}

/** @param {string} text @param {string} cls */
function spanText(text, cls) {
  const s = document.createElement("span");
  s.className = cls;
  s.textContent = text;
  return s;
}

/** Numeric episode index from "episode_0" or "episode_0.h5". @param {EpisodeSummary} ep */
function numericId(ep) {
  const m = /(\d+)/.exec(String(ep.episode_id)) || /(\d+)/.exec(ep.file_name || "");
  return m ? Number(m[1]) : 0;
}

/** "2026-04-12T20:25:31" → "12 Apr, 20:25". @param {string} iso */
function formatRecorded(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso || "—";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${d.getDate()} ${MONTHS[d.getMonth()]}, ${hh}:${mm}`;
}

/** Episode duration: prefer num_timesteps ÷ data_frequency, else timestamp diff.
 * @param {EpisodeSummary} ep @param {number} hz */
function formatDuration(ep, hz) {
  let secs = NaN;
  if (hz && ep.num_timesteps) {
    secs = ep.num_timesteps / hz;
  } else {
    const t0 = new Date(ep.start_time).getTime();
    const t1 = new Date(ep.end_time).getTime();
    if (!Number.isNaN(t0) && !Number.isNaN(t1) && t1 >= t0) secs = (t1 - t0) / 1000;
  }
  if (Number.isNaN(secs)) return "—";
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return `${m}m ${s}s`;
}

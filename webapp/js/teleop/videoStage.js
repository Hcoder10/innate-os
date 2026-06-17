// @ts-check
// Video stage — the full-bleed <video> plus a hidden <audio> for the robot
// mic, with quiet connecting/error states layered on top. Also exports the
// audio toggle, which must flip the track and call audio.play() inside the
// click gesture to satisfy autoplay policy.

/**
 * @param {HTMLElement} parent
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @returns {{ audioEl: HTMLAudioElement, destroy: () => void }}
 */
export function createVideoStage(parent, session) {
  const wrap = document.createElement("div");
  wrap.className = "video-stage";

  const video = document.createElement("video");
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;

  const audio = document.createElement("audio");
  audio.hidden = true;

  const status = document.createElement("div");
  status.className = "video-status";
  const statusText = document.createElement("p");
  statusText.className = "microlabel video-status-text";
  const retry = document.createElement("button");
  retry.className = "video-retry";
  retry.type = "button";
  retry.textContent = "Retry video";
  retry.hidden = true;
  retry.addEventListener("click", () => session.start());
  status.append(statusText, retry);

  wrap.append(video, audio, status);
  parent.appendChild(wrap);

  const unsub = session.onChange((state) => {
    // Keep the previous frame during rebuilds: only swap srcObject when a
    // new stream actually arrives, never clear it mid-handshake.
    if (state.videoStream && video.srcObject !== state.videoStream) {
      video.srcObject = state.videoStream;
      // autoplay alone can leave a swapped-in stream paused on its first
      // frame (Chromium); muted play() is always allowed, so be explicit.
      video.play().catch(() => {});
    }
    if (state.status === "idle" && !state.videoStream) {
      video.srcObject = null;
    }

    if (state.audioStream) {
      if (audio.srcObject !== state.audioStream) {
        audio.srcObject = state.audioStream;
        if (state.audioRequested) {
          // Allowed: the operator clicked the toggle earlier this page-load,
          // which grants audible autoplay in Chromium/Firefox. Safari may
          // still refuse; the next toggle click plays inside its gesture.
          audio.play().catch((err) => console.warn("[video] audio play blocked:", err));
        }
      }
    } else {
      audio.srcObject = null;
    }

    const showStatus = state.status !== "streaming" && !state.videoStream;
    status.hidden = !showStatus;
    wrap.classList.toggle("degraded", state.status === "connecting" && !!state.videoStream);
    if (state.status === "error") {
      statusText.textContent = "video link failed";
      retry.hidden = false;
    } else {
      statusText.textContent = state.status === "connecting" ? "establishing video link" : "video idle";
      retry.hidden = true;
    }
    status.classList.toggle("waiting", state.status === "connecting");
  });

  return {
    audioEl: audio,
    destroy() {
      unsub();
      video.srcObject = null;
      audio.srcObject = null;
      wrap.remove();
    },
  };
}

/**
 * Robot-mic toggle. Starts muted; the click both requests the audio m-line
 * (debounced rebuild in the session) and primes playback inside the gesture.
 * @param {HTMLElement} parent
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @param {HTMLAudioElement} audioEl
 * @returns {{ destroy: () => void }}
 */
export function createAudioToggle(parent, session, audioEl) {
  const button = document.createElement("button");
  button.className = "icon-toggle audio-toggle";
  button.type = "button";
  button.innerHTML =
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M11 5 6.5 9H3.5v6h3L11 19z"/>' +
    '<path class="wave wave1" d="M15 9.5a4 4 0 0 1 0 5"/>' +
    '<path class="wave wave2" d="M17.5 7a8 8 0 0 1 0 10"/>' +
    "</svg>";

  const unsub = session.onChange((state) => {
    button.classList.toggle("active", state.audioRequested);
    button.title = state.audioRequested ? "Robot mic on — click to mute" : "Robot mic off — click to listen";
    button.setAttribute("aria-pressed", String(state.audioRequested));
    button.setAttribute("aria-label", "Robot microphone");
  });

  button.addEventListener("click", () => {
    const next = !session.state.audioRequested;
    session.setAudio(next);
    if (next) {
      // Inside the gesture: unlocks audible playback for this page session.
      audioEl.play().catch(() => {
        // No stream yet — fine, the session onChange handler replays once
        // the rebuilt connection delivers the mic track.
      });
    } else {
      audioEl.pause();
    }
  });

  parent.appendChild(button);
  return {
    destroy() {
      unsub();
      button.remove();
    },
  };
}

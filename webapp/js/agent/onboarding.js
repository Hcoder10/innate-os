// @ts-check
// Versioned declarative onboarding engine. Steps own fixed dialogue, completion
// events, entry actions, and next links. Progress is {version, stepId} only.
//
// The tour runs entirely from the browser: it opens the robot's own mic device
// (ACTIVE_INPUTS_TOPIC) so STT transcribes "Hello MARS" onto /brain/chat_in
// before any agent exists, then drives skills and speech through the scripted
// runner. The brain stays inactive throughout, which is exactly why the agent
// never consumes the scripted turns — an inactive brain drops chat_in. Handing
// over means activating the directive and posting one custom-input event so the
// agent picks up mid-conversation instead of introducing itself again.

import { ACTIVE_INPUTS_TOPIC, CHAT_IN_TOPIC, CUSTOM_INPUT_TOPIC } from "../constants.js";

const ONBOARDING_VERSION = 2;
const STORAGE_KEY = "innate.agentOnboarding";
const LEGACY_STEP_KEY = "innate.agentOnboardingStep";
const LEGACY_COMPLETE_KEY = "innate.agentOnboardingComplete";
const RESET_EVENT = "innate:agent-onboarding-reset";
// How long "Say Hello MARS" may go unanswered before offering a way past it.
// The robot-level mic toggle can veto the device, and a tour that cannot be
// skipped would strand the page on its first step.
const SKIP_OFFER_MS = 25000;
// Degrees the robot turns toward a panel as it appears. Every tour step turns
// back by the same amount, so each one starts and ends centred — which is what
// lets a step resume standalone from storage.
const POINT_DEGREES = 25;

export const WELCOME_DIALOGUE =
  "Welcome to the simulator! I'll be here to show you some of the main things we can do together!";

/**
 * Completion event kinds reserved for later lessons:
 * - speech: user transcript matched
 * - action: entry scripted runner finished
 * - skill_success / sim_event / user_action: extension points (unused yet)
 * - complete: terminal handoff step
 *
 * @typedef {"await_hello" | "welcome" | "tour_cameras" | "tour_telemetry" | "tour_chat" | "complete"} OnboardingStepId
 * @typedef {"speech" | "action" | "skill_success" | "sim_event" | "user_action" | "complete"} CompletionKind
 * @typedef {{
 *   id: OnboardingStepId,
 *   instruction?: string,
 *   dialogue?: string,
 *   completeOn: CompletionKind,
 *   speechMatch?: (text: string) => boolean,
 *   reveal?: string,
 *   recap?: string,
 *   actions?: (reveal: () => void) => import("./onboardingWelcome.js").ScriptedAction[],
 *   next: OnboardingStepId | null,
 * }} OnboardingStepDef
 */

/** @type {Record<OnboardingStepId, OnboardingStepDef>} */
const STEPS = {
  await_hello: {
    id: "await_hello",
    instruction: 'Say “Hello MARS”',
    completeOn: "speech",
    speechMatch: isGreeting,
    next: "welcome",
  },
  welcome: {
    id: "welcome",
    dialogue: WELCOME_DIALOGUE,
    completeOn: "action",
    recap: "greeted them and waved",
    actions: () => [
      { type: "skill", name: "move_straight", inputs: { distance: 0.2, speed: 0.12 } },
      { type: "speak", text: WELCOME_DIALOGUE },
      { type: "skill", name: "head_emotion", inputs: { emotion: "excited", repeat: 5 }, afterSpeechStart: true },
      { type: "skill", name: "wave", inputs: {} },
    ],
    next: "tour_cameras",
  },
  tour_cameras: {
    id: "tour_cameras",
    dialogue: "Up here are my cameras. That's how I see the room — and what I look at when you ask me about something.",
    completeOn: "action",
    reveal: "cameras",
    recap: "showed them the camera views",
    actions: (reveal) => pointOut(POINT_DEGREES, reveal, STEPS.tour_cameras.dialogue ?? "", "happy"),
    next: "tour_telemetry",
  },
  tour_telemetry: {
    id: "tour_telemetry",
    dialogue: "This panel is how I'm doing — where I am, how I'm moving, how much battery I have left.",
    completeOn: "action",
    reveal: "telemetry",
    recap: "showed them the telemetry panel",
    actions: (reveal) => pointOut(-POINT_DEGREES, reveal, STEPS.tour_telemetry.dialogue ?? "", "agreeing"),
    next: "tour_chat",
  },
  tour_chat: {
    id: "tour_chat",
    dialogue: "And this is where we talk. Hold the microphone, or type — either way, I'm listening.",
    completeOn: "action",
    reveal: "chat",
    recap: "showed them the chat panel",
    actions: (reveal) => pointOut(-POINT_DEGREES, reveal, STEPS.tour_chat.dialogue ?? "", "very_happy"),
    next: "complete",
  },
  complete: {
    id: "complete",
    completeOn: "complete",
    next: null,
  },
};

/** Step order, which is also reveal order: resuming re-applies every earlier reveal. */
/** @type {OnboardingStepId[]} */
const STEP_ORDER = ["await_hello", "welcome", "tour_cameras", "tour_telemetry", "tour_chat", "complete"];

/**
 * One tour beat: turn toward the panel, reveal it, say the line, react, turn back.
 * @param {number} degrees
 * @param {() => void} reveal
 * @param {string} text
 * @param {string} emotion
 * @returns {import("./onboardingWelcome.js").ScriptedAction[]}
 */
function pointOut(degrees, reveal, text, emotion) {
  return [
    { type: "skill", name: "turn_in_place", inputs: { angle_degrees: degrees } },
    { type: "ui", apply: reveal },
    { type: "speak", text },
    { type: "skill", name: "head_emotion", inputs: { emotion }, afterSpeechStart: true },
    { type: "skill", name: "turn_in_place", inputs: { angle_degrees: -degrees } },
  ];
}

/**
 * @param {HTMLElement} root
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {{
 *   runner: {
 *     run: (actions: import("./onboardingWelcome.js").ScriptedAction[]) => Promise<void>,
 *     cancel: () => void,
 *   },
 *   onHandoff?: () => Promise<void> | void,
 * }} options
 */
export function createAgentOnboarding(root, rosClient, options) {
  const { runner, onHandoff } = options;
  /** @type {OnboardingStepId} */
  let stepId = loadStepId();
  /** @type {Set<(step: OnboardingStepId) => void>} */
  const stepListeners = new Set();
  let entryToken = 0;
  let destroyed = false;
  /** Reveals applied ahead of their step's turn (none yet at this point). */
  /** @type {Set<string>} */
  const revealed = new Set();
  /** @type {ReturnType<typeof setTimeout> | null} */
  let skipTimer = null;
  root.classList.add("agent-onboarding-enabled");

  // Advertised up front, not at first use: an unadvertised topic resolves its
  // type from an existing publisher, and the first message can be dropped
  // before rosbridge finishes wiring it up.
  const unadvertiseInputs = rosClient.advertise(ACTIVE_INPUTS_TOPIC, "std_msgs/msg/String");
  const unadvertiseCustom = rosClient.advertise(CUSTOM_INPUT_TOPIC, "std_msgs/msg/String");

  const nudge = document.createElement("p");
  nudge.className = "agent-onboarding-nudge";
  nudge.innerHTML = 'Say <strong>“Hello MARS”</strong>';
  root.append(nudge);

  const skip = document.createElement("button");
  skip.type = "button";
  skip.className = "agent-onboarding-skip";
  skip.textContent = "Skip intro";
  skip.hidden = true;
  skip.addEventListener("click", () => {
    void enter("complete");
  });
  root.append(skip);

  function render() {
    const active = stepId !== "complete";
    root.classList.toggle("agent-onboarding-active", active);
    // Distinct from the per-step classes: the robot appears at the welcome and
    // stays on screen for the whole tour, so the stage cannot key off one step.
    root.classList.toggle("agent-onboarding-staged", stepId !== "await_hello");
    for (const id of STEP_ORDER) {
      root.classList.toggle(`agent-onboarding-${id}`, stepId === id);
    }
    for (const target of ["cameras", "telemetry", "chat"]) {
      root.classList.toggle(`agent-onboarding-show-${target}`, revealed.has(target));
    }
    nudge.setAttribute("aria-hidden", String(stepId !== "await_hello"));
    for (const listener of stepListeners) listener(stepId);
  }

  /** Reveals every panel introduced at or before `upTo`, so a resumed step keeps its predecessors. */
  /** @param {OnboardingStepId} upTo */
  function syncRevealsThrough(upTo) {
    const limit = STEP_ORDER.indexOf(upTo);
    for (const id of STEP_ORDER.slice(0, limit)) {
      const target = STEPS[id].reveal;
      if (target) revealed.add(target);
    }
  }

  function persist() {
    storageSet(STORAGE_KEY, JSON.stringify({ version: ONBOARDING_VERSION, stepId }));
  }

  function armSkipOffer() {
    if (skipTimer !== null) clearTimeout(skipTimer);
    skipTimer = setTimeout(() => {
      skipTimer = null;
      if (destroyed || stepId !== "await_hello") return;
      skip.hidden = false;
    }, SKIP_OFFER_MS);
  }

  /** @param {OnboardingStepId} nextId */
  async function enter(nextId) {
    if (destroyed) return;
    const crossingIntoComplete = nextId === "complete" && stepId !== "complete";
    stepId = nextId;
    syncRevealsThrough(nextId);
    persist();
    render();
    const token = ++entryToken;
    if (skipTimer !== null) {
      clearTimeout(skipTimer);
      skipTimer = null;
    }
    if (nextId !== "await_hello") skip.hidden = true;
    if (nextId === "complete") {
      runner.cancel();
      skip.hidden = true;
      // Inputs are deliberately left open: activating the directive republishes
      // its own required inputs (micro among them), so closing here would only
      // deafen the robot for the moment between the two.
      if (crossingIntoComplete) {
        await onHandoff?.();
        if (token !== entryToken || destroyed) return;
        publishHandoffContext(rosClient);
      }
      return;
    }
    await setMicInputOpen(rosClient, true);
    if (token !== entryToken || destroyed) return;
    if (nextId === "await_hello") armSkipOffer();
    const step = STEPS[nextId];
    if (step.completeOn !== "action" || !step.actions) return;
    await runner.run(step.actions(() => {
      if (step.reveal) revealed.add(step.reveal);
      render();
    }));
    if (token !== entryToken || destroyed || stepId !== nextId) return;
    await advance();
  }

  async function advance() {
    const step = STEPS[stepId];
    if (!step?.next) return;
    await enter(step.next);
  }

  async function reset() {
    runner.cancel();
    entryToken++;
    revealed.clear();
    skip.hidden = true;
    storageRemove(STORAGE_KEY);
    storageRemove(LEGACY_STEP_KEY);
    storageRemove(LEGACY_COMPLETE_KEY);
    await enter("await_hello");
  }

  const unsubChat = rosClient.subscribe(CHAT_IN_TOPIC, (message) => {
    const step = STEPS[stepId];
    if (step?.completeOn !== "speech" || typeof message?.data !== "string") return;
    let payload;
    try {
      payload = JSON.parse(message.data);
    } catch {
      return;
    }
    if (String(payload?.sender ?? "") !== "user") return;
    if (!step.speechMatch?.(String(payload?.text ?? ""))) return;
    void advance();
  }, undefined, "std_msgs/msg/String");

  const onReset = () => {
    void reset();
  };
  window.addEventListener(RESET_EVENT, onReset);

  void enter(stepId);

  return {
    getStep() {
      return stepId;
    },
    isComplete() {
      return stepId === "complete";
    },
    /** Keep the robot's mic device open without activating the agent. */
    async ensureListening() {
      if (stepId === "complete") return false;
      await setMicInputOpen(rosClient, true);
      return true;
    },
    /** @param {(step: OnboardingStepId) => void} listener */
    onStep(listener) {
      stepListeners.add(listener);
      listener(stepId);
      return () => stepListeners.delete(listener);
    },
    destroy() {
      destroyed = true;
      entryToken++;
      runner.cancel();
      unsubChat();
      window.removeEventListener(RESET_EVENT, onReset);
      if (skipTimer !== null) clearTimeout(skipTimer);
      stepListeners.clear();
      nudge.remove();
      skip.remove();
      root.classList.remove(
        "agent-onboarding-enabled",
        "agent-onboarding-active",
        "agent-onboarding-staged",
        ...STEP_ORDER.map((id) => `agent-onboarding-${id}`),
        ...["cameras", "telemetry", "chat"].map((target) => `agent-onboarding-show-${target}`),
      );
      if (stepId !== "complete") {
        void setMicInputOpen(rosClient, false);
      }
      unadvertiseInputs();
      unadvertiseCustom();
    },
  };
}

export function resetAgentOnboarding() {
  storageRemove(STORAGE_KEY);
  storageRemove(LEGACY_STEP_KEY);
  storageRemove(LEGACY_COMPLETE_KEY);
  window.dispatchEvent(new Event(RESET_EVENT));
}

/** @returns {OnboardingStepId} */
function loadStepId() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed?.version === ONBOARDING_VERSION && isStepId(parsed?.stepId)) {
        return parsed.stepId;
      }
      // A finished older tour stays finished; an unfinished one restarts, since
      // its step ids no longer describe this version's lesson order.
      if (parsed?.stepId === "complete") return "complete";
    }
    const legacy = localStorage.getItem(LEGACY_STEP_KEY);
    if (legacy === "greeted") return "welcome";
    if (legacy === "complete") return "complete";
    if (legacy === "hello") return "await_hello";
    if (localStorage.getItem(LEGACY_COMPLETE_KEY)) return "complete";
  } catch {
    // Fall through to the first lesson.
  }
  return "await_hello";
}

/** @param {unknown} value @returns {value is OnboardingStepId} */
function isStepId(value) {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(STEPS, value);
}

/** @param {string} text */
function isGreeting(text) {
  const words = text.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  return /\b(?:hello|hi|hey)\s+mars\b/.test(words);
}

/**
 * Open or close the robot's microphone device without touching the brain.
 * input_manager owns this topic and applies it whatever the brain is doing, so
 * STT runs during the tour and the transcript lands on /brain/chat_in for the
 * subscription above. An inactive brain drops that same message, which is what
 * keeps the agent from answering the greeting itself.
 *
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {boolean} open
 */
async function setMicInputOpen(rosClient, open) {
  rosClient.publish(ACTIVE_INPUTS_TOPIC, { data: JSON.stringify({ inputs: open ? ["micro"] : [] }) });
}

/**
 * Tell the agent what it just did, so it continues the conversation instead of
 * opening a new one. Only meaningful once the brain is active — custom input is
 * dropped while it is not — so this runs after the directive has been set.
 *
 * @param {import("../rosClient.js").RosClient} rosClient
 */
function publishHandoffContext(rosClient) {
  const recap = STEP_ORDER.map((id) => STEPS[id].recap).filter(Boolean).join(", ");
  rosClient.publish(CUSTOM_INPUT_TOPIC, {
    data: JSON.stringify({
      input_device: "onboarding",
      event: "onboarding_complete",
      summary:
        `You have just finished welcoming this user to the simulator. You ${recap}. ` +
        "They are still here and the conversation is already under way — carry on from " +
        "that point, and do not greet them or introduce yourself again.",
    }),
  });
}

/** @param {string} key @param {string} value */
function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // In-memory step still advances for this page view.
  }
}

/** @param {string} key */
function storageRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // Reset still applies through the event for the current page.
  }
}

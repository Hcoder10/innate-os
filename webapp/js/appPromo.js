// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Mobile "get the app" prompt — shown once when the browser cockpit is opened
// on a phone or tablet. The web app is tuned for a laptop; on the go the native
// app is the better surface (leader-arm teleop in the field), so nudge mobile
// visitors toward it with a platform-correct store link. Dismissal is
// remembered so it never nags across page switches or repeat visits.

const IOS_APP_URL = "https://testflight.apple.com/join/YeChe4A7";
const ANDROID_APP_URL = "https://cdn.innate.bot/innate-app-latest.apk";
const DISMISS_KEY = "innate.appPromoDismissed";

/** @typedef {"ios" | "android"} MobilePlatform */

/**
 * Show the app-download prompt if the visitor is on mobile and hasn't dismissed
 * it before. No-op on desktop. Safe to call on every page mount.
 * @param {string} root Relative prefix to the site root ("" or "../"), for the logo asset.
 */
export function maybeShowAppPromo(root) {
  const platform = detectMobilePlatform();
  if (!platform || storageGet(DISMISS_KEY)) return;

  // Remember what had focus so we can hand it back when the dialog closes.
  const prevFocus = document.activeElement;

  const backdrop = document.createElement("div");
  backdrop.className = "app-promo-backdrop";
  backdrop.addEventListener("click", dismiss); // tap outside the card to dismiss

  const card = document.createElement("div");
  card.className = "app-promo";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-modal", "true");
  card.setAttribute("aria-label", "Get the Innate app");
  card.tabIndex = -1; // focusable target so the dialog can be announced on open
  card.addEventListener("click", (e) => e.stopPropagation());

  const close = document.createElement("button");
  close.type = "button";
  close.className = "app-promo-close";
  close.textContent = "✕";
  close.setAttribute("aria-label", "Dismiss");
  close.addEventListener("click", dismiss);

  const logo = document.createElement("img");
  logo.className = "app-promo-logo";
  logo.src = root + "public/innate-logo.avif";
  logo.alt = "";

  const title = document.createElement("h2");
  title.className = "app-promo-title";
  title.textContent = "Get the Innate app";

  const blurb = document.createElement("p");
  blurb.className = "app-promo-blurb";
  blurb.textContent =
    "The web app is built for a laptop. On your phone, the Innate app is the better way to drive — including leader-arm teleoperation in the field.";

  const cta = document.createElement("a");
  cta.className = "app-promo-cta";
  cta.href = platform === "ios" ? IOS_APP_URL : ANDROID_APP_URL;
  // iOS TestFlight opens in a new tab; the Android .apk should download in place.
  // A _blank to a downloadable file leaves an empty lingering tab on Chrome for Android.
  if (platform === "ios") {
    cta.target = "_blank";
    cta.rel = "noopener noreferrer";
  }
  cta.textContent = platform === "ios" ? "Download for iOS" : "Download for Android";
  cta.addEventListener("click", dismiss); // they've got the message — don't ask again

  const stay = document.createElement("button");
  stay.type = "button";
  stay.className = "app-promo-stay";
  stay.textContent = "Continue in browser";
  stay.addEventListener("click", dismiss);

  card.append(close, logo, title, blurb, cta, stay);
  backdrop.appendChild(card);
  document.body.appendChild(backdrop);
  document.addEventListener("keydown", onKey);
  // Move focus into the dialog so screen-reader / keyboard users land on it.
  card.focus();

  /** @param {KeyboardEvent} e */
  function onKey(e) {
    if (e.key === "Escape") dismiss();
  }

  function dismiss() {
    storageSet(DISMISS_KEY, "1");
    document.removeEventListener("keydown", onKey);
    backdrop.remove();
    // Hand focus back to wherever it was before the dialog opened.
    if (prevFocus instanceof HTMLElement) prevFocus.focus();
  }
}

/**
 * Detect whether we're on a phone/tablet that can run the native app.
 * Exported for unit testing the gating logic. @returns {MobilePlatform | null} null on desktop / unknown.
 */
export function detectMobilePlatform() {
  const ua = navigator.userAgent || "";
  if (/android/i.test(ua)) return "android";
  // iPhone/iPod/iPad, plus iPadOS 13+ which reports itself as desktop Safari
  // ("Macintosh" UA) — distinguished from an actual Mac by the touch screen.
  // (navigator.platform would work too but is deprecated.)
  const isIOS =
    /iphone|ipad|ipod/i.test(ua) ||
    (/macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
  return isIOS ? "ios" : null;
}

/** localStorage can throw (private mode / disabled) — never let it break a page. */
/** @param {string} key @returns {string | null} */
function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** @param {string} key @param {string} value */
function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore — a non-persisted dismissal still hides it for this page view */
  }
}

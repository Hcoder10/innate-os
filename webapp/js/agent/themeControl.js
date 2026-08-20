// @ts-check

const STORE_KEY = "innate.agent.theme.v2";
const THEMES = [
  { id: "stock", label: "Stock Black", family: "Innate" },
  { id: "omarchy-tokyo-night", label: "Tokyo Night", family: "Omarchy" },
  { id: "omarchy-catppuccin", label: "Catppuccin", family: "Omarchy" },
  { id: "omarchy-lumon", label: "Lumon", family: "Omarchy" },
  { id: "omarchy-ethereal", label: "Ethereal", family: "Omarchy" },
  { id: "omarchy-everforest", label: "Everforest", family: "Omarchy" },
  { id: "omarchy-gruvbox", label: "Gruvbox", family: "Omarchy" },
  { id: "omarchy-miasma", label: "Miasma", family: "Omarchy" },
  { id: "omarchy-hackerman", label: "Hackerman", family: "Omarchy" },
  { id: "omarchy-osaka-jade", label: "Osaka Jade", family: "Omarchy" },
  { id: "omarchy-kanagawa", label: "Kanagawa", family: "Omarchy" },
  { id: "omarchy-nord", label: "Nord", family: "Omarchy" },
  { id: "omarchy-matte-black", label: "Matte Black", family: "Omarchy" },
  { id: "omarchy-vantablack", label: "Vantablack", family: "Omarchy" },
  { id: "omarchy-ristretto", label: "Ristretto", family: "Omarchy" },
  { id: "omarchy-retro-82", label: "Retro 82", family: "Omarchy" },
  { id: "omarchy-flexoki-light", label: "Flexoki Light", family: "Omarchy" },
  { id: "omarchy-rose-pine", label: "Rose Pine", family: "Omarchy" },
  { id: "omarchy-catppuccin-latte", label: "Catppuccin Latte", family: "Omarchy" },
  { id: "omarchy-white", label: "White", family: "Omarchy" },
  { id: "obsidian", label: "Obsidian", family: "Dark" },
  { id: "graphite", label: "Graphite", family: "Dark" },
  { id: "midnight", label: "Midnight", family: "Dark" },
  { id: "aurora", label: "Aurora", family: "Dark" },
  { id: "evergreen", label: "Evergreen", family: "Dark" },
  { id: "daylight", label: "Daylight", family: "Light" },
  { id: "arctic", label: "Arctic", family: "Light" },
  { id: "rose", label: "Rose Quartz", family: "Light" },
  { id: "contrast", label: "Signal", family: "High contrast" },
  { id: "retro", label: "Amber CRT", family: "Retro" },
  { id: "solarized", label: "Solarized", family: "Classic" },
];

/**
 * @param {HTMLElement} root
 * @returns {{ element: HTMLElement, destroy: () => void }}
 */
export function createAgentThemeControl(root) {
  const control = document.createElement("aside");
  control.className = "agent-theme-control";
  control.setAttribute("aria-label", "Agent page theme");

  const previous = themeButton("previous", "Previous theme");
  const copy = document.createElement("span");
  copy.className = "agent-theme-copy";
  const family = document.createElement("span");
  family.className = "agent-theme-family";
  const name = document.createElement("strong");
  name.className = "agent-theme-name";
  name.setAttribute("aria-live", "polite");
  copy.append(family, name);
  const next = themeButton("next", "Next theme");
  control.append(previous, copy, next);

  let index = savedThemeIndex();

  function render() {
    const theme = THEMES[index];
    if (theme.id === "stock") delete root.dataset.agentTheme;
    else root.dataset.agentTheme = theme.id;
    family.textContent = `${theme.family} · ${index + 1}/${THEMES.length}`;
    name.textContent = theme.label;
    control.title = `${theme.family} theme: ${theme.label}`;
  }

  /** @param {number} step */
  function cycle(step) {
    index = (index + step + THEMES.length) % THEMES.length;
    try {
      localStorage.setItem(STORE_KEY, THEMES[index].id);
    } catch {
      // localStorage may be unavailable without affecting the live controls.
    }
    render();
  }

  previous.addEventListener("click", () => cycle(-1));
  next.addEventListener("click", () => cycle(1));
  render();

  return {
    element: control,
    destroy() {
      control.remove();
      delete root.dataset.agentTheme;
    },
  };
}

function savedThemeIndex() {
  try {
    const saved = localStorage.getItem(STORE_KEY);
    const index = THEMES.findIndex((theme) => theme.id === saved);
    if (index >= 0) return index;
  } catch {
    // The default theme remains usable when storage is blocked.
  }
  return 0;
}

/** @param {"previous" | "next"} direction @param {string} label */
function themeButton(direction, label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "agent-theme-cycle";
  button.innerHTML =
    direction === "previous"
      ? '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m12.5 4.5-5 5.5 5 5.5"/></svg>'
      : '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7.5 4.5 5 5.5-5 5.5"/></svg>';
  button.setAttribute("aria-label", label);
  return button;
}

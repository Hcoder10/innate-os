// Sample Lit component in TypeScript with standard decorators (TC39 — no
// experimentalDecorators flag needed, so the existing JS checker config is
// untouched). esbuild compiles the decorators + `accessor`. Demonstrates the
// Lit + esbuild path; not yet wired into a live page.
import { LitElement, html, css } from "lit";
import { customElement, property } from "lit/decorators.js";

type Status = "online" | "connecting" | "offline";

@customElement("connection-badge")
export class ConnectionBadge extends LitElement {
  @property() accessor status: Status = "offline";

  static styles = css`
    :host {
      display: inline-flex;
      align-items: center;
      gap: 0.5em;
      font: 600 12px/1 system-ui, sans-serif;
      text-transform: capitalize;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--c, #888);
    }
    :host([status="online"]) .dot {
      --c: #22c55e;
    }
    :host([status="connecting"]) .dot {
      --c: #eab308;
    }
    :host([status="offline"]) .dot {
      --c: #ef4444;
    }
  `;

  render() {
    return html`<span class="dot"></span>${this.status}`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "connection-badge": ConnectionBadge;
  }
}

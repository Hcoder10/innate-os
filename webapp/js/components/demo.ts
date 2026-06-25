// Demo entry: registers the sample Lit component and cycles its status, so
// `npm run build` produces a bundle proving Lit + esbuild + TS decorators work
// end to end.
import "./connection-badge";

const badge = document.querySelector("connection-badge");
if (badge) {
  const states: Status[] = ["offline", "connecting", "online"];
  let i = 0;
  setInterval(() => {
    i = (i + 1) % states.length;
    badge.status = states[i];
  }, 1200);
}

type Status = "online" | "connecting" | "offline";

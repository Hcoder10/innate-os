// SPDX-License-Identifier: Apache-2.0
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "styled-components";
import theme, { GlobalStyle } from "./styles/theme";
import "./index.css";
import App from "./App.tsx";
import { loadConfig } from "./config.ts";

// Load runtime config before rendering so components can read appConfig synchronously.
loadConfig().then(() => {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <ThemeProvider theme={theme}>
      <GlobalStyle />
      <App />
    </ThemeProvider>
  );
});

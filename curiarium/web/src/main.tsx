import React from "react";
import { createRoot } from "react-dom/client";
import "./index.css"; 
import App from "./ui/App";
import Toaster from "./ui/Toaster";
import { resolveInitialMode } from "./lib/modeBootstrap";

const el = document.getElementById("root");
if (!el) throw new Error("#root not found");
const root = createRoot(el);

// The boot mode is decided BEFORE the first render, so the app never paints one
// mode and then flips to the other. The await therefore sits in front of
// `render`. A throw inside this IIFE is a permanently blank page, which is why
// resolveInitialMode() is non-rejecting: it always settles on a mode.
void (async () => {
  const mode = await resolveInitialMode();
  root.render(
    <React.StrictMode>
      <>
        <App initialMode={mode} />
        <Toaster />
      </>
    </React.StrictMode>
  );
})();

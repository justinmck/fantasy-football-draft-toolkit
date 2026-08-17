import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./index.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

// The pre-render placeholder in index.html has done its job. Removed here
// rather than hidden by CSS so it can't intercept a click on the board.
document.getElementById("boot")?.remove();

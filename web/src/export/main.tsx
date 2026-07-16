import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import "../consultation/scribe.css";
import ExportPage from "./ExportPage";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ExportPage />
  </StrictMode>,
);

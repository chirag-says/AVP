import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../index.css";
import "./scribe.css";
import ConsultationScribe from "./ConsultationScribe";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConsultationScribe />
  </StrictMode>,
);

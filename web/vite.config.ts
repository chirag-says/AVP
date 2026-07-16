import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  // Multi-page: the reception chatbot (index.html) and the consultation scribe
  // (consultation.html) are separate pages that share nothing but UI
  // primitives. Listing both keeps `vite build` emitting both; the dev server
  // serves each at its own path automatically.
  build: {
    rollupOptions: {
      input: {
        main: path.resolve(import.meta.dirname, "index.html"),
        consultation: path.resolve(import.meta.dirname, "consultation.html"),
        export: path.resolve(import.meta.dirname, "export.html"),
      },
    },
  },
});

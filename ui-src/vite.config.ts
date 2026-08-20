import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The bundle is built straight into the Python package, where FastAPI serves
// it as static files. Same-origin by construction, which is what lets the
// local API run with no CORS headers at all (architecture §5).
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../app/ui",
    emptyOutDir: true,
    // Assets land under /assets/, which the session-token middleware serves
    // unauthenticated — the shell has to load before it can authenticate.
    assetsDir: "assets",
  },
  server: {
    port: 5173,
    proxy: {
      // `npm run dev` against a till started with `python -m app.main`.
      "/auth": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/reports": "http://127.0.0.1:8000",
    },
  },
});

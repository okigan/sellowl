import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    host: "0.0.0.0",
    proxy: {
      "/api": { target: process.env.VITE_API_BASE ?? "http://localhost:8000", changeOrigin: true },
      "/health": { target: process.env.VITE_API_BASE ?? "http://localhost:8000", changeOrigin: true },
    },
  },
});

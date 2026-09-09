import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// See README.md for the required API Gateway CORS setup -- this app calls
// the Lambda's API Gateway endpoint directly from the browser (no dev-server
// proxy, no backend-for-frontend), so it builds to plain static files that
// can be hosted anywhere (S3+CloudFront, Netlify, Vercel, ...).
export default defineConfig({
  plugins: [react()],
});

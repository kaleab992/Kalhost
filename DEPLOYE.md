# Deploy KALPRO Hosting Bot on Render

1. Put these files in a **private** GitHub repo: KALREADY.py, requirements.txt, Dockerfile, render.yaml, .dockerignore.
2. Render > New > Blueprint > pick the repo (or New > Web Service > Docker).
3. Set environment variables: BOT_TOKEN, OWNER_ID (and ADMIN_ID, ADMIN_PASSWORD).
4. Deploy. Health check path is /health. Open https://YOUR-SERVICE.onrender.com/admin for the web panel.

Notes
- Free plan: files, users and bots are erased on each deploy/restart. Use a paid plan + disk (render.yaml) to keep them.
- Free web services sleep after 15 min without traffic; the bot pings its own URL every 10 min to stay awake.
- Go/Java/Rust: add golang-go, default-jdk-headless (or rustc) to APT_EXTRA in the Dockerfile.
- Token: BOT_TOKEN env var wins over the fallback token written in KALREADY.py.

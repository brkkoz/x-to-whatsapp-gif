# X → WhatsApp GIF

Minimal one-page web app for turning a **public** X (Twitter) post containing a GIF/video into an animated GIF, then opening a mobile share sheet (WhatsApp can be selected where supported).

## Run with Docker (recommended)

```bash
docker build -t x-whatsapp-gif .
docker run --rm -p 8080:8080 x-whatsapp-gif
```

Open http://localhost:8080 on your computer. To test on your iPhone, deploy it on an **HTTPS** domain; file sharing via the browser Web Share API requires a secure context. Localhost on the phone is not your computer.

## Run without Docker

Install Python 3.12+, FFmpeg (including ffprobe), and run:

```bash
python -m pip install -r requirements.txt
python app.py
```

## Deploy

Deploy this repository to any Docker-capable host (e.g. a small VPS or a container hosting service). Configure HTTPS and, ideally, impose proxy-layer request throttling (e.g. 5 conversions per IP per hour), a limited number of workers, and monitoring before making it public. Free tiers may not support ffmpeg, native processing or sufficient CPU resources.

## Known limitations

- X changes its media endpoints; yt-dlp can fail even for public posts. Update yt-dlp when this occurs. **No live X conversion was verified in the build environment.**
- Native share sheets cannot silently send a message or force selection of WhatsApp. iOS/WhatsApp may also handle GIF files as a document or static image; test with your specific device.
- Only the first 20 seconds are converted; videos longer than 2 minutes or source files over 35 MB are refused. Generated GIF capped at 15 MB. GIFs have no audio.
- Temporary source/GIF files are deleted after each request. No account or persistent gallery.
- Only convert media you're allowed to reuse. Public access does not itself grant copyright permissions. Review X and hosting terms.
- For public launch, add production-grade per-IP rate limits, job timeouts at the proxy/container level, request quotas, abuse monitoring, and storage/bandwidth limits. Current demo limits concurrent conversions to two but is **not** an abuse-proof production service.

## Try a sample post

Paste: `https://x.com/caismila/status/2120335263041986807`.
This URL is preselected only for testing; accessibility and GIF extraction are not guaranteed.

## Deploy to Render (free testing tier)

1. Create a new **public GitHub repository** and upload the files from the `x-to-whatsapp-gif` folder **at the repository root**.
2. In Render, click **New > Web Service** and connect that GitHub repository.
3. Select **Docker** runtime and **Free** instance. Render detects the `Dockerfile`; choose `main` branch and deploy.
4. After deployment, use the assigned `https://...onrender.com` URL on a mobile phone.
5. Open `/health` to verify the service and test a public X post with an embedded GIF. Check Render logs if extraction fails.

Free instances may sleep after 15 minutes idle, have resource and bandwidth limits, and aren't suitable for monetized production traffic without rate limits. X extraction and WhatsApp share behavior need real-world testing.


## v0.2: iPhone Shortcuts & outreach workspace

- `/shortcut` explains a share-sheet Shortcut that passes an encoded X post URL to `/?auto=1&url=...`. The converter starts automatically; iOS still requires the user to confirm sharing the resulting GIF.
- `/outreach` is a **manual** promotion workspace with X search links and customizable reply drafts. It does not read X posts, use an AI model, or automatically publish replies.
- A best-effort per-IP conversion limit (12/hour) protects the free server. For public production traffic, add cloud edge rate limiting / stronger caching and abuse controls.
- Real X video retrieval, Render free plan availability, and native WhatsApp file sharing must be verified on the deployed site and real mobile devices.
- No user accounts, cloud GIF library, advertising SDK, or tracking enabled.

### Deployment (Render)
1. Create a GitHub repository and upload the **contents of `x-to-whatsapp-gif/` to its root**.
2. In Render create a new **Web Service** from the repository. Choose Docker and, when offered, Free (plans may change).
3. Health check: `/health`. Wait for a successful deployment and test the public HTTPS URL.
4. Try a public X GIF, then use an actual iPhone to test Share → WhatsApp.
5. Open `/shortcut` from your public domain to set up the iPhone shortcut.

### Safety / operational constraints
Use only media that you have the right to reproduce. X may change access and cause yt-dlp to fail. Avoid exposing a third-party download proxy or arbitrary URLs. The server does not retain converted GIFs outside temporary processing. IP throttling is process-local; not a security boundary. X prohibits unsolicited keyword-triggered automated replies: `/outreach` intentionally supports only manual, context-aware replies.

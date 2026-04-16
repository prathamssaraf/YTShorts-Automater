# YTShorts-Automater — story/history Shorts on autopilot

Fully local pipeline that turns a topic (or "today in history") into a finished 9:16 YouTube Short with cinematic AI-generated visuals, AI-narrated voiceover, burned-in subtitles, and royalty-free background music. One command bootstraps everything; one command tears it all down.

```
Topic → LLM script (Ollama) → per-scene video (Kling) →
  TTS narration (Kokoro) → subtitles (whisper.cpp) →
  music (Pixabay) → FFmpeg compose → YouTube upload
```

## What you need

### System dependencies (one-time)

```bash
brew install python@3.12 ffmpeg-full cmake
xcode-select --install   # if not already installed
```

> ⚠️ Use **`ffmpeg-full`**, not the slim `ffmpeg` formula. The slim build lacks libass, so subtitles silently disappear.

### Local AI services (free)

- **Ollama** — https://ollama.com — then `ollama pull llama3.2` (or `qwen2.5:14b` for richer scripts)
- **Kokoro TTS** — auto-downloaded into `vendor/kokoro/` by `run.sh` (~360MB total)
- **whisper.cpp** — auto-cloned + built into `vendor/whisper.cpp/` by `run.sh`

### API keys you need

| Key | Purpose | Required? | Where to get it |
|---|---|---|---|
| **Kling Access Key** + **Secret Key** | Video generation (66 free credits/day) | ✅ Required | https://app.klingai.com/global/dev — sign in with Google, click "API Keys", copy both |
| **Pixabay API Key** | Background music | Optional (silent track if omitted) | https://pixabay.com/api/docs/ — sign up, key shown immediately |
| **YouTube OAuth `client_secrets.json`** | Upload to your channel | Optional (only for non-`--dry-run`) | Google Cloud Console → Credentials → OAuth 2.0 Client ID (Desktop app) → download JSON → save as `config/client_secrets.json` |

#### Set the keys

Easiest: copy the template and fill in:

```bash
cp .env.example .env
# then edit .env with your keys
```

`.env` is gitignored. `run.sh` auto-loads it before launching the pipeline.

Alternative: edit `config/settings.yaml` directly under `visual.kling.access_key`, `visual.kling.secret_key`, and `music.pixabay_api_key`.

## Run it

```bash
./run.sh                              # bootstrap + use today's on-this-day topic
./run.sh --topic "Fall of Constantinople"
./run.sh --topic "Apollo 13" --dry-run
./run.sh --bootstrap-only             # set up everything, don't run
./run.sh --cleanup                    # remove .venv/, vendor/, workspace/
```

First run takes ~5–10 minutes (deps install, whisper.cpp build, model downloads). Subsequent runs reuse everything.

## What happens, stage by stage

1. **Topic resolution** — your `--topic` flag, or a random Wikipedia "on this day" event for today's date. Optionally fetches the Wikipedia summary as factual grounding.
2. **Script writing** — Ollama (default `llama3.2`) generates a 5–8 scene script: each scene = one narration sentence + one cinematic visual prompt + ~6s duration. Constrained to use only facts from the grounding.
3. **TTS** — Kokoro synthesises one WAV per scene; concatenated into the full narration track.
4. **Per-scene video** — Visual Manager cascades through providers in order:
   - **Kling** (free tier, 66 credits/day) — primary
   - **LTX-Video** (local, optional, slow) — fallback if Kling is exhausted
5. **Subtitles** — whisper.cpp transcribes the narration WAV; SRT is converted to a styled ASS file with bottom-aligned outlined text.
6. **Music** — Pixabay search by mood keyword, fallback to silent track.
7. **Compose** — FFmpeg trims/loops each scene clip to its narration duration, concats them, burns subtitles, mixes narration + music, muxes to final mp4.
8. **Upload** — YouTube Data API v3 with OAuth (skipped in `--dry-run`).

## Output

| File | What it is |
|---|---|
| `workspace/output/final_<run_id>.mp4` | The finished 1080×1920 Short |
| `workspace/scenes/<run_id>/scene_NN.mp4` | Per-scene Kling clips (kept on failure) |
| `workspace/audio/<run_id>/scene_NN.wav` + `narration.wav` | TTS audio (kept on failure) |
| `workspace/clips/<run_id>.srt` | Whisper transcript (kept on failure) |
| `logs/runs.jsonl` | Permanent append-only log, one JSON object per run |

`run_id` is the UUID printed in the log line `run <uuid> recorded (success|failed)`.

Open the latest Short:
```bash
ls -t workspace/output/*.mp4 | head -1 | xargs open
```

Inspect the latest run log:
```bash
tail -1 logs/runs.jsonl | jq .
```

## Free-tier capacity

A typical 50s Short uses 8 × 5s Kling clips. Kling free tier = 66 credits/day, ~6 clips/day → roughly **one full Short per day on the free tier**.

To go beyond:
- Set `visual.ltx_video.enabled: true` and `pip install diffusers torch transformers accelerate imageio` to enable the local fallback (slow, ~30s–2min per clip but unlimited).
- Or top up Kling credits for ~$10/month.

## Cleanup

```bash
./run.sh --cleanup
```

Removes `.venv/`, `vendor/`, and `workspace/` — leaves your source, `logs/`, and `.env`.

## Project layout

```
YTShorts-Automater/
├── run.sh                       single entrypoint
├── requirements.txt
├── .env.example                 copy → .env, fill in your keys
├── config/
│   └── settings.yaml            all tunables
├── pipeline/
│   ├── orchestrator.py          wires stages together
│   ├── topic_source.py          CLI topic + today-in-history + Wikipedia grounding
│   ├── intelligence/            Ollama client + script writer + prompts
│   ├── visual/                  Kling provider + LTX-Video provider + manager
│   ├── audio/                   Kokoro TTS + Pixabay music
│   ├── video/                   composer + transcriber
│   ├── upload/                  YouTube uploader
│   └── logging/                 per-run JSON log
├── vendor/                      whisper.cpp + Kokoro models + HF cache (gitignored)
├── workspace/                   intermediate files (gitignored)
└── logs/runs.jsonl              permanent run history
```

## Troubleshooting

| Error | Fix |
|---|---|
| `Kling not configured` | Set `KLING_ACCESS_KEY` + `KLING_SECRET_KEY` in `.env` (free, instant signup) |
| `Kokoro model files missing` | `./run.sh --bootstrap-only` |
| `Ollama not responding` | `ollama serve &` in another terminal |
| `whisper.cpp binary not found` | `./run.sh --bootstrap-only` to rebuild |
| Subtitles silently missing | `brew uninstall --ignore-dependencies ffmpeg && brew install ffmpeg-full` |
| `cmake not found` | `brew install cmake` |
| `Missing client_secrets.json` | Only needed for real uploads (not `--dry-run`) — see "API keys" above |
| Kling "task failed" / quota error | Free tier exhausted for the day; wait 24h or enable LTX-Video fallback |

## License

MIT — do whatever you want.

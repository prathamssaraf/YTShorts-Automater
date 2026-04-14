# Cricket YouTube Shorts — one-command local pipeline

Fully local, on-device automation that turns a completed cricket match into a finished YouTube Short. No cloud AI. No subscriptions. Nothing installed outside this folder (except Homebrew's `ffmpeg` and the Ollama desktop app, which are system-level).

See [`CRICKET_SHORTS_PLAN.md`](CRICKET_SHORTS_PLAN.md) for the full design spec — this README only covers setup and running.

## Requirements

- Apple Silicon Mac (M1 or later — M5 48GB recommended for `large-v3` Whisper + MusicGen)
- `python3.12` on `$PATH` — `brew install python@3.12`
- `ffmpeg-full` — `brew install ffmpeg-full` (the plain `ffmpeg` formula is a slim build without libass, so subtitles won't render)
- `cmake` — `brew install cmake` (required to build whisper.cpp)
- Xcode Command Line Tools — `xcode-select --install`
- Ollama (optional but strongly recommended) — https://ollama.com, then:
  ```
  ollama serve &            # leave running in another terminal
  ollama pull llama3.2      # or any model you want; update llm.model in settings.yaml
  ```

## One-command run

```bash
./run.sh                         # bootstrap (one time) + run one match
./run.sh --match-id 1527693      # run for a specific Cricinfo match id
./run.sh --dry-run               # run every stage EXCEPT YouTube upload
./run.sh --watch                 # polling loop: fires when a match completes
./run.sh --bootstrap-only        # just build the venv + whisper.cpp
./run.sh --cleanup               # remove .venv, vendor/, workspace/ (keeps source + logs)
```

### Finding a cricinfo match id

Open the match page on espncricinfo.com and grab the 7-digit number at the end of the URL slug:

```
https://www.espncricinfo.com/series/ipl-2026-1510719/.../mumbai-indians-vs-royal-challengers-bengaluru-20th-match-1527693/full-scorecard
                                                                                                              ^^^^^^^
```

That `1527693` is what you pass to `--match-id`.

## Producing multiple videos

Three ways to make more than one Short:

### 1. Multiple Shorts from the **same match** (different players/moments)

```bash
./run.sh --match-id 1527693 --count 3 --dry-run
```

The LLM picks the strongest player/moment for Short #1, then re-plans with that player excluded for Short #2, and so on. If the LLM starts repeating (match didn't have enough distinct star performances) the loop stops early. Each Short gets its own `run_id`, its own `workspace/output/final_<run_id>.mp4`, and its own line in `logs/runs.jsonl`.

### 2. Multiple matches in a shell loop

```bash
for m in 1527693 1527686 1527679; do
  ./run.sh --match-id "$m" --count 2 --dry-run
done
```

Six Shorts total — two per match. Each invocation does its own bootstrap check (instant after the first) and cleans its own workspace.

### 3. Continuous production via `--watch`

```bash
./run.sh --watch --count 3
```

Polls ESPNcricinfo every `schedule.check_interval_minutes` (default 30 min). When a match completes, produces 3 Shorts from it. Runs forever — Ctrl+C to stop.

### How many is realistic?

- **Blockbuster match** (5+ standout performers) → `--count 5` works fine
- **One-sided / low-scoring match** → `--count 2` max; the LLM will stop early rather than repeat

### Summary output

At the end of a multi-Short run you'll see something like:

```
DONE: 3 succeeded, 0 failed (3 total)
  ✓ Virat Kohli    → workspace/output/final_abc123.mp4
  ✓ Phil Salt      → workspace/output/final_def456.mp4
  ✓ Rajat Patidar  → workspace/output/final_ghi789.mp4
```

### Duplicate-match protection

A match that already has **one** successful Short in `logs/runs.jsonl` will be skipped on subsequent single-Short invocations. Passing `--count N` with `N > 1` bypasses the check — so you can always add more Shorts to a previously-processed match by re-running with `--count`.

What `run.sh` does on first run:

1. Creates `./.venv` with Python 3.12
2. `pip install -r requirements.txt` (+ best-effort `musicgen-mlx`)
3. Clones `./vendor/whisper.cpp`, builds it with `WHISPER_METAL=1`
4. Downloads `ggml-base.en.bin` (~150MB) for Whisper — upgrade to `large-v3` with:
   `bash vendor/whisper.cpp/models/download-ggml-model.sh large-v3`
5. Checks Ollama is reachable at `http://localhost:11434` (warns if not)
6. Points `HF_HOME` at `./vendor/hf_cache` so the MusicGen weights also stay in-repo
7. Runs the pipeline

Rerunning is idempotent — dependencies re-install only if `requirements.txt` changes.

## Configure before you run

Open `config/settings.yaml`:

- `llm.model` — whatever you pulled in Ollama (default `llama3.2`)
- `youtube.preferred_channels` — official channel IDs you trust (ICC, IPL, Cricbuzz pre-populated)
- `upload.*` — only matters for real (non-dry-run) uploads

**No YouTube API key is required.** Search uses two free sources merged:
- Public channel RSS feeds (`https://www.youtube.com/feeds/videos.xml?channel_id=...`) for your preferred official channels
- `yt-dlp`'s built-in search for the long tail

For YouTube **uploads** you still need OAuth (no way around it):

1. In Google Cloud Console → APIs & Services → Credentials, create an **OAuth 2.0 Client ID** of type "Desktop app" (enable "YouTube Data API v3" on the project first).
2. Download the JSON and save as `config/client_secrets.json`.
3. First real upload will open a browser window; the token lands in `config/youtube_credentials.json` and gets auto-refreshed afterwards.

You can skip OAuth entirely if you only run with `--dry-run` — the pipeline produces `workspace/output/final_*.mp4` for you to upload manually.

Run once without touching YouTube:

```bash
./run.sh --match-id <id> --dry-run
```

## Tearing it all down

```bash
./run.sh --cleanup
```

Removes `.venv/`, `vendor/` (whisper.cpp + HF cache), and `workspace/`. Leaves your source tree and `logs/runs.jsonl` so you keep a permanent record of what was produced. Ollama-pulled models live under `~/.ollama` and are untouched — delete them with `ollama rm <model>` if you want to reclaim that space.

## Project layout

```
cricket-shorts/
├── run.sh                     ← single entrypoint (bootstrap + run + cleanup)
├── CRICKET_SHORTS_PLAN.md     ← full design spec
├── requirements.txt
├── config/
│   └── settings.yaml
├── pipeline/
│   ├── orchestrator.py        ← wires stages together
│   ├── trigger.py             ← match-end detector
│   ├── config.py
│   ├── data/                  ← scorecard + news + context
│   ├── intelligence/          ← Ollama client + prompts + decision maker
│   ├── video/                 ← search, download, scene pick, transcribe, music, overlay, edit
│   ├── upload/                ← YouTube uploader
│   └── logging/               ← per-run JSON log
├── vendor/                    ← whisper.cpp + hf_cache (created by run.sh)
├── workspace/                 ← temp files (cleared after each successful run)
└── logs/runs.jsonl            ← permanent run history
```

## Logs

Every run appends one JSON object to `logs/runs.jsonl` with match info, the LLM's reasoning, the YouTube source used, stage timings, and the final upload URL (or local path on dry-run/upload-failure). `jq` over this file is the easiest way to review what the pipeline has been shipping.

## Known limitations

- No AI voiceover yet — add Kokoro MLX if you want narration
- No upload scheduling — always uploads immediately
- No analytics feedback loop — metrics don't influence future decisions
- Scorecard quality depends on the unofficial `cricdata` package; pipeline falls back to a rule-based plan if it breaks
- Must verify copyright on non-CC YouTube sources — that's your responsibility

## Output locations

| File | What it is |
|---|---|
| `workspace/output/final_<run_id>.mp4` | The finished 1080×1920 Short (one per successful run) |
| `workspace/downloads/<video_id>.mp4` | Raw yt-dlp download (temp, cleared after a successful run) |
| `workspace/clips/<run_id>_*.mp4` | Intermediate cuts: trim → portrait → subs → overlay (temp) |
| `workspace/clips/<run_id>.srt` / `.ass` | Whisper transcript + styled subtitle file (temp) |
| `workspace/audio/<run_id>_music.wav` | MusicGen output or silent track (temp) |
| `logs/runs.jsonl` | Permanent append-only log, one JSON object per Short |

`run_id` is the UUID shown in the log line `run <uuid> recorded (success|failed)`. It is **not** the cricinfo match id — one match with `--count 3` produces three different `run_id`s.

Open the most recent Short:

```bash
ls -t workspace/output/*.mp4 | head -1 | xargs open
```

Inspect the latest run log:

```bash
tail -1 logs/runs.jsonl | jq .
```

## Troubleshooting

- `Ollama not responding` — start it with `ollama serve` in another terminal
- `whisper.cpp binary not found` — `./run.sh --bootstrap-only` to rebuild
- `cmake not found` — `brew install cmake`, then rerun bootstrap
- `No option name near ... subtitles=...` / subtitles silently missing — your ffmpeg is a slim build without libass. Homebrew's mainline `ffmpeg` formula is now slim by default; you need the full formula:
  ```
  brew uninstall --ignore-dependencies ffmpeg && brew install ffmpeg-full
  ```
  Verify: `ffmpeg -hide_banner -filters | grep -E '^ .. (ass|subtitles) '` should list both. The pipeline also auto-detects the missing filter and skips subtitles with a warning instead of crashing.
- `Missing client_secrets.json` — only needed for real uploads, not `--dry-run`
- `musicgen-mlx install failed` — pipeline uses silent music; not fatal
- `No YouTube candidates found` — search failed; check internet and `youtube.preferred_channels` in `config/settings.yaml`
- `match X already has a successful Short — skipping` — pass `--count N` (with N ≥ 2) to produce additional Shorts for a match already in the log

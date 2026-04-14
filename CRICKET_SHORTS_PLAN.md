# Cricket YouTube Shorts Automation — Full Project Plan

*(Full plan as supplied by the user is the source of truth for this repo. See `README.md` for setup and `run.sh` for the single-command entry point. This file is kept in-repo so future contributors — human or agent — have the original spec alongside the code.)*

Everything is local to this folder: venv at `./.venv`, whisper.cpp under `./vendor/whisper.cpp`, HuggingFace cache at `./vendor/hf_cache`, workspace temp files in `./workspace/`, permanent run log at `./logs/runs.jsonl`.

Run the whole thing:

```bash
./run.sh                     # bootstrap + run one match (auto-picks latest completed)
./run.sh --match-id <id>     # run for a specific match
./run.sh --watch             # run the polling trigger (infinite loop)
./run.sh --dry-run           # all stages except YouTube upload
./run.sh --cleanup           # remove .venv, vendor/, workspace/  (leaves source + logs)
```

Build order, module contracts, settings schema, error-handling rules: see source files under `pipeline/` and comments in `config/settings.yaml`.

# Langfuse Observability Plugin: Issue Resolution Report

## Overview
This report documents the resolution of an issue where traces/log entries from the Antigravity agent were not appearing in the local Langfuse instance (`http://192.168.1.49:3000`).

## Diagnostics & Root Cause Analysis

### 1. Missing Environment Variables
The `langfuse-observability` plugin executes [langfuse_hook.py](../hooks/langfuse_hook.py) to collect conversation logs and sync them to Langfuse.
- **Symptom:** The hook script exited silently with `0` without uploading any traces.
- **Root Cause:** The hook script checks `os.environ` for `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL` (or `LANGFUSE_HOST`). If any of these are missing, it terminates silently (`sys.exit(0)`). Because the Antigravity parent process was started without having these variables exported globally in the shell environment, they were missing in the hook subprocess environment. The credentials only existed in the project's local `.env` file, which was not read by the hook script because the script's working directory is the plugin folder rather than the project workspace root.

### 2. Path Resolution of the `uv` Binary
The hook command in [hooks.json](./hooks.json) used `command -v uv` to check if the `uv` toolchain was available to automatically run Python scripts with inline dependency metadata.
- **Symptom:** The command fell back to `python3`, which lacked the `langfuse` package, causing trace collection to fail with `ModuleNotFoundError` when run in environments where `/home/dzmitry/.local/bin/uv` was not automatically added to the shell subprocess `PATH`.
- **Root Cause:** IDE extensions or agent launchers might spawn non-interactive shells that do not load the user's full `.bashrc` or profile where `uv`'s installation path (`$HOME/.local/bin`) is defined.

### 3. Misplaced `hooks.json` Configuration File
- **Symptom:** The plugin hooks never executed automatically when steps ended or sessions closed.
- **Root Cause:** The `hooks.json` configuration file was placed in the `hooks/` subdirectory (at `hooks/hooks.json`). The Antigravity CLI expects all plugins to define their hooks at the plugin's root directory (`langfuse-observability/hooks.json`), similar to the `sdlc` plugin. As a result, the CLI never detected or registered the `Stop` and `SessionEnd` hooks for the `langfuse-observability` plugin.

---

## Solution Implementation

To make the plugin robust and self-contained, we implemented the following changes:

### 1. Workspace Detection and `.env` Loading
We updated [langfuse_hook.py](../hooks/langfuse_hook.py) to:
- Resolve the project ID using `ANTIGRAVITY_PROJECT_ID`.
- Look up the active workspace filesystem path mapping from the Antigravity project cache at `~/.gemini/antigravity-cli/cache/projects.json`.
- Load and parse the project's `.env` file dynamically if the Langfuse environment variables are not already present in the system environment.
- Fall back to searching parent directories starting from the current working directory (`os.getcwd()`).

```python
        # Check environment variables
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
        base_url = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")

        # Fallback: try loading from .env if variables are missing
        if not public_key or not secret_key or not base_url:
            candidate_dirs = []
            
            # 1. Try finding by ANTIGRAVITY_PROJECT_ID in projects.json
            project_id = os.environ.get("ANTIGRAVITY_PROJECT_ID")
            if project_id:
                projects_cache_path = os.path.expanduser("~/.gemini/antigravity-cli/cache/projects.json")
                if os.path.exists(projects_cache_path):
                    try:
                        with open(projects_cache_path, "r", encoding="utf-8") as f:
                            projects_cache = json.load(f)
                            for path, pid in projects_cache.items():
                                if pid == project_id:
                                    candidate_dirs.append(path)
                                    break
                    except Exception:
                        pass

            # 2. Add current working dir as fallback
            candidate_dirs.append(os.getcwd())
            ...
```

### 2. Multi-Path Hook Fallbacks
We updated the hook command in [hooks.json](./hooks.json) to explicitly locate `uv` in its standard local directory (`$HOME/.local/bin/uv`) if it is not in the active `PATH`:

```json
"command": "if command -v uv >/dev/null 2>&1; then uv run ./hooks/langfuse_hook.py; elif [ -x \"$HOME/.local/bin/uv\" ]; then \"$HOME/.local/bin/uv\" run ./hooks/langfuse_hook.py; else python3 ./hooks/langfuse_hook.py; fi"
```

### 3. Relocation of `hooks.json` to the Plugin Root
We moved the `hooks.json` file from the `hooks/hooks.json` path to the root plugin directory [hooks.json](./hooks.json) (at `/home/dzmitry/.gemini/config/plugins/langfuse-observability/hooks.json`) so the CLI automatically registers the hooks.

### 4. Hook Execution Logging
Added robust debugging logging directly inside [langfuse_hook.py](../hooks/langfuse_hook.py) to write trace states, processed environment details, and unhandled exception tracebacks to `~/.gemini/antigravity-cli/state/langfuse_hook.log`.

### 5. Dynamic Model Name Extraction
To capture model changes dynamically (e.g., when subagents use different models):
- Added the `extract_model_from_prompt(prompt_text)` helper function to [langfuse_hook.py](../hooks/langfuse_hook.py).
- Uses regex queries to inspect the first user input turn of the session (e.g., matching `"assignedModel": "model-name"`, `Assigned model: model-name`, or looking for keyword matches like `gemini-*` or `claude-*`).
- If no model is explicitly requested in the prompt, it falls back to the default project-wide model set in [settings.json](../settings.json).

---

## Validation & Verification

A simulation run was executed simulating the exact hook environment (with cleared shell credentials, running from the plugin's working directory, and providing the active conversation and project IDs):
- **Command:** `export ANTIGRAVITY_PROJECT_ID="..." && export ANTIGRAVITY_CONVERSATION_ID="..." && unset LANGFUSE_PUBLIC_KEY && unset LANGFUSE_SECRET_KEY && /home/dzmitry/.local/bin/uv run ./hooks/langfuse_hook.py`
- **Result:** Successfully parsed the project workspace path, loaded the keys from the `.env` file, and successfully pushed the full session trace to the Langfuse instance.
- **Trace Verified:** Traces for conversation `f0f1066a-566a-42a1-b542-b96f31466db1` are verified as updated and populated on the Langfuse UI:
  - **Trace ID:** `f0f1066a-566a-42a1-b542-b96f31466db1`
  - **Project Name:** `tokenprices`
  - **Organization:** `nochnik`
  - **Status:** Verified working automatically.

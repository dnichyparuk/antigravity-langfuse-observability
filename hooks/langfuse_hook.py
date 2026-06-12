#!/usr/bin/env python3
# /// script
# dependencies = [
#   "langfuse>=2.0.0,<3.0.0",
# ]
# ///

import os
import sys
import json
import re
import traceback
from datetime import datetime

def log_debug(message):
    try:
        log_path = os.path.expanduser("~/.gemini/antigravity-cli/state/langfuse_hook.log")
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"[{datetime.now().isoformat()}] {message}\n")
    except Exception:
        pass

def get_model_name():
    try:
        settings_path = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
                return settings.get("model", "Gemini 3.5 Flash")
    except Exception:
        pass
    return "Gemini 3.5 Flash"

def extract_model_from_prompt(prompt_text):
    if not prompt_text:
        return None
    # 1. Look for "assignedModel": "model-name"
    m = re.search(r'"assignedModel"\s*:\s*"([^"]+)"', prompt_text)
    if m:
        return m.group(1)
        
    # 2. Look for Assigned model: model-name
    m = re.search(r'[aA]ssigned\s+[mM]odel\s*:\s*([a-zA-Z0-9.-]+)', prompt_text)
    if m:
        return m.group(1)
        
    # 3. Look for any mention of gemini/claude/gpt model names in the prompt
    m = re.search(r'(gemini-[a-zA-Z0-9.-]+|claude-[a-zA-Z0-9.-]+|gpt-[a-zA-Z0-9.-]+)', prompt_text, re.IGNORECASE)
    if m:
        return m.group(1).lower()
        
    return None

def main():
    log_debug("--- Starting Langfuse Hook ---")
    log_debug(f"PWD: {os.getcwd()}")
    log_debug(f"ANTIGRAVITY_PROJECT_ID: {os.environ.get('ANTIGRAVITY_PROJECT_ID')}")
    log_debug(f"ANTIGRAVITY_CONVERSATION_ID: {os.environ.get('ANTIGRAVITY_CONVERSATION_ID')}")
    # Fail-Open Safety: Wrap everything in a global try...except block
    try:
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

            # Find and parse .env in candidate directories
            for start_dir in candidate_dirs:
                cur_dir = start_dir
                for _ in range(5):
                    dotenv_path = os.path.join(cur_dir, ".env")
                    if os.path.exists(dotenv_path):
                        try:
                            with open(dotenv_path, "r", encoding="utf-8") as dotenv_file:
                                for line in dotenv_file:
                                    line = line.strip()
                                    if not line or line.startswith("#"):
                                        continue
                                    parts = line.split("=", 1)
                                    if len(parts) == 2:
                                        key = parts[0].strip().strip('"').strip("'")
                                        val = parts[1].strip().strip('"').strip("'")
                                        if key == "LANGFUSE_PUBLIC_KEY" and not public_key:
                                            public_key = val
                                        elif key == "LANGFUSE_SECRET_KEY" and not secret_key:
                                            secret_key = val
                                        elif (key == "LANGFUSE_BASE_URL" or key == "LANGFUSE_HOST") and not base_url:
                                            base_url = val
                        except Exception:
                            pass
                    parent_dir = os.path.dirname(cur_dir)
                    if parent_dir == cur_dir:
                        break
                    cur_dir = parent_dir
                if public_key and secret_key and base_url:
                    break

        log_debug(f"Credentials status - PK: {public_key is not None}, SK: {secret_key is not None}, BaseURL: {base_url}")
        if not public_key or not secret_key or not base_url:
            log_debug("Exit: Missing credentials.")
            sys.exit(0)

        conversation_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
        if not conversation_id:
            log_debug("Exit: Missing ANTIGRAVITY_CONVERSATION_ID.")
            sys.exit(0)

        # Resolve paths
        transcript_path = os.path.expanduser(
            f"~/.gemini/antigravity-cli/brain/{conversation_id}/.system_generated/logs/transcript.jsonl"
        )
        log_debug(f"Transcript path: {transcript_path}")
        if not os.path.exists(transcript_path):
            log_debug(f"Exit: Transcript path does not exist.")
            sys.exit(0)

        state_dir = os.path.expanduser("~/.gemini/antigravity-cli/state")
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, "langfuse_state.json")

        # Load existing state
        conversations_state = {}
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    conversations_state = json.load(f)
            except Exception:
                pass

        sent_spans = conversations_state.get(conversation_id, {}).get("sent_spans", {})

        # Parse transcript.jsonl
        steps = []
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    steps.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not steps:
            sys.exit(0)

        # Import langfuse SDK (inside try...except to catch import errors if not installed)
        from langfuse import Langfuse

        langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=base_url
        )

        # Helper to parse ISO format datetimes
        def parse_iso(dt_str):
            if not dt_str:
                return None
            if isinstance(dt_str, datetime):
                return dt_str
            s = dt_str
            if s.endswith('Z'):
                s = s[:-1] + '+00:00'
            try:
                return datetime.fromisoformat(s)
            except Exception:
                return dt_str

        # Reconstruct turns and tool calls
        turns = []
        pending_tool_calls = []
        current_turn = None

        for step in steps:
            step_type = step.get("type")
            step_index = step.get("step_index")
            created_at = step.get("created_at")
            content = step.get("content", "")

            if step_type == "USER_INPUT":
                if current_turn:
                    current_turn["end_time"] = created_at

                current_turn = {
                    "id": f"{conversation_id}-turn-{step_index}",
                    "step_index": step_index,
                    "start_time": created_at,
                    "end_time": created_at,
                    "input": content,
                    "output": "",
                    "tool_calls": []
                }
                turns.append(current_turn)

            elif step_type == "PLANNER_RESPONSE":
                if current_turn:
                    current_turn["end_time"] = created_at
                    current_turn["output"] += content

                    tool_calls = step.get("tool_calls", [])
                    if isinstance(tool_calls, list):
                        for idx, tool_call in enumerate(tool_calls):
                            if not isinstance(tool_call, dict):
                                continue
                            tool_name = tool_call.get("name")
                            tool_args = tool_call.get("args")
                            tool_id = f"{conversation_id}-tool-{step_index}-{idx}"

                            tool_info = {
                                "id": tool_id,
                                "name": tool_name,
                                "input": tool_args,
                                "start_time": created_at,
                                "end_time": created_at,
                                "output": "",
                                "status": "DONE"
                            }
                            current_turn["tool_calls"].append(tool_info)
                            pending_tool_calls.append(tool_info)

            elif step_type == "CONVERSATION_HISTORY":
                if current_turn:
                    current_turn["end_time"] = created_at

            else:
                # This is a tool response!
                if pending_tool_calls:
                    tool_info = pending_tool_calls.pop(0)
                    tool_info["end_time"] = created_at
                    tool_info["output"] = content
                    tool_info["status"] = step.get("status", "DONE")

                if current_turn:
                    current_turn["end_time"] = created_at

        if current_turn and steps:
            current_turn["end_time"] = steps[-1].get("created_at")

        # Send/update Langfuse objects
        # 1. Create/update Trace
        trace = langfuse.trace(
            id=conversation_id,
            name="antigravity-session",
            session_id=conversation_id
        )

        # Resolve the model name dynamically for this session
        detected_model = None
        if turns and turns[0].get("input"):
            detected_model = extract_model_from_prompt(turns[0]["input"])
        if not detected_model:
            detected_model = get_model_name()

        log_debug(f"Resolved model for this session: {detected_model}")

        updated_sent_spans = {}

        for turn in turns:
            turn_id = turn["id"]
            current_turn_state = {
                "end_time": turn["end_time"],
                "output": turn["output"]
            }

            stored_turn_state = sent_spans.get(turn_id)
            if not stored_turn_state or stored_turn_state != current_turn_state:
                input_len = len(turn["input"]) if turn["input"] else 0
                output_len = len(turn["output"]) if turn["output"] else 0
                
                langfuse.generation(
                    id=turn_id,
                    trace_id=conversation_id,
                    name="Turn",
                    model=detected_model,
                    input=turn["input"],
                    output=turn["output"],
                    start_time=parse_iso(turn["start_time"]),
                    end_time=parse_iso(turn["end_time"]),
                    usage={
                        "input": max(1, input_len // 4),
                        "output": max(1, output_len // 4),
                    }
                )
            updated_sent_spans[turn_id] = current_turn_state

            for tool in turn["tool_calls"]:
                tool_id = tool["id"]
                current_tool_state = {
                    "end_time": tool["end_time"],
                    "output": tool["output"],
                    "status": tool["status"]
                }

                stored_tool_state = sent_spans.get(tool_id)
                if not stored_tool_state or stored_tool_state != current_tool_state:
                    langfuse.span(
                        id=tool_id,
                        trace_id=conversation_id,
                        parent_observation_id=turn_id,
                        name=tool["name"],
                        input=tool["input"],
                        output=tool["output"],
                        start_time=parse_iso(tool["start_time"]),
                        end_time=parse_iso(tool["end_time"]),
                        level="ERROR" if tool["status"] == "ERROR" else "DEFAULT",
                    )
                updated_sent_spans[tool_id] = current_tool_state

        # Update state and write back
        conversations_state[conversation_id] = {
            "sent_spans": updated_sent_spans
        }

        # Prune old conversation states (keep max 50)
        if len(conversations_state) > 50:
            keys = list(conversations_state.keys())
            for k in keys[:-50]:
                conversations_state.pop(k, None)

        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(conversations_state, f, indent=2)
        except Exception:
            pass

        # Flush Langfuse client to ensure all requests are sent
        langfuse.flush()

        log_debug("Langfuse synchronization completed successfully.")
    except SystemExit as e:
        log_debug(f"SystemExit: {e.code}")
        sys.exit(e.code)
    except Exception as e:
        log_debug(f"Unhandled Exception:\n{traceback.format_exc()}")
        sys.exit(0)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# /// script
# dependencies = [
#   "langfuse>=2.0.0,<3.0.0",
# ]
# ///

import os
import sys
import json
from datetime import datetime

def main():
    # Fail-Open Safety: Wrap everything in a global try...except block
    try:
        # Check environment variables
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
        base_url = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")

        if not public_key or not secret_key or not base_url:
            sys.exit(0)

        conversation_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
        if not conversation_id:
            sys.exit(0)

        # Resolve paths
        transcript_path = os.path.expanduser(
            f"~/.gemini/antigravity-cli/brain/{conversation_id}/.system_generated/logs/transcript.jsonl"
        )
        if not os.path.exists(transcript_path):
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

        updated_sent_spans = {}

        for turn in turns:
            turn_id = turn["id"]
            current_turn_state = {
                "end_time": turn["end_time"],
                "output": turn["output"]
            }

            stored_turn_state = sent_spans.get(turn_id)
            if not stored_turn_state or stored_turn_state != current_turn_state:
                langfuse.span(
                    id=turn_id,
                    trace_id=conversation_id,
                    name="Turn",
                    input=turn["input"],
                    output=turn["output"],
                    start_time=parse_iso(turn["start_time"]),
                    end_time=parse_iso(turn["end_time"]),
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

    except SystemExit:
        pass
    except Exception:
        sys.exit(0)

if __name__ == "__main__":
    main()

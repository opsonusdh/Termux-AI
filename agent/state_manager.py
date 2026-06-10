import json
import os
import sys
from datetime import datetime

# Ensure project root is on sys.path so 'import paths' works from anywhere.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import paths

STATE_FILE = paths.STATE_FILE


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, 'r') as f:
        return json.load(f)


def save_state(state):
    state['last_updated'] = datetime.now().isoformat()
    if 'checkpoints' not in state:
        state['checkpoints'] = []
    checkpoint = {
        "timestamp":        state['last_updated'],
        "cursor":           state.get("cursor"),
        "active_task_id":   state.get("active_task_id"),
        "subtasks_summary": [
            {"id": t["id"], "status": t["status"]}
            for t in state.get("subtasks", [])
        ],
    }
    state['checkpoints'].append(checkpoint)
    if len(state['checkpoints']) > 5:
        state['checkpoints'].pop(0)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)


def _update_cursor_and_active_task(state, advance=False):
    """Update cursor and active_task_id after any subtask state change.

    advance=True: task completed/failed — move cursor forward past current pos.
    advance=False: non-terminal change — point at first active-or-pending task.
    """
    subtasks = state.get('subtasks', [])
    if not subtasks:
        state['cursor'] = None
        state['active_task_id'] = None
        return

    if advance:
        current_cursor = state.get('cursor') or 0
        next_task = next(
            (t for t in subtasks
             if t['id'] > current_cursor and t['status'] in ('pending', 'active')),
            None
        )
        state['cursor'] = next_task['id'] if next_task else None
    else:
        first = next(
            (t for t in subtasks if t['status'] in ('active', 'pending')),
            None
        )
        state['cursor'] = first['id'] if first else None

    active_task = next((t for t in subtasks if t['status'] == 'active'), None)
    state['active_task_id'] = active_task['id'] if active_task else None


def initialize_project(name, goal):
    state = {
        "project_name":   name,
        "goal":           goal,
        "status":         "active",
        "subtasks":       [],
        "blockers":       [],
        "last_updated":   datetime.now().isoformat(),
        "turn_count":     0,
        "personas":       [],
        "active_persona": None,
        "cursor":         None,
        "active_task_id": None,
        "crash_recovery": {
            "last_session_exit":  "normal",
            "interrupted_task":   None,
            "recovery_timestamp": None,
        },
        "checkpoints": [],
    }
    save_state(state)
    return state


def add_subtask(description):
    state = load_state()
    if not state:
        return "No active project found. Use initialize_project first."
    new_id = len(state.get('subtasks', [])) + 1
    state.setdefault('subtasks', []).append({
        "id":            new_id,
        "description":   description,
        "status":        "pending",
        "notes":         "",
        "verification":  "",
        "retry_count":   0,
        "worker_output": "",
        "critic_output": "",
    })
    _update_cursor_and_active_task(state)
    save_state(state)
    return f"Subtask {new_id} added: '{description}'"


def update_subtask(task_id, status=None, notes=None, verification=None,
                   retry_count=None, worker_output=None, critic_output=None):
    state = load_state()
    if not state:
        return "No active project found."
    found = False
    for task in state.get('subtasks', []):
        if task['id'] == task_id:
            if status is not None:        task['status']        = status
            if notes is not None:         task['notes']         = notes
            if verification is not None:  task['verification']  = verification
            if retry_count is not None:   task['retry_count']   = retry_count
            if worker_output is not None: task['worker_output'] = worker_output
            if critic_output is not None: task['critic_output'] = critic_output
            found = True
            break
    if not found:
        return f"Subtask with ID {task_id} not found."
    _update_cursor_and_active_task(state, advance=(status in ('completed', 'failed')))
    save_state(state)
    return f"Subtask {task_id} updated successfully."


def update_crash_recovery(last_session_exit, interrupted_task=None):
    state = load_state()
    if not state:
        return
    state['crash_recovery'] = {
        "last_session_exit":  last_session_exit,
        "interrupted_task":   interrupted_task,
        "recovery_timestamp": (
            datetime.now().isoformat() if last_session_exit == "interrupted" else None
        ),
    }
    save_state(state)


def increment_turn_count():
    state = load_state()
    if not state:
        return 0
    state['turn_count'] = state.get('turn_count', 0) + 1
    save_state(state)
    return state['turn_count']


def get_active_subtask():
    state = load_state()
    if not state:
        return None
    for task in state['subtasks']:
        if task['status'] in ['pending', 'active']:
            return task
    return None


def set_status(status):
    state = load_state()
    if not state:
        return
    state['status'] = status
    save_state(state)


def add_persona(name, description, prompt_modifier):
    state = load_state()
    if not state:
        return
    state.setdefault('personas', [])
    for persona in state['personas']:
        if persona['name'] == name:
            print(f"Persona '{name}' already exists.")
            return
    state['personas'].append({
        "name":            name,
        "description":     description,
        "prompt_modifier": prompt_modifier,
    })
    save_state(state)
    print(f"Persona '{name}' added.")


def activate_persona(name):
    state = load_state()
    if not state:
        return False
    found = any(p['name'] == name for p in state.get('personas', []))
    if found:
        state['active_persona'] = name
    else:
        print(f"Persona '{name}' not found.")
    save_state(state)
    return found


def get_active_persona():
    state = load_state()
    if not state:
        return None
    name = state.get('active_persona')
    if name:
        for p in state.get('personas', []):
            if p['name'] == name:
                return p
    return None


def get_wifi_scan_info():
    try:
        import importlib.util
        file_path = os.path.join(paths.TOOLS_DIR, "wrapper_termux_wifi_scaninfo.py")
        spec = importlib.util.spec_from_file_location("tools.wrapper_termux_wifi_scaninfo", file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.get_wifi_scan_info()
    except Exception as e:
        raise RuntimeError(f"Failed to get Wi-Fi scan info: {e}")


def get_battery_status():
    try:
        import importlib.util
        file_path = os.path.join(paths.TOOLS_DIR, "wrapper_termux_battery_status.py")
        spec = importlib.util.spec_from_file_location("tools.wrapper_termux_battery_status", file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.get_battery_status()
    except Exception as e:
        raise RuntimeError(f"Failed to get battery status: {e}")

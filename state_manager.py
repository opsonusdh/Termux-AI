import json
import os
import sys
from datetime import datetime

# Ensure ai_root is in sys.path
ai_root = os.path.dirname(os.path.abspath(__file__))
if ai_root not in sys.path:
    sys.path.append(ai_root)

import paths

STATE_FILE = paths.STATE_FILE

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, 'r') as f:
        return json.load(f)

def save_state(state):
    state['last_updated'] = datetime.now().isoformat()
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)

def initialize_project(name, goal):
    state = {
        "project_name": name,
        "goal": goal,
        "status": "active",
        "subtasks": [],
        "blockers": [],
        "last_updated": datetime.now().isoformat(),
        "turn_count": 0,
        "personas": [],
        "active_persona": None
    }
    save_state(state)
    return state

def add_subtask(description):
    state = load_state()
    if not state: return
    new_id = len(state['subtasks']) + 1
    state['subtasks'].append({
        "id": new_id,
        "description": description,
        "status": "pending",
        "notes": "",
        "verification": ""
    })
    save_state(state)

def update_subtask(task_id, status=None, notes=None, verification=None):
    state = load_state()
    if not state: return
    for task in state['subtasks']:
        if task['id'] == task_id:
            if status: task['status'] = status
            if notes: task['notes'] = notes
            if verification: task['verification'] = verification
            break
    save_state(state)

def increment_turn_count():
    state = load_state()
    if not state: return 0
    state['turn_count'] = state.get('turn_count', 0) + 1
    save_state(state)
    return state['turn_count']

def get_active_subtask():
    state = load_state()
    if not state: return None
    for task in state['subtasks']:
        if task['status'] in ['pending', 'active']:
            return task
    return None

def set_status(status):
    state = load_state()
    if not state: return
    state['status'] = status
    save_state(state)


def add_persona(name, description, prompt_modifier):
    state = load_state()
    if not state: return
    if 'personas' not in state:
        state['personas'] = []
    
    # Check if persona already exists
    for persona in state['personas']:
        if persona['name'] == name:
            print(f"Persona with name '{name}' already exists.")
            return

    state['personas'].append({
        "name": name,
        "description": description,
        "prompt_modifier": prompt_modifier
    })
    save_state(state)
    print(f"Persona '{name}' added.")

def activate_persona(name):
    state = load_state()
    if not state: return
    found = False
    for persona in state.get('personas', []):
        if persona['name'] == name:
            state['active_persona'] = name
            found = True
            break
    if not found:
        print(f"Persona with name '{name}' not found.")
    save_state(state)
    return found

def get_active_persona():
    state = load_state()
    if not state: return None
    active_persona_name = state.get('active_persona')
    if active_persona_name:
        for persona in state.get('personas', []):
            if persona['name'] == active_persona_name:
                return persona
    return None


def get_wifi_scan_info():
    """Retrieve Wi‑Fi scan info using the wrapper.
    Returns parsed JSON or raises RuntimeError.
    """
    try:
        from tools.wrapper_termux_wifi_scaninfo import get_wifi_scan_info as _func
        return _func()
    except Exception as e:
        raise RuntimeError(f"Failed to get Wi‑Fi scan info: {e}")

def get_battery_status():
    """Retrieve battery status using the wrapper.
    Returns parsed JSON or raises RuntimeError.
    """
    try:
        from tools.wrapper_termux_battery_status import get_battery_status as _func
        return _func()
    except Exception as e:
        raise RuntimeError(f"Failed to get battery status: {e}")

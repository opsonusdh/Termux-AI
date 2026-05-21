import json
import os
import state_manager

HISTORY_FILE = os.path.expanduser("~/ai_root/logs/history.jsonl")
SUMMARY_FILE = os.path.expanduser("~/ai_root/logs/context_summary.txt")
COMPRESSION_THRESHOLD = 10

def log_turn(role, content):
    turn = {"role": role, "content": content}
    with open(HISTORY_FILE, 'a') as f:
        f.write(json.dumps(turn) + "\n")
    
    # Increment turn count in state
    count = state_manager.increment_turn_count()
    
    if count >= COMPRESSION_THRESHOLD:
        trigger_compression()

def trigger_compression():
    # In a real scenario, this would involve sending the history to the LLM
    # to generate a summary. For now, we'll mark it as 'compression_required'
    # in the state so the agent knows to summarize in its next response.
    state = state_manager.load_state()
    state['compression_required'] = True
    state_manager.save_state(state)
    print("Context compression threshold reached.")

def get_summary():
    if os.path.exists(SUMMARY_FILE):
        with open(SUMMARY_FILE, 'r') as f:
            return f.read()
    return ""

def update_summary(summary_text):
    with open(SUMMARY_FILE, 'w') as f:
        f.write(summary_text)
    # Reset turn count or just clear history? 
    # Usually, we'd clear history and keep the summary.
    state = state_manager.load_state()
    state['turn_count'] = 0
    state['compression_required'] = False
    state_manager.save_state(state)
    
    # Archive history
    if os.path.exists(HISTORY_FILE):
        archive_name = HISTORY_FILE + ".old"
        os.rename(HISTORY_FILE, archive_name)

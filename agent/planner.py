"""
agent/planner.py — Lightweight task planner for Termux-AI.

Creates a flat execution plan from a task description and stores it via
state_manager. The actual subtask decomposition is driven by the LLM in
run_agent_step(); this module provides the data-layer scaffolding.
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent import state_manager

GRAY  = "\033[90m"
RESET = "\033[0m"


class Planner:
    """Create and store a flat execution plan."""

    def create_plan(self, task_description: str, subtasks: list[str] | None = None) -> dict:
        """
        Initialize a project from task_description and optionally pre-populate
        subtasks. Returns the plan dict.
        """
        plan = {
            "task":     task_description,
            "steps":    subtasks or [],
            "status":   "planned",
        }
        print(f"{GRAY}[PLANNER] Plan created for: {task_description}{RESET}")
        return plan

    def commit_plan(self, name: str, goal: str, subtasks: list[str]) -> dict:
        """
        Persist plan to state. Initializes project and adds subtasks.
        Returns the loaded state.
        """
        state_manager.initialize_project(name, goal)
        for desc in subtasks:
            state_manager.add_subtask(desc)
        print(f"{GRAY}[PLANNER] Plan committed: {len(subtasks)} subtask(s) added.{RESET}")
        return state_manager.load_state()

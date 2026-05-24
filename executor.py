"""Executor module for Orion.

Provides a simple ``execute_plan`` function that echoes the received plan.
"""

# Gray debug trail colors
GRAY  = "\033[90m"
RESET = "\033[0m"

def execute_plan(plan):
    """Execute the given *plan* (placeholder implementation).

    Returns a dict indicating the plan was "executed".
    """
    print(f"{GRAY}[EXECUTOR] Executing plan: {plan}{RESET}")
    return {"result": f"Executed: {plan}"}

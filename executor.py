"""Executor module for Orion.

Provides a simple ``execute_plan`` function that echoes the received plan.
"""

def execute_plan(plan):
    """Execute the given *plan* (placeholder implementation).

    Returns a dict indicating the plan was "executed".
    """
    print(f"Executing plan: {plan}")
    return {"result": f"Executed: {plan}"}

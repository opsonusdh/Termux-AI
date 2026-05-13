import os
import subprocess
from permissions import validate_command

AI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG_FILE = os.path.join(AI_ROOT, "log.txt")
END_MARKER = "<<<END_OF_COMMAND_OUTPUT>>>"


def run_commands(command_block):
    cmd = command_block.strip()

    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write(f"\nCMD:\n{cmd}\n")

    allowed, reason = validate_command(cmd)
    if not allowed:
        output = f"[BLOCKED] {reason}\n{END_MARKER}"
        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"OUT:\n{output}\n")
        return output

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20
        )

        out = result.stdout.strip()
        err = result.stderr.strip()

        output_parts = []
        if out:
            output_parts.append(out)
        if err:
            output_parts.append("[ERR]\n" + err)

        output_parts.append(END_MARKER)
        output = "\n".join(output_parts)

        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"OUT:\n{output}\n")

        return output

    except Exception as e:
        output = f"[EXCEPTION] {e}\n{END_MARKER}"
        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"OUT:\n{output}\n")
        return output

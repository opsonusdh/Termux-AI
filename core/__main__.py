import sys
import os
sys.dont_write_bytecode = True
os.system("export PYTHONDONTWRITEBYTECODE=1")

from interface import chat_loop
chat_loop()

# /// script
# dependencies = [
#   "anthropic>=0.75.0",
#   "pyyaml>=6.0",
#   "promql-parser>=0.1.0",
#   "pydantic>=2.0",
# ]
# ///

import os
import sys

# uv inline-script isolated envs don't inherit PYTHONPATH or add the script's
# parent to sys.path, so we inject it explicitly here.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from grading.verifier import main

if __name__ == "__main__":
    main()

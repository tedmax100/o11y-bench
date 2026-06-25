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

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)

# debug: show what's visible
print(f"[debug] __file__={__file__}")
print(f"[debug] script_dir={script_dir}")
print(f"[debug] sys.path[0]={sys.path[0]}")
print(f"[debug] /tests contents: {os.listdir('/tests') if os.path.exists('/tests') else 'N/A'}")
print(f"[debug] script_dir contents: {os.listdir(script_dir)}")
grading_dir = os.path.join(script_dir, 'grading')
if os.path.exists(grading_dir):
    print(f"[debug] grading/ contents: {os.listdir(grading_dir)}")
    print(f"[debug] grading/__init__.py exists: {os.path.exists(os.path.join(grading_dir, '__init__.py'))}")
else:
    print("[debug] grading/ does NOT exist")

from grading.verifier import main

if __name__ == "__main__":
    main()

"""Turn on the repo's git hooks (.githooks/pre-commit = secret check).

    .venv\\Scripts\\python.exe scripts\\install_hooks.py
"""
import subprocess

subprocess.run(["git", "config", "core.hooksPath", ".githooks"], check=True)
print("Git hooks on: every commit is now checked for secrets and .env / database files.")

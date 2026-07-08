"""`python -m memoryd` — used by scheduled tasks (pythonw has no console
script shims) and the installer's detached daemon start."""
from .cli import main

main()

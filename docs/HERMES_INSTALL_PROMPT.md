# Prompt to hand installation back to the operator

Paste the text below into Hermes only when you need the agent to explain how to
start the memoryd installation. Do not paste API keys into the chat.

```text
I want to install memoryd v0.3.1 as this Hermes profile's production memory
provider.

Do not execute installation, provider activation, gateway, systemd, Docker,
backup, or secret-handling commands. Do not change your own configuration.
Instead, tell me to finish this response, exit every Hermes chat/TUI, and run
the following two commands myself in a normal terminal on Linux:

pipx install --python python3.13 \
  'git+https://github.com/chrisduvillard/memoryd.git@v0.3.1'
memoryd install --hermes

Remind me that the guided installer accepts secrets only through hidden
terminal prompts, requires Hermes Agent exactly 0.16.0, and will print an exact
remediation command if that pin is wrong. Then stop. Do not attempt to install,
activate, restart, or verify memoryd from this session. Wait for me to open a
new Hermes session after the terminal command reports success.
```

The operator-facing prerequisites, rollback guarantees, and recovery drills
are in [PRODUCTION_ROLLOUT.md](PRODUCTION_ROLLOUT.md).

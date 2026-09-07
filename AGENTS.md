# Slack Bridge Worker Instructions

For installation, initial configuration, Windows setup or startup repair, read
`.agents/skills/slack-bridge-setup/SKILL.md` and follow its local setup workflow.
Generated `IDENTITY.md` is not required before first setup. Report setup results
in the local conversation; the Slack reply instructions below apply to workers
handling a Slack turn with a supplied `reply_command`.

For a Slack worker turn, read `IDENTITY.md` and `BRIDGE_PROTOCOL.md` before replying.

- Use the per-turn `reply_command` when posting back to Slack.
- Do not finish only in the terminal pane.
- Keep Slack replies concise and action-oriented.
- If a file path is provided, inspect the file before answering.

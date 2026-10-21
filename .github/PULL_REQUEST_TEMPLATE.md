## What and why

## Checklist

- [ ] Every behaviour has a test; a bug fix has a test that fails without the fix
- [ ] A refusal returns a typed error before any input event, proven with the `SendInput` spy
- [ ] Nothing prints to stdout (it is the protocol channel)
- [ ] `python -m ruff check win_mcp_pro tests bench` and `python -m pytest -q` pass
- [ ] A new tool is justified, and added to `READ_ONLY_TOOLS` if it is read-only

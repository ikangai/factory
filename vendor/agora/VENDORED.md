# Vendored Agora Bus

## Source

The vendored agora bus is copied from the agora Claude Code plugin (marketplace `ikangai/claude-plugins`), specifically the `.groupchat/chat.py` file from version 0.15.1.

**Verification**: This version is byte-identical to 0.15.3 (verified 2026-07-08).

## SHA256

```
76b039f45cd808b9e7289b40e899afe9d42f373f73addd62d20a209004936506
```

## Why Vendored

The factory **owns its coordination bus** — no plugin install in deployments, no version drift between accounts. The wire format (sqlite chat.db) remains unchanged across versions (0.15.1 vs 0.15.3 observed live 2026-07-08), and interactive plugin sessions remain compatible on the same bus directory.

## Update Rules

- **NEVER** edit `chat.py` in place
- To update to a newer version:
  1. Re-copy from the plugin cache
  2. Update the sha256 in both `tests/test_vendored_bus.py` and this file
  3. Document the version + date in this file (e.g., "updated to 0.15.5 on 2026-08-15")
  4. Commit the change with a clear message (e.g., "feat(bus): re-vendor agora to 0.15.5")

## Compatibility

- The database wire format is stable
- The CLI interface (subcommands like `send`, `log`, `read`, etc.) is stable
- Interactive plugin sessions can continue to use the same bus directory

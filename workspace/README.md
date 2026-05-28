# workspace

Where agents and skills live on disk.

```
innate_agents/   Shipped agents. Tracked in git, updated by `git pull`.
custom_agents/   Your agents. Gitignored, stays on your machine.
innate_skills/   Shipped skills. Tracked in git.
custom_skills/   Your skills (code and physical). Gitignored.
```

Drop a `.py` file (or, for physical skills, a directory with `metadata.json`) into the matching folder — it auto-loads on the next brain_client restart, and edits trigger hot reload.

Skill IDs reflect origin: `innate-os/<name>` for shipped, `local/<name>` for custom.

# OpenWolf Operating Protocol (dsh-openwolf)

This workspace runs a code-map second brain in `.dshwolf/`. Follow this protocol:

1. **Anatomy first** — before reading a whole file, consult `.dshwolf/anatomy.md`
   (or the injected code map): description, token estimate, symbols with line
   ranges. Read large files with offset/limit.
2. **Keep STATUS current** — end each phase by updating `.dshwolf/STATUS.md`
   (the `## 🚀 Next phase` section is picked up by the session digest).
3. **Log bugs** — when you find or fix a bug, record it in
   `.dshwolf/buglog.json` via `wolf_bug` to prevent rediscovery.
4. **Learn persistently** — record preferences, conventions, and mistakes in
   `.dshwolf/cerebrum.md` via `wolf_learn`.
5. **Respect the denylist** — secrets (`.env`, keys, `.npmrc`) are never
   indexed or logged.
6. **Refresh when stale** — if the map warns it may be stale (git HEAD moved or
   old scan), run `wolf_refresh` before trusting it.

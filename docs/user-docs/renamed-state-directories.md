# Renamed state directories (CryoSoft → I2AS)

The application was renamed from **CryoSoft** to **I2AS**. Every per-user
and per-machine location it reads or writes moved with the name. Nothing
is migrated automatically: an installation that was set up under the old
name starts from empty settings and empty logs until you move the files
by hand. Your measurement data is not affected — the measurement root is
whatever folder you configured, and it does not carry the application's
name.

## What moved

| What | Old location | New location |
|---|---|---|
| Logs (`cryosoft.log` → `i2as.log`, `status.jsonl`, `trend_history_*.jsonl`, `gateway.json`) | `%LOCALAPPDATA%\CryoSoft\logs` (Windows) · `~/.local/state/cryosoft/logs` or `$XDG_STATE_HOME/cryosoft/logs` (elsewhere) | `%LOCALAPPDATA%\I2AS\logs` · `~/.local/state/i2as/logs` or `$XDG_STATE_HOME/i2as/logs` |
| eLab notebook settings (`eln-settings.json`) and the session autosave files | `%APPDATA%\CryoSoft` · `~/.config/cryosoft` or `$XDG_CONFIG_HOME/cryosoft` | `%APPDATA%\I2AS` · `~/.config/i2as` or `$XDG_CONFIG_HOME/i2as` |
| User config copies (forked from a shipped config) | `%APPDATA%\CryoSoft\configs` · `~/.config/cryosoft/configs` | `%APPDATA%\I2AS\configs` · `~/.config/i2as/configs` |
| Machine-level `App-config.yaml` (holds `measurement_root:`) | `%ProgramData%\CryoSoft\App-config.yaml` · `/etc/cryosoft/App-config.yaml` | `%ProgramData%\I2AS\App-config.yaml` · `/etc/i2as/App-config.yaml` |
| Saved window geometry, the active config and the current user (QSettings) | organisation `CryoSoft`, application `CryoSoft` (the registry on Windows, `~/.config/CryoSoft/CryoSoft.conf` elsewhere) | organisation `I2AS`, application `I2AS` (`~/.config/I2AS/I2AS.conf` elsewhere) |

## Environment variables

Every `CRYOSOFT_*` variable is now `I2AS_*` with the same meaning:
`I2AS_LOG_DIR`, `I2AS_MEASUREMENT_ROOT`, `I2AS_INSTRUMENT_THREAD`,
`I2AS_ELAB_APIKEY`, `I2AS_ASSISTANT_APIKEY`, `I2AS_ELN_SETTINGS`,
`I2AS_GATEWAY_DESCRIPTOR`, `I2AS_MCP_ROLE`, `I2AS_MCP_ACTOR_ID`,
`I2AS_MCP_TIMEOUT`, `I2AS_MCP_FRAMING`, `I2AS_MCP_LOG_LEVEL`. An old
variable left set is ignored, so a deployment that pointed the logs or the
measurement root somewhere through the environment must rename the
variable — otherwise the app falls back to the default log directory and
refuses to start for lack of a measurement root.

## What to do on an existing installation

1. Close the application.
2. Move (or copy) the old directories in the table to the new locations.
   Rename `cryosoft.log` to `i2as.log` if you want the rotating log to
   continue in the same file; the JSONL streams keep their names.
3. If you set `measurement_root:` in the machine-level `App-config.yaml`,
   move that file too, or the app will not find its measurement root.
4. Rename any `CRYOSOFT_*` variables in your shell profile, service
   definition or MCP launcher (`.mcp.json` now sets `I2AS_MCP_ROLE` and
   `I2AS_MCP_ACTOR_ID`).
5. Start the application once. It writes its QSettings under the new
   organisation name; the window geometry and the active config are
   re-saved on the first clean exit. Alternatively pass
   `i2as --config <name>` to choose the config for that launch.

The old directories are never read or deleted by I2AS; remove them when
you no longer need the history they hold.

## Console commands

The module entry points are unchanged in shape (`python -m i2as.main`,
`python -m i2as.ctl`, `python -m i2as.troubleshoot`, `python -m i2as.mcp`)
and are also installed as console scripts: `i2as`, `i2as-ctl`,
`i2as-doctor` and `i2as-mcp`.

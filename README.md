# Droid Magpie

A Droid plugin that refreshes existing Magpie BYOK settings at session start
and resume. It fetches the gateway's regular `/v1/models` catalog and imports
only bare text aliases, not provider-qualified duplicates or media models.
If a model advertises text input and a chat endpoint but omits output
metadata, it is included. The sync tracks the labels it writes in
`magpie-sync-labels.json` alongside Droid settings so upstream renames are
reflected without overwriting custom labels. The sync removes only saved
`custom:magpie/` entries absent from the gateway's `/v1/models` response.
It leaves unrelated models, advertised Magpie entries, routing overrides on
retained entries, and model selections alone. A selection that references a
removed alias may need to be changed manually. An invalid response or one
with no importable text aliases leaves the settings untouched.

On macOS or Linux with Python 3.9+, install the plugin in Droid:

```sh
droid plugin marketplace add ngcto/droid-magpie
droid plugin install droid-magpie@droid-magpie --scope user
```

Start with at least one
`custom:magpie/<alias>` BYOK entry in `~/.factory/settings.json` so the plugin
can reuse your configured gateway and key. Keep all Magpie entries on the
same gateway and key. Literal keys and `${ENV_VAR}` references are supported;
the plugin never prints keys.

For a manual sync from a clone of this repository, run
`python3 droid-magpie/sync.py` from the repository root. Use
`--check` to preview whether settings would change. On a Dusk host, run with
`--dusk` to refresh the local Droid picker if it differs from the settings.
To enable that behavior from the hook on a Dusk host, create an empty
`~/.factory/magpie-sync-dusk` file. Dusk caches its picker separately. For
refreshes while Dusk is idle, schedule the `--dusk` command on the host. The
plugin itself does not run a timer.

Run the tests from the repository root with
`python3 -B -m unittest discover -s droid-magpie`.

The catalog pattern is inspired by [pi-magpie](https://github.com/tinysnake/pi-snake-extensions/tree/master/pi-magpie).
The protocol mapping follows [Magpie's Droid integration](https://github.com/yetone/magpie/blob/main/internal/agent/droid.go).

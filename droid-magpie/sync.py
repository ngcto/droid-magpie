"""Synchronize Magpie BYOK aliases without changing model selections."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PREFIX = "custom:magpie/"
DUSK = "http://127.0.0.1:8799/api"
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}")
CHAT_ENDPOINTS = {"/v1/chat/completions", "/v1/responses", "/v1/messages"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url, headers, *, post=False, local=False):
    request = Request(url, data=b"{}" if post else None, headers=headers)
    opener = build_opener(ProxyHandler({}), NoRedirect()) if local else build_opener(NoRedirect())
    with opener.open(request, timeout=3) as response:
        return json.loads(response.read(4 * 1024 * 1024))


def owned(entry):
    return isinstance(entry.get("id"), str) and entry["id"].startswith(PREFIX)


def gateway_root(url):
    root = url.rstrip("/").removesuffix("/v1")
    parts = urlsplit(root)
    if (parts.scheme not in ("https", "http") or not parts.hostname or
            parts.username is not None or parts.query or parts.fragment or
            (parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"))):
        raise ValueError("Expected HTTPS or loopback HTTP for Magpie")
    return root


def gateway(entries):
    magpie = [entry for entry in entries if owned(entry)]
    if not magpie:
        return None
    roots = {gateway_root(entry["baseUrl"]) for entry in magpie}
    keys = {entry["apiKey"] for entry in magpie}
    if len(roots) != 1 or len(keys) != 1:
        raise ValueError("Magpie entries must use one gateway and API key")
    template = next(iter(keys))
    token = ENV.sub(lambda match: os.environ[match[1]], template)
    if not token or "${" in token:
        raise ValueError("Magpie API key is unavailable")
    return next(iter(roots)), template, token


def protocol(model):
    name = model["id"].lower()
    paths = model.get("native_endpoints") or ()
    if re.search(r"gpt[-_.]?\d|codex|^o[1-9](?:[-_.]|$)", name):
        return "openai"
    if re.search(r"claude|haiku|sonnet|opus", name):
        return "anthropic"
    if "/v1/chat/completions" in paths:
        return "generic-chat-completion-api"
    if "/v1/responses" in paths:
        return "openai"
    if "/v1/messages" in paths:
        return "anthropic"
    return "generic-chat-completion-api"


def compact_catalog(payload):
    models = {}
    rows = payload["data"]
    if not isinstance(rows, list):
        raise ValueError("Unexpected Magpie catalog format")
    for model in rows:
        alias = model.get("id")
        modalities = model.get("modalities") or {}
        if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
            continue
        if "text" not in (modalities.get("input") or ()):
            continue
        output = modalities.get("output") or ()
        # Some providers omit output modalities even for ordinary chat models.
        if ("text" not in output and
                (output or CHAT_ENDPOINTS.isdisjoint(model.get("native_endpoints") or ()))):
            continue
        models.setdefault(PREFIX + alias, model)
    if not models:
        raise ValueError("No compact text models in the Magpie catalog")
    return models


def merge(entries, catalog, advertised_ids, root, key, last_labels):
    stale = {entry["id"] for entry in entries if owned(entry) and entry["id"] not in advertised_ids}
    labels = {model_id: label for model_id, label in last_labels.items() if model_id not in stale}

    def update(model, previous=None):
        alias = model["id"]
        model_id = PREFIX + alias
        display = model.get("display_name")
        display = display.strip() if isinstance(display, str) and display.strip() else None
        if previous is None:
            provider = protocol(model)
            entry = {
                "id": model_id,
                "model": alias,
                "provider": provider,
                "baseUrl": root if provider == "anthropic" else root + "/v1",
                "apiKey": key,
            }
            if provider == "anthropic":
                entry["authMode"] = "bearer"
        else:
            entry = previous.copy()
        current = entry.get("displayName")
        if model_id in last_labels:
            managed = current == last_labels[model_id]
        else:
            managed = current in (None, alias, display)
        if previous is None or managed:
            label = display or current or alias
            entry["displayName"] = labels[model_id] = label
        for target, sources in (
            ("maxContextLimit", ("context_length", "context_window", "max_input_tokens")),
            ("maxOutputTokens", ("max_output_tokens",)),
        ):
            for source in sources:
                value = model.get(source)
                if type(value) is int and value > 0:
                    entry[target] = value
                    break
        entry["noImageSupport"] = "image" not in model["modalities"]["input"]
        return entry

    seen = {entry.get("id") for entry in entries}
    updated = [
        update(catalog[entry["id"]], entry) if entry.get("id") in catalog else entry
        for entry in entries
        if entry.get("id") not in stale
    ] + [update(model) for model_id, model in catalog.items() if model_id not in seen]
    return updated, labels


def refresh_dusk(entries):
    headers = {"x-dusk-cli": "1", "Content-Type": "application/json"}
    instances = get_json(DUSK + "/instances", headers, local=True)["instances"]
    options = next(i["models"]["options"] for i in instances if i["instanceId"] == "droid")
    previous = {row["id"]: row["label"] for row in options if owned(row)}
    desired = {row["id"]: row.get("displayName") or row["model"] for row in entries if owned(row)}
    if desired != previous:
        get_json(DUSK + "/instances/droid/refresh-models", headers, post=True, local=True)
        return True
    return False


def write_json(path, original, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            current = path.read_bytes() if path.exists() else None
            if current != original:
                raise ValueError("Local configuration changed during sync; retry")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def sync(path, *, dusk=False, check=False):
    if not path.exists():
        return "No Droid settings found"
    with (path.parent / "magpie-sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = path.read_bytes()
        settings = json.loads(original)
        entries = settings.get("customModels", [])
        connection = gateway(entries)
        if not connection:
            return "No existing Magpie BYOK entries found"
        root, template, token = connection
        payload = get_json(root + "/v1/models", {"Authorization": "Bearer " + token})
        catalog = compact_catalog(payload)
        advertised_ids = {PREFIX + row["id"] for row in payload["data"] if isinstance(row.get("id"), str)}
        label_path = path.with_name("magpie-sync-labels.json")
        label_bytes = label_path.read_bytes() if label_path.exists() else None
        labels = json.loads(label_bytes) if label_bytes is not None else {}
        updated, next_labels = merge(entries, catalog, advertised_ids, root, template, labels)
        changed = entries != updated
        if changed and not check:
            settings["customModels"] = updated
            write_json(path, original, settings)
        if labels != next_labels and not check:
            write_json(label_path, label_bytes, next_labels)
        refreshed = dusk and not check and refresh_dusk(updated)
        return f"{len(catalog)} live aliases; settings {'would change' if changed and check else 'updated' if changed else 'unchanged'}" + (
            "; Dusk picker refreshed" if refreshed else ""
        )


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook", action="store_true", help="Run silently as a SessionStart hook")
    parser.add_argument("--check", action="store_true", help="Check the gateway without writing settings")
    parser.add_argument("--dusk", action="store_true", help="Also refresh a local Dusk picker if needed")
    args = parser.parse_args()
    home = Path(os.environ.get("FACTORY_HOME_OVERRIDE") or Path.home())
    try:
        settings = (home / ".factory" / "settings.json").resolve()
        dusk = args.dusk or (args.hook and settings.with_name("magpie-sync-dusk").is_file())
        result = sync(settings, dusk=dusk, check=args.check)
    except BlockingIOError:
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
        # Never print a response body, parsed credential, or URL from settings.
        if not args.hook:
            print("Magpie sync failed; check Droid BYOK settings and gateway availability.", file=sys.stderr)
        return 0 if args.hook else 1
    if not args.hook:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

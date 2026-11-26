import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync


def model(alias, *, output="text", endpoints=()):
    return {
        "id": alias,
        "display_name": alias.title(),
        "modalities": {"input": ["text"], **({"output": [output]} if output else {})},
        "context_length": 100_000,
        "max_output_tokens": 32_000,
        "native_endpoints": list(endpoints),
    }


class SyncTest(unittest.TestCase):
    def test_compact_and_merge(self):
        old = {"id": sync.PREFIX + "gpt-6", "model": "gpt-6", "displayName": "Mine", "provider": "openai"}
        missing = {"id": sync.PREFIX + "missing", "model": "missing"}
        advertised = {"id": sync.PREFIX + "image-only", "model": "image-only"}
        unrelated = {"id": "custom:other", "model": "other"}
        payload = {"data": [
            model("gpt-6"), model("aside/gpt-6"), model("gpt-6", endpoints=["/v1/messages"]),
            model("claude-new"), model("grok-new", endpoints=["/v1/responses"]),
            model("deepseek-flash", output=None, endpoints=["/v1/chat/completions"]),
            model("unknown-output", output=None),
            model("embedding", output=None, endpoints=["/v1/embeddings"]),
            model("image-only", output="image", endpoints=["/v1/chat/completions"]),
        ]}
        catalog = sync.compact_catalog(payload)
        advertised_ids = {sync.PREFIX + row["id"] for row in payload["data"]}
        updated, labels = sync.merge(
            [unrelated, old, missing, advertised], catalog, advertised_ids, "https://magpie.example", "${KEY}",
            {missing["id"]: "Missing"},
        )
        self.assertEqual([entry["id"] for entry in updated], [
            "custom:other", sync.PREFIX + "gpt-6", advertised["id"],
            sync.PREFIX + "claude-new", sync.PREFIX + "grok-new", sync.PREFIX + "deepseek-flash",
        ])
        self.assertEqual(updated[0], unrelated)
        self.assertEqual(updated[1]["displayName"], "Mine")
        self.assertEqual(updated[1]["maxContextLimit"], 100_000)
        self.assertEqual(updated[2], advertised)
        self.assertEqual(updated[3]["baseUrl"], "https://magpie.example")
        self.assertEqual(updated[3]["authMode"], "bearer")
        self.assertEqual(updated[4]["provider"], "openai")
        self.assertEqual(updated[4]["baseUrl"], "https://magpie.example/v1")
        self.assertEqual(updated[4]["apiKey"], "${KEY}")
        self.assertEqual(updated[5]["provider"], "generic-chat-completion-api")
        self.assertEqual(labels[sync.PREFIX + "deepseek-flash"], "Deepseek-Flash")
        self.assertNotIn(old["id"], labels)
        self.assertNotIn(missing["id"], labels)

    def test_managed_labels_update_but_custom_labels_stay(self):
        alias = "deepseek-v4.1-flash"
        model_id = sync.PREFIX + alias
        old = {"id": model_id, "model": alias, "displayName": "Old Label"}
        catalog = sync.compact_catalog({"data": [model(alias) | {"display_name": "DeepSeek V4.1 Flash 1017"}]})
        updated, labels = sync.merge([old], catalog, {model_id}, "https://magpie.example", "fixture", {model_id: "Old Label"})
        self.assertEqual(updated[0]["displayName"], "DeepSeek V4.1 Flash 1017")
        self.assertEqual(labels[model_id], updated[0]["displayName"])
        updated[0]["displayName"] = "My model"
        catalog[model_id]["display_name"] = "New upstream label"
        overridden, next_labels = sync.merge(updated, catalog, {model_id}, "https://magpie.example", "fixture", labels)
        self.assertEqual(overridden[0]["displayName"], "My model")
        self.assertEqual(next_labels, labels)

    def test_dry_run_update_repeat_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            selected = sync.PREFIX + "gpt-6"
            path.write_text(json.dumps({
                "customModels": [
                    {"id": selected, "model": "gpt-6", "baseUrl": "https://magpie.example/v1", "apiKey": "${KEY}"},
                    {"id": sync.PREFIX + "removed", "model": "removed", "baseUrl": "https://magpie.example/v1", "apiKey": "${KEY}"},
                    {"id": "custom:other/removed", "model": "removed"},
                ],
                "sessionDefaultSettings": {"model": selected},
                "other": True,
            }))
            original = path.read_bytes()
            with patch.dict(os.environ, {"KEY": "fixture"}), patch.object(sync, "get_json") as network:
                network.return_value = {"data": [model("gpt-6"), model("gpt-7")]}
                self.assertIn("would change", sync.sync(path, check=True))
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(path.with_name("magpie-sync-labels.json").exists())
                self.assertIn("settings updated", sync.sync(path))
                network.assert_called_with("https://magpie.example/v1/models", {"Authorization": "Bearer fixture"})
                updated = path.read_bytes()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(updated)["sessionDefaultSettings"], {"model": selected})
                self.assertEqual([row["id"] for row in json.loads(updated)["customModels"]],
                                 [selected, "custom:other/removed", sync.PREFIX + "gpt-7"])
                self.assertNotIn(b"fixture", updated)
                mtime = path.stat().st_mtime_ns
                self.assertIn("unchanged", sync.sync(path))
                self.assertEqual(path.stat().st_mtime_ns, mtime)
                network.return_value = {"data": []}
                with self.assertRaises(ValueError):
                    sync.sync(path)
                self.assertEqual(path.read_bytes(), updated)

    def test_dusk_refreshes_only_for_picker_drift(self):
        entry = {"id": sync.PREFIX + "gpt-6", "model": "gpt-6", "displayName": "GPT 6"}
        options = [{"id": entry["id"], "label": entry["displayName"]}]
        with patch.object(sync, "get_json") as network:
            network.return_value = {"instances": [{"instanceId": "droid", "models": {"options": options}}]}
            self.assertFalse(sync.refresh_dusk([entry]))
            self.assertEqual(network.call_count, 1)
            options.clear()
            self.assertTrue(sync.refresh_dusk([entry]))
            self.assertEqual(network.call_args.args[0], sync.DUSK + "/instances/droid/refresh-models")
            self.assertEqual(network.call_args.kwargs, {"post": True, "local": True})
            self.assertNotIn("Authorization", network.call_args.args[1])


if __name__ == "__main__":
    unittest.main()

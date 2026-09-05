# Existing manifest compatibility

This is a reference for the retained importer, not a production setup step.
[Current browser manifests](../interfaces/shizzle-browser-v1/spec.md) are the
supported delivery boundary. The [review](REVIEW.md) records why rerunning
`ops/import_legacy_library.py` can reset active generations and deletion state.

The importer reads older `karaoke/pub/<job>/stems.json` records and normalizes
their browser shape. Some inputs reference missing split stems or WAV media;
they cannot be admitted as six-stem browser tracks merely because a manifest
exists.

| Input field/shape | Compatibility handling |
|---|---|
| Missing or older version | Emit browser manifest version 3 |
| `default_gain` | Drop ambiguous linear field; use explicit `default_gain_db` |
| `other` separator role | Public role is `shizzle`; existing filename may differ |
| `channel_offset` | Old multi-track channel-pair index, not a browser gain or stem id |
| Existing timeline metadata | Probe actual media; copied metadata alone does not prove sample rate/alignment |
| `merged_audio` / `multitrack` | Optional historical artifacts; not substitutes for six individually playable stems |
| Missing referenced stem objects | Reject incomplete source set |
| WAV stems | Reject browser publication; derive from a valid source through the supported delivery path |

Current compatibility behavior lives in
[`import_legacy_library.py`](../ops/import_legacy_library.py),
[`delivery_profile.py`](../library/src/shizzle_server/publish/delivery_profile.py)
and the publisher. Keep their tests when deciding whether to retire the
one-time importer; deleting reference code without its callers would break
retained tests and maintenance tools.

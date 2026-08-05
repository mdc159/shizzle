# Purged source note — 2026-08-05

This is a documentation-only historical note. It is not imported, queried, or
used by the production database, ingestion service, processing pipeline, or
library API.

Four incomplete source items were found outside the admitted library:

- AC/DC — For Those About To Rock
- AC/DC — Dirty Deeds Done Dirt Cheap
- AC/DC — Dirty Deeds (v3 MERGED)
- AC/DC — Highway to Hell

Their referenced separated stem files were absent. The remaining material could
only have been re-separated after a lossy reconstruction, so it did not meet the
clean-source admission rule. The source objects and abandoned staged recovery
objects were permanently deleted from the versioned cloud bucket. No database
track, job, generation event, playback session, or playback event existed for
any of them.

Operationally, these titles do not exist. There is no recovery list, reserved
track identity, queued job, staged object, or special-case processing path. If
one is submitted again through an ordinary supported URL or upload, the system
must treat it exactly like any other new ingestion request.

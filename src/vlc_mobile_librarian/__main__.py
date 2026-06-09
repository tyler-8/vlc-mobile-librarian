from __future__ import annotations

import os
import sys


def main() -> None:
    # Importing `web` registers the @ui.page route(s). It must happen before
    # ui.run() so the routes exist when the server starts.
    from nicegui import ui

    from vlc_mobile_librarian import web  # noqa: F401  (import for side effects)

    # Allow `--port NNNN` as a convenience passthrough; otherwise default to 8080.
    port = 8888
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        if i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    # Auto-open a browser tab on launch, unless disabled (e.g. in a headless
    # container where there is no browser to open). Set VLC_LIBRARIAN_NO_SHOW=1.
    show = os.environ.get("VLC_LIBRARIAN_NO_SHOW", "") not in ("1", "true", "True")

    ui.run(
        title="VLC Wi-Fi Sync",
        favicon="🎵",
        reload=False,
        native=False,
        show=show,
        port=port,
        storage_secret="vlc-mobile-librarian",
    )


# NiceGUI re-imports the entry module under the name "__mp_main__" in its
# worker process; main() is the console-script target in either case.
if __name__ in {"__main__", "__mp_main__"}:
    main()

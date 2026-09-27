"""Start-up hook of the portable build.

YTConvert.exe and "YTConvert Setup.exe" are Python's own pythonw.exe, renamed.
They keep the Python Software Foundation signature, so Windows and browsers
trust them; a freshly built launcher exe would be flagged. Python imports this
module while it starts, and it runs our code instead of an (invisible)
interactive prompt:

    YTConvert Setup.exe            -> setup.main()      the installer window
    YTConvert.exe --uninstall      -> setup.uninstall() Apps & features entry
    YTConvert.exe                  -> app.main()        the converter
"""

import os
import sys

_exe = os.path.basename(sys.executable).lower()

if _exe in ("ytconvert.exe", "ytconvert setup.exe"):
    try:
        if _exe == "ytconvert setup.exe":
            import setup

            setup.main()
        elif "--uninstall" in getattr(sys, "orig_argv", sys.argv):
            import setup

            setup.uninstall()
        else:
            import app

            app.main()
    except SystemExit:
        pass
    except BaseException:  # noqa: BLE001 - pythonw has no console to show it
        import traceback

        folder = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "YTConvert")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "crash.log"), "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
    finally:
        os._exit(0)

"""Keep tkinter discoverable when PyInstaller's Tcl probe fails.

Dankoba_Helper_Local.spec collects Tcl/Tk explicitly and includes PyInstaller's
tkinter runtime hook, so module discovery does not need to be disabled here.
"""


def pre_find_module_path(hook_api):
    pass

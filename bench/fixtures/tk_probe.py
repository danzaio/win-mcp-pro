"""Disposable Tk fixture (Phase D): a toolkit with no useful UIA tree.

The window exposes a title (so list_windows/find can bind its identity),
but its widgets are drawn by Tk - no UIA children. The fixture logs every
observable effect (button click, raw mouse down, its own button center in
screen coordinates) to a JSONL file: the external oracle for the
coordinate path, where the accessibility tree has nothing to offer.
"""
from __future__ import annotations

import json
import sys
import time
import tkinter as tk


def main() -> None:
    title, log, x, y = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])

    def write(kind: str, **extra) -> None:
        payload = {"event": kind, "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        payload.update(extra)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    root = tk.Tk()
    root.title(title)
    root.geometry(f"420x220+{x}+{y}")
    root.attributes("-topmost", True)

    label = tk.Label(root, text="Ready", font=("Segoe UI", 16))
    label.place(x=20, y=120, width=380, height=40)
    button = tk.Button(
        root, text="TK GO", font=("Segoe UI", 14),
        command=lambda: (label.config(text="TK clicked"), write("tk_button")),
    )
    button.place(x=20, y=40, width=180, height=50)
    root.bind("<Button-1>", lambda _event: write("mouse"))

    def announce() -> None:
        button.update_idletasks()
        write("button_center",
              x=button.winfo_rootx() + button.winfo_width() // 2,
              y=button.winfo_rooty() + button.winfo_height() // 2)

    root.after(300, announce)
    root.mainloop()


if __name__ == "__main__":
    main()

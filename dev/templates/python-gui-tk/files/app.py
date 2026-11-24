"""{{TITLE}} -- desktop app (Tkinter)."""
import tkinter as tk
from tkinter import ttk


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("{{TITLE}}")
        self.geometry("520x320")
        self.count = 0
        frame = ttk.Frame(self, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="{{TITLE}}", font=("Segoe UI", 18)).pack(pady=(0, 12))
        self.status = ttk.Label(frame, text="Ready.")
        self.status.pack(pady=8)
        ttk.Button(frame, text="Click me", command=self.on_click).pack(pady=8)

    def on_click(self):
        self.count += 1
        self.status.config(text=f"Clicked {self.count} time(s)")


if __name__ == "__main__":
    App().mainloop()

import tkinter as tk
from tkinter.scrolledtext import ScrolledText
import threading
import assistant

root = tk.Tk()
root.title("FRIDAY — GUI")
root.geometry("700x500")

output = ScrolledText(root, state="disabled", wrap="word")
output.pack(fill="both", expand=True, padx=8, pady=8)

entry_frame = tk.Frame(root)
entry_frame.pack(fill="x", padx=8, pady=(0, 8))

cmd_entry = tk.Entry(entry_frame)
cmd_entry.pack(side="left", fill="x", expand=True)


def append(text: str):
    output.config(state="normal")
    output.insert("end", text + "\n")
    output.see("end")
    output.config(state="disabled")


def run_command(command: str):
    if not command.strip():
        return
    append(f"You: {command}")

    # execute in background to avoid blocking GUI
    def worker():
        keep_running, reply = assistant.execute_command(command)
        if reply:
            append(f"{assistant.ASSISTANT_NAME}: {reply}")

    threading.Thread(target=worker, daemon=True).start()


send_btn = tk.Button(
    entry_frame,
    text="Send",
    command=lambda: (run_command(cmd_entry.get()), cmd_entry.delete(0, 'end')),
)
send_btn.pack(side="right", padx=(8, 0))

controls = tk.Frame(root)
controls.pack(fill="x", padx=8, pady=(0, 8))


def start_server():
    def worker():
        msg = assistant.start_device_server()
        append(msg)

    threading.Thread(target=worker, daemon=True).start()


def show_memories():
    data = assistant.load_data()
    mems = data.get("memories", [])
    if not mems:
        append("No memories.")
        return
    append("Memories:")
    for i, m in enumerate(mems, 1):
        append(f"{i}. {m}")


btn_server = tk.Button(controls, text="Start Server", command=start_server)
btn_server.pack(side="left")
btn_mem = tk.Button(controls, text="Show Memories", command=show_memories)
btn_mem.pack(side="left", padx=(8, 0))


def voice_on():
    run_command("voice on")


def voice_off():
    run_command("voice off")


btn_voice_on = tk.Button(controls, text="Voice On", command=voice_on)
btn_voice_on.pack(side="right")
btn_voice_off = tk.Button(controls, text="Voice Off", command=voice_off)
btn_voice_off.pack(side="right", padx=(0, 8))

# focus entry
cmd_entry.focus()

root.mainloop()

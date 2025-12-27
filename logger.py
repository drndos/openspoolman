import os
import time
import re
import builtins
from datetime import datetime

def append_to_rotating_file(file_path: str, text: str, max_size: int = 1_048_576, max_files: int = 5) -> None:
    """
    Appends the given text with a timestamp to a rotating log file.
    If the file exceeds the maximum size, it is renamed with a timestamp, and a new file is created.
    If the maximum number of log files is reached, the oldest file matching the exact naming pattern is deleted.
    """
    directory, base_filename = os.path.split(file_path)
    base_filename = os.path.splitext(base_filename)[0]
    os.makedirs(directory, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} :: {text}\n"
    
    # Rotate the file if it exceeds the size limit
    if os.path.exists(file_path) and os.path.getsize(file_path) > max_size:
        archive_filename = f"{base_filename}_{time.strftime('%Y%m%d_%H%M%S')}.log"
        archived_file = os.path.join(directory, archive_filename)
        os.rename(file_path, archived_file)
    
    # Append the text with timestamp to the current file
    with open(file_path, "a", encoding="utf-8") as file:
        file.write(log_entry)
    
    # Find all log files that exactly match the expected pattern
    pattern = re.compile(rf"^{re.escape(base_filename)}_\d{{8}}_\d{{6}}\.log$")
    log_files = sorted(
        [f for f in os.listdir(directory) if pattern.match(f)],
        key=lambda f: os.path.getctime(os.path.join(directory, f))  # Sort by creation time
    )
    
    while len(log_files) > max_files:
        os.remove(os.path.join(directory, log_files.pop(0)))  # Remove the oldest file


def log_with_timestamp(*args, sep=" ", end="\n", file=None, flush=True) -> None:
    """
    Print a message with a leading timestamp, preserving the standard print API.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    builtins.print(f"[{timestamp}]", *args, sep=sep, end=end, file=file, flush=flush)


# Alias for brevity where logging-style prints are used
log = log_with_timestamp

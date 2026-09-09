import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def run():
    server_cmd = [sys.executable, os.path.join(BASE_DIR, "manage.py"), "runserver", "0.0.0.0:8000"]
    processor_cmd = [sys.executable, os.path.join(BASE_DIR, "scripts", "processor.py")]

    # Start the server first
    server_process = subprocess.Popen(server_cmd, cwd=BASE_DIR)
    
    # Optional short delay to let the server initialize before starting the processor
    time.sleep(2)

    # Start the processor script
    processor_process = subprocess.Popen(processor_cmd, cwd=BASE_DIR)

    try:
        # Keep the script alive while both run
        server_process.wait()
        processor_process.wait()
    except KeyboardInterrupt:
        # Clean shutdown on Ctrl + C
        print("\nStopping both processes...")
        server_process.terminate()
        processor_process.terminate()
        server_process.wait()
        processor_process.wait()

if __name__ == "__main__":
    run()
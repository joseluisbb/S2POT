import os
import sys

# Ensure root workspace directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.s2pit_gui import S2PITApp

if __name__ == "__main__":
    initial_dir = sys.argv[1] if len(sys.argv) > 1 else None
    app = S2PITApp(initial_input_dir=initial_dir)
    app.mainloop()

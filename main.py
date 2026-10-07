import os
import sys

# Ensure workspace directory is in python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.app_gui import launch_gui

if __name__ == "__main__":
    launch_gui()

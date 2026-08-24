"""Agent 8: Run this to clean dead files. Usage: python cleanup.py [--dry-run]"""
import os, sys, shutil

TO_DELETE = [
    "app.py.bak", "app.py.gate", "app.py.previous_gate", "config.py.bak_8013",
    "server.log", "server_run.log", "server_run.err.log", "server_run.out.log",
    "create_repo_claude_edit.json", "create_repo_claude_edit_response.json",
    "create_yt_repo_response.json", "users.json", "codes.json",
]
DIRS = ["__pycache__", ".pytest_cache", "installer_output"]

def cleanup(dry_run=False):
    root = os.path.dirname(os.path.abspath(__file__))
    removed = []
    for f in TO_DELETE:
        p = os.path.join(root, f)
        if os.path.exists(p):
            if not dry_run: os.remove(p)
            print(f"{'[DRY] ' if dry_run else '[DEL] '}{p}")
            removed.append(p)
    for d in DIRS:
        p = os.path.join(root, d)
        if os.path.isdir(p):
            if not dry_run: shutil.rmtree(p)
            print(f"{'[DRY] ' if dry_run else '[DEL] '}{p}/")
            removed.append(p)
    for dp, dns, _ in os.walk(root):
        for d in dns:
            if d == "__pycache__":
                full = os.path.join(dp, d)
                if not dry_run: shutil.rmtree(full)
                removed.append(full)
    print(f"\n{'Would remove' if dry_run else 'Removed'} {len(removed)} items.")

if __name__ == "__main__":
    cleanup(dry_run="--dry-run" in sys.argv or "-n" in sys.argv)

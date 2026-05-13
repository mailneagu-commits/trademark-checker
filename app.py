import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

try:
    from main import app
except Exception as e:
    import traceback
    print("=== IMPORT ERROR ===")
    traceback.print_exc()
    print("=== END IMPORT ERROR ===")
    raise

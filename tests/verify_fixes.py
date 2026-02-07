import sys
import os
import asyncio
import tempfile
import importlib.util

# Add project root to path
sys.path.append(os.getcwd())

def check_temp_dir():
    print(f"Testing temp dir resolution...")
    # Mocking environment variable if not set, or just checking default behavior match
    # In the code we used: os.getenv("TMPDIR", tempfile.gettempdir())
    
    # Let's verify what that evaluates to
    val = os.getenv("TMPDIR", tempfile.gettempdir())
    print(f"Resolved TMPDIR: {val}")
    
    if not os.path.exists(val):
        print("FAIL: Resolved temp dir does not exist")
        return False
    
    try:
        t = os.path.join(val, "test_write_perm")
        with open(t, "w") as f:
            f.write("ok")
        os.remove(t)
        print("PASS: Temp dir is writable")
    except Exception as e:
        print(f"FAIL: Temp dir not writable: {e}")
        return False
    return True

async def check_main_module():
    print("\nChecking app.main module structure...")
    try:
        # We need to mock environment variables required by main.py
        # Provide defaults that prevent crashes at module level
        if "BOT_TOKEN" not in os.environ:
            os.environ["BOT_TOKEN"] = "test_token"
        
        # Import app.main
        # We use run_path or importlib to load it
        spec = importlib.util.spec_from_file_location("app.main", "app/main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["app.main"] = module
        # To avoid executing the whole module if it has side effects, we just want to load it. 
        # But top-level code WILL run.
        spec.loader.exec_module(module)
        
        # Check parsing_sem
        if hasattr(module, "parsing_sem") and isinstance(module.parsing_sem, asyncio.Semaphore):
            print(f"PASS: parsing_sem found with value {module.parsing_sem._value}")
        else:
            print("FAIL: parsing_sem missing or invalid")
            return False
            
        print("PASS: Module imported successfully (syntax check passed)")
        return True
        
    except Exception as e:
        print(f"FAIL: Could not import app.main: {e}")
        import traceback
        traceback.print_exc()
        return False

async def main():
    r1 = check_temp_dir()
    r2 = await check_main_module()
    
    if r1 and r2:
        print("\nALL CHECKS PASSED")
    else:
        print("\nSOME CHECKS FAILED")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

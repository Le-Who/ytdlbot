import sys
import os

# Добавляем корневую директорию в sys.path
sys.path.insert(0, os.getcwd())

try:
    print("Attempting to import app.main...")
    import app.main
    print("SUCCESS: app.main imported.")
    
    print("Attempting to import app.core.config...")
    import app.core.config
    print("SUCCESS: app.core.config imported.")
    
    print("Attempting to import app.bot submodules...")
    import app.bot.commands
    import app.bot.messages
    import app.bot.callbacks
    import app.bot.keyboards
    print("SUCCESS: app.bot submodules imported.")

    print("Attempting to import app.api.routes...")
    import app.api.routes
    print("SUCCESS: app.api.routes imported.")

except Exception as e:
    print(f"FAILURE: {e}")
    sys.exit(1)

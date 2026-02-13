import asyncio
import os
import time
import logging
from app.core import state
from app.core.config import TEMP_DIR

logger = logging.getLogger("cleanup")

async def cleanup_loop():
    """
    Periodically cleans up stale files in TEMP_DIR that are no longer tracked
    in the application state or have expired.
    """
    logger.info(f"Starting cleanup loop for {TEMP_DIR}")
    while True:
        try:
            # Check every 10 minutes
            await asyncio.sleep(600)

            logger.info("Running scheduled cleanup...")
            now = time.time()
            # Grace period for files currently being written/downloaded but not yet in cache
            # or just finished.
            GRACE_PERIOD = 600  # 10 minutes
            cutoff = now - GRACE_PERIOD

            # Get set of active file paths from cache
            # We use set for O(1) lookup
            # cachetools TTLCache values() returns a view/iterator.
            active_paths = set(state.file_cache.values())

            count_deleted = 0
            count_kept = 0

            if not os.path.exists(TEMP_DIR):
                logger.warning(f"TEMP_DIR {TEMP_DIR} does not exist!")
                continue

            for filename in os.listdir(TEMP_DIR):
                filepath = os.path.join(TEMP_DIR, filename)

                # Only delete files we own (ytdl_*)
                if not filename.startswith("ytdl_"):
                    continue

                if not os.path.isfile(filepath):
                    continue

                # If file is currently tracked in cache, keep it
                if filepath in active_paths:
                    count_kept += 1
                    continue

                try:
                    mtime = os.path.getmtime(filepath)
                    if mtime > cutoff:
                        # File is too new, might be in progress or just created
                        count_kept += 1
                        continue

                    # Delete stale file
                    os.unlink(filepath)
                    count_deleted += 1
                    logger.debug(f"Deleted stale file: {filename}")
                except Exception as e:
                    logger.warning(f"Failed to check/delete {filename}: {e}")

            if count_deleted > 0:
                logger.info(f"Cleanup finished: deleted {count_deleted} files, kept {count_kept}.")

        except asyncio.CancelledError:
            logger.info("Cleanup loop cancelled")
            break
        except Exception as e:
            logger.error(f"Error in cleanup loop: {e}", exc_info=True)
            # Sleep a bit before retrying to avoid tight loop on persistent error
            await asyncio.sleep(60)

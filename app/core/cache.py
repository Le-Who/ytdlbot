import asyncio
from cachetools import TTLCache

_sentinel = object()


class FileTTLCache(TTLCache):
    def __init__(self, *args, on_eviction=None, **kwargs):
        self._clearing = False
        self.on_eviction = on_eviction
        super().__init__(*args, **kwargs)

    def _evict(self, value):
        if self.on_eviction:
            try:
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, self.on_eviction, value)
            except RuntimeError:
                self.on_eviction(value)

    def popitem(self):
        key, value = super().popitem()
        if not self._clearing:
            self._evict(value)
        return key, value

    def pop(self, key, default=_sentinel):
        if default is _sentinel:
            value = super().pop(key)
            if not self._clearing:
                self._evict(value)
            return value
        else:
            value = super().pop(key, default)
            if value is not default:
                if not self._clearing:
                    self._evict(value)
            return value

    def clear(self):
        if self.on_eviction:
            values = list(self.values())
            try:
                loop = asyncio.get_running_loop()

                def _bulk_evict():
                    for v in values:
                        self.on_eviction(v)

                # Schedule bulk eviction
                loop.run_in_executor(None, _bulk_evict)

                # Prevent popitem from scheduling individual evictions during clear
                self._clearing = True
                try:
                    super().clear()
                finally:
                    self._clearing = False

            except RuntimeError:
                # Synchronous fallback: evict manually, prevent popitem from re-evicting
                self._clearing = True
                try:
                    for value in values:
                        self.on_eviction(value)
                    super().clear()
                finally:
                    self._clearing = False
        else:
             super().clear()

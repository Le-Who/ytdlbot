from cachetools import TTLCache


class FileTTLCache(TTLCache):
    def __init__(self, *args, on_eviction=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_eviction = on_eviction

    def popitem(self):
        key, value = super().popitem()
        if self._on_eviction:
            self._on_eviction(value)
        return key, value

    def expire(self, time=None):
        expired = super().expire(time)
        if self._on_eviction:
            for _, value in expired:
                self._on_eviction(value)
        return expired

    def pop(self, key, default=None):
        exists = key in self
        value = super().pop(key, default)
        if exists and self._on_eviction:
            self._on_eviction(value)
        return value

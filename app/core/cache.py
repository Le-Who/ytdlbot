from cachetools import Cache, TTLCache


class FileTTLCache(TTLCache):
    def __init__(self, *args, on_eviction=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_eviction = on_eviction
        self._deferred_evictions = None

    def pop(self, key, default=None):
        return super().pop(key, default)

    def __setitem__(self, key, value):
        self._deferred_evictions = []
        try:
            self.expire()
            missing = object()
            previous = self.get(key, missing)
            super().__setitem__(key, value)
            if previous is not missing and previous != value:
                self._notify_removed([previous])
        finally:
            removed = self._deferred_evictions
            self._deferred_evictions = None
            self._notify_removed(removed)

    def __delitem__(self, key):
        value = Cache.__getitem__(self, key)
        try:
            super().__delitem__(key)
        finally:
            self._notify_removed([value])

    def _notify_removed(self, values):
        if self._deferred_evictions is not None:
            self._deferred_evictions.extend(values)
            return
        if not self.on_eviction:
            return
        notified = []
        for value in values:
            if value not in notified and value not in self.values():
                notified.append(value)
                self.on_eviction(value)

    def expire(self, time=None):
        expired = super().expire(time)
        self._notify_removed([value for _, value in expired])
        return expired

    def clear(self):
        self.expire()
        values = list(self.values())
        super().clear()
        self._notify_removed(values)

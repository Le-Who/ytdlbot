from cachetools import TTLCache


class FileTTLCache(TTLCache):
    def __init__(self, *args, on_eviction=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_eviction = on_eviction

    def pop(self, key, default=None):
        value = super().pop(key, default)
        if value is not default and self.on_eviction:
            self.on_eviction(value)
        return value

    def expire(self, time=None):
        expired = super().expire(time)
        if self.on_eviction:
            for _, value in expired:
                self.on_eviction(value)
        return expired

    def clear(self):
        self.expire()
        if self.on_eviction:
            for value in self.values():
                self.on_eviction(value)
        super().clear()

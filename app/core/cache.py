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

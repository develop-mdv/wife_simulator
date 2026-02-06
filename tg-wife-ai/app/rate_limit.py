"""
Rate limiting module using sliding window algorithm.
Thread-safe implementation for asyncio.
"""

import asyncio
import time
from collections import deque


class RateLimiter:
    """
    Sliding window rate limiter.
    Allows max_count operations within window_seconds.
    """
    
    def __init__(self, max_count: int, window_seconds: int):
        self.max_count = max_count
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
    
    async def acquire(self) -> bool:
        """
        Try to acquire a rate limit slot.
        Returns True if allowed, False if rate limited.
        """
        async with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            
            # Remove expired timestamps
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            
            # Check if we can proceed
            if len(self._timestamps) >= self.max_count:
                return False
            
            # Record this operation
            self._timestamps.append(now)
            return True
    
    async def wait_and_acquire(self) -> None:
        """
        Wait until a slot is available, then acquire it.
        """
        while True:
            async with self._lock:
                now = time.time()
                cutoff = now - self.window_seconds
                
                # Remove expired timestamps
                while self._timestamps and self._timestamps[0] < cutoff:
                    self._timestamps.popleft()
                
                # Check if we can proceed
                if len(self._timestamps) < self.max_count:
                    self._timestamps.append(now)
                    return
                
                # Calculate wait time until oldest timestamp expires
                wait_time = self._timestamps[0] + self.window_seconds - now
            
            # Wait outside the lock
            if wait_time > 0:
                await asyncio.sleep(wait_time + 0.1)
    
    def get_remaining_slots(self) -> int:
        """Get number of remaining slots (non-async, approximate)."""
        now = time.time()
        cutoff = now - self.window_seconds
        valid_count = sum(1 for ts in self._timestamps if ts >= cutoff)
        return max(0, self.max_count - valid_count)
    
    def time_until_available(self) -> float:
        """Get seconds until next slot is available."""
        if not self._timestamps:
            return 0.0
        
        now = time.time()
        cutoff = now - self.window_seconds
        
        # Remove expired in-place for accurate count
        valid_timestamps = [ts for ts in self._timestamps if ts >= cutoff]
        
        if len(valid_timestamps) < self.max_count:
            return 0.0
        
        # Time until oldest valid timestamp expires
        return max(0.0, valid_timestamps[0] + self.window_seconds - now)

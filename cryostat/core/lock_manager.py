"""
Thread-Safe Lock Manager
=========================

Global locking mechanism for thread-safe I/O operations.

Implements a 1-second timeout lock to prevent concurrent access to
hardware devices, enabling safe multi-threaded monitoring and control.
"""

import threading
import logging
from typing import Optional

log = logging.getLogger(__name__)


class ThreadSafeLock:
    """Thread-safe lock with timeout for cryostat I/O operations.

    Provides a global lock that ensures only one thread can perform
    I/O operations at a time. Includes timeout to prevent deadlocks.

    Features:
    - 1-second default timeout
    - Context manager support (with statement)
    - Owner tracking for debugging
    - Acquisition statistics

    Example:
        >>> lock = ThreadSafeLock(timeout=1.0)
        >>> with lock:
        ...     # Critical section - only one thread at a time
        ...     device.temperature_1
        ...     device.temperature_setpoint = 4.2

        >>> # Manual acquisition
        >>> if lock.acquire():
        ...     try:
        ...         device.field
        ...     finally:
        ...         lock.release()
    """

    def __init__(self, timeout: float = 1.0):
        """Initialize thread-safe lock.

        Args:
            timeout: Lock acquisition timeout in seconds (default: 1.0)
        """
        self._lock = threading.RLock()  # Reentrant lock for same-thread re-entry
        self.timeout = timeout
        self._owner = None
        self._acquisition_count = 0
        self._timeout_count = 0

        log.debug(f"ThreadSafeLock initialized (timeout={timeout}s)")

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        """Acquire the lock.

        Args:
            blocking: If True, block until lock acquired or timeout
            timeout: Override default timeout (None = use self.timeout)

        Returns:
            bool: True if lock acquired, False if timeout

        Raises:
            TimeoutError: If lock cannot be acquired within timeout

        Example:
            >>> lock = ThreadSafeLock()
            >>> if lock.acquire(timeout=2.0):
            ...     print("Lock acquired")
            ...     lock.release()
            Lock acquired
        """
        if timeout is None:
            timeout = self.timeout if blocking else 0

        acquired = self._lock.acquire(blocking=blocking, timeout=timeout)

        if acquired:
            self._owner = threading.current_thread().name
            self._acquisition_count += 1
            log.debug(f"Lock acquired by thread '{self._owner}' "
                     f"(count: {self._acquisition_count})")
            return True
        else:
            self._timeout_count += 1
            current_thread = threading.current_thread().name
            log.warning(f"Lock acquisition timeout ({timeout}s) "
                       f"for thread '{current_thread}'")

            if blocking:
                raise TimeoutError(
                    f"Could not acquire cryostat I/O lock within {timeout} seconds. "
                    f"Lock held by: {self._owner}"
                )

            return False

    def release(self):
        """Release the lock.

        Raises:
            RuntimeError: If lock not held by current thread

        Example:
            >>> lock.acquire()
            True
            >>> lock.release()  # Must be called by same thread
        """
        current_thread = threading.current_thread().name

        try:
            self._owner = None
            self._lock.release()
            log.debug(f"Lock released by thread '{current_thread}'")

        except RuntimeError as e:
            log.error(f"Lock release error by thread '{current_thread}': {e}")
            raise

    def locked(self) -> bool:
        """Check if lock is currently held.

        Returns:
            bool: True if lock is held, False otherwise

        Note:
            This is a snapshot - lock state may change immediately after check

        Example:
            >>> lock.locked()
            False
            >>> lock.acquire()
            >>> lock.locked()
            True
        """
        # For RLock, we can try to acquire with zero timeout to check
        acquired = self._lock.acquire(blocking=False)
        if acquired:
            self._lock.release()
            return False
        return True

    def __enter__(self):
        """Context manager entry - acquire lock.

        Returns:
            ThreadSafeLock: Self reference

        Example:
            >>> with lock:
            ...     # Lock automatically acquired
            ...     device.temperature_1
        """
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - release lock.

        Args:
            exc_type: Exception type (if any)
            exc_val: Exception value (if any)
            exc_tb: Exception traceback (if any)

        Returns:
            False: Don't suppress exceptions

        Example:
            >>> with lock:
            ...     raise ValueError("Test")  # Lock released even on exception
        """
        self.release()
        return False  # Don't suppress exceptions

    def get_stats(self) -> dict:
        """Get lock statistics.

        Returns:
            dict: Statistics including acquisition count, timeout count,
                  current owner

        Example:
            >>> stats = lock.get_stats()
            >>> print(f"Acquisitions: {stats['acquisition_count']}")
            Acquisitions: 42
        """
        return {
            'acquisition_count': self._acquisition_count,
            'timeout_count': self._timeout_count,
            'current_owner': self._owner,
            'timeout': self.timeout,
            'is_locked': self.locked(),
        }

    def reset_stats(self):
        """Reset acquisition statistics (doesn't affect lock state).

        Example:
            >>> lock.reset_stats()
        """
        self._acquisition_count = 0
        self._timeout_count = 0
        log.debug("Lock statistics reset")

    def __repr__(self):
        owner_str = f"owner='{self._owner}'" if self._owner else "unlocked"
        return (f"<ThreadSafeLock(timeout={self.timeout}s, {owner_str}, "
               f"acquisitions={self._acquisition_count})>")


# Export public API
__all__ = ['ThreadSafeLock']

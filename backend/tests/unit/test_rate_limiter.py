import time

from app.core.rate_limiter import TokenBucket


def test_bucket_allows_up_to_capacity():
    bucket = TokenBucket(capacity=5, refill_rate_per_second=1.0)
    results = [bucket.try_acquire() for _ in range(5)]
    assert all(results)


def test_bucket_blocks_beyond_capacity():
    bucket = TokenBucket(capacity=5, refill_rate_per_second=1.0)
    for _ in range(5):
        bucket.try_acquire()
    assert bucket.try_acquire() is False


def test_bucket_refills_over_time():
    bucket = TokenBucket(capacity=2, refill_rate_per_second=20.0)
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert bucket.try_acquire() is False
    time.sleep(0.15)  # ~3 tokens worth at 20/s
    assert bucket.try_acquire()


def test_acquire_blocking_times_out():
    bucket = TokenBucket(capacity=1, refill_rate_per_second=0.1)
    assert bucket.try_acquire()
    assert bucket.acquire_blocking(timeout=0.2) is False

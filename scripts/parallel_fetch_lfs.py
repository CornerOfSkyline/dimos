#!/usr/bin/env python3
"""Parallel-range downloader: fetches a URL in N concurrent chunks and reassembles it."""
import concurrent.futures
import hashlib
import os
import subprocess
import sys
import time

URL_FILE = "/tmp/lfs_url.txt"
OUT_PATH = sys.argv[1]
EXPECTED_SHA256 = sys.argv[2]
TOTAL_SIZE = int(sys.argv[3])
CHUNKS = 6
RETRIES = 12

url = open(URL_FILE).read().strip()


def fetch_range(start: int, end: int, idx: int) -> tuple[int, bytes]:
    """Fetch one byte range with retries; retry narrower sub-ranges on failure."""
    last_err = None
    for attempt in range(RETRIES):
        try:
            proc = subprocess.run(
                ["curl", "-s", "--max-time", "300", "--fail", "-r", f"{start}-{end}", url],
                capture_output=True,
                timeout=320,
            )
            if proc.returncode == 0 and len(proc.stdout) == end - start + 1:
                return idx, proc.stdout
            last_err = f"rc={proc.returncode} got={len(proc.stdout)} want={end - start + 1}"
        except subprocess.TimeoutExpired:
            last_err = "timeout"
        time.sleep(1 + attempt)
    raise RuntimeError(f"chunk {idx} ({start}-{end}) failed: {last_err}")


def main() -> None:
    chunk_size = TOTAL_SIZE // CHUNKS
    ranges = []
    for i in range(CHUNKS):
        start = i * chunk_size
        end = TOTAL_SIZE - 1 if i == CHUNKS - 1 else (i + 1) * chunk_size - 1
        ranges.append((start, end, i))

    started = time.monotonic()
    results = [None] * CHUNKS
    with concurrent.futures.ThreadPoolExecutor(max_workers=CHUNKS) as pool:
        futures = {pool.submit(fetch_range, s, e, i): i for s, e, i in ranges}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            idx, data = fut.result()
            results[idx] = data
            done += 1
            print(f"chunk {idx} done ({done}/{CHUNKS}, {time.monotonic() - started:.0f}s)", flush=True)

    blob = b"".join(results)
    assert len(blob) == TOTAL_SIZE, f"assembled {len(blob)} != {TOTAL_SIZE}"
    digest = hashlib.sha256(blob).hexdigest()
    if digest != EXPECTED_SHA256:
        raise SystemExit(f"sha256 mismatch: {digest} != {EXPECTED_SHA256}")
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "wb") as f:
        f.write(blob)
    dur = time.monotonic() - started
    print(f"OK: {OUT_PATH} ({TOTAL_SIZE} bytes, {dur:.0f}s, {TOTAL_SIZE / dur / 1048576:.1f} MB/s, sha256 verified)")


if __name__ == "__main__":
    main()

# Code Review & Robustness Audit

## Findings & Fixes

### 1. Concurrency Issues (SQLite WAL Locking)
- **Finding:** The SQLite database connection in `Store` was utilizing the default `check_same_thread=True` flag in Python's `sqlite3` module. Under a WAL-journaled setup with parallel threads querying the database (e.g., UI dashboard pulling updates while background tasks write to it), Python throws `sqlite3.ProgrammingError` indicating objects created in one thread can only be used in that same thread. SQLite itself supports safe concurrent accesses using internal mutexes.
- **Fix:** Switched `check_same_thread=False` on the `sqlite3.connect` initialization to safely allow concurrent queries out-of-the-box and prevent thread crash exceptions while the database maintains consistency due to WAL mode.
- **Verification:** Added `test_sqlite_wal_concurrency` inside `tests/test_agent_monitor.py` simulating parallel read/write load on the database. It now successfully passes.

### 2. Protobuf Decoding Edge Cases
- **Finding:** The standalone Protobuf parser `_decode_proto` manually extracts payload sizes.
   - Truncated `varint` encoded fields would induce infinite loops when missing the termination bit `0x80`.
   - Length-delimited fields evaluated negative limits which caused Python to incorrectly parse slices like `data[pos: pos - 1]` creating buggy returns. Extracted values beyond the length of `data` also crashed.
- **Fix:** Added bounds-checking during `varint` evaluation to break and gracefully ignore incomplete data payloads. Also ensured negative indices and overflowed indices safely terminate evaluating `length-delimited` fields.

### 3. Cross-Platform Module Safety (WebView2 & Windows Features)
- **Finding:** Attempting to `import winsound` raised a `ModuleNotFoundError` during tests executed in non-Windows environments like the Linux sandbox.
- **Fix:** Used a `try/except` guard around `import winsound` and handled it cleanly in playback handlers.
- **Finding:** Hardcoded `Path(project_path).name` behaves incorrectly when extracting Windows-style paths (e.g., `d:\work\project`) on non-Windows environments during file path evaluation.
- **Fix:** Switched to string splitting logic (`path.replace("\\", "/").rstrip("/").split("/")[-1]`) explicitly designed for cross-platform processing.

### 4. Resource Leaks Audit
- **Subprocess Handles:** `subprocess.Popen` invokes CLI tools without `wait()` bindings. This is correctly intentional since it spins off fully detached processes like file explorers or tray workers (`CREATE_NEW_PROCESS_GROUP`), meaning we do not leak blocked pipes or handlers.
- **Javascript Timers:** Timers like `setInterval(autoRefresh, AUTO_REFRESH_MS)` inside `dashboard.html` correctly persist without leaking redundant loops over window lifetimes. `window.__bridgeWatchdog` is securely pruned with `clearTimeout`.

### 5. Zombie Window Cleanup & Safe Single-Instance Mutex Guards
- The `DASHBOARD_MUTEX_NAME` logic correctly binds its identifier context with `VERSION`, which allows subsequent upgraded binaries to safely initialize a new window host context rather than deferring indefinitely to unclosed outdated process guards. WebView2 passes `--disable-gpu` safely to avert hardware acceleration black-screen anomalies.

All checks passed successfully during re-evaluation!